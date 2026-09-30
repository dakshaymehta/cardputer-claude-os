// Worker entrypoint.
//
// Two product surfaces share this Worker:
//
//   1. Push-to-Claude (existing) — single-turn voice/text chat with
//      Haiku. Keeps the original /ask, /ask-text, /reset endpoints
//      and KV-backed conversation history.
//
//   2. Cardputer Pager + Central Console (new) — fire-and-monitor
//      cloud agents using the Managed Agents API. Each session gets
//      a SessionRouter Durable Object that mirrors event history
//      and serves the Pager (poll) + Console (SSE) surfaces.
//
// Both surfaces auth via the same DEVICE_SECRET, sent as
// `x-device-secret` (device-side) or `?token=...` (browser).

import {
  authenticate,
  handleConfirm,
  handleDelete,
  handleInterrupt,
  handlePoll,
  handleRename,
  handleReply,
  handleSessions,
  handleSpawn,
} from "./pager.js";
import {
  handleConsolePage,
  handleFileDownload,
  handleFilesList,
  handleStream,
} from "./console_routes.js";

export { SessionRouter } from "./router.do.js";

// ---- Push-to-Claude (existing) -------------------------------------

const CHAT_SYSTEM_PROMPT =
  "You are Claude responding on a 240x135 pixel handheld LCD. " +
  "Reply in 1-3 short sentences. Plain ASCII when possible. " +
  "No markdown, no lists, no code fences. " +
  "Be direct; assume the user can't scroll. " +
  "You may receive a few prior turns of conversation history; " +
  "treat the latest user message as the current question.";

const CHAT_MODEL = "claude-haiku-4-5-20251001";
const HISTORY_MAX_MESSAGES = 8;
const HISTORY_TTL_SECONDS = 24 * 3600;

function authOk(request, env) {
  return request.headers.get("x-device-secret") === env.DEVICE_SECRET;
}

function historyKey(deviceSecret) {
  return `turns:${deviceSecret}`;
}

async function getHistory(env, deviceSecret) {
  if (!env.HISTORY) return [];
  try {
    const raw = await env.HISTORY.get(historyKey(deviceSecret));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

async function appendTurn(env, deviceSecret, userMsg, assistantMsg) {
  if (!env.HISTORY) return;
  const hist = await getHistory(env, deviceSecret);
  hist.push({ role: "user", content: userMsg });
  hist.push({ role: "assistant", content: assistantMsg });
  const trimmed = hist.slice(-HISTORY_MAX_MESSAGES);
  await env.HISTORY.put(historyKey(deviceSecret), JSON.stringify(trimmed), {
    expirationTtl: HISTORY_TTL_SECONDS,
  });
}

async function callHaiku(env, deviceSecret, userMessage) {
  const history = await getHistory(env, deviceSecret);
  const messages = [...history, { role: "user", content: userMessage }];

  const claudeResp = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-api-key": env.ANTHROPIC_API_KEY,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({
      model: CHAT_MODEL,
      max_tokens: 250,
      system: CHAT_SYSTEM_PROMPT,
      messages,
    }),
  });

  if (!claudeResp.ok) {
    const detail = (await claudeResp.text()).slice(0, 300);
    return { ok: false, status: claudeResp.status, detail };
  }
  const data = await claudeResp.json();
  // Replies can lead with a thinking block (extended thinking, or a
  // proxy fronting a reasoning model), so collect every text block
  // instead of trusting content[0].
  const blocks = Array.isArray(data.content) ? data.content : [];
  const text = blocks
    .filter((b) => b?.type === "text" && b.text)
    .map((b) => b.text)
    .join("\n")
    .trim();
  if (!text) {
    console.log("chat: no text in reply", JSON.stringify({
      model: data.model,
      stop_reason: data.stop_reason,
      types: blocks.map((b) => b?.type),
      usage: data.usage,
    }));
    return { ok: true, text: "(empty)" };
  }
  await appendTurn(env, deviceSecret, userMessage, text);
  return { ok: true, text };
}

async function handleAsk(request, env) {
  if (!authOk(request, env)) return jsonResp({ error: "unauthorized" }, 401);

  const audioBytes = await request.arrayBuffer();
  if (audioBytes.byteLength < 200) {
    return jsonResp(
      { error: "audio too short", bytes: audioBytes.byteLength },
      400,
    );
  }

  const form = new FormData();
  form.append(
    "file",
    new Blob([audioBytes], { type: "audio/wav" }),
    "audio.wav",
  );
  form.append("model", "whisper-1");
  form.append("response_format", "text");

  const whisperResp = await fetch(
    "https://api.openai.com/v1/audio/transcriptions",
    {
      method: "POST",
      headers: { Authorization: `Bearer ${env.OPENAI_API_KEY}` },
      body: form,
    },
  );
  if (!whisperResp.ok) {
    const detail = (await whisperResp.text()).slice(0, 300);
    return jsonResp(
      { error: "whisper failed", status: whisperResp.status, detail },
      502,
    );
  }
  const transcript = (await whisperResp.text()).trim();
  if (!transcript) {
    return jsonResp({ transcript: "", response: "(no speech)" });
  }

  const deviceSecret = request.headers.get("x-device-secret");
  const result = await callHaiku(env, deviceSecret, transcript);
  if (!result.ok) {
    return jsonResp(
      {
        transcript,
        error: "claude failed",
        status: result.status,
        detail: result.detail,
      },
      502,
    );
  }
  return jsonResp({ transcript, response: result.text });
}

async function handleAskText(request, env) {
  if (!authOk(request, env)) return jsonResp({ error: "unauthorized" }, 401);
  let data;
  try {
    data = await request.json();
  } catch {
    return jsonResp({ error: "invalid json" }, 400);
  }
  const prompt = ((data.prompt || data.text || "") + "").trim();
  if (!prompt) return jsonResp({ error: "empty prompt" }, 400);

  const deviceSecret = request.headers.get("x-device-secret");
  const result = await callHaiku(env, deviceSecret, prompt);
  if (!result.ok) {
    return jsonResp(
      {
        transcript: prompt,
        error: "claude failed",
        status: result.status,
        detail: result.detail,
      },
      502,
    );
  }
  return jsonResp({ transcript: prompt, response: result.text });
}

async function handleReset(request, env) {
  if (!authOk(request, env)) return jsonResp({ error: "unauthorized" }, 401);
  const deviceSecret = request.headers.get("x-device-secret");
  if (env.HISTORY) {
    await env.HISTORY.delete(historyKey(deviceSecret));
  }
  return jsonResp({ ok: true, cleared: true });
}

// ---- Router ---------------------------------------------------------

// (method, path) → handler. The Pager handlers receive the resolved
// auth object; the chat handlers do their own auth (header-only —
// voice uploads aren't sent from a browser, so no `?token=` escape
// hatch is needed there).
const PAGER_ROUTES = {
  "POST /pager/spawn": handleSpawn,
  "GET /pager/sessions": handleSessions,
  "GET /pager/poll": handlePoll,
  "POST /pager/interrupt": handleInterrupt,
  "POST /pager/reply": handleReply,
  "POST /pager/confirm": handleConfirm,
  "POST /pager/delete": handleDelete,
  "POST /pager/rename": handleRename,

  "GET /console/stream": handleStream,
  "GET /console/sessions": handleSessions,
  "GET /console/files": handleFilesList,
  "GET /console/file": handleFileDownload,
  "POST /console/spawn": handleSpawn,
  "POST /console/reply": handleReply,
  "POST /console/interrupt": handleInterrupt,
  "POST /console/delete": handleDelete,
  "POST /console/rename": handleRename,
  "POST /console/confirm": handleConfirm,
};

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const key = `${request.method} ${url.pathname}`;

    if (request.method === "GET" && url.pathname === "/") {
      return new Response("push-to-claude relay ok\n", {
        headers: { "content-type": "text/plain" },
      });
    }

    // Push-to-Claude (single-turn). Untouched.
    if (request.method === "POST" && url.pathname === "/ask")
      return handleAsk(request, env);
    if (request.method === "POST" && url.pathname === "/ask-text")
      return handleAskText(request, env);
    if (request.method === "POST" && url.pathname === "/reset")
      return handleReset(request, env);

    // Console page (no auth at the page itself; auth is on the
    // subsequent fetch calls, where the user types the secret).
    if (
      request.method === "GET" &&
      (url.pathname === "/console" || url.pathname === "/console/")
    ) {
      return handleConsolePage();
    }

    if (PAGER_ROUTES[key]) {
      const auth = await authenticate(request, env);
      if (!auth) return jsonResp({ error: "unauthorized" }, 401);
      try {
        return await PAGER_ROUTES[key](request, env, auth);
      } catch (err) {
        // Inner handlers normally shape their own errors, but DO RPC
        // and upstream Anthropic errors can bubble. Always return
        // JSON so the Pager + Console can render something useful.
        return jsonResp(
          { error: "internal", message: String(err?.message || err) },
          500,
        );
      }
    }

    return new Response("not found\n", { status: 404 });
  },
};

function jsonResp(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });
}
