# Cardputer Worker

A small Cloudflare Worker that powers two surfaces on the Cardputer:

- **Push to Claude** — single-turn voice/text chat with Haiku.
- **Claude Pager + Central Console** — fire-and-monitor cloud agents
  using the [Managed Agents API]. Sessions run in cloud containers
  with bash + file + web tools, stream events back through the Worker,
  and sync artifacts to your Mac.

```
Cardputer ──► Cloudflare Worker ──► OpenAI Whisper          (chat: voice STT)
   Pager        ├─► /v1/messages           Claude Haiku    (chat: reply)
   Browser  ──► ├─► /v1/sessions/...       Managed Agents  (pager + console)
   Mac sync ──► └─► /v1/files/...          Files API       (artifact pull)
                  │
                  ├─ KV "HISTORY"   per-device chat memory (24h TTL)
                  ├─ KV "INDEX"     per-device session list, agent IDs, spend
                  └─ DO  SessionRouter  one-per-session event mirror
```

[Managed Agents API]: https://platform.claude.com/docs/en/managed-agents/overview

## Endpoints

### Push to Claude (single-turn chat)

| Method | Path        | Body            | Returns                       |
| ------ | ----------- | --------------- | ----------------------------- |
| `POST` | `/ask`      | raw WAV audio   | `{ transcript, response }`    |
| `POST` | `/ask-text` | JSON `{prompt}` | `{ transcript, response }`    |
| `POST` | `/reset`    | empty           | `{ ok: true, cleared: true }` |
| `GET`  | `/`         | —               | health probe                  |

### Pager (Cardputer-side, polling)

| Method | Path               | Body / params                          | Returns                        |
| ------ | ------------------ | -------------------------------------- | ------------------------------ |
| `POST` | `/pager/spawn`     | `{prompt, title?, kind?, session_id?}` | `{ok, session_id, title}`      |
| `GET`  | `/pager/sessions`  | —                                      | `{sessions: [...]}`            |
| `GET`  | `/pager/poll`      | `?session=<id>&since=<seq>&wait=1`     | `{meta, summary, events, seq}` |
| `POST` | `/pager/reply`     | `{session_id, prompt}`                 | `{ok}`                         |
| `POST` | `/pager/interrupt` | `{session_id, prompt?}`                | `{ok}`                         |
| `POST` | `/pager/confirm`   | `{session_id, tool_use_id, approve}`   | `{ok}`                         |
| `POST` | `/pager/delete`    | `{session_id}`                         | `{ok}`                         |
| `POST` | `/pager/rename`    | `{session_id, title}`                  | `{ok, meta}`                   |

### Central Console (browser-side, SSE)

| Method | Path                                                     | Notes                                        |
| ------ | -------------------------------------------------------- | -------------------------------------------- |
| `GET`  | `/console`                                               | Self-contained HTML console UI               |
| `GET`  | `/console/stream?session=<id>`                           | Server-Sent Events stream of session events  |
| `GET`  | `/console/sessions`                                      | Same shape as `/pager/sessions`              |
| `GET`  | `/console/files?session=<id>`                            | List artifact files in the session container |
| `GET`  | `/console/file?session=<id>&file_id=<fid>`               | Stream one artifact file                     |
| `POST` | `/console/{spawn,reply,interrupt,confirm,delete,rename}` | Mirror of pager/\*                           |

All authenticated endpoints accept either `x-device-secret` (header,
Cardputer) or `?token=` (query string, browser). Both must match the
Worker's `DEVICE_SECRET` secret.

## One-time setup

You'll need:

- A Cloudflare account (free tier is fine for this volume)
- An [Anthropic API key](https://console.anthropic.com/)
- An [OpenAI API key](https://platform.openai.com/api-keys) (for Whisper STT — only needed if you want voice; you can skip if you only use `/ask-text`)
- Node.js 18+ on your laptop

### 1. Install Wrangler and log in

```bash
cd worker
npm install
npx wrangler login
```

### 2. Create the KV namespaces

Two namespaces — one for chat history (Push to Claude), one for the
session index (Pager + Console).

```bash
npx wrangler kv namespace create HISTORY
npx wrangler kv namespace create INDEX
```

Wrangler prints an `id` for each. Paste them into `worker/wrangler.toml`,
replacing `REPLACE_WITH_YOUR_HISTORY_KV_ID` and `REPLACE_WITH_YOUR_INDEX_KV_ID`.

### 3. Set the secrets

```bash
npx wrangler secret put ANTHROPIC_API_KEY   # used for Haiku chat AND Managed Agents
npx wrangler secret put OPENAI_API_KEY      # Whisper for voice (skip if you don't use voice)
npx wrangler secret put DEVICE_SECRET       # any random 32+ char string
```

**Using a gateway for chat (optional).** Push to Claude only needs
`POST /v1/messages`, so it can go through any Anthropic-compatible
gateway (OmniRoute, LiteLLM, …). Set `CHAT_BASE_URL` (and usually
`CHAT_MODEL`, e.g. an OmniRoute combo name) under `[vars]` in
`wrangler.toml`, and put the gateway key in a separate secret:

```bash
npx wrangler secret put CHAT_API_KEY
```

The key is sent as both `x-api-key` and `Authorization: Bearer`. The
gateway must be reachable at a public HTTPS URL, since the Worker runs
on Cloudflare's edge, not your machine. The Pager and Console still need
`ANTHROPIC_API_KEY` against `api.anthropic.com`, because gateways don't
proxy the Managed Agents API.

Voice transcription works the same way. Set `STT_BASE_URL` and
`STT_MODEL` (e.g. `groq/whisper-large-v3-turbo` through OmniRoute) to
use any OpenAI-compatible `/v1/audio/transcriptions` instead of OpenAI
Whisper. It reuses `CHAT_API_KEY` unless you set `STT_API_KEY`, and
`OPENAI_API_KEY` is then not needed.

To keep your KV ids and gateway URLs out of git, put them in a copy
named `worker/wrangler.local.toml` (gitignored) and deploy with
`npx wrangler deploy -c wrangler.local.toml`.

Generate a `DEVICE_SECRET` with:

```bash
openssl rand -base64 32
```

Save the same `DEVICE_SECRET` — you'll paste it into the device
config and (optionally) into the Mac sync config in later steps.

### 4. Deploy

```bash
npx wrangler deploy
```

The first deploy creates the `SessionRouter` Durable Object class via
the migration block in `wrangler.toml`. Subsequent deploys are normal.

Wrangler prints your Worker URL, e.g.
`https://push-to-claude.<your-subdomain>.workers.dev`. The console
lives at `<that URL>/console`. Save the base URL too.

### 5. Point the device at your Worker

On your laptop, in the cloned repo:

```bash
cp buddy/device/apps/config.example.py buddy/device/apps/config.py
```

Edit `buddy/device/apps/config.py`:

```python
WORKER_BASE = "https://push-to-claude.<your-subdomain>.workers.dev"
DEVICE_SECRET = "<the same DEVICE_SECRET you put on the Worker>"
```

Then push the apps to the Cardputer:

```bash
python3 .claude/skills/m5-onboard/scripts/install_apps.py --port <PORT> --src buddy
```

Boot the device → pick an app from the launcher:

- **Push to Claude** — tap SPACE to record voice, T to type.
- **Pager** — Compose screen by default; type a task and press Enter
  to fire off a Managed Agents session. → arrow to Inbox to triage
  active sessions, Enter to drill into Detail.

### 6. (Optional) Open the Central Console on your Mac

Open `https://<your-worker>.workers.dev/console` in any browser. On
first load it asks for your `DEVICE_SECRET`; store it in localStorage
and the console reconnects automatically afterward. New sessions you
fire from the device show up in the left rail; sessions you fire from
the console show up on the device. Same source of truth.

### 7. (Optional) Set up Mac artifact sync

Agents save user-facing artifacts into `/workspace/out/` inside their
container. The `mac/claude-pull` script syncs those to
`~/ClaudeRuns/<title>-<id>/` on your Mac and pings you with a banner
when a session completes.

```bash
./mac/install_launchd.sh        # writes a stub config and exits
$EDITOR ~/.config/claude-pager/config.json   # paste worker_base + device_secret
./mac/install_launchd.sh        # second run actually installs the agent
```

The launchd job runs every 60 s. Logs land at
`/tmp/claude-pull.{out,err}.log`. Run manually with `mac/claude-pull -v`.

## Local development

```bash
npx wrangler dev
```

Wrangler boots a local proxy at `http://127.0.0.1:8787` with live reload.
For local secrets, create `worker/.dev.vars` (gitignored):

```
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
DEVICE_SECRET=...
```

## Tail production logs

```bash
npx wrangler tail
```

## Cost notes

- **Whisper** (`whisper-1`) is $0.006 / minute of audio. The device caps
  recordings at 6 s, so each `/ask` is ~$0.0006.
- **Claude Haiku 4.5** is around $1 / MTok input, $5 / MTok output as of
  this writing. With a 250-token output cap and short prompts, each turn
  is well under a cent.
- **Managed Agents** (Pager + Console) is meaningfully more expensive —
  each session keeps a container hot for its lifetime, plus Opus 4.7
  inference. A short 1-minute task is typically a few cents; a long
  research/coding session can hit $0.50–$2. The Worker enforces a
  per-device daily spawn cap (`PAGER_DAILY_SPAWN_CAP`, default 30) as a
  cheap fork-bomb guard. Increase or decrease in `wrangler.toml`.
- **Workers** free tier: 100k requests/day. **KV** free tier: 100k
  reads/day, 1k writes/day. Plenty for personal use.

## Privacy

Conversation history is stored in Workers KV, keyed by `DEVICE_SECRET`,
with a 24-hour TTL. Hit `POST /reset` (the launcher binds this to a key
combo on the device) to clear it sooner. Whisper transcripts are not
stored anywhere by this Worker — they pass through to Claude and back.

Anthropic and OpenAI's data-retention policies apply to whatever you
send them. Read theirs.
