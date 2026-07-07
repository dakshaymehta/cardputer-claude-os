# Cardputer Claude OS

A DIY "OS" bundle for the [M5Stack Cardputer](https://shop.m5stack.com/) —
flash UIFlow firmware, install a launcher, and ship a tiny suite of apps
that turn the Cardputer into a hand-held Claude device:

- **Claude Buddy** — pair the Cardputer with Claude Code over BLE; watch
  agent runs, token spend, and queue depth from your pocket.
- **Push to Claude** — hold SPACE to record a voice question, release to
  send. Whisper transcribes, Claude Haiku 4.5 replies on the LCD, and a
  per-device 24-hour memory keeps context across turns. Type-mode also
  available for noisy rooms.
- **Claude Pager** — type a task on the QWERTY, fire it off as a
  long-running [Managed Agents] session in the cloud, and watch live
  status (`bash: pytest …`, `wrote auth_test.py`, `idle ✓`) on the LCD.
  Inbox lists active sessions; Detail screen lets you reply, interrupt,
  or approve pending tool calls from your pocket. Pairs with the
  **Central Console** browser UI on your Mac and the `claude-pull`
  artifact sync.
- **Cardputer MCP** — turn the device into a pocket pager for any
  Model-Context-Protocol-speaking agent (Claude Code, Cursor,
  Claude Desktop, Managed Agents, etc.). The agent can buzz you
  with a colored banner + speaker chirp (`notify`), ask a
  multiple-choice question you answer on the QWERTY (`ask`), or
  demand a physical gesture for destructive operations (`confirm`).
  Runs locally over stdio/BLE — no cloud, no Wi-Fi required — **and
  now over an [MCP tunnel] so cloud agents (Managed Agents, the
  Messages API) can reach the device in your pocket too.** The
  hold-to-confirm gesture becomes a **hardware approval key**: an
  autonomous cloud agent physically cannot run an irreversible
  operation without your thumb on the device — no prompt injection
  can synthesize a sustained keypress, and an unreachable device
  fails closed (the agent stops, never auto-proceeds).

[MCP tunnel]: https://platform.claude.com/docs/en/agents-and-tools/mcp-tunnels/overview

- **Hello / Snake** — minimal example app + a snake game so the bundle
  isn't all serious business.

[Managed Agents]: https://platform.claude.com/docs/en/managed-agents/overview

## Use cases

The Cardputer is a credit-card-sized ESP32 with a QWERTY keyboard,
240×135 LCD, speaker, and microphone. Paired with Claude, it becomes
something no phone or laptop can be: a dedicated physical interface
between you and your AI agents. Here are the ways people are using it.

### 🛡️ Physical 2FA for AI agents

The `confirm` tool demands a sustained 3-second keypress on the
Cardputer before a destructive operation proceeds. No prompt injection,
hallucination, or runaway agent loop can synthesize a physical gesture
on a device in your pocket. Use it as a hardware gate before:

- Production deploys and force pushes
- Database mutations (`DROP TABLE`, `DELETE` without `WHERE`)
- Financial transactions or paid API calls
- File deletions that can't be undone

This is the "2FA for AI" moment — the agent asks permission through a
channel it cannot compromise.

### 📟 Pocket agent monitor

Fire off a [Managed Agents] coding task from the QWERTY and watch the
agent work in real time from your pocket. The Pager app shows a live
ticker of what the agent is doing — `bash: pytest`, `wrote auth.py`,
`idle ✓` — and buzzes you when it finishes or needs approval. Walk
away from your laptop, grab coffee, and still know the moment your
CI is green or a deploy needs a Y/N.

### 🎤 Hands-free voice assistant

Tap SPACE, ask a question out loud, and Claude Haiku 4.5 answers on the
LCD. Whisper handles transcription; a 24-hour per-device memory keeps
context across turns. Switch to type-mode with `T` for noisy
environments. Useful for:

- Quick lookups while your hands are busy (cooking, soldering, walking)
- Rapid-fire Q&A sessions without context-switching to a browser
- Learning and tutoring — ask follow-up questions naturally

### 🔔 MCP notification pager

Register the Cardputer as an MCP server with Claude Code, Cursor, or
any MCP-speaking client. The agent can:

- **Buzz you** with a colored banner and speaker chirp when a long task
  finishes — no need to keep a terminal tab in focus
- **Ask a quick question** with numbered choices (press 1–4 on the
  QWERTY) — the agent blocks until you answer, then continues
- **Demand confirmation** before anything irreversible — see "Physical
  2FA" above

All over local BLE. No cloud, no Wi-Fi, sub-second round trips. The
device works offline and in airplane mode.

### 🖥️ Browser-based agent console

The Central Console is a self-contained dark-theme HTML UI served
directly from the Cloudflare Worker. No build step, no dependencies.
Use it on your Mac to:

- Watch live event streams from running agents with syntax-highlighted
  bash and inline diffs
- Approve or deny pending tool calls from the browser
- Browse and download artifacts the agent saved
- Fire new tasks — they appear on the Cardputer's Pager too

### 🔧 Portable task launcher for coding agents

Type a task on the Cardputer's QWERTY keyboard (`refactor auth module`,
`write tests for the API`, `research X and write a summary`) and press
Enter. A cloud agent spins up, does the work, and the device notifies
you when it's done. Reply from the device if the agent needs guidance,
or interrupt it if it goes off-track. All without opening a laptop.

### 📊 BLE dev environment sidecar

Claude Buddy pairs with Claude Desktop's Hardware Buddy mode over BLE.
Glance at the Cardputer to see:

- Current agent activity and approval prompts
- Token spend and queue depth
- Connection status

Approve or deny tool calls with Y/N on the keyboard — useful when
Claude is running long autonomous sessions and you want a physical
killswitch nearby.

### 🧪 Hardware prototyping platform

The bundle is designed to be extended. Drop a `.py` file into
`buddy/device/apps/`, push it to the device, and it shows up in the
launcher automatically. The Cardputer gives you:

- Full QWERTY keyboard input
- 240×135 color LCD
- BLE and WiFi radios
- Speaker and PDM microphone (Cardputer-Adv)
- MicroPython with M5Stack's hardware libraries

Use it as a starting point for your own MCP tools, custom agent
interfaces, or IoT experiments. Crib from
[`hello_cardputer.py`](buddy/device/apps/hello_cardputer.py) for the
minimal conventions.

---

> **Forked from** [`moremas/build-with-claude`](https://github.com/moremas/build-with-claude).
> This fork adds the `worker/` directory (a Cloudflare Worker that
> handles voice STT + Claude chat with conversation memory) and the
> `Push to Claude` device app that talks to it. Everything else
> originates from upstream — credit and thanks to the original authors.

## What's new in this fork

| Addition                         | Where                                                                                        | What it does                                                                                                                                                                                                                                                                                                                        |
| -------------------------------- | -------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Cardputer MCP (host bridge)**  | [`mcp/`](mcp/)                                                                               | Model Context Protocol server (`bleak`-based) that any Claude/MCP client can register, over **stdio or streamable-http**. Six tools: `notify`, `ask`, `confirm`, `show`, `progress`, `device_status`. Talks BLE to the device app, and logs every `confirm` decision to a local consent audit trail (`~/.cardputer-mcp/audit.log`). |
| **MCP tunnel + HTTP daemon**     | [`mcp/auth.py`](mcp/auth.py) + [`tunnel/`](tunnel/) + [`mac/`](mac/)                         | The cloud-bridge path. `CARDPUTER_HTTP=1` runs the same server as a bearer-authed streamable-http daemon (launchd); `tunnel/` (cloudflared + mcp-proxy) exposes it through an Anthropic [MCP tunnel] so Managed Agents / the Messages API can `notify`/`ask`/`confirm` on the device — outbound-only, fail-closed.                  |
| **Cardputer MCP (device app)**   | [`buddy/device/apps/cardputer_mcp.py`](buddy/device/apps/cardputer_mcp.py)                   | BLE GATT peripheral on a fresh service UUID block (`a5cd0001-…`), distinct from Buddy's NUS. Renders notifications, ask-question modals, and a hold-Y confirmation gesture; sends acks via TX notifications.                                                                                                                        |
| **Cloudflare Worker relay**      | [`worker/`](worker/)                                                                         | Auth-gated edge endpoint. Whisper for STT, Claude Haiku 4.5 for the reply, Workers KV for per-device conversation memory (last 8 messages, 24 h TTL).                                                                                                                                                                               |
| **Voice + chat app**             | [`buddy/device/apps/push_to_claude.py`](buddy/device/apps/push_to_claude.py)                 | On-device client. Streams WAV to the Worker as it records (flat RAM footprint), text-fallback mode, scrollable replies, `/reset` shortcut.                                                                                                                                                                                          |
| **Pager device app**             | [`buddy/device/apps/pager.py`](buddy/device/apps/pager.py)                                   | Three-screen UI (Compose / Inbox / Detail) for firing and triaging Managed Agents sessions from the QWERTY. Long-polls the Worker for live event ticker.                                                                                                                                                                            |
| **SessionRouter Durable Object** | [`worker/src/router.do.js`](worker/src/router.do.js)                                         | One DO per Anthropic session. Lazily polls the Managed Agents `events.list` endpoint, mirrors events into DO storage, and serves both the Pager (poll) and Console (SSE).                                                                                                                                                           |
| **Central Console (browser)**    | [`worker/src/console.html`](worker/src/console.html)                                         | Single-file dark-theme HTML console served from the Worker. Live event stream, syntax-highlighted bash, inline diffs for `str_replace`, file pills, interrupt + reply. Token-gated, no build step.                                                                                                                                  |
| **Mac artifact sync**            | [`mac/claude-pull`](mac/claude-pull) + [`launchd plist`](mac/com.claude.pager.pull.plist)    | Stdlib Python script run every 60 s by launchd. Pulls each session's `/workspace/out/` files into `~/ClaudeRuns/<title>-<id>/` and posts a banner notification when a session completes.                                                                                                                                            |
| **Externalized device config**   | [`buddy/device/apps/config.example.py`](buddy/device/apps/config.example.py)                 | Worker URL + device secret loaded from a gitignored `config.py` so secrets never enter the repo.                                                                                                                                                                                                                                    |
| **Cardputer Companion skill**    | [`.claude/skills/cardputer-companion/SKILL.md`](.claude/skills/cardputer-companion/SKILL.md) | Instructions-only Agent Skill. The behavioral counterpart to the MCP server: it teaches Claude _when_ to reach for `notify`/`ask`/`confirm` and _how_ to format for the 240×135 LCD — mandating physical `confirm` before irreversible ops, buzzing only on long-task completion, and otherwise staying quiet.                      |

See [`worker/README.md`](worker/README.md) for the full Cloudflare deploy
guide.

## Buy a Cardputer

The bundle targets the **M5Stack Cardputer-Adv** (the version with PDM
mic + speaker, required for Push to Claude). Get one direct from
[shop.m5stack.com](https://shop.m5stack.com/) — search "Cardputer".
The original Cardputer (non-Adv) works for everything except the voice
app.

## Quick start — flash a Cardputer

1. Clone this repo locally — anywhere is fine:
   ```bash
   git clone https://github.com/dakshaymehta/cardputer-claude-os.git
   ```
   The skill auto-detects the buddy bundle relative to its own install location, so the clone path doesn't matter. `~/Downloads/m5stack/` and `~/Desktop/m5stack/` are also checked as conventional fallbacks.
2. Plug the Cardputer into your laptop via USB-C
3. Open Claude Code and start a new chat
4. Point Claude Code to the repo folder
5. Type `m5-onboard go`

That's it — Claude will automatically flash the firmware and push the apps onto the device.

### When Claude prompts you to put the device into download mode

Halfway through, Claude will pause and ask you to do this on the **back** of the device:

1. Hold down the **G0** button on the Cardputer
2. While still holding G0, press the **Reset** button
3. Release Reset first, then release G0
4. The screen goes dark — device is in download mode

Claude takes over from there.

### What happens next

- **Firmware writes to the device** (~180 seconds)
- **Apps push to the device** (~100 seconds)
- **Device reboots** straight into the launcher — pick an app and go

Done. Power the device on/off with the side switch.

---

## Quick start — Cardputer MCP (let any agent reach the device)

Turn the Cardputer into a pocket pager that any MCP-speaking client
— Claude Code, Claude Desktop, Cursor, Codex, Managed Agents (via the
[MCP tunnel](#quick-start--cardputer-over-mcp-tunnels-cloud-agents)
below), or anything that supports the Model Context Protocol — can
reach. Six tools land on first connect:

- `cardputer.notify(title, body, urgency)` — flash a banner on the
  device and chirp the speaker. Urgency colors the header
  (info=dark, warn=yellow, crit=red) and varies the beep pattern.
  Returns once the banner is shown; auto-clears after 5 s. A
  per-agent floor (default ~1 non-`crit` notify per 60 s) drops
  spammy banners with `rate-limited`; `crit` always rings.
- `cardputer.ask(question, choices, timeout_s)` — show a numbered
  multiple-choice question; the user presses 1–4 on the QWERTY;
  the chosen string returns to the agent. Blocks the agent until
  the user answers, ESCs, or `timeout_s` elapses.
- `cardputer.confirm(title, details, timeout_s)` — display a red
  danger banner and demand a physical gesture before resolving as
  `confirmed`. The whole point is that a prompt injection cannot
  synthesize a sustained physical keypress. Reserve this for
  irreversible operations (deploys, force pushes, DROP TABLE,
  charges, etc.). Pass `details` — the **actual** command / SQL /
  diff / payee — and the device renders it in a **scrollable action
  diff above the gesture**, so the user approves _what they read_,
  not just an 18-char title (the hardware-wallet model). On the
  current firmware the gesture is **rapid Y taps** (the screen says
  "TAP Y fast for 3s") because the keyboard driver has no
  auto-repeat — see _Known limitations_ in
  [`mcp/README.md`](mcp/README.md). Every `confirm` decision
  (confirmed, cancelled, or timed out) is appended to a local
  **consent audit log** at `~/.cardputer-mcp/audit.log` — who asked,
  what they asked you to approve, and the outcome — so the physical
  gesture leaves a durable trail you can review later.
- `cardputer.show(text, channel)` — write one **ambient status
  line** to the device's idle screen (silent, non-blocking, ignores
  DND). Glance at your pocket to see what a long task is doing
  (`running pytest`, `wrote auth.py`, `idle`); each `channel` gets
  its own line so several agents can share the screen.
- `cardputer.progress(label, percent, channel)` — draw a **live
  progress bar** (0–100%) on the idle screen — the visual sibling of
  `show`. Call it as a long task advances (`0 → 25 → 60 → 100`) and a
  green bar fills on your pocket; it shares the `show` channel ring, so
  a channel can flip between a status line and a bar. Silent, ambient,
  ignores DND.
- `cardputer.device_status()` — a **read-only** check of whether the
  device is reachable and its state (`online; dnd=off; fw=0.4.2;
caps=…; battery=87%`). Passive (no radio wake), so an agent can ask
  "is my human reachable / heads-down?" before deciding to interrupt,
  powered by a ~10 s device heartbeat.

The whole stack is local — stdio MCP between your client and the
host-side `bleak` bridge, then BLE-GATT to the device. No cloud
trip, no Wi-Fi required. Pairing cache lives at
`~/.cardputer-mcp/paired.json` so reconnects skip the BLE scan.

### Setup

1. **Push the device app** (no firmware re-flash needed if you've
   already onboarded the device):

   ```bash
   python3 .claude/skills/m5-onboard/scripts/install_apps.py \
       --port <PORT> --src buddy
   ```

2. **Set up the host bridge:**

   ```bash
   cd mcp
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Register the MCP server with Claude Code:**

   ```bash
   claude mcp add cardputer \
       "$(pwd)/.venv/bin/python" \
       "$(pwd)/server.py"
   ```

   (Cursor, Codex, Claude Desktop, etc. each have their own
   MCP-server registration UI; point them at the same
   `.venv/bin/python server.py` pair.)

4. **On the device:** boot the launcher, pick **cardputer_mcp**
   from the menu. The screen will read `waiting for bridge` and
   display the device's `CardputerMCP_XXXXXX` BLE name. First
   tool call from the agent triggers a scan + connect; the
   screen flips to a green `READY`.

5. **Try it.** In a fresh Claude Code session:

   > Buzz the Cardputer with title "tests passing" and body
   > "127 ok in 4.2s".

   You should see the banner flash on the device, hear a chirp,
   and Claude gets back `"shown"`. The whole round trip is
   sub-second after the first connect.

### Make Claude reach for the device on its own (the companion skill)

The three tools above are the device's _hands_; out of the box Claude
only uses them when you explicitly ask it to ("buzz the Cardputer…").
The bundled [`cardputer-companion`](.claude/skills/cardputer-companion/SKILL.md)
Agent Skill adds the _manners_ — it teaches Claude **when** to reach
for them and **how** to format for the tiny screen, with no extra code:

- **Confirm before irreversible ops.** Production deploys, force pushes,
  `DROP TABLE`, `rm -rf`, paid side-effects → a physical hold-to-confirm
  gesture, never a chat-message "are you sure?" (which a prompt injection
  could forge). If the device is unreachable, Claude stops rather than
  treating an absent safety device as approval.
- **Buzz only when it matters.** One `notify` on completion of a
  genuinely long task you're waiting on away from the keyboard — not a
  play-by-play. The default posture is _quiet_.
- **Ask when blocked and you're away** — a 2–4 choice question on the
  QWERTY instead of stalling in chat.
- **240×135 formatting** baked into every device message.

It loads automatically in Claude Code whenever the `cardputer` MCP
tools are registered (the skill lives at `.claude/skills/` in this
repo, so it's discovered alongside `m5-onboard`). Nothing to install;
nothing to configure. To see it in action, register the MCP server,
then give Claude a real task — e.g. "run the test suite and let me
know how it goes" — and step away from the laptop.

### macOS Bluetooth permission

The first BLE scan from the bridge triggers a macOS permission
prompt. Approve it once; `bleak` caches the grant. If Claude Code
runs inside a sandboxed terminal multiplexer that's not seeing
the prompt, grant Bluetooth to the terminal app itself under
**System Settings → Privacy & Security → Bluetooth**.

### Verifying without an MCP client

[`mcp/smoke_test.py`](mcp/smoke_test.py) exercises the bridge
directly (imports `Bridge` and calls the tools as Python), with
no MCP registration needed. Useful for confirming BLE
connectivity, debugging gesture handling, and validating new
firmware before plugging it into a real session:

```bash
cd mcp && .venv/bin/python smoke_test.py
```

It runs a `notify`, then an `ask` (you press 1–3 on the device),
then a `confirm` (you tap Y rapidly per the caveat above).

See [`mcp/README.md`](mcp/README.md) for the full architecture
notes, wire-protocol pointer, known limitations, and roadmap;
the BLE wire format lives in
[`buddy/references/mcp_protocol.md`](buddy/references/mcp_protocol.md).

---

## Quick start — Cardputer over MCP tunnels (cloud agents)

The local stdio path above only works for an agent running on the same
laptop. **MCP tunnels** extend the device to **cloud Claude** — a
[Managed Agents] session in the Console or a Messages-API agent — so an
autonomous agent grinding through a 40-minute job in the cloud can buzz
the device in your pocket and, crucially, **demand a physical hold-Y
before any irreversible step**. The cloud literally cannot type on the
Cardputer's keyboard, so no prompt injection or runaway loop can forge
consent; if the device is unreachable the agent **fails closed** and
stops. It's a hardware approval key for AI.

```
 cloud Claude            your Mac (always-on)
 (Managed Agent  ─tunnel─▶  cloudflared ─▶ mcp-proxy ─▶ 127.0.0.1:9000 ─BLE─▶ Cardputer
  / Messages API)  outbound-only           (Docker)     cardputer-mcp daemon
```

The same daemon also serves **local Claude Code** over loopback, so one
BLE owner and one physical gate covers cloud and local agents alike.

[Managed Agents]: https://platform.claude.com/docs/en/managed-agents/overview

### What you need

- **MCP tunnels + Managed Agents beta access** (request in the Console).
  Tunnels work from Console Managed Agents and the Messages API — **not**
  the claude.ai consumer app.
- **Docker Desktop** and the device flashed with `cardputer_mcp`.

### Setup (≈10 min, one-time)

1. **Run the always-on bridge daemon** (owns the BLE link, serves
   streamable-http on `127.0.0.1:9000`):

   ```bash
   ./mac/install_cardputer_bridge.sh        # writes a stub env, exits
   $EDITOR ~/.config/cardputer-bridge/env   # set random tokens + tunnel domain
   ./mac/install_cardputer_bridge.sh        # renders + loads the launchd agent
   ```

   Approve the one-time macOS Bluetooth prompt for the daemon.

2. **Point local Claude Code at the same daemon** (unified gate — the
   installer prints this with your token filled in):

   ```bash
   claude mcp add --transport http cardputer \
       http://127.0.0.1:9000/mcp \
       --header "Authorization: Bearer <your-local-token>"
   ```

3. **Stand up the tunnel** and attach it to a cloud agent — full
   walkthrough (Console steps, cert generation, Managed-Agent +
   Messages-API usage, and a 6-step verification checklist) in
   [`tunnel/README.md`](tunnel/README.md):

   ```bash
   cd tunnel
   cp env.example .env && $EDITOR .env      # TUNNEL_DOMAIN + TUNNEL_TOKEN
   ./gen-certs.sh                           # CA + server cert; upload data/ca.crt in Console
   docker compose up -d
   ```

   Then attach `https://cardputer.<your-tunnel-domain>/mcp` (with your
   cloud bearer token) to a Managed Agent and ask it to confirm a
   destructive op — the device flashes red with `from:managed-agent`,
   you tap Y rapidly for ~3s, the agent proceeds.

### Security model in one breath

Outbound-only (no inbound ports); inner TLS terminated by a cert **only
you** hold (Cloudflare can't read payloads); a **bearer token** on the
daemon gates the otherwise-unauthenticated tunnel and labels which agent
is asking; the **physical gesture** is the un-forgeable consent; and
**fail-closed** means a dark device is never a yes. The
[`cardputer-companion`](.claude/skills/cardputer-companion/SKILL.md)
skill teaches Claude to honor all of this. Every `confirm` decision is
recorded to a local **consent audit log** (`~/.cardputer-mcp/audit.log`)
— the daemon-authored first rung of the receipts ladder. On-device
action diffs ship today; _cryptographically_ signed consent receipts and
multi-person quorum are documented as a future ladder in
[`docs/superpowers/`](docs/superpowers/).

---

## Quick start — Push to Claude (voice + chat)

The voice app needs a Cloudflare Worker you control. Roughly 10 minutes
of one-time setup; after that every voice/text turn is a single tap.

1. Deploy the Worker — follow [`worker/README.md`](worker/README.md). You'll end up with a Worker URL and a `DEVICE_SECRET` you generated.
2. Point the device at it:
   ```bash
   cp buddy/device/apps/config.example.py buddy/device/apps/config.py
   ```
   Edit `config.py`, paste in your `WORKER_BASE` and `DEVICE_SECRET`.
3. Push the apps to the Cardputer (no firmware re-flash needed):
   ```bash
   python3 .claude/skills/m5-onboard/scripts/install_apps.py --port <PORT> --src buddy
   ```
4. Boot the device → **Push to Claude** → tap SPACE.

`config.py` is gitignored — your secret stays on your machine.

---

## Quick start — Claude Pager + Central Console (cloud agents)

The Pager turns the Cardputer into a remote control + status display
for [Anthropic Managed Agents] sessions. Type a task on the QWERTY,
fire it, and watch the device tick through `bash`, `write`, `idle ✓`
in real time. A self-contained HTML console on your Mac mirrors the
same sessions with a full terminal-style event log; a launchd job
syncs artifacts the agent saves into `~/ClaudeRuns/`.

[Anthropic Managed Agents]: https://platform.claude.com/docs/en/managed-agents/overview

The Pager rides on the same Worker as Push to Claude — finish that
quick start first. Then:

1. **Provision an extra KV namespace** for the session index:

   ```bash
   cd worker
   npx wrangler kv namespace create INDEX
   ```

   Paste the returned id into `worker/wrangler.toml`, replacing
   `REPLACE_WITH_YOUR_INDEX_KV_ID`.

2. **Add the Durable Object migration** (already in `wrangler.toml`)
   and redeploy:

   ```bash
   npx wrangler deploy
   ```

   First deploy registers the `SessionRouter` DO via the v1 migration
   block; subsequent deploys are normal.

3. **Open the Central Console** at
   `https://<your-worker>.workers.dev/console`. On first load it asks
   for your `DEVICE_SECRET` (same value as the Cardputer); the secret
   is stored in browser localStorage and never sent anywhere except
   your Worker. Hit `+ New`, type a task, watch it run.

4. **Push the Pager app** to the Cardputer. It ships as pre-compiled
   bytecode (`.mpy`) because the source-form is too large to parse
   inside the launcher's residual heap:

   ```bash
   pip3 install --user --break-system-packages mpy-cross
   python3 buddy/scripts/push_pager_mpy.py --port <PORT>
   ```

   Reboot the device and pick **Pager** from the launcher menu.

5. **(Optional) Mac artifact sync.** Agents save user-facing
   artifacts into `/workspace/out/` inside their container. The
   `mac/claude-pull` script mirrors them to `~/ClaudeRuns/<title>-<id>/`
   on a 60-second launchd schedule and pings you with a banner when
   a session completes.
   ```bash
   ./mac/install_launchd.sh        # writes a stub config and exits
   $EDITOR ~/.config/claude-pager/config.json   # paste worker_base + device_secret
   ./mac/install_launchd.sh        # second run actually loads launchd
   ```
   Logs land at `/tmp/claude-pull.{out,err}.log`. Run manually with
   `./mac/claude-pull -v`.

### Using the Pager

Three screens, switched with the arrow cluster:

```
COMPOSE   ← →   INBOX   →   DETAIL
                            (Enter on a row)
```

- **Compose** — type a task and Enter to launch. `→` jumps to Inbox
  without sending.
- **Inbox** — live list of recent sessions with status pip + last-tool
  subline. Refreshed every 4 s. Up/Down to scroll, Enter to drill into
  Detail, `D` to delete, `N` to jump back to Compose.
- **Detail** — live ticker for one session. Long-polls the Worker so
  deltas show within ~1 s of the agent acting.
  - `R` reply (sends a follow-up message)
  - `I` interrupt (sends `user.interrupt`)
  - `Y/N` approve/deny pending tool confirmation (when present)
  - `Esc` back to Inbox

Notifications fire across **every** screen — the Pager polls the
Worker every 15 s in the background. When an agent transitions:

| Trigger                   | Beep               | Banner                        |
| ------------------------- | ------------------ | ----------------------------- |
| `running` → `idle`        | A5 → E6 chirp      | green **DONE: <title>**       |
| pending tool confirmation | triple D6 urgent   | yellow **NEEDS YOU: <title>** |
| → `terminated`            | A4 → A3 descending | red **ERROR: <title>**        |

State is persisted to `/flash/.pager_notif.json` so the same DONE
doesn't re-fire after a reboot.

### Using the Central Console

Browser tab at `<your-worker>/console`. Token-gated, dark theme,
monospace. Left rail = sessions, main pane = event stream with:

- syntax-highlighted bash blocks
- inline diffs for `str_replace` tool calls
- collapsible tool-result blocks
- pending-confirmation `y`/`n` buttons in the composer
- file pills along the bottom — click to download

Press `n` (when no input is focused) to fire a new task. Use `⌘/Ctrl-Enter`
in the composer or the spawn modal to send.

### Cost guard

Each Managed Agents session keeps a cloud container hot for its
lifetime — typically a few cents to a couple of dollars per task.
The Worker enforces a per-device daily spawn cap (`PAGER_DAILY_SPAWN_CAP`
in `wrangler.toml`, default 30). Bump or lower it to taste.

---

## Using Claude Buddy (BLE)

1. Power on the Cardputer
2. Pick **Claude Buddy** from the launcher menu
3. In Claude Desktop: **Help → Troubleshooting → Enable Developer Tools** (one-time, persists)
4. Then **Developer menu → Hardware Buddy → Connect**

## WiFi auto-connect

The launcher tries to bring up WiFi on every boot and shows the result
on screen — `Connected · IP: 192.168.x.x` on success, `WiFi: offline`
on failure (the launcher always continues either way). Out of the box
the credentials in [`buddy/device/wifi_event.py`](buddy/device/wifi_event.py)
are blank, so you'll see `WiFi: offline`. Edit that file to fill in
your own SSID + password, or remove the `_connect_wifi_with_splash()`
call near the top of `main()` to skip the auto-connect entirely.

## Adding your own app

1. Drop a `.py` file into `buddy/device/apps/`
2. Push just the apps without re-flashing:
   ```bash
   python3 .claude/skills/m5-onboard/scripts/install_apps.py --port <PORT> --src buddy
   ```
3. The launcher auto-discovers the new app on next boot

Crib from `buddy/device/apps/hello_cardputer.py` — it's the smallest example of the conventions (keyboard polling, font, exit behaviour).

## Getting back to stock UIFlow

The buddy bundle takes over the boot flow via `/flash/main.py`. Remove
that file and UIFlow's stock launcher boots normally on the next reset.
From the device REPL:

```python
import os
os.remove('/flash/main.py')
import machine; machine.reset()
```

To also drop the apps under `/flash/apps/`, walk that directory the
same way and remove what you don't want.

If you want a fresh UIFlow firmware on top, re-run `m5-onboard go`
_without_ `--apps`: the skill flashes UIFlow and stops, leaving the
filesystem alone.

---

## Prerequisites

You need **Python 3.10+**, **git**, and **Claude Code** on your laptop. `pyserial` ships vendored inside `.claude/skills/m5-onboard/scripts/vendor/`. `esptool` is GPL-licensed and is **not** vendored — the skill auto-installs it via pip on first run if it isn't already in your environment, so the user-facing experience is still a single command. To pre-install explicitly: `python3 -m pip install --user -r requirements.txt`.

For the Push-to-Claude Worker you also need **Node.js 18+** and a
Cloudflare account; full instructions in [`worker/README.md`](worker/README.md).

Bootstrap if needed:

- **macOS** — `python3` usually pre-installed; if not, `brew install python`
- **Linux (Debian/Ubuntu)** — `sudo apt-get install -y python3 python3-pip git`
- **Windows** — `winget install -e --id Python.Python.3.13` and `winget install -e --id Git.Git`

**Windows + older boards only:** the CH9102 USB-UART driver is needed for Basic / Fire / Core2 / StickC. Download from [WCH](https://www.wch.cn/downloads/CH343SER_EXE.html). Cardputer-Adv and CoreS3 use the in-box composite-USB driver and need nothing extra.

**Want `--apps buddy` to point at a different bundle?** The default resolves to the `buddy/device/` directory next to the skill in this repo, with `~/Downloads/m5stack/` and `~/Desktop/m5stack/` checked as fallbacks. To override (e.g. you maintain a fork or have a customized bundle elsewhere), set `M5_BUDDY_DIR`:

```bash
export M5_BUDDY_DIR=/path/to/buddy/device
```

## Troubleshooting

- **Download-mode prompt keeps retrying** — you're releasing G0 too early. Release Reset first, keep holding G0 for about a second, then release.
- **"No USB-UART bridge found" (older boards)** — install the CH9102 driver on Windows; on macOS/Linux, unplug and replug.
- **Claude Buddy never connects over BLE** — make sure the buddy launcher (not UIFlow's) owns `/flash/main.py`. The skill handles this automatically on install.
- **Push to Claude shows "Not configured"** — copy `config.example.py` to `config.py` and fill in `WORKER_BASE` + `DEVICE_SECRET`, then re-push the apps.
- **Push to Claude returns "unauthorized"** — the `DEVICE_SECRET` in `config.py` doesn't match the one set on the Worker. Re-run `wrangler secret put DEVICE_SECRET` and update `config.py` to match.
- **Something else feels broken** — run `python3 .claude/skills/m5-onboard/scripts/smoke_test.py --port <PORT>` for an I2C + LCD + speaker + button check.

## What's in this repo

- **`.claude/skills/m5-onboard/`** — the onboarding skill. Detect port, flash UIFlow, install apps. See [`.claude/skills/m5-onboard/SKILL.md`](.claude/skills/m5-onboard/SKILL.md) for the full playbook and every gotcha baked into the scripts.
- **`.claude/skills/cardputer-companion/`** — the runtime-etiquette skill. Teaches Claude when and how to use the Cardputer MCP tools (`notify`/`ask`/`confirm`). Instructions only — no scripts. See [`.claude/skills/cardputer-companion/SKILL.md`](.claude/skills/cardputer-companion/SKILL.md).
- **`buddy/`** — the MicroPython app bundle that gets installed. See [`buddy/README.md`](buddy/README.md) for device-side layout and iteration tooling.
- **`worker/`** — the Cloudflare Worker that powers Push to Claude (voice + chat memory). See [`worker/README.md`](worker/README.md) for deploy instructions.

The three are decoupled by design: the `m5-onboard` skill can install any bundle via `--apps <path>`, `buddy` is just what ships here, and the worker is optional (only the Push-to-Claude app uses it).

## Contributing

PRs welcome — especially new launcher apps, new boards, and improvements
to the onboarding flow. Open an issue first if you're planning anything
non-trivial. The code is small and intentionally tries to stay readable
end-to-end.

## License

This project's own code is licensed under **Apache 2.0** — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

`pyserial` (BSD-3-Clause, Apache-compatible) is the only third-party package bundled in `.claude/skills/m5-onboard/scripts/vendor/`. `esptool` (GPLv2+) is intentionally not vendored; it's declared as a pip dependency in [`requirements.txt`](requirements.txt) so the repository itself stays cleanly Apache-2.0. See [`LICENSE-THIRD-PARTY.md`](LICENSE-THIRD-PARTY.md) for details.

Forked from [`moremas/build-with-claude`](https://github.com/moremas/build-with-claude); upstream Apache-2.0 license preserved in `LICENSE` and `NOTICE`.
