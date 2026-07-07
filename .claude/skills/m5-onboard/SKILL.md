---
name: m5-onboard
description: End-to-end onboarding for a freshly-plugged-in M5Stack ESP32 device — detect it on USB, identify the exact model, flash UIFlow 2.0 firmware, and install a MicroPython app bundle (e.g. the Claude Buddy + Hello + Snake payload) so the device boots into user software instead of UIFlow's pairing screen. Works on macOS, Linux, and Windows; Python, esptool, and pyserial are auto-installed by the skill if missing. The apps we ship run over BLE / USB. Use this skill whenever the user mentions an M5Stack, M5Core, M5Stick, Cardputer, Cardputer-Adv, CoreS3, Core2, UIFlow, "flashing" or "provisioning" an ESP32 dev board, or plugs in "a new device" after previously working with M5Stack hardware. Also use it if the user asks to push app .py files onto an already-flashed device, install "the games" or "buddy" onto a board, or reset one to factory UIFlow. Trigger even when the user doesn't explicitly name the skill — "I have another one of these", "same thing for this device", "put the game on this one too", "m5-onboard", or the canonical provisioning shorthand "m5-onboard go" all count.
---

# M5Stack Onboarding

This skill automates the full cold-start workflow for an M5Stack ESP32 device: detect on USB, identify model, flash UIFlow 2.0, and push a MicroPython app bundle onto `/flash/` so the device boots into user software. The apps we ship (Claude Buddy, Snake, Hello) talk over BLE or USB. The workflow runs on macOS, Linux, and Windows; the skill was developed against an M5Stack Basic v2.6 (CH9102 bridge, ESP32-D0WDQ6-V3, 16 MB flash) and generalized to cover the rest of the Core family, with the Cardputer-Adv (ESP32-S3, native USB) as the current default target.

## Installation (one-time, per machine)

The skill lives at `.claude/skills/m5-onboard/` inside this repo, so Claude Code auto-discovers it whenever the repo is added as a plugin (or the repo is opened as the working directory and `.claude/skills/` is on the discovery path). No symlink step is needed in the normal case — clone the repo, point Claude Code at it, and the skill is live.

```bash
git clone <m5stack repo>             # anywhere — clone location doesn't matter
```

The `buddy/` sibling directory ships in the same repo, so `--apps buddy` works out of the box after a single clone — the skill resolves the bundle relative to its own install location (via `os.path.realpath(__file__)`), so the repo can live anywhere on disk. `~/Downloads/m5stack/buddy/device` and `~/Desktop/m5stack/buddy/device` are checked as conventional fallbacks if the script-relative path doesn't pan out. Set `M5_BUDDY_DIR` only if you want to point at a different bundle than the one bundled with this clone — see the Platform notes section.

## When to use

Use this when a user plugs in an M5Stack device and wants it provisioned. The decision tree:

- **Fresh/unknown device** → run `onboard.py --apps buddy` end-to-end (detect → identify → flash → install apps). This is the default path.
- **Already-flashed device, user just wants apps installed/refreshed** → run `install_apps.py --src buddy` (or any `--src <path>` to a directory of `.py` files).
- **Flashed device, something feels broken** → run `smoke_test.py` (I2C + LCD + speaker + button check).
- **User wants to know what's on the bus / what the device can do** → `smoke_test.py`.

If multiple devices are plugged in, ask which port to target — don't guess. If the user is provisioning a device they previously worked with (e.g. "same thing as last time" or "another Buddy"), default to `--apps buddy` unless they say otherwise.

### Which variant to assume

The rig this skill lives on provisions **Cardputer-Adv** boards overwhelmingly, so `onboard.py` now defaults to `--variant cardputer-adv`. In practice that means:

- If the user says nothing about the model, go with the default. They're almost certainly holding a Cardputer-Adv.
- If the user says "Cardputer" (no "Adv"), ask — the two models share a form factor but take different firmware images, and flashing the wrong one boot-loops the device.
- If the user names any other board ("Core2", "CoreS3", "Basic", "Fire"), pass the matching `--variant` explicitly — the default won't apply.
- The chip is ESP32-S3 either way, and `detect.py` won't be able to tell Cardputer from Cardputer-Adv before UIFlow is flashed (same native USB-JTAG VID, no pre-flash I2C probe). So this is a user-intent question, not a hardware-fingerprint one.

## The workflow

The main orchestrator is `scripts/onboard.py`. It drives the sub-scripts in order and handles the handoffs between them (waiting for reboots, capturing MAC, reporting progress). Prefer calling it directly over stitching the sub-scripts yourself unless the user asks for a partial run.

The default provisioning command (fresh Cardputer-Adv, install the buddy bundle):

```
python3 scripts/onboard.py --apps buddy
```

**How to invoke this from Claude Code's Bash tool.** Do NOT call `onboard.py` as a foreground Bash command. The Bash tool captures output and does not stream it back to the assistant until the command exits — and this command runs 2–3 minutes. That silence looks identical to a hang, and the assistant will usually give up before the button-dance prompt ever reaches the user. Instead, always run with `run_in_background: true`, `tee` to a log file, and then use the Monitor tool (or periodic `tail` via Read) to surface stage banners, heartbeats, and prompts to the user in real time. `2>&1` is not the fix — all progress already writes to stderr, which a terminal shows fine. The fix is streaming semantics, not redirection. The pattern that works:

```
# Launch (background, tee log):
python3 scripts/onboard.py --apps buddy 2>&1 | tee /tmp/m5-onboard.log

# Monitor (surfaces key events without drowning in byte-progress spam):
tail -f /tmp/m5-onboard.log | grep -E --line-buffered \
  "^====|heartbeat|Heads up|Enter download mode|download mode!|rebooted into UIFlow|Manual reset|DONE|ERROR|Error|Traceback|FAIL|failed|No USB|not detected|Attempt [0-9]|Device already in download|Download mode port|Post-flash port|Waiting for device"
```

Users running `onboard.py` directly in their own terminal (not via Claude Code) will see all output live — no changes needed there.

If `--port` is omitted, `detect.py` picks the most likely candidate across all three OSes: native-USB ESP32-S3 (`/dev/cu.usbmodem*` on macOS, `/dev/ttyACM*` on Linux, `COMx` on Windows), or a CH9102/CP210x UART bridge on older boards. Bluetooth-serial ports are filtered out. If multiple candidates are present, it asks.

The known apps name `buddy` resolves to the `buddy/device/` directory in this repo (custom launcher + Hello + Claude Buddy BLE client + Snake). Any other `--apps` value is treated as a filesystem path.

To skip re-flashing and just push (or refresh) the apps onto an already-provisioned device:

```
python3 scripts/install_apps.py --port <PORT> --src buddy
```

Where `<PORT>` is whatever `detect.py` printed on the last full run — for example `/dev/cu.usbmodem1101`, `/dev/ttyACM0`, or `COM3`.

### Stages

1. **Detect** (`detect.py`) — enumerate serial ports, filter to USB-UART bridges (CH9102 vendor `0x1A86`, Silabs CP210x `0x10C4`, FTDI `0x0403`) or the ESP32-S3 native USB-JTAG interface (`0x303A`). Probe with esptool to confirm the chip. Port names differ per OS (`/dev/cu.usbmodem*` on macOS, `/dev/ttyACM*`/`ttyUSB*` on Linux, `COMx` on Windows) but pyserial abstracts that.
2. **Identify** (`detect.py`) — alongside port discovery, `detect.py` reads the factory-test partition signature and/or scans I2C once UIFlow is on, and cross-references `references/hardware_signatures.md` to suggest the right firmware variant (Basic-16MB, Core2, CoreS3, Cardputer-Adv, etc.). User-facing variant choice happens via `onboard.py --variant`; there is no separate `detect.py --identify` flag.
3. **Fetch firmware** (`fetch_firmware.py`) — query the M5Burner manifest API and download the appropriate UIFlow 2.0 binary into the system temp dir. Cached between runs — safe to clear the cache anytime, it just re-downloads.
4. **Flash** (`flash.py`) — `esptool write_flash 0x0 <image>` at **460800 baud** for UART bridges, `--no-stub` at 115200 baud for native-USB S3 devices. 921600 fails intermittently on the CH9102 bridge — do not increase it. Native-USB flash can intermittently throw `Lost connection, retrying` mid-erase; esptool recovers. The post-flash `watchdog-reset` teardown step can fail even when the flash itself succeeded — `flash.py` parses esptool's stdout, treats that specific failure pattern as non-fatal when `Hash of data verified` appeared, and `onboard.py` falls back to `flash.native_reset()` and then manual-RESET coaching if needed.
5. **Install apps** (optional, `install_apps.py`) — paste-mode REPL upload of every `.py` from a source directory into `/flash/`, then reboot via `repl_reset` (DTR/RTS is a no-op on native USB — don't reach for it). Source layout: root `*.py` → `/flash/`, `apps/*.py` → `/flash/apps/` (UIFlow's stock launcher scans that). When the bundle ships a root `main.py`, `install_apps.py` also sets NVS `boot_option=2` so UIFlow's own launcher doesn't run and our `main.py` takes over the boot flow — critical for BLE-using apps on ESP32-S3 (see gotchas below).
6. **Smoke test** (optional, `smoke_test.py`) — I2C scan, LCD test pattern, speaker beep, button read.

## Critical gotchas (baked into the scripts — do not second-guess)

These are things the scripts already handle correctly but which you should not override if the user asks you to "just run esptool manually" or similar:

- **Native-USB ESP32-S3 boards (Cardputer, Cardputer-Adv, CoreS3) require a physical button dance to enter download mode — and the exact dance differs by board.** There is no software path. The chip has no DTR/RTS bridge, so nothing esptool or pyserial can do will put it into the ROM bootloader — the user has to hold GPIO0 low across a reset. **The Cardputer-Adv has NO reset button** — the "reset" is the side **power switch**, and BtnG0 is the small flush-mounted button on the **back** (a fingernail helps). `onboard.py:_wait_for_download_port` prompts for this at runtime during FLASH. The exact sequence for the Cardputer-Adv:

  1. **Slide the side power switch to OFF.**
  2. **Press and HOLD BtnG0** (back of device).
  3. **While still holding BtnG0, slide the power switch back to ON.**
  4. **Release BtnG0.** The screen should now be fully dark — that's how you know the device is in download mode.

  Other native-USB boards (CoreS3, original Cardputer) have a physical reset button (BtnRST): hold BtnG0, tap BtnRST, release BtnRST first, keep holding BtnG0 ~1 s, then release. If the device reboots back into UIFlow instead, BtnG0 was released too early (before the power switch / reset settled) — the coaching retries and tells the user to hold it longer. The detection downstream only watches the USB bus (the port drops, then the ROM bootloader appears), so it is method-agnostic — only the coaching wording is board-specific. Do NOT try to automate this with `esptool --before default_reset` or pyserial's DTR/RTS; both are no-ops on native USB (the pins aren't wired to EN), and adding them just hides the real prompt. Keep the SKILL.md steps here and the runtime prompt in `onboard.py:_download_mode_steps` in sync.
- **Do not unplug the device during FLASH.** Especially on native USB. A mid-flash disconnect leaves the internal flash in an inconsistent state. Mask ROM is usually reachable afterwards (re-enter download mode the same way — on the Cardputer-Adv, hold BtnG0 while switching the power OFF→ON), so the recovery is just to re-run `m5-onboard go` — it's idempotent and will re-enter download mode, re-flash, re-push apps. Don't panic and don't start opening the case; the mask ROM is in silicon and survives a corrupted flash as long as the USB PHY is intact.
- **Baud rate is 460800 on UART bridges, 115200 with `--no-stub` on native USB.** Not 921600 on either. The CH9102 bridge loses sync on `erase_flash` at 921600 (not theoretical — it fails). Native USB's stub-baud-bump path produces "Lost connection" mid-flash; 115200 no-stub is counterintuitively faster end-to-end because it never fails.
- **NVS writes must use `set_str`, not `set_blob`** *(relevant to `install_apps.py`'s `boot_option` setter).* UIFlow's startup calls `nvs.get_str()` and ESP-IDF tags blob and string entries separately. A blob-tagged key returns `ESP_ERR_NVS_NOT_FOUND` to `get_str`, and the device boot-loops. If a prior attempt wrote a blob, call `nvs.erase_key(name)` before `set_str`.
- **REPL multi-line blocks need paste mode.** Sending `try:`/`except:` line-by-line makes the REPL accumulate indentation forever. Use Ctrl-E to enter paste mode, send the block, Ctrl-D to execute. `mpy_repl.py` wraps this.
- **Hard reset is DTR=False, RTS=True, 100ms, RTS=False — but only on UART-bridge devices.** On native-USB ESP32-S3 boards the DTR/RTS lines aren't wired to EN/GPIO0, so that pulse is a silent no-op. Use `mpy_repl.repl_reset()` (sends `machine.reset()` through the REPL) for post-install reboots on those devices — `install_apps.py` already does this. If you bypass `install_apps.py` and stitch your own flow, don't reach for DTR/RTS on a usbmodem port and expect a reboot; files will be on disk but the old code will still be running. That regression bit us once.
- **The idle heap-debug loop is normal.** UIFlow 2.0 prints asyncio diagnostics while waiting at the pairing screen. Don't interpret it as a hang.
- **Cardputer-Adv (ESP32-S3) BLE peripherals require NVS `boot_option=2` + a custom `main.py`.** UIFlow's default `boot_option=1` starts a background Flow-pairing BLE advertise that wedges the NimBLE controller — subsequent `gap_advertise(adv_data=...)` calls from user code hit OSError(-519) "Memory Capacity Exceeded" regardless of payload shape, and the device ends up advertising with empty AD fields that iOS and the desktop Claude Buddy app filter out. The bundle's `main.py` lives at `/flash/` and takes over the boot flow (showing a simple menu over `/flash/apps/`), never touches BLE itself, and leaves the controller pristine for whichever app the user picks. `install_apps.py` now sets `boot_option=2` automatically when the bundle ships a root `main.py` — don't regress that behavior.

## After provisioning (what the user sees on the device)

Once `m5-onboard go` finishes at the `DONE` banner, the device is ready to use on its own:

- **Power.** Slide the switch on the right edge of the Cardputer-Adv to turn it on. Same switch turns it off. The board runs off its internal LiPo when unplugged; USB-C charges it.
- **Boot.** A short boot log scrolls, then the launcher menu appears automatically. The menu lists every `.py` in `/flash/apps/` plus the top-level `/flash/*.py` entries.
- **Navigation.** Arrow keys (or the keyboard's trackpoint-style cursor keys) scroll the menu; Enter launches the highlighted app; ESC returns to the launcher from inside an app.
- **Event WiFi auto-connect.** The bundle's `main.py` connects to a hard-coded event WiFi (SSID `cardputer`) on every boot and shows the result on the LCD before the launcher menu appears. Credentials live in `buddy/device/wifi_event.py`; the connect is best-effort and the launcher always continues even if the connect fails. If you're using this bundle outside the event, edit `wifi_event.py` or remove the `_connect_wifi_with_splash()` call from `main.py`.
- **Claude Buddy over BLE.** First time only: in Claude Desktop, **Help → Troubleshooting → Enable Developer Tools** (one-time, persists across launches). Then **Developer menu → Hardware Buddy → Connect**. BLE works regardless of the WiFi state — the link to Claude.app is local.
- **Getting back to UIFlow.** The buddy bundle ships only a `main.py` at `/flash/` (no replacement `boot.py`), so the stock UIFlow `boot.py` is never touched and there's no `boot_uiflow.py` backup to restore. Revert by removing our `main.py` from the device REPL: `os.remove('/flash/main.py')` followed by `machine.reset()`. UIFlow's stock launcher takes over on the next boot. To start completely fresh including the firmware, re-run the skill without `--apps`.

## Files

- `scripts/onboard.py` — main orchestrator
- `scripts/detect.py` — port discovery + chip ID
- `scripts/fetch_firmware.py` — M5Burner API + download
- `scripts/flash.py` — esptool wrapper
- `scripts/install_apps.py` — push a directory of `.py` files into `/flash/` via paste-mode REPL; backs up `boot.py` as `boot_uiflow.py` before overwriting; also writes the `boot_option` NVS key when the bundle ships a root `main.py`
- `scripts/smoke_test.py` — I2C + LCD + speaker + buttons
- `scripts/mpy_repl.py` — shared serial/REPL helpers (paste mode, hard reset, boot-log capture)
- `references/hardware_signatures.md` — chip + I2C fingerprints → model → firmware
- `references/uiflow2_nvs.md` — NVS key reference with types and failure modes

## Dependencies

- `pyserial` — vendored at `.claude/skills/m5-onboard/scripts/vendor/serial/` (pinned 3.5, BSD-3-Clause).
- `esptool` — pip dependency, declared in `requirements.txt`. Importable check happens via `importlib.util.find_spec("esptool")`; binary backstop search covers `~/Library/Python/*/bin/` on macOS, `~/.local/bin/` on Linux, `%APPDATA%\Python\Python3XX\Scripts\` on Windows.

`onboard.py` runs a preflight check at startup: if `esptool` (or, in the rare prune-vendor case, `pyserial`) is missing, it lists what's needed and asks the user whether to install now. On `Y` (or Enter) it runs `python -m pip install --user <missing>` in the current interpreter, then verifies. Inside a venv the `--user` flag is dropped so the install lands in the venv's site-packages. Non-interactive callers (piped stdin) get a manual-install hint instead of a prompt.

Python itself has to exist before this skill can do anything — you can't bootstrap an interpreter from inside one. Same story for `git` (needed once, to clone the skill and bundle repos). Claude's responsible for detecting both and installing whatever's missing *before* running any `scripts/*.py` invocation. Detection is just running `python3 --version` / `python --version` and `git --version` — if either fails, Claude fetches them with the host's native package manager before anything else.

**Per-OS dependency bootstrap (Claude's responsibility if missing):**

*Git:*

- **Windows** — `winget install -e --id Git.Git --silent --accept-source-agreements --accept-package-agreements`. Adds `git.exe` to PATH; may need a new shell for it to be visible.
- **macOS** — `/usr/bin/git` ships with Xcode Command Line Tools; `git --version` on a fresh macOS triggers the tools installer automatically. If you want to avoid the GUI prompt, install via `xcode-select --install` (still opens a dialog) or `brew install git`.
- **Linux** — `sudo apt-get install -y git` / `sudo dnf install -y git` / `sudo pacman -S --noconfirm git`.

*Python:*

- **Windows** — `winget install -e --id Python.Python.3.13 --silent --accept-source-agreements --accept-package-agreements`. Takes ~30 seconds, no UI, gets PATH right. If the current shell can't see `python` afterwards, tell the user to close and reopen the terminal (Windows updates PATH only on new shells).
- **macOS** — Python 3 is usually pre-installed as `/usr/bin/python3` on any current macOS (shipped by Apple). If for some reason it isn't, `brew install python@3.13` via Homebrew is the go-to; if Homebrew itself is missing, offer to install it via `/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"` (but only if the user confirms — Homebrew is a larger commitment than winget).
- **Linux** — use the distro package manager. Debian/Ubuntu: `sudo apt-get update && sudo apt-get install -y python3 python3-pip`. Fedora: `sudo dnf install -y python3 python3-pip`. Arch: `sudo pacman -S --noconfirm python python-pip`. You may need to sudo and should surface the password prompt to the user if needed.

**pyserial — bundled with the skill:**

A pinned `pyserial 3.5` ships under `scripts/vendor/` (BSD-3-Clause, Apache-compatible). Every script that imports `serial` calls `vendor_path.ensure_on_syspath()` before the first third-party import, which prepends `scripts/vendor/` to `sys.path`, so the vendored copy resolves regardless of whatever the user has system-wide. Net effect: port enumeration and REPL I/O work on a fresh clone with zero pip step. ~500 KB, pure-Python, same tree on macOS / Linux / Windows.

**esptool — pip dependency, auto-installed on first run:**

`esptool` is GPLv2+ and is intentionally **not** vendored — keeping the repository cleanly Apache-2.0 means the GPL bits live in the user's pip-managed environment, not in the tree. The skill's preflight checks for an importable `esptool` and, if missing, prompts to install it (`python -m pip install --user esptool` — `--user` dropped inside a venv so it lands in site-packages). For subprocess calls we use `[sys.executable, "-m", "esptool", ...]`; the subprocess inherits user-site so the pip-installed module imports cleanly. `requirements.txt` declares this for explicit setup; the prompt path is the default for first-time attendees who haven't run pip yet.

Non-interactive callers (piped stdin, CI) skip the prompt and get a `python -m pip install --user esptool` hint instead.

**Fallback if someone prunes `scripts/vendor/`:**

The same preflight path also re-installs pyserial via pip if the vendor copy is gone. This handles the case where someone downloaded a source-only zip that excluded vendor, or manually trimmed the repo to save space.

**USB driver — Windows-specific, only for older boards:**

The CH9102 USB-UART driver is still a manual install on Windows — WCH doesn't publish a winget manifest. Only needed for UART-bridge boards (Basic, Fire, Core2, StickC). Native-USB ESP32-S3 boards (Cardputer, Cardputer-Adv, CoreS3) enumerate as composite USB-CDC devices using Windows' in-box drivers and need no extra install.

## Platform notes

The skill runs on macOS, Linux, and Windows. Non-obvious bits:

- **Port naming.** pyserial abstracts the lookup but what the user sees looks different per OS. Pass whichever form `detect.py` reports:
  - macOS: `/dev/cu.usbmodem1101` (native USB) or `/dev/cu.usbserial-XXXX` (CH9102)
  - Linux: `/dev/ttyACM0` (native USB) or `/dev/ttyUSB0` (UART bridge)
  - Windows: `COM3`, `COM4`, etc. (Device Manager → Ports if unsure)
- **Linux permissions — read this before blaming hardware.** On most distros, accessing `/dev/ttyUSB*` / `/dev/ttyACM*` without sudo requires group membership (`dialout` on Debian/Ubuntu/Arch, `uucp` on Fedora). Symptom: `detect.py` finds the port, but the flash step fails with `Permission denied` or `Could not open port`. Fix once, long-term:
  ```bash
  sudo usermod -aG dialout $USER
  # log out / log back in — group change only takes effect for new sessions
  ```
  `sudo python3 scripts/onboard.py ...` works as a one-off but adding the group membership is strictly better because pyserial's port-open in user mode succeeds cleanly from then on.
- **Windows PATH gotchas.** Python's `pip install --user esptool` lands the executable in `%APPDATA%\Python\Python3XX\Scripts\`. If that directory isn't on PATH, `pip` prints a warning and nothing else picks up the install. `detect.py` looks there directly as a backstop, so the skill still works even without PATH fixed. But if you're invoking esptool outside the skill (or hitting "esptool not found" errors from other tools), either:
  - Re-run the Python installer and tick "Add Python to PATH" (the install's default), OR
  - Add `%APPDATA%\Python\Python3XX\Scripts` to PATH via System Properties → Environment Variables, OR
  - Use `python -m esptool ...` which always works regardless of PATH.
- **Windows Store Python.** Newer Windows 11 machines may have Python pre-installed via Microsoft Store. It works but has quirky PATH behavior (lives under `%LOCALAPPDATA%\Packages\PythonSoftwareFoundation.Python.*\`). `detect.py` checks that location too. If you have the choice, the `winget install Python.Python.3.13` version is more predictable.
- **Bundle path resolution.** `install_apps.py`'s `--src buddy` shorthand resolves in this order:
  1. `$M5_BUDDY_DIR` if set — explicit override, always wins. Useful when you want to point at a fork or a customized bundle that isn't in this clone.
  2. The `buddy/device/` directory inside this repo, found via `os.path.realpath(__file__)` walking up from `install_apps.py`. Works for any clone location.
  3. `~/Downloads/m5stack/buddy/device`.
  4. `~/Desktop/m5stack/buddy/device`.

  Most installs hit (2). Set `M5_BUDDY_DIR` only for the unusual case of pointing at a bundle outside this clone: `export M5_BUDDY_DIR=/path/to/buddy/device` (Unix) or `$env:M5_BUDDY_DIR="C:\path\to\buddy\device"` (PowerShell).
- **Firmware cache.** Downloaded firmware lands at `~/.cache/m5-onboard/` (or `$XDG_CACHE_HOME/m5-onboard/`), created at mode 0700 if missing. Cache files are MD5-verified at write time and re-verified on hit. Clearing the cache is safe; the next run re-downloads.
