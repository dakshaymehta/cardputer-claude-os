"""End-to-end onboarding for an M5Stack.

Detect -> identify -> fetch firmware -> flash -> (optional apps
install). Each stage prints its status so the user can see progress
over what takes a couple of minutes in total.

This is the entrypoint. Works on macOS (``/dev/cu.usbmodem*``),
Linux (``/dev/ttyACM*`` / ``/dev/ttyUSB*``), and Windows (``COMx``)
— pyserial abstracts the port-name differences, and the skill's
preflight checks handle missing dependencies (Python itself on
Windows via winget, esptool and pyserial via pip on any OS).

If a stage fails, the error message points at the specific
sub-script to rerun so you don't have to re-flash to fix a typo.
"""

from __future__ import annotations

import argparse
import glob
import importlib
import importlib.util
import os
import shutil
import subprocess
import random
import sys
import threading
import time
from pathlib import Path

# Put the bundled scripts/vendor/ on sys.path so esptool and
# pyserial are available out of the box on a fresh clone — no
# pip-install step. See scripts/vendor_path.py for the helper and
# scripts/vendor/__init__.py for what's vendored and why. If the
# vendor dir isn't present (e.g. someone pruned it), this is a
# no-op and the preflight below falls back to pip.
import vendor_path
vendor_path.ensure_on_syspath()

SCRIPTS_DIR = Path(__file__).resolve().parent

# The canonical UIFlow boot.py. The firmware image ships with a correct copy
# at the right offset, but an aborted onboarding run or manual REPL session
# can overwrite it. We restore it after every flash so the screen always
# lights up with UIFlow's startup UI regardless of prior accidents.
#
# Sent as MicroPython code in paste mode: the outer triple-single-quotes
# wrap the file content so indentation is preserved exactly.
_WRITE_BOOT_PY_SCRIPT = """\
content = '''# -*- encoding: utf-8 -*-
# boot.py
import M5
import esp32
import time

NETWORK_TIMEOUT = 60

if __name__ == "__main__":
    M5.begin()
    from startup import startup

    nvs = esp32.NVS("uiflow")
    try:
        tz = nvs.get_str("tz")
        time.timezone(tz)
    except:
        pass

    try:
        boot_option = nvs.get_u8("boot_option")
    except:
        boot_option = 1

    startup(boot_option, NETWORK_TIMEOUT)
'''
with open('/flash/boot.py', 'w') as f:
    f.write(content)
print("BOOT-PY-OK")
"""


def banner(msg: str) -> None:
    sys.stderr.write(f"\n==== {msg} ====\n")
    sys.stderr.flush()


# Per-stage flavor for the every-15-seconds heartbeat. Each pool is
# shuffled once per Heartbeat instance and walked in order, so within
# a run you don't see the same quip twice until the pool is exhausted.
# Across runs the order differs. Lines are written to be whimsical
# but anchored to what's actually happening on the wire — never
# misleading about success or failure, since the Heartbeat thread is
# only alive while the stage is making forward progress.
_HEARTBEAT_QUIPS: dict[str, tuple[str, ...]] = {
    "DETECT": (
        "Asking the OS what's plugged in.",
        "Filtering USB-UART bridges and native-USB ESP32-S3 devices.",
        "Probing the chip identity over esptool.",
        "Looking for a friendly silicon face on the bus.",
    ),
    "FETCH FIRMWARE": (
        "Pulling firmware from M5Stack's CDN.",
        "Bytes are arriving from across the Pacific.",
        "Streaming the binary, hashing as we go. Nothing slips past the MD5.",
        "Aliyun OSS is shipping firmware, one packet at a time.",
        "Caching to ~/.cache/m5-onboard/ so the next run is faster.",
        "Verifying Content-MD5 in flight; corrupt bytes don't make the cut.",
    ),
    "FLASH": (
        "Whispering UIFlow into the chip's silicon ear.",
        "Bytes are arriving. Bytes are settling in. None are going home.",
        "The ROM bootloader is being agreeable, sector by sector.",
        "Erasing the past. Writing the future. Don't unplug the time machine.",
        "Painting fresh firmware over the old. The chip is patient.",
        "Negotiating with flash storage. It signs every page in triplicate.",
        "ESP32-S3 is reading its new instruction manual, one chapter at a time.",
        "The chip and esptool are performing a careful, rehearsed dance.",
        "Sectors are being prepared with the dignity of a librarian.",
        "115200 baud, no stub. Slow and steady. The hare lost this race.",
    ),
    "RESTORE BOOT.PY": (
        "Tucking the stock UIFlow boot.py into a safe drawer.",
        "Backing up the original boot path before our launcher takes over.",
        "Preserving the factory state. Just in case you change your mind.",
        "Saving boot_uiflow.py — your escape hatch back to UIFlow.",
    ),
    "INSTALL APPS": (
        "Beaming Python source through the REPL pipe, one file at a time.",
        "MicroPython is filing its new apps with quiet enthusiasm.",
        "Files are landing in /flash/ like paper airplanes in a gym.",
        "Dictating Python to a very polite listener.",
        "Each file gets its own little ceremony before the next.",
        "Paste mode is the slowest courier with the cleanest handwriting.",
        "Bytes over UART. The wire never complains.",
        "Base64-decoded chunks are arriving at the device, in order.",
        "Writing apps that the launcher will discover on next boot.",
        "MicroPython is opening files and writing bytes. Small life, good life.",
    ),
}


def _quips_for_stage(stage: str) -> list[str]:
    """Return a copy of the matching pool, or [] if no prefix matches.

    Match by prefix so labels with parenthetical suffixes — e.g.
    "FETCH FIRMWARE (cardputer-adv)" or "FLASH (write)" — still
    resolve to the right pool.
    """
    for prefix, lines in _HEARTBEAT_QUIPS.items():
        if stage.startswith(prefix):
            return list(lines)
    return []


class Heartbeat:
    """Background thread that prints a stage-flavored "still alive" tick
    every N seconds.

    Stages like FETCH FIRMWARE (network download), FLASH (esptool runs
    its own progress, but the post-flash wait and port re-enumeration
    can sit quiet for seconds), and the button-dance phases can go
    quiet long enough to look hung. A 15 s tick from a daemon thread
    gives a steady "still alive, here's where we are" signal without
    touching the stage's own logic. The line includes a whimsical
    quip from the matching pool so 8 minutes of FLASH doesn't read
    like a stuck process — see ``_HEARTBEAT_QUIPS``.

    Use as a context manager — the thread stops when the block exits,
    even on exception.
    """

    def __init__(self, stage: str, interval: float = 15.0) -> None:
        self.stage = stage
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._start = 0.0
        # Shuffle the matching quip pool once per instance so the
        # tick-by-tick sequence is round-robin (no immediate repeats)
        # but unpredictable across runs.
        self._quips = _quips_for_stage(stage)
        random.shuffle(self._quips)
        self._quip_idx = 0

    def _next_quip(self) -> str:
        if not self._quips:
            return ""
        q = self._quips[self._quip_idx % len(self._quips)]
        self._quip_idx += 1
        return q

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            elapsed = int(time.monotonic() - self._start)
            quip = self._next_quip()
            if quip:
                sys.stderr.write(
                    f"  [heartbeat {self.stage} {elapsed}s] {quip}\n"
                )
            else:
                # Stages without a matching pool (future additions, custom
                # callers) keep the old plain-text format so monitoring
                # filters that only look for "[heartbeat" still match.
                sys.stderr.write(
                    f"  [heartbeat] {self.stage} — {elapsed}s elapsed\n"
                )
            sys.stderr.flush()

    def __enter__(self) -> "Heartbeat":
        self._start = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 1)


def run_sub(script: str, *args: str) -> None:
    subprocess.check_call(
        [sys.executable, str(SCRIPTS_DIR / script), *args],
    )


def _is_native_usb(port: str) -> bool:
    """True when the port belongs to an Espressif native USB peripheral.

    ESP32-S3/C3 expose a built-in USB-JTAG/Serial CDC device (VID 0x303A).
    UART-bridge chips (CH9102, CP210x, FTDI) have different VIDs.

    We check by VID via pyserial's list_ports so this works on macOS
    (/dev/cu.usbmodemX), Linux (/dev/ttyACMX), and Windows (COMx) without
    relying on port-name patterns that differ per OS.
    """
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            if p.device == port and p.vid == 0x303A:
                return True
    except Exception:
        pass
    # Fallback for macOS/Linux if list_ports is unavailable
    name = os.path.basename(port)
    return "usbmodem" in name or name.startswith("ttyACM")



def _port_exists(port: str) -> bool:
    """Cross-platform check for whether a serial port is present.

    os.path.exists() works on macOS/Linux (/dev/...) but not on Windows
    (COM ports have no filesystem path). Use pyserial's list_ports instead,
    which works everywhere.
    """
    try:
        from serial.tools import list_ports
        return any(p.device == port for p in list_ports.comports())
    except Exception:
        return os.path.exists(port)


def _port_in_download_mode(port: str) -> bool:
    """True if ``port`` is enumerated as an ESP32-S3 USB-JTAG/Serial device
    in ROM download mode (PID 0x1001) rather than as UIFlow's application
    CDC (PID 0x816b).

    On native-USB ESP32-S3 boards, the built-in USB-Serial/JTAG controller
    flips its USB product ID based on firmware state: 0x1001 when the ROM
    bootloader is driving it, 0x816b (or similar) when user firmware is
    running. Returns False for UART-bridge devices — they don't have a
    "download mode PID" in the same sense; their download state is driven
    by DTR/RTS strap, not by the device's USB descriptor.
    """
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            if p.device == port:
                return (p.vid or 0) == 0x303A and (p.pid or 0) == 0x1001
    except Exception:
        pass
    return False


def _wait_for_port(port: str, timeout: float = 20.0) -> bool:
    """Block until the named serial port reappears after a device reset.

    Works on macOS, Linux, and Windows.

    Strategy:
      1. Give the device up to 3 s to disappear (avoids mistaking a
         stale port for a live re-enumeration).
      2. Poll until it comes back, up to `timeout` seconds.
      3. Sleep 0.5 s extra on success so the kernel driver is ready.
    """
    deadline = time.time() + timeout
    drop_by = time.time() + 3.0
    while _port_exists(port) and time.time() < drop_by:
        time.sleep(0.1)
    while time.time() < deadline:
        if _port_exists(port):
            time.sleep(0.5)
            return True
        time.sleep(0.3)
    return False


def _espressif_ports() -> set[str]:
    """Return the set of currently visible Espressif native-USB port paths.

    Uses serial.tools.list_ports so it works on macOS, Linux, and Windows
    without relying on /dev glob patterns.  VID 0x303A covers both UIFlow
    mode (PID 0x816b etc.) and USB-JTAG download mode (PID 0x1001).
    """
    try:
        from serial.tools import list_ports
        return {p.device for p in list_ports.comports() if p.vid == 0x303A}
    except Exception:
        # Fallback: glob POSIX device nodes if pyserial isn't available.
        # No meaningful fallback on Windows — COM ports have no
        # filesystem path, so if pyserial is missing we just return
        # an empty set there. In practice pyserial is a hard
        # dependency of the skill's preflight so this path rarely
        # matters, but being honest about the Windows case keeps us
        # from returning spurious results.
        if os.name == "nt":
            return set()
        return set(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*"))


# Distinguishing "in ROM bootloader / download mode" from "running user
# code" over native USB is surprisingly tricky by PID alone:
#   0x1001 — ROM USB-JTAG/Serial download mode on some silicon/efuse combos.
#   0x8120 — ESP32-S3 USB-Serial/JTAG. Used by BOTH the ROM bootloader on
#            R0.2 AND by user firmware that hasn't overridden the USB
#            descriptor (e.g. UIFlow v2.4.2 runs at 0x8120 with product
#            string "M5Stack UiFlow 2.0"). A naive PID check mis-fires.
#   0x816b — M5Stack vendor PID set by some UIFlow builds (v2.4.3 did;
#            v2.4.2 didn't). Same story.
#
# The reliable signal is the USB **product string**. The ROM bootloader
# always advertises itself as "USB JTAG/serial debug unit" — user apps
# override that. So we match on product string rather than PID.
_ROM_BOOTLOADER_PRODUCT = "USB JTAG/serial debug unit"


def _native_bootloader_responds(port: str) -> bool:
    """True if esptool can talk to the ROM bootloader on ``port``.

    USB descriptors alone are not enough: after a flaky flash or USB
    glitch, the OS can still show PID 0x1001 while the serial side
    returns nothing. A cheap ``chip_id`` round-trip catches that before
    we skip the button-dance path and fail mid-flash.
    """
    cmd = [
        sys.executable,
        "-m",
        "esptool",
        "--port",
        port,
        "--baud",
        "115200",
        "--before",
        "no_reset",
        "--after",
        "no_reset",
        "--no-stub",
        "chip_id",
    ]
    env = vendor_path.subprocess_env() if vendor_path.is_available() else None
    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=25,
            check=True,
            env=env,
        )
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def _is_download_port(p) -> bool:
    if p.vid != 0x303A:
        return False
    # PID 0x1001 is unambiguous — ROM mode, accept.
    if p.pid == 0x1001:
        return True
    # Otherwise the PID overlaps user firmware; require the product
    # string to confirm we're talking to the ROM bootloader.
    return (p.product or "").strip() == _ROM_BOOTLOADER_PRODUCT


def _download_mode_steps(variant: str) -> str:
    """Device-specific instructions for entering ROM download mode.

    ESP32-S3 native USB has no software path into download mode — GPIO0 must
    be held low across a reset — but *how* you trigger that reset differs by
    board:

      * Cardputer-Adv has NO reset button; the "reset" is the side POWER
        SWITCH. Hold BtnG0 (small button on the BACK) while switching the
        power OFF then ON.
      * Other native-USB boards (CoreS3, original Cardputer) have a physical
        reset button (BtnRST); hold BtnG0 while tapping it.

    Detection downstream is method-agnostic (it watches the USB bus for the
    port to drop and the ROM bootloader to appear), so only this wording is
    board-specific. Single source of truth — SKILL.md mirrors these steps.
    """
    if variant == "cardputer-adv":
        return (
            "  The Cardputer-Adv has NO reset button. Download mode is entered\n"
            "  with the side POWER SWITCH plus BtnG0 (the small button on the\n"
            "  BACK of the device; it's flush-mounted, a fingernail helps).\n"
            "  1. Slide the side power switch to OFF.\n"
            "  2. Press and HOLD BtnG0 (back of device).\n"
            "  3. While still holding BtnG0, slide the power switch back to ON.\n"
            "  4. Release BtnG0. Screen should be fully dark = download mode.\n"
        )
    return (
        "  ESP32-S3 native USB needs GPIO0 held low across a reset (no software\n"
        "  path exists). Button layout varies by board — check M5's docs if the\n"
        "  names below don't match your unit:\n"
        "  1. Press and HOLD BtnG0 (GPIO0 strap).\n"
        "  2. While still holding BtnG0, trigger a reset (tap BtnRST; on boards\n"
        "     with no reset button, slide the side power OFF then ON).\n"
        "  3. Release the reset first, then keep holding BtnG0 ~1 more second.\n"
        "  4. Release BtnG0. Screen should be fully dark = download mode.\n"
    )


def _wait_for_download_port(
    current_port: str,
    variant: str = "cardputer-adv",
    per_attempt_timeout: float = 30.0,
    max_attempts: int = 10,
) -> str | None:
    """Prompt the user to do G0+RESET until the ROM bootloader shows up.

    ESP32-S3 native USB has no software path into download mode — no
    DTR/RTS wiring to EN/GPIO0 (unlike UART-bridge boards), and while
    `machine.bootloader()` exists in UIFlow's MicroPython it leaves the
    chip in a state macOS can't enumerate. The only reliable entry is
    holding GPIO0 low while pulsing EN, which requires physical button
    presses.

    Humans frequently get the timing wrong on the first try — usually
    releasing G0 before the ROM finishes reading the strap. Instead of
    a single attempt-then-fail, we loop: each iteration watches for the
    port to drop and a new port to appear, classifies what showed up
    (ROM bootloader? User firmware? Nothing?), and re-prompts with
    targeted guidance based on what actually happened.

    Returns the bootloader port path on success, or None if the user
    gives up after `max_attempts` (Ctrl-C also exits cleanly).
    """
    from serial.tools import list_ports as _lp

    def _dl_port() -> str | None:
        for p in _lp.comports():
            if _is_download_port(p):
                return p.device
        return None

    def _describe() -> str:
        parts = []
        for p in _lp.comports():
            if p.vid == 0x303A:
                parts.append(
                    f"{p.device}@{hex(p.pid)}({(p.product or '').strip()})"
                )
        return ", ".join(parts) or "<no Espressif ports>"

    # Fast-path: if the device is ALREADY in download mode when we
    # enter here (e.g. a previous flash attempt bailed out mid-run,
    # or the user did the dance before starting the skill), skip
    # the "wait for drop and reappear" transition detection and
    # proceed directly to flash. Previously we'd prompt for the dance
    # regardless, which meant users had to do it twice on every retry
    # — confusing and slow.
    existing_dl = _dl_port()
    if existing_dl and _native_bootloader_responds(existing_dl):
        sys.stderr.write(
            f"\nDevice already in download mode at {existing_dl} — skipping dance.\n"
        )
        return existing_dl
    if existing_dl:
        sys.stderr.write(
            "\nUSB lists ROM bootloader, but esptool could not open a session "
            "(stale link?). Falling through to the normal download-mode steps "
            "below.\n"
        )

    for attempt in range(1, max_attempts + 1):
        if attempt == 1:
            sys.stderr.write(
                "\n---- Enter download mode ----\n"
                + _download_mode_steps(variant)
            )
        else:
            sys.stderr.write(f"\n---- Attempt {attempt}/{max_attempts} ----\n")
        sys.stderr.write(
            f"  Current port state: {_describe()}\n"
            f"  Waiting up to {per_attempt_timeout:.0f} s for port to drop...\n"
        )
        sys.stderr.flush()

        # Phase 1: wait for current_port to disappear. If the user is
        # fiddling with the keyboard the port may not change at all —
        # that's fine, we just keep waiting until they actually press
        # RESET or the attempt times out.
        drop_deadline = time.time() + per_attempt_timeout
        last_print = 0.0
        while current_port in _espressif_ports() and time.time() < drop_deadline:
            if time.time() - last_print > 3.0:
                sys.stderr.write(f"  [still present] {_describe()}\n")
                sys.stderr.flush()
                last_print = time.time()
            time.sleep(0.1)

        if current_port in _espressif_ports():
            if variant == "cardputer-adv":
                sys.stderr.write(
                    "  No power-cycle detected. With BtnG0 (back) held down, slide\n"
                    "  the side power switch OFF then back ON.\n"
                )
            else:
                sys.stderr.write(
                    "  No reset detected. Trigger a reset while holding BtnG0\n"
                    "  (tap BtnRST, or power-cycle if the board has no reset button).\n"
                )
            continue

        sys.stderr.write(f"  [port dropped] now: {_describe()}\n")
        sys.stderr.flush()

        # Phase 2: watch what comes back. Give it 10 s — the ROM download
        # timeout is ~5 s; if user firmware boots instead we'll see that too.
        phase2_deadline = time.time() + 10.0
        last_seen = _describe()
        while time.time() < phase2_deadline:
            dl = _dl_port()
            if dl:
                sys.stderr.write(f"  [download mode!] {_describe()}\n")
                return dl
            current = _describe()
            if current != last_seen:
                sys.stderr.write(f"  [reappeared] {current}\n")
                sys.stderr.flush()
                last_seen = current
            time.sleep(0.05)

        # Classify why this attempt failed and coach accordingly.
        final_state = _describe()
        if "USB JTAG/serial debug unit" in final_state or "0x1001" in final_state:
            # Shouldn't reach here — phase 2 would have returned above.
            return _dl_port()
        if "M5Stack UiFlow" in final_state or "0x8120" in final_state:
            if variant == "cardputer-adv":
                sys.stderr.write(
                    "  Device rebooted into UIFlow instead of download mode.\n"
                    "  BtnG0 was not held low when power came back on.\n"
                    "  Hold BtnG0 (back of device, small and flush — use a\n"
                    "  fingernail) MORE firmly, and keep holding it for a full\n"
                    "  second AFTER you slide the power switch to ON. Try again.\n"
                )
            else:
                sys.stderr.write(
                    "  Device rebooted into UIFlow instead of download mode.\n"
                    "  BtnG0 was not held low when the reset released.\n"
                    "  Hold BtnG0 MORE firmly and keep holding it for a full\n"
                    "  second AFTER the reset. Try again.\n"
                )
        else:
            if variant == "cardputer-adv":
                sys.stderr.write(
                    "  Device did not re-enumerate. It may be stuck — slide the\n"
                    "  side power switch OFF then ON (without BtnG0) to recover,\n"
                    "  then we'll try again.\n"
                )
            else:
                sys.stderr.write(
                    "  Device did not re-enumerate. It may be stuck — press BtnRST\n"
                    "  alone (without BtnG0) to recover, then we'll try again.\n"
                )
            # Wait for device to come back before the next attempt.
            recovery_deadline = time.time() + 30.0
            while time.time() < recovery_deadline:
                if any(p.vid == 0x303A for p in _lp.comports()):
                    break
                time.sleep(0.5)

    sys.stderr.write(
        f"\nGave up after {max_attempts} attempts. Re-run onboard.py to try again.\n"
    )
    return None


def _wait_for_any_usbmodem(current_port: str, timeout: float = 20.0) -> str | None:
    """Wait for any Espressif native-USB port to reappear after a flash.

    Used post-flash when the device reboots into UIFlow. Any VID=0x303A
    port will do; we just need somewhere to talk to the REPL.

    Phase 1: wait for current_port to leave the list (device rebooting).
    Phase 2: wait for any Espressif port to appear and stay for 0.5 s.
    """
    drop_deadline = time.time() + 10.0
    while current_port in _espressif_ports() and time.time() < drop_deadline:
        time.sleep(0.1)

    deadline = time.time() + timeout
    while time.time() < deadline:
        candidates = sorted(_espressif_ports())
        if candidates:
            found = candidates[0]
            time.sleep(0.5)
            if found in _espressif_ports():
                return found
        time.sleep(0.2)
    return None


# ---------------------------------------------------------------------------
# Preflight: check for pyserial + esptool and offer to install them.
#
# These checks must live in this file (not in detect/mpy_repl) because those
# sibling modules import `serial` at module load — if pyserial is missing,
# importing them fails before we can prompt. So we do a stdlib-only check
# first, run pip if the user agrees, and only then import the rest of the
# pipeline.

# Minimum esptool version we need. flash.py invokes esptool with
# `--after watchdog_reset`, which was added in esptool 4.8. Older
# esptool fails mid-flash with an opaque argparse error AFTER the
# button dance, which is the worst possible time. Bumped to 4.8 as
# the floor; requirements.txt pins >= 4.11 for the version we test
# against.
_MIN_ESPTOOL = (4, 8)


def _pyserial_present() -> bool:
    # find_spec doesn't execute the package, so it's safe even if pyserial
    # has broken init — we only care whether it's importable at all.
    return importlib.util.find_spec("serial") is not None


def _esptool_version() -> tuple[int, ...] | None:
    """Return (major, minor, ...) tuple if esptool is importable; None otherwise.

    Tolerates non-numeric suffixes (e.g. ``4.8.0.dev0``) by stopping at
    the first non-digit in each component. Used by the preflight to
    detect installs that are too old to support the esptool flags
    flash.py uses.
    """
    try:
        import esptool  # noqa: F401
    except Exception:
        # Importing esptool runs its top-level code, which can fail in
        # weird ways on broken installs (missing transitive deps, etc).
        # Treat any import failure as "version unknown" rather than
        # crashing here.
        return None
    raw = getattr(esptool, "__version__", "")
    parts: list[int] = []
    for piece in raw.split(".")[:3]:
        digits = ""
        for ch in piece:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else None


def _esptool_version_str() -> str:
    v = _esptool_version()
    return ".".join(str(x) for x in v) if v else "unknown"


def _esptool_path_candidates() -> list[str]:
    """Same search as detect.find_esptool, stdlib-only, returns paths.

    Kept in sync with detect.find_esptool. When pip puts esptool in a
    user-install dir that isn't on $PATH (very common on Windows and on
    macOS framework Python), `shutil.which` misses it but a direct
    existence check works.
    """
    paths: list[str] = []
    paths += glob.glob(os.path.expanduser("~/Library/Python/*/bin/esptool"))
    paths += glob.glob(os.path.expanduser("~/Library/Python/*/bin/esptool.py"))
    paths += [
        os.path.expanduser("~/.local/bin/esptool"),
        os.path.expanduser("~/.local/bin/esptool.py"),
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths += glob.glob(
            os.path.join(appdata, "Python", "Python*", "Scripts", "esptool.exe")
        )
        paths += glob.glob(
            os.path.join(appdata, "Python", "Python*", "Scripts", "esptool.py")
        )
    for root in (r"C:\Python313", r"C:\Python312", r"C:\Python311", r"C:\Python310"):
        paths += [
            os.path.join(root, "Scripts", "esptool.exe"),
            os.path.join(root, "Scripts", "esptool.py"),
        ]
    return paths


def _esptool_present() -> bool:
    # Importable check first: flash.py invokes esptool via
    # ``python -m esptool`` (subprocess inherits user-site), so
    # importability is what actually matters. The binary hunt below
    # is a backstop for environments where esptool only landed in a
    # bin/Scripts directory and the importable module isn't on
    # sys.path for some reason.
    if importlib.util.find_spec("esptool") is not None:
        return True
    for name in ("esptool.py", "esptool", "esptool.exe"):
        if shutil.which(name):
            return True
    return any(os.path.isfile(p) for p in _esptool_path_candidates())


def _check_port_access(port: str) -> None:
    """On Linux, refuse to continue with an inaccessible port and
    surface the specific group the user is missing from.

    Without this, port-open fails ~30 seconds later as a generic
    ``PermissionError: [Errno 13]`` deep inside pyserial — opaque
    enough that it sends new attendees down a 20-minute "is the
    cable bad?" detour. The fix is documented in SKILL.md, but a
    programmatic check forces it into the user's first 5 seconds
    of output.

    No-op on macOS and Windows: there's no equivalent group-gate
    on those platforms, and the path checks below would false-
    positive for COMx which doesn't show up in the filesystem.
    """
    if sys.platform != "linux":
        return
    if not port or not os.path.exists(port):
        # pick_port handles "no port found" already; if we got here
        # with a missing path, defer to the next stage's error.
        return
    if os.access(port, os.R_OK | os.W_OK):
        return
    # Resolve the group name that owns the device node so the fix
    # we suggest is specific to the user's distro (dialout on
    # Debian/Ubuntu/Arch, uucp on Fedora/RHEL).
    grp_name = "dialout"
    try:
        import grp
        gid = os.stat(port).st_gid
        grp_name = grp.getgrgid(gid).gr_name
    except Exception:
        pass
    sys.stderr.write(
        "\nERROR: cannot read/write {port}.\n"
        "On Linux this means you're not in the '{grp}' group.\n"
        "Fix once, long-term:\n"
        "  sudo usermod -aG {grp} $USER\n"
        "  # then log out and log back in (group changes take effect\n"
        "  # for new sessions only — a new terminal is not enough)\n"
        "Or as a one-off, prefix this command with sudo. The skill\n"
        "doesn't need root for anything else, so the group fix is\n"
        "strictly better.\n".format(port=port, grp=grp_name)
    )
    sys.exit(2)


def _requirements_path() -> str:
    """Absolute path to the repo-root requirements.txt.

    Four levels up from this file:
    ``<repo>/.claude/skills/m5-onboard/scripts/onboard.py`` →
    ``<repo>/.claude/skills/m5-onboard/`` →
    ``<repo>/.claude/skills/`` → ``<repo>/.claude/`` → repo root.
    Stable across the canonical ``~/Downloads/m5stack/`` and any
    clone-elsewhere setup.
    """
    return os.path.normpath(
        os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..", "requirements.txt"
        )
    )


def _preflight() -> None:
    """Ensure pyserial + esptool are importable AND esptool is recent enough;
    install via pip from requirements.txt if anything is missing or stale.

    Three failure modes we explicitly guard against, all surfaced
    early so a user doesn't blow time on the button dance just to
    fail mid-flash:

      1. esptool not importable → install (after y/n prompt).
      2. esptool too old (< _MIN_ESPTOOL) → upgrade. flash.py uses
         ``--after watchdog_reset`` (esptool 4.8+); a 4.7.0 install
         would silently get past this preflight under the old
         "importable means OK" check, then fail mid-FLASH with an
         opaque argparse error.
      3. Post-install re-verification: after pip runs, we check
         again that esptool is the right version. Catches the
         Ubuntu 22.04 trap where a too-old pip silently resolves
         back to 4.7.0 because it can't fetch newer sdists.

    pyserial is vendored under ``scripts/vendor/`` (BSD-3-Clause);
    esptool is GPLv2+ and pip-installed on first run.
    """
    pyserial_ok = _pyserial_present()
    esp_ver = _esptool_version()
    esp_ok = esp_ver is not None and esp_ver >= _MIN_ESPTOOL

    # Happy path: everything resolves and is recent enough.
    if pyserial_ok and esp_ok:
        sys.stderr.write(
            "Deps OK: pyserial from scripts/vendor/, "
            "esptool {} from user/system site-packages.\n".format(
                _esptool_version_str()
            )
        )
        return

    # Build a human-readable list of what's missing or stale, plus
    # the matching pip-install spec list (for the manual-install
    # hint when we can't prompt).
    missing: list[str] = []
    install_spec: list[str] = []
    if not pyserial_ok:
        missing.append("pyserial (not importable)")
        install_spec.append("pyserial")
    if esp_ver is None:
        missing.append("esptool (not importable)")
        install_spec.append("esptool>={}.{}".format(*_MIN_ESPTOOL))
    elif not esp_ok:
        missing.append(
            "esptool {} (need >= {}.{})".format(
                _esptool_version_str(), *_MIN_ESPTOOL
            )
        )
        install_spec.append("esptool>={}.{}".format(*_MIN_ESPTOOL))

    sys.stderr.write("Missing or stale Python dependencies:\n")
    for m in missing:
        sys.stderr.write("  - {}\n".format(m))

    # Inside a venv, ``--user`` would install outside the venv and
    # wouldn't be importable by this process.
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)

    # Prefer ``-r requirements.txt`` so the repo's version pins flow
    # through. Fall back to per-package specs if the file is missing
    # (someone trimmed the repo).
    req = _requirements_path()
    cmd_base = [sys.executable, "-m", "pip", "install"]
    if not in_venv:
        cmd_base.append("--user")
    if os.path.isfile(req):
        cmd = cmd_base + ["-r", req]
        why = "via {}".format(req)
    else:
        cmd = cmd_base + install_spec
        why = "as fallback (requirements.txt missing)"

    # Non-interactive callers (CI, scripts redirecting stdin) shouldn't
    # hang on input(). Bail with a clear manual-install hint instead.
    if not sys.stdin.isatty():
        sys.stderr.write(
            "stdin is not a tty; can't prompt interactively. Install with:\n"
            "  {}\n".format(" ".join(cmd))
        )
        sys.exit(2)

    prompt = "Install/upgrade now with pip ({})? [Y/n] ".format(why)
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        sys.stderr.write("\nNo input available; aborting.\n")
        sys.exit(2)
    if answer in ("n", "no"):
        sys.stderr.write(
            "Aborted. Install manually and re-run:\n  {}\n".format(" ".join(cmd))
        )
        sys.exit(2)

    sys.stderr.write("Running: {}\n".format(" ".join(cmd)))
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError as e:
        sys.stderr.write(
            "pip install failed (exit {}). Fix manually and re-run.\n".format(
                e.returncode
            )
        )
        sys.exit(2)

    # Post-install re-verification. importlib caches negative results
    # for the lifetime of the interpreter, so we have to invalidate
    # before retrying find_spec / re-importing esptool.
    importlib.invalidate_caches()
    try:
        # Drop any cached esptool module so the next import sees the
        # newly-installed copy. Safe even if esptool was never
        # imported in this process.
        sys.modules.pop("esptool", None)
    except Exception:
        pass

    if not _pyserial_present():
        sys.stderr.write(
            "After pip install, pyserial is still not importable. "
            "Something is wrong with your Python environment.\n"
        )
        sys.exit(2)
    new_ver = _esptool_version()
    if new_ver is None or new_ver < _MIN_ESPTOOL:
        sys.stderr.write(
            "After pip install, esptool is {} but we need >= {}.{}.\n"
            "This usually means your pip is too old to fetch newer\n"
            "esptool releases (Ubuntu 22.04 ships a pip with this bug).\n"
            "Try upgrading pip first:\n"
            "  {} -m pip install --user --upgrade pip\n"
            "then re-run this command.\n".format(
                _esptool_version_str(),
                _MIN_ESPTOOL[0],
                _MIN_ESPTOOL[1],
                sys.executable,
            )
        )
        sys.exit(2)
    sys.stderr.write(
        "Deps installed: esptool {}.\n".format(_esptool_version_str())
    )

    # pip wrote new files into site-packages / user scripts dir. Clear
    # the import system's finder cache so the re-check picks them up
    # without having to restart the interpreter.
    importlib.invalidate_caches()

    still_missing: list[str] = []
    if "pyserial" in missing and not _pyserial_present():
        still_missing.append("pyserial")
    if "esptool" in missing and not _esptool_present():
        still_missing.append("esptool")
    if still_missing:
        missing_str = ", ".join(still_missing)
        if sys.platform == "win32":
            path_hint = (
                "On Windows, pip's user-install Scripts dir is usually\n"
                "  %APPDATA%\\Python\\Python3XX\\Scripts\\\n"
                "Add it to PATH in System Properties → Environment Variables,\n"
                "or re-run the Python installer with 'Add Python to PATH' ticked.\n"
                "Open a new terminal after changing PATH for it to take effect.\n"
            )
        elif sys.platform == "darwin":
            path_hint = (
                'On macOS try: export PATH="$HOME/Library/Python/3.X/bin:$PATH"\n'
                "(replace 3.X with your actual Python minor version).\n"
            )
        else:
            path_hint = (
                'On Linux try: export PATH="$HOME/.local/bin:$PATH"\n'
                "Add that line to ~/.bashrc or ~/.zshrc to persist.\n"
            )
        sys.stderr.write(
            "Install reported success but {} still not found. Check PATH —\n"
            "pip may have dropped scripts somewhere the shell can't see.\n"
            "{}".format(missing_str, path_hint)
        )
        sys.exit(2)
    sys.stderr.write("Dependencies installed.\n\n")


def _write_boot_py(port: str) -> None:
    """Write the canonical UIFlow boot.py to /flash/boot.py on the device.

    Called after every flash. The firmware image contains the correct
    boot.py at offset 0, so on a clean flash this is a no-op in practice.
    But if a previous onboarding attempt corrupted the file (e.g. via a
    raw REPL session that wrote something partial), this restores it and
    guarantees UIFlow's startup() sequence runs and the screen comes up.
    """
    s = mpy_repl.open_port(port)
    try:
        # UIFlow may still be booting; wait for REPL prompt before sending.
        mpy_repl.wait_for_boot(s, timeout=15.0)
        mpy_repl.interrupt_to_repl(s)
        out = mpy_repl.exec_and_capture(s, _WRITE_BOOT_PY_SCRIPT, settle=1.0)
        if "BOOT-PY-OK" not in out:
            sys.stderr.write("warning: boot.py write may not have succeeded:\n")
            sys.stderr.write(out + "\n")
        else:
            sys.stderr.write("boot.py written OK.\n")
    finally:
        s.close()


def main() -> int:
    # Preflight runs *before* we import sibling modules. Those modules
    # (detect, mpy_repl) import `serial` at load time, so if pyserial is
    # missing the import itself would crash before the user sees the
    # install prompt. Preflight is stdlib-only for that reason.
    _preflight()

    # Safe to pull in everything that depends on pyserial / esptool now.
    global detect, fetch_firmware, flash, install_apps, mpy_repl
    import detect
    import fetch_firmware
    import flash
    import install_apps
    import mpy_repl

    ap = argparse.ArgumentParser(description="Onboard an M5Stack end-to-end.")
    ap.add_argument("--port", help="Serial port (autodetected if omitted).")
    ap.add_argument(
        "--variant",
        default="cardputer-adv",
        choices=[
            "basic-16mb", "basic-4mb", "fire",
            "core2", "tough",
            "cores3",
            "cardputer", "cardputer-adv",
        ],
        help=(
            "Firmware variant. Defaults to cardputer-adv since that's the "
            "hardware this rig provisions most often. Override for any "
            "other board — flashing the wrong variant boot-loops the "
            "device until it's re-flashed with the right one."
        ),
    )
    ap.add_argument(
        "--skip-flash",
        action="store_true",
        help=(
            "Device already has UIFlow — skip the flash stage. Combine "
            "with --apps to just push a fresh app bundle onto an already-"
            "provisioned device."
        ),
    )
    ap.add_argument(
        "--apps",
        help=(
            "After flash, install a bundle of .py files onto /flash/ so "
            "the device boots into user software instead of UIFlow's "
            "pairing screen. Accepts a directory path or a well-known "
            "name: {}.".format(", ".join(sorted(install_apps.KNOWN_BUNDLES)))
        ),
    )
    args = ap.parse_args()

    banner("DETECT")
    with Heartbeat("DETECT"):
        port = detect.pick_port(args.port)
        # Surface dialout/group permission issues now, before the
        # esptool probe (which would fail with a less-specific
        # PermissionError) and well before the user invests time in
        # the button dance.
        _check_port_access(port)
        native = _is_native_usb(port)
        if native:
            # Skip esptool probe on native USB: the DTR/RTS reset it uses to
            # enter download mode is unreliable when UIFlow is running, and
            # even a failed probe leaves the device mid-reboot. We know the
            # chip is ESP32-S3 from the port type; variant was given by the
            # user. If --skip-flash we just want the REPL, so skip probe too.
            sys.stderr.write(f"Native USB port — skipping esptool probe.\n")
            info = {"chip": "ESP32-S3 (native USB)", "mac": "?", "flash_size": "?"}
        else:
            try:
                info = detect.probe(port)
            except Exception as e:
                sys.stderr.write(f"warn: esptool probe failed ({e}); continuing.\n")
                info = {"chip": "unknown", "mac": "unknown", "flash_size": "unknown"}
        sys.stderr.write(
            f"Found {info.get('chip', '?')} on {port} "
            f"(MAC {info.get('mac', '?')}, flash {info.get('flash_size', '?')})\n"
        )

    # Upfront notice for native-USB boards. The button dance is the only
    # interactive step in the flow and the one most likely to look like a
    # hang if the prompt scrolls off. Surface it BEFORE the long FETCH
    # stage so the user has a heads-up.
    if native and not args.skip_flash:
        sys.stderr.write(
            "\n---- Heads up: button dance needed during FLASH ----\n"
            "  This board uses native USB; there is no software path into\n"
            "  download mode. When the FLASH stage begins you'll need to:\n"
            + _download_mode_steps(args.variant)
            + "  Watch for the 'Enter download mode' prompt.\n"
        )
        sys.stderr.flush()

    if not args.skip_flash:
        banner(f"FETCH FIRMWARE ({args.variant})")
        with Heartbeat(f"FETCH FIRMWARE ({args.variant})"):
            manifest = fetch_firmware.fetch_manifest()
            entry, version = fetch_firmware.pick_firmware(manifest, args.variant)
            image = fetch_firmware.download(entry, version)
            sys.stderr.write(
                f"Firmware: {entry.get('name')} {version.get('version')} @ {image}\n"
            )

        banner("FLASH")
        if native:
            # ESP32-S3 with native USB requires GPIO0 held low during reset
            # to enter download mode — a hardware strap that can't be
            # software-triggered (see _wait_for_download_port docstring for
            # what we've ruled out). The wait function handles prompting,
            # per-attempt coaching, and retry. No heartbeat here — the wait
            # function already emits "[still present]" ticks every 3 s.
            bl_port = _wait_for_download_port(
                port, variant=args.variant, per_attempt_timeout=45.0
            )
            if not bl_port:
                sys.stderr.write(
                    "\nCould not reach download mode. Re-run when ready.\n"
                )
                return 1
            sys.stderr.write(f"Download mode port: {bl_port} — flashing now...\n")
            sys.stderr.flush()
            port = bl_port
            # Skip separate erase_flash — write_flash erases sectors as
            # it writes. native=True: run at 115200 with --no-stub,
            # which is actually faster on native USB than the stub's
            # baud-bumping path and has none of the "Lost connection"
            # mid-flash failures. See flash.py module docstring.
            #
            # after="watchdog-reset": --no-stub leaves the chip in ROM mode
            # when the flash finishes ("Staying in bootloader"), and then
            # REPL calls hit a non-REPL port and silently fail. We need to
            # force a reboot into UIFlow. On native USB the usual options
            # don't work:
            #   - hard-reset uses the RTS pin which isn't wired to EN,
            #     so it's a no-op (esptool prints the message anyway).
            #   - default-reset does DTR/RTS toggling, also a no-op.
            # watchdog-reset writes to the RTC watchdog registers to
            # trigger a real chip reset — the only path that actually
            # reboots on native USB. Verified: device re-enumerates as
            # UIFlow within ~1 s.
            # esptool 4.x only accepts underscore spellings for these
            # (``no_reset``, ``watchdog_reset``); esptool 5.x accepts
            # both. Use the underscore form so we work on either.
            with Heartbeat("FLASH (write)"):
                flash.write(port, image, "0x0",
                            before="no_reset", after="watchdog_reset", native=True)
        else:
            with Heartbeat("FLASH (erase+write)"):
                flash.erase(port)
                flash.write(port, image, "0x0")

        # After flashing, the device should reboot into UIFlow.
        # UART-bridge devices (usbserial-*) reappear as a running-mode
        # port in < 1 s after esptool's RTS pulse.
        # ESP32-S3 native USB (usbmodem*) takes 4–8 s to re-enumerate.
        # If esptool's post-flash watchdog_reset step failed (known
        # flake on this build — flash.write treats it as non-fatal so
        # we land here with the device stuck in download mode), the
        # port is still there but at PID 0x1001 (USB JTAG). We detect
        # that and kick it out of download mode explicitly.
        sys.stderr.write("Waiting for device to re-enumerate after flash...\n")
        sys.stderr.flush()
        with Heartbeat("POST-FLASH RE-ENUMERATE"):
            if native:
                new_port = _wait_for_any_usbmodem(port, timeout=20.0)
                if new_port:
                    if new_port != port:
                        sys.stderr.write(f"Post-flash port: {port} → {new_port}\n")
                    port = new_port
                # Is the device actually running UIFlow, or stuck in
                # download mode from a failed auto-reset? Re-probe the PID.
                still_in_download = _port_in_download_mode(port)
                if still_in_download:
                    sys.stderr.write(
                        "Device is still in download mode after flash. "
                        "Attempting a standalone reset via esptool...\n"
                    )
                    if flash.native_reset(port):
                        # esptool round-tripped — the watchdog fired. Give
                        # UIFlow 5 s to boot before the next stage hits the
                        # REPL.
                        time.sleep(5)
                        still_in_download = _port_in_download_mode(port)
                    if still_in_download:
                        # Both the inline and standalone resets failed.
                        # The flash is written; we just need the user to
                        # press RESET on the device to boot into UIFlow.
                        if args.variant == "cardputer-adv":
                            sys.stderr.write(
                                "\n---- Manual reset needed ----\n"
                                "  The flash is written correctly but the "
                                "automatic reset failed.\n"
                                "  Slide the side power switch OFF then ON ONCE "
                                "to boot into UIFlow.\n"
                                "  Do NOT hold BtnG0 — we want UIFlow, not "
                                "download mode.\n"
                            )
                        else:
                            sys.stderr.write(
                                "\n---- Manual reset needed ----\n"
                                "  The flash is written correctly but the "
                                "automatic reset failed.\n"
                                "  Please press BtnRST ONCE (do NOT hold BtnG0) "
                                "to boot into UIFlow.\n"
                            )
                        # Wait up to 45 s for the PID to flip back to 0x816b.
                        deadline = time.monotonic() + 45
                        while time.monotonic() < deadline:
                            if not _port_in_download_mode(port):
                                break
                            time.sleep(0.5)
                        else:
                            sys.stderr.write(
                                "Device still in download mode. Later stages "
                                "may fail; you can power-cycle the device and "
                                "re-run with --skip-flash to recover.\n"
                            )
            elif not _wait_for_port(port, timeout=20.0):
                sys.stderr.write(
                    f"warning: {port} did not reappear within 20 s. "
                    "If the next stage fails, try unplugging and replugging "
                    "the USB cable then re-run with --skip-flash.\n"
                )

        # Restore boot.py. A freshly-flashed image already has the right
        # file, but this is a cheap safety net against any prior corruption.
        sys.stderr.write("Restoring boot.py...\n")
        with Heartbeat("RESTORE BOOT.PY"):
            _write_boot_py(port)

    if args.apps:
        banner("INSTALL APPS")
        with Heartbeat("INSTALL APPS"):
            src = install_apps.resolve_src(args.apps)
            sys.stderr.write("installing from {}\n".format(src))
            install_apps.install(port=port, src_dir=src)

    banner("DONE")
    if args.apps:
        sys.stderr.write(
            "Device is flashed and the app bundle is installed. Unplug, "
            "power on, and it should boot straight into the launcher.\n"
            "\n"
            "To connect from Claude Desktop:\n"
            "  1. Help menu → Troubleshooting → Enable Developer Tools\n"
            "  2. Developer menu → launch Hardware Buddy\n"
            "Then pick 'Claude Buddy' from the device launcher and Connect.\n"
        )
    else:
        sys.stderr.write(
            "Device is flashed with stock UIFlow. No apps installed — "
            "pass --apps to push a launcher bundle on the next run.\n"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
