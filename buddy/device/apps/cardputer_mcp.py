"""Cardputer MCP — device-side endpoint for the cardputer-mcp host
bridge (see /mcp/README.md and /buddy/references/mcp_protocol.md).

Iteration 2. This app:
  - Brings up a BLE peripheral on the `a5cd0001-…` service UUID, advertising
    as `CardputerMCP_<6 hex>`.
  - Parses line-delimited JSON over the RX characteristic.
  - Implements `notify` (visual banner + speaker chirp) and `ask`
    (renders question + choices, waits for 1–4 keypress or ESC).
  - Sends framed acks/events on TX, chunked at 20 bytes.

The BLE init sequence, IRQ pattern, advertise-cascade, and
`gatts_set_buffer` ordering are copied straight from buddy_ble.py —
they encode hard-won lessons about the stripped UIFlow 2.0 NimBLE
build. Don't reorder unless you've also re-run the experiments that
established the ordering; the failures are subtle (silent dropped
bytes, controller wedges that need a power cycle) and won't show up
in casual testing.

The chrome / exit conventions match the other apps in this directory
so the suite feels coherent. UIFlow's launcher does a machine.reset()
to come back, which means each app boots a fresh BLE stack — that's
why we don't have to worry about clashing with Buddy's NUS service.
"""

import json
import time

import bluetooth
import machine
import micropython
import M5
from hardware import MatrixKeyboard
from micropython import const


# ---- IRQ + flag constants (UIFlow 2.0 / MicroPython 1.22+ values) --

_IRQ_CENTRAL_CONNECT = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)
_IRQ_GATTS_WRITE = const(3)

_FLAG_READ = const(0x0002)
_FLAG_WRITE_NR = const(0x0004)
_FLAG_WRITE = const(0x0008)
_FLAG_NOTIFY = const(0x0010)


# ---- protocol constants --------------------------------------------
#
# Keep in sync with /mcp/server.py and /buddy/references/mcp_protocol.md.
# Grep for `a5cd` if you change any UUID — there's no central manifest.

SERVICE_UUID = bluetooth.UUID("a5cd0001-c0de-4abe-9c1a-4d5e6f7a8b90")
RX_UUID = bluetooth.UUID("a5cd0002-c0de-4abe-9c1a-4d5e6f7a8b90")  # host → device
TX_UUID = bluetooth.UUID("a5cd0003-c0de-4abe-9c1a-4d5e6f7a8b90")  # device → host

_RX_CHAR = (RX_UUID, _FLAG_WRITE | _FLAG_WRITE_NR)
_TX_CHAR = (TX_UUID, _FLAG_READ | _FLAG_NOTIFY)
_SVC = (SERVICE_UUID, (_RX_CHAR, _TX_CHAR))

_FW_VERSION = "0.2.0"
_CAPS = ["notify", "ask", "confirm"]
_MTU = 20  # default ATT MTU minus framing; chunk every TX write at this

# How long the user must hold Y for `confirm` to succeed. Picked high
# enough that a casual key-press can't accidentally trigger a
# destructive action — the value any real "are you sure?" UX would
# want is "long enough that no reflex can produce it." 3 s is the
# sweet spot: short enough that the user doesn't lose patience,
# long enough that no prompt injection's worth of "tap Y now" advice
# could time it precisely.
_CONFIRM_HOLD_MS = 3000

# Maximum gap between consecutive Y events that still counts as
# "still held." MatrixKeyboard surfaces autorepeat events (or in
# the worst case, the user hammers Y manually) — either way, if Y
# stops landing for longer than this, treat it as a release and
# reset the hold timer. 300 ms covers worst-case autorepeat cadence
# and rapid finger-tap gaps without making accidental release
# undetectable.
_CONFIRM_KEY_GAP_MS = 300


# ---- UI constants --------------------------------------------------

_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777
_GREEN = 0x60A060
_RED = 0xCC4040
_YELLOW = 0xCCB444

_LCD = M5.Lcd
_W = 240
_H = 135

# How long a notify banner stays on screen before reverting to the
# idle status display, in ms. Long enough to read a 3-line body
# comfortably, short enough that a stale notification doesn't loiter.
_NOTIFY_LINGER_MS = 5000


# ---- BLE peripheral ------------------------------------------------
#
# Module-level singleton because NimBLE on UIFlow 2.0 can't
# re-register GATT services on an already-active stack. Each app boot
# is a fresh machine.reset()-driven process anyway, so the singleton
# only really matters within one app entry — but it lets a hypothetical
# future re-entry (without reset) reuse handles.

_stack = None


def _mac_suffix(mac_bytes):
    """Last 3 MAC bytes as uppercase hex, no separator.

    Six hex chars gives the device a stable, scannable identifier that
    distinguishes multiple Cardputers in range without revealing the
    whole BT MAC.
    """
    return "".join("{:02X}".format(b) for b in mac_bytes[-3:])


def _ensure_stack():
    """Initialize the BLE stack on first call; cache and return after.

    Mirrors buddy_ble._ensure_stack — the ordering here is load-bearing.
    See buddy_ble.py for the full failure analysis; the short version:

      1. BLE() (then sleep 300 ms — premature active(True) C-faults)
      2. active(True) if not already active
      3. settle 250 ms
      4. config(gap_name=...)
      5. gatts_register_services((_SVC,))
      6. DO NOT call gatts_set_buffer here — that wedges adv_data
         acceptance; defer to after the first gap_advertise.
    """
    global _stack
    if _stack is not None:
        return _stack

    print("mcp_ble: ensure_stack: BLE()")
    ble = bluetooth.BLE()
    time.sleep_ms(300)

    try:
        pre_active = ble.active()
    except Exception:
        pre_active = False
    print("mcp_ble: ensure_stack: pre_active=", pre_active)
    if not pre_active:
        ble.active(True)
    time.sleep_ms(250)

    mac = ble.config("mac")[1]
    name = "CardputerMCP_{}".format(_mac_suffix(mac))
    ble.config(gap_name=name)

    print("mcp_ble: ensure_stack: register_services")
    ((rx_h, tx_h),) = ble.gatts_register_services((_SVC,))
    print("mcp_ble: ensure_stack: done")

    _stack = {"ble": ble, "rx": rx_h, "tx": tx_h, "name": name}
    return _stack


class MCPBLE:
    """BLE peripheral for the cardputer-mcp protocol. Unauthenticated
    on UIFlow 2.0 (same constraint as Buddy — see protocol.md).

    Callbacks invoked in IRQ/scheduler context:
      on_command(msg)  — one parsed JSON line received on RX
      on_state(state)  — "connected" / "disconnected"

    Both callbacks should be cheap — flag-and-return is the right
    shape. Heavy work (drawing, speaker, complex parsing) should be
    deferred to the main loop via flags.
    """

    def __init__(self, on_command, on_state):
        self._on_command = on_command
        self._on_state = on_state

        stack = _ensure_stack()
        self._ble = stack["ble"]
        self._rx_h = stack["rx"]
        self._tx_h = stack["tx"]
        self._name = stack["name"]

        # Init instance state BEFORE wiring the IRQ. A late DISCONNECT
        # from a prior session could fire the handler the moment we
        # re-attach, and _irq's first access is `_shutting_down`.
        self._conn = None
        self._rx_buf = bytearray()
        self._shutting_down = False

        self._ble.irq(self._irq)

        try:
            self._advertise()
        except OSError as e:
            print("mcp_ble: initial advertise failed, scheduling retry:", e)
            try:
                micropython.schedule(self._rearm_adv, 0)
            except RuntimeError:
                pass

        # gatts_set_buffer AFTER the first gap_advertise. The reverse
        # order locks the controller into accepting only empty
        # adv_data on this build. Verified the hard way (see
        # buddy_ble.py and ble_on_micropython.md).
        try:
            self._ble.gatts_set_buffer(self._rx_h, 512, True)
        except OSError as e:
            print("mcp_ble: gatts_set_buffer failed:", e)

    @property
    def name(self):
        return self._name

    @property
    def connected(self):
        return self._conn is not None

    # --- IRQ dispatch ----------------------------------------------

    def _irq(self, event, data):
        if self._shutting_down:
            return
        if event == _IRQ_CENTRAL_CONNECT:
            conn, _at, _addr = data
            self._conn = conn
            self._rx_buf = bytearray()
            self._on_state("connected")
            # Send `hello` after the central has had a moment to
            # subscribe to TX. Scheduling out of IRQ context also
            # avoids any reentrancy concern from the gatts_notify
            # write while we're still in the connect IRQ.
            try:
                micropython.schedule(self._send_hello, 0)
            except RuntimeError:
                # Schedule queue full — try inline. If it fails the
                # host will see no hello and disconnect after 5 s.
                self._send_hello(None)

        elif event == _IRQ_CENTRAL_DISCONNECT:
            self._conn = None
            self._rx_buf = bytearray()
            self._on_state("disconnected")
            # Re-advertise off-IRQ. NimBLE returns OSError(-30) if we
            # call gap_advertise the instant DISCONNECT fires.
            try:
                micropython.schedule(self._rearm_adv, 0)
            except RuntimeError:
                try:
                    self._advertise()
                except OSError as e:
                    print("mcp_ble: inline re-advertise failed:", e)

        elif event == _IRQ_GATTS_WRITE:
            conn, handle = data
            if handle == self._rx_h:
                self._rx_buf += self._ble.gatts_read(self._rx_h)
                # Split on newline and dispatch one line at a time.
                # Heavy parsing (json.loads) on the IRQ context is
                # what Buddy does too — if it gets in the way of
                # the BLE stack we'll move it behind a queue.
                while True:
                    nl = self._rx_buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(self._rx_buf[:nl])
                    # MicroPython bytearray doesn't support `del buf[:n]`,
                    # so we copy. Lines are short; cost is negligible.
                    self._rx_buf = bytearray(self._rx_buf[nl + 1 :])
                    try:
                        msg = json.loads(line)
                        self._on_command(msg)
                    except Exception as e:
                        print("mcp_ble: bad line:", e)

    # --- outbound --------------------------------------------------

    def _send_hello(self, _):
        # Give the central a beat to subscribe to TX before we emit
        # the first notification. Without this, hello is sent before
        # the central has written the CCCD descriptor, and the
        # notification is dropped silently — the host then disconnects
        # after its 5 s hello-timeout. 1500 ms covers worst-case
        # service-discovery + CCCD-write on a chatty macOS host.
        # We run in scheduler context (micropython.schedule), so
        # time.sleep_ms is fine here — it doesn't block IRQs.
        time.sleep_ms(1500)
        # Re-check the connection: macOS can drop the link during the
        # sleep window (especially the first time, around the
        # Bluetooth-permission prompt). Sending into a dead conn
        # would just produce a misleading "notify failed" log.
        if self._conn is None or self._shutting_down:
            return
        self.send(
            {
                "event": "hello",
                "version": _FW_VERSION,
                "name": "Cardputer",
                "caps": _CAPS,
                "model": "cardputer-adv",
                "mtu": _MTU,
            }
        )

    def send(self, payload):
        """Push one JSON object to the host as one `\\n`-terminated
        line, chunked at 20 bytes. Returns False if no link.

        Pace the chunks with an inter-chunk sleep. ESP32 NimBLE's
        notification queue is shallow — bursting gatts_notify back-to-
        back faster than the radio can transmit causes the controller
        to silently drop the later chunks (observed on Windows: 2–3
        chunks land, the rest vanish, the host RPC times out). 40 ms
        covers the typical Windows-negotiated connection interval
        (~30 ms) with margin; a 3-chunk ack ends up under ~120 ms
        end-to-end which is well inside any RPC timeout.
        """
        if self._conn is None:
            return False
        try:
            data = (json.dumps(payload) + "\n").encode()
        except Exception as e:
            print("mcp_ble: send encode failed:", e)
            return False
        try:
            first = True
            for i in range(0, len(data), _MTU):
                if not first:
                    time.sleep_ms(40)
                first = False
                self._ble.gatts_notify(self._conn, self._tx_h, data[i : i + _MTU])
        except OSError as e:
            print("mcp_ble: notify failed:", e)
            return False
        return True

    # --- adv / lifecycle -------------------------------------------

    def _rearm_adv(self, _):
        """Scheduler-context retry around `_advertise`.

        Same staircase as Buddy's _rearm_adv — NimBLE rejects the
        first gap_advertise after a paired disconnect with OSError(-30)
        or ENODEV; wall-time delays let the controller finish cleaning
        up the prior link.
        """
        for attempt in range(5):
            try:
                self._ble.gap_advertise(None)
            except OSError:
                pass
            time.sleep_ms(150 * (attempt + 1))
            try:
                self._advertise()
                return
            except OSError as e:
                print("mcp_ble: re-advertise attempt", attempt + 1, "err:", e)
        print("mcp_ble: giving up on re-advertise; power-cycle to recover")

    def _advertise(self):
        """Try a cascade of advertising payloads, from rich to empty.

        Empirically, a wedged NimBLE stack (from prior failed
        advertises or a controller still cleaning up a disconnect)
        will reject payloads it would otherwise accept. The cascade
        gives us the best chance of the device showing up SOMETHING
        in scanners rather than staying dark.
        """
        uuid_le = bytes(SERVICE_UUID)
        uuid_ad = bytes([len(uuid_le) + 1, 0x07]) + uuid_le
        name_bytes = self._name.encode()
        name_ad = bytes([len(name_bytes) + 1, 0x09]) + name_bytes

        candidates = [
            ("adv=UUID resp=name", {"adv_data": uuid_ad, "resp_data": name_ad}),
            ("adv=UUID", {"adv_data": uuid_ad}),
            ("adv=name", {"adv_data": name_ad}),
            ("resp=name", {"adv_data": b"", "resp_data": name_ad}),
            ("empty", {}),
        ]
        # 250 ms advertising interval — same compromise Buddy reached.
        # 100 ms triggers NimBLE faults in busy RF environments;
        # 250 ms is still well inside "responsive discovery" range.
        adv_interval_us = 250_000
        last_err = None
        for label, kwargs in candidates:
            try:
                self._ble.gap_advertise(None)
            except OSError:
                pass
            try:
                print("mcp_ble: gap_advertise shape:", label)
                self._ble.gap_advertise(adv_interval_us, **kwargs)
                print("mcp_ble: advertising as", self._name, "shape:", label)
                return
            except OSError as e:
                print("mcp_ble: adv shape", label, "err:", e)
                last_err = e
        raise last_err if last_err is not None else OSError("advertise failed")

    def deinit(self):
        """Cleanly tear down the peripheral surface.

        Three-layer defense against late events painting over the
        launcher (same pattern as buddy_ble.deinit):
          1. _shutting_down → IRQ early-outs
          2. ble.irq(None) → stops dispatch entirely
          3. callbacks replaced with no-ops as a final safety net
        """
        self._shutting_down = True
        try:
            self._ble.irq(None)
        except (OSError, TypeError):
            pass
        self._on_command = lambda _m: None
        self._on_state = lambda _s: None
        try:
            self._ble.gap_advertise(None)
        except OSError:
            pass
        if self._conn is not None:
            try:
                self._ble.gap_disconnect(self._conn)
            except OSError:
                pass


# ---- app ------------------------------------------------------------


class App:
    """UI + command dispatch.

    The contract with MCPBLE: command/state callbacks (IRQ context)
    update flags and queue tiny side effects (send ack, set dirty
    flag). The main loop renders, drives the speaker, and checks
    timeouts. This split keeps the IRQ path short and the UI work
    serialized on the main thread, avoiding torn LCD frames.
    """

    def __init__(self):
        self.state = "idle"  # "idle" | "notify" | "ask" | "confirm"
        self.ble_connected = False

        # Notify state.
        self.notify_data = None  # {"title", "body", "urgency"}
        self.notify_expires_at = 0

        # Ask state.
        self.pending_ask = None  # {"id", "question", "choices", "deadline"}

        # Confirm state. The hold timer is tracked by two timestamps:
        #   _y_held_since_ms — ticks_ms() when we first saw Y in the
        #                      current hold run; None when not held.
        #   _last_y_seen_ms  — ticks_ms() of the most recent Y event.
        #                      Used to detect release via gap > threshold.
        self.pending_confirm = None  # {"id", "title", "danger", "deadline"}
        self._y_held_since_ms = None
        self._last_y_seen_ms = None

        # Side-effect queue (set from IRQ, drained in main loop).
        self._dirty = True
        self._pending_chirp = None  # urgency string or None

        self.ble = MCPBLE(self._on_command, self._on_state)

    # --- callbacks from BLE (IRQ context) --------------------------

    def _on_state(self, state):
        self.ble_connected = state == "connected"
        if state == "disconnected":
            # Peer is gone; we can't send acks. Clear any blocking
            # operation so the screen reverts and a future connection
            # doesn't see stale state.
            if self.pending_ask:
                self.pending_ask = None
                self.state = "idle"
            if self.pending_confirm:
                self.pending_confirm = None
                self._y_held_since_ms = None
                self._last_y_seen_ms = None
                self.state = "idle"
        # Force a redraw to reflect status in the idle banner.
        if self.state == "idle":
            self._dirty = True

    def _on_command(self, msg):
        cmd = msg.get("cmd")
        mid = msg.get("id", "")
        if cmd == "notify":
            self._cmd_notify(msg, mid)
        elif cmd == "ask":
            self._cmd_ask(msg, mid)
        elif cmd == "confirm":
            self._cmd_confirm(msg, mid)
        elif cmd == "ping":
            self.ble.send({"ack": "ping", "id": mid, "ok": True})
        elif cmd == "cancel":
            self._cmd_cancel(msg, mid)
        else:
            self.ble.send(
                {"ack": cmd or "?", "id": mid, "ok": False, "err": "unknown cmd"}
            )

    def _cmd_notify(self, msg, mid):
        self.notify_data = {
            "title": str(msg.get("title", ""))[:64],
            "body": str(msg.get("body", ""))[:240],
            "urgency": msg.get("urgency", "info"),
        }
        self.notify_expires_at = time.ticks_add(time.ticks_ms(), _NOTIFY_LINGER_MS)
        # Notify never pre-empts a blocking modal — the user is in the
        # middle of answering an ask or holding a confirm and shouldn't
        # have the screen yanked out from under them. We still ack and
        # chirp so the host knows the message was delivered; the visible
        # banner is just suppressed until the modal clears. A future
        # iter can stack notifications as a corner-chip overlay.
        if self.state not in ("ask", "confirm"):
            self.state = "notify"
            self._dirty = True
        self._pending_chirp = self.notify_data["urgency"]
        self.ble.send({"ack": "notify", "id": mid, "ok": True})

    def _cmd_ask(self, msg, mid):
        choices_in = msg.get("choices", [])
        if not isinstance(choices_in, list) or len(choices_in) < 2 or len(choices_in) > 4:
            self.ble.send(
                {"ack": "ask", "id": mid, "ok": False, "err": "need 2–4 choices"}
            )
            return

        # Refuse to pre-empt a pending confirm. The whole point of
        # confirm is that the user is committing to a destructive
        # action; an arriving ask could be the agent trying to wriggle
        # out of it or — much worse, in the prompt-injection threat
        # model — a malicious tool result trying to swap the screen
        # for something innocuous. Return busy and make the host retry.
        if self.pending_confirm:
            self.ble.send(
                {"ack": "ask", "id": mid, "ok": False, "err": "confirm pending; retry"}
            )
            return

        # If there's already a pending ask, cancel it first so the
        # host's prior RPC sees a clean resolution rather than a
        # silently-replaced request.
        if self.pending_ask:
            self.ble.send(
                {
                    "ack": "ask",
                    "id": self.pending_ask["id"],
                    "ok": False,
                    "cancelled": True,
                }
            )

        timeout_s = max(1, min(600, int(msg.get("timeout_s", 60))))
        self.pending_ask = {
            "id": mid,
            "question": str(msg.get("question", ""))[:120],
            "choices": [str(c)[:32] for c in choices_in],
            "deadline": time.ticks_add(time.ticks_ms(), timeout_s * 1000),
        }
        self.state = "ask"
        self._dirty = True
        self._pending_chirp = "info"
        # Acknowledge receipt immediately; the resolution ack lands
        # when the user answers, timeout fires, or cancel arrives.
        self.ble.send({"ack": "ask", "id": mid, "pending": True})

    def _cmd_cancel(self, msg, mid):
        """Cancel a pending blocking operation (ask or confirm).

        We match `target_id` against whichever blocking modal is
        currently pending. If neither matches, report a clear error —
        cancels for already-resolved requests aren't catastrophic but
        the host should know its bookkeeping is off.
        """
        target = msg.get("target_id")
        if self.pending_ask and self.pending_ask["id"] == target:
            self.ble.send(
                {"ack": "ask", "id": target, "ok": False, "cancelled": True}
            )
            self.pending_ask = None
            self.state = "idle"
            self._dirty = True
            self.ble.send({"ack": "cancel", "id": mid, "ok": True})
            return
        if self.pending_confirm and self.pending_confirm["id"] == target:
            self.ble.send(
                {"ack": "confirm", "id": target, "ok": False, "cancelled": True}
            )
            self.pending_confirm = None
            self._y_held_since_ms = None
            self._last_y_seen_ms = None
            self.state = "idle"
            self._dirty = True
            self.ble.send({"ack": "cancel", "id": mid, "ok": True})
            return
        self.ble.send(
            {"ack": "cancel", "id": mid, "ok": False, "err": "no matching pending"}
        )

    def _cmd_confirm(self, msg, mid):
        """Show a destructive-confirmation prompt requiring a hold-Y gesture.

        Pre-empts both pending ask and pending confirm — the new request
        gets the modal regardless of what was there. A user holding Y on
        the prior confirm doesn't get to confirm the new one for free,
        because we reset the hold timer when entering the new state.
        """
        title = str(msg.get("title", ""))[:64]
        timeout_s = max(5, min(120, int(msg.get("timeout_s", 30))))
        danger = bool(msg.get("danger", True))

        if self.pending_ask:
            self.ble.send(
                {
                    "ack": "ask",
                    "id": self.pending_ask["id"],
                    "ok": False,
                    "cancelled": True,
                    "reason": "confirm preempted",
                }
            )
            self.pending_ask = None

        if self.pending_confirm:
            self.ble.send(
                {
                    "ack": "confirm",
                    "id": self.pending_confirm["id"],
                    "ok": False,
                    "cancelled": True,
                    "reason": "newer confirm preempted",
                }
            )

        self.pending_confirm = {
            "id": mid,
            "title": title,
            "danger": danger,
            "deadline": time.ticks_add(time.ticks_ms(), timeout_s * 1000),
        }
        # Start with no hold in progress. Even if the user happened to
        # be holding Y from the prior screen, they restart from zero —
        # the new confirm is a fresh consent, not an inherited one.
        self._y_held_since_ms = None
        self._last_y_seen_ms = None
        self.state = "confirm"
        self._dirty = True
        # `crit` chirp regardless of `danger` flag — the audible cue
        # is what makes "wait, what's about to happen?" register if
        # the user isn't looking at the device. A non-danger confirm
        # is unusual enough that we leave it loud.
        self._pending_chirp = "crit"
        self.ble.send({"ack": "confirm", "id": mid, "pending": True})

    # --- keyboard (main-loop context) ------------------------------

    def handle_keypress(self, k):
        """Return True if the app should exit (back to launcher)."""
        if self.state == "confirm" and self.pending_confirm:
            if isinstance(k, int):
                # Y / y advances the hold. The actual "did we hit
                # threshold?" check happens here too so confirmation
                # fires the moment the user's hold qualifies.
                if k in (ord("y"), ord("Y")):
                    now = time.ticks_ms()
                    if self._y_held_since_ms is None:
                        self._y_held_since_ms = now
                    self._last_y_seen_ms = now
                    held_ms = time.ticks_diff(now, self._y_held_since_ms)
                    if held_ms >= _CONFIRM_HOLD_MS:
                        self.ble.send(
                            {
                                "ack": "confirm",
                                "id": self.pending_confirm["id"],
                                "ok": True,
                                "confirmed": True,
                                "hold_ms": held_ms,
                            }
                        )
                        self.pending_confirm = None
                        self._y_held_since_ms = None
                        self._last_y_seen_ms = None
                        self.state = "idle"
                        self._dirty = True
                    else:
                        # Progress update — main-loop redraw handles it.
                        self._dirty = True
                    return False
                # N / n / ESC cancel the confirm without exiting the app.
                # We accept any of three keys because the right choice
                # depends on muscle memory: power users tend toward ESC,
                # phone-style flows expect N, and "tap Y or N" is a
                # universally familiar binary prompt.
                if k in (ord("n"), ord("N"), 0x1B):
                    self.ble.send(
                        {
                            "ack": "confirm",
                            "id": self.pending_confirm["id"],
                            "ok": False,
                            "cancelled": True,
                        }
                    )
                    self.pending_confirm = None
                    self._y_held_since_ms = None
                    self._last_y_seen_ms = None
                    self.state = "idle"
                    self._dirty = True
                    return False
                # Q exits the app entirely. The finally-block in run()
                # sends a cancellation ack so the host doesn't hang.
                if _is_q(k):
                    return True
            return False

        if self.state == "ask" and self.pending_ask:
            # 1–4 picks the corresponding choice.
            if isinstance(k, int) and ord("1") <= k <= ord("4"):
                idx = k - ord("1")
                if idx < len(self.pending_ask["choices"]):
                    self.ble.send(
                        {
                            "ack": "ask",
                            "id": self.pending_ask["id"],
                            "ok": True,
                            "choice": self.pending_ask["choices"][idx],
                        }
                    )
                    self.pending_ask = None
                    self.state = "idle"
                    self._dirty = True
                return False
            # ESC cancels the ask without exiting the app.
            if isinstance(k, int) and k == 0x1B:
                self.ble.send(
                    {
                        "ack": "ask",
                        "id": self.pending_ask["id"],
                        "ok": False,
                        "cancelled": True,
                    }
                )
                self.pending_ask = None
                self.state = "idle"
                self._dirty = True
                return False
            # Q exits the app entirely. The finally-block in run()
            # sends a cancellation ack so the host doesn't hang.
            if _is_q(k):
                return True
            return False

        # idle or notify: any of Q, ESC exits.
        if _is_q(k):
            return True
        if isinstance(k, int) and k == 0x1B:
            return True
        return False

    # --- main-loop tick --------------------------------------------

    def tick(self):
        # Drain side-effect queue from any IRQ-context updates.
        if self._pending_chirp is not None:
            chirp = self._pending_chirp
            self._pending_chirp = None
            _chirp(chirp)

        # Timers.
        now = time.ticks_ms()
        if self.state == "notify":
            if time.ticks_diff(self.notify_expires_at, now) <= 0:
                self.state = "idle"
                self.notify_data = None
                self._dirty = True
        elif self.state == "ask" and self.pending_ask:
            if time.ticks_diff(self.pending_ask["deadline"], now) <= 0:
                self.ble.send(
                    {
                        "ack": "ask",
                        "id": self.pending_ask["id"],
                        "ok": False,
                        "timed_out": True,
                    }
                )
                self.pending_ask = None
                self.state = "idle"
                self._dirty = True
        elif self.state == "confirm" and self.pending_confirm:
            # Detect Y release: if no Y event has landed within
            # _CONFIRM_KEY_GAP_MS, the user has let go and the hold
            # resets to zero. This is the gate that makes "hold Y for
            # 3 s" actually require a sustained press — without it the
            # first Y forever-counts as held.
            if self._y_held_since_ms is not None and self._last_y_seen_ms is not None:
                if time.ticks_diff(now, self._last_y_seen_ms) > _CONFIRM_KEY_GAP_MS:
                    self._y_held_since_ms = None
                    self._last_y_seen_ms = None
                    self._dirty = True
            # Host-supplied timeout. Wins even if the user happens to
            # be holding Y — the host already gave up waiting, so a
            # late confirmation would resolve a dead RPC.
            if time.ticks_diff(self.pending_confirm["deadline"], now) <= 0:
                self.ble.send(
                    {
                        "ack": "confirm",
                        "id": self.pending_confirm["id"],
                        "ok": False,
                        "timed_out": True,
                    }
                )
                self.pending_confirm = None
                self._y_held_since_ms = None
                self._last_y_seen_ms = None
                self.state = "idle"
                self._dirty = True
            # Smooth-progress redraw while held — without this the bar
            # only updates on key events, which would be jerky between
            # autorepeat ticks. ~25 fps full redraw is well within the
            # LCD driver's headroom.
            elif self._y_held_since_ms is not None:
                self._dirty = True

        if self._dirty:
            self.redraw()
            self._dirty = False

    # --- rendering -------------------------------------------------

    def redraw(self):
        if self.state == "confirm":
            self._draw_confirm()
        elif self.state == "ask":
            self._draw_ask()
        elif self.state == "notify":
            self._draw_notify()
        else:
            self._draw_idle()

    def _draw_idle(self):
        _LCD.fillScreen(_BLACK)
        _LCD.fillRect(0, 0, _W, 20, _DARK)
        _LCD.fillRect(0, 20, _W, 1, _ORANGE)
        _LCD.setTextSize(1)
        _LCD.setTextColor(_ORANGE, _DARK)
        _LCD.drawString("Cardputer MCP", 6, 5)

        # Status line — green when an MCP host is paired, gray otherwise.
        status_text = "READY" if self.ble_connected else "waiting for bridge"
        status_color = _GREEN if self.ble_connected else _GRAY_MID
        _LCD.setTextSize(2)
        _LCD.setTextColor(status_color, _BLACK)
        _LCD.drawString(
            status_text, (_W - _LCD.textWidth(status_text)) // 2, 42
        )

        # Device identity — useful when the user has multiple devices
        # in range or is trying to figure out which one to pair with.
        _LCD.setTextSize(1)
        _LCD.setTextColor(_GRAY_MID, _BLACK)
        _LCD.drawString(self.ble.name, (_W - _LCD.textWidth(self.ble.name)) // 2, 74)

        _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
        _LCD.setTextColor(_GRAY_MID, _DARK)
        hint = "Q  back to menu"
        _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)

    def _draw_notify(self):
        if not self.notify_data:
            return
        urgency = self.notify_data["urgency"]
        # Header color by urgency — a wordless signal that's faster to
        # parse than the urgency text would be.
        header_bg = {
            "crit": _RED,
            "warn": _YELLOW,
            "info": _DARK,
        }.get(urgency, _DARK)

        _LCD.fillScreen(_BLACK)
        _LCD.fillRect(0, 0, _W, 20, header_bg)
        _LCD.fillRect(0, 20, _W, 1, _ORANGE)
        _LCD.setTextSize(1)
        _LCD.setTextColor(_CREAM, header_bg)
        _LCD.drawString(urgency.upper(), 6, 5)

        # Title — size 2, single line, truncated to fit.
        _LCD.setTextSize(2)
        _LCD.setTextColor(_CREAM, _BLACK)
        title = self.notify_data["title"][:18]
        _LCD.drawString(title, 6, 28)

        # Body — size 1, wrapped at ~38 chars/line, max 4 lines so we
        # leave room for the hint strip without overlap.
        _LCD.setTextSize(1)
        _LCD.setTextColor(_CREAM, _BLACK)
        body = self.notify_data["body"]
        lines = [body[i : i + 38] for i in range(0, len(body), 38)][:4]
        y = 56
        for line in lines:
            _LCD.drawString(line, 6, y)
            y += 12

        _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
        _LCD.setTextColor(_GRAY_MID, _DARK)
        hint = "auto-clears - ESC dismiss"
        _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)

    def _draw_ask(self):
        if not self.pending_ask:
            return

        _LCD.fillScreen(_BLACK)
        _LCD.fillRect(0, 0, _W, 20, _DARK)
        _LCD.fillRect(0, 20, _W, 1, _ORANGE)
        _LCD.setTextSize(1)
        _LCD.setTextColor(_ORANGE, _DARK)
        _LCD.drawString("ASK", 6, 5)

        # Question (size 1, wraps at ~38 chars, max 2 lines).
        question = self.pending_ask["question"]
        q_lines = [question[i : i + 38] for i in range(0, len(question), 38)][:2]
        _LCD.setTextSize(1)
        _LCD.setTextColor(_CREAM, _BLACK)
        y = 28
        for line in q_lines:
            _LCD.drawString(line, 6, y)
            y += 12

        # Choices, numbered 1–4. Number is in orange to draw the eye
        # to the actionable digit; choice text is in cream.
        y = 60
        for i, choice in enumerate(self.pending_ask["choices"]):
            _LCD.setTextSize(1)
            _LCD.setTextColor(_ORANGE, _BLACK)
            _LCD.drawString("{}.".format(i + 1), 6, y)
            _LCD.setTextColor(_CREAM, _BLACK)
            _LCD.drawString(choice[:32], 22, y)
            y += 12

        _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
        _LCD.setTextColor(_GRAY_MID, _DARK)
        hint = "1-4 pick - ESC cancel"
        _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)

    def _draw_confirm(self):
        if not self.pending_confirm:
            return

        _LCD.fillScreen(_BLACK)

        # Red header band — danger signal. We use the same chrome
        # rhythm as the other states (header + hairline + body + hint
        # strip) so a user can't be fooled into thinking this is an
        # ordinary prompt, but the color shift makes the urgency
        # readable at a glance.
        _LCD.fillRect(0, 0, _W, 20, _RED)
        _LCD.fillRect(0, 20, _W, 1, _ORANGE)
        _LCD.setTextSize(1)
        _LCD.setTextColor(_CREAM, _RED)
        _LCD.drawString("DANGER  CONFIRM", 6, 5)

        # Title — size 2 for weight; truncated to fit one line. We
        # deliberately do NOT wrap the title: if the action is too
        # complex to describe in 18 chars, the host is over-using
        # confirm and should be using ask instead.
        _LCD.setTextSize(2)
        _LCD.setTextColor(_RED, _BLACK)
        title = self.pending_confirm["title"][:18]
        _LCD.drawString(title, (_W - _LCD.textWidth(title)) // 2, 28)

        # Instruction line. Plain, unambiguous, telegraphs the
        # gesture without any "press to continue" ambiguity.
        _LCD.setTextSize(1)
        _LCD.setTextColor(_CREAM, _BLACK)
        instr = "HOLD Y for 3 seconds"
        _LCD.drawString(instr, (_W - _LCD.textWidth(instr)) // 2, 60)

        # Progress bar. Empty outline always visible; fills red as the
        # hold accumulates. Geometry: 200 px wide, 10 px tall, centered.
        bar_w = 200
        bar_h = 10
        bar_x = (_W - bar_w) // 2
        bar_y = 78
        _LCD.drawRect(bar_x, bar_y, bar_w, bar_h, _CREAM)
        if self._y_held_since_ms is not None:
            held_ms = time.ticks_diff(time.ticks_ms(), self._y_held_since_ms)
            # Clamp visually so we don't overshoot the inner area while
            # the threshold-check / state-transition is in flight.
            progress = held_ms / _CONFIRM_HOLD_MS
            if progress > 1.0:
                progress = 1.0
            elif progress < 0.0:
                progress = 0.0
            fill_w = int((bar_w - 2) * progress)
            if fill_w > 0:
                _LCD.fillRect(bar_x + 1, bar_y + 1, fill_w, bar_h - 2, _RED)

        # Status text under the bar — tells the user what's happening
        # right now (a release is otherwise silent and you'd wonder
        # why the bar reset).
        _LCD.setTextSize(1)
        if self._y_held_since_ms is not None:
            held_ms = time.ticks_diff(time.ticks_ms(), self._y_held_since_ms)
            remaining = max(0, _CONFIRM_HOLD_MS - held_ms)
            secs = remaining / 1000.0
            status = "holding... {:.1f}s left".format(secs)
            _LCD.setTextColor(_RED, _BLACK)
        else:
            status = "release detected - try again"
            _LCD.setTextColor(_GRAY_MID, _BLACK)
        # Suppress the "release detected" string on first paint when
        # the user hasn't tried yet. _y_held_since_ms is None at start
        # too, so we differentiate via _last_y_seen_ms — if we've never
        # seen Y, show a quiet hint instead of a misleading "release"
        # message.
        if self._y_held_since_ms is None and self._last_y_seen_ms is None:
            status = "press and hold Y"
            _LCD.setTextColor(_GRAY_MID, _BLACK)
        _LCD.drawString(status, (_W - _LCD.textWidth(status)) // 2, 96)

        # Hint strip — same shape as other states.
        _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
        _LCD.setTextColor(_GRAY_MID, _DARK)
        hint = "HOLD Y - N/ESC cancel"
        _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)

    def teardown(self):
        """Best-effort cleanup before the launcher returns.

        Sends a cancellation ack for any pending blocking operation
        (ask or confirm) so the host's RPC doesn't time out — it gets
        a clean 'cancelled' result instead. Then tears down the BLE
        peripheral.
        """
        if self.pending_ask and self.ble.connected:
            try:
                self.ble.send(
                    {
                        "ack": "ask",
                        "id": self.pending_ask["id"],
                        "ok": False,
                        "cancelled": True,
                        "reason": "device-exit",
                    }
                )
            except Exception as e:
                print("cardputer_mcp: teardown ack failed:", e)
        if self.pending_confirm and self.ble.connected:
            try:
                self.ble.send(
                    {
                        "ack": "confirm",
                        "id": self.pending_confirm["id"],
                        "ok": False,
                        "cancelled": True,
                        "reason": "device-exit",
                    }
                )
            except Exception as e:
                print("cardputer_mcp: teardown ack failed:", e)
        try:
            self.ble.deinit()
        except Exception as e:
            print("cardputer_mcp: deinit warning:", e)


# ---- helpers --------------------------------------------------------


def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        print("cardputer_mcp: setFont fallback:", e)


def _is_q(k):
    if k is None:
        return False
    if isinstance(k, int):
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return False
    if isinstance(k, str) and k:
        return k.lower() == "q"
    return False


def _chirp(urgency):
    """Play a short audible cue based on notify urgency.

    Defensive: M5.Speaker isn't guaranteed available on every build
    or every Cardputer variant (the original Cardputer has no
    speaker; only Cardputer-Adv does). Any failure falls through
    silently — the visual banner is still the primary channel.
    """
    try:
        spk = M5.Speaker
    except Exception:
        return
    try:
        if urgency == "crit":
            for f in (660, 880, 660):
                spk.tone(f, 80)
                time.sleep_ms(40)
        elif urgency == "warn":
            spk.tone(660, 100)
            time.sleep_ms(60)
            spk.tone(880, 100)
        else:  # info
            spk.tone(880, 60)
    except Exception as e:
        # Common failure: the build's Speaker API is shaped differently.
        # Iter 3 can probe and adapt; for now silence is acceptable.
        print("cardputer_mcp: chirp skipped:", e)


# ---- main loop ------------------------------------------------------


def run():
    _set_font()
    app = App()
    app.redraw()

    kb = MatrixKeyboard()
    # Same 400 ms debounce as the other apps — selecting the entry
    # in App List can otherwise register as the first keypress.
    time.sleep_ms(400)

    try:
        while True:
            kb.tick()
            k = kb.get_key()
            if k is not None and app.handle_keypress(k):
                return
            app.tick()
            time.sleep_ms(40)
    finally:
        app.teardown()
        try:
            _LCD.fillScreen(_BLACK)
        except Exception as e:
            print("cardputer_mcp: clear warning:", e)
        time.sleep_ms(200)
        machine.reset()


# UIFlow's App List invokes apps both as __main__ and via import;
# bare call here matches the other apps in this directory.
run()
