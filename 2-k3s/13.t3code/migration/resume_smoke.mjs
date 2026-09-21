#!/usr/bin/env node
// Resume one existing thread on a running T3 server and wait for the provider
// to answer. Run inside the pod, where `node` and a bearer token are at hand:
//
//   export T3_TOKEN=$(t3 auth session issue --base-dir "$T3CODE_HOME" --ttl 10m --token-only)
//   node resume_smoke.mjs http://127.0.0.1:3773 <threadId> [prompt]
//
// The token comes from the environment, not argv, so it does not show in
// /proc/*/cmdline. Sends a `thread.turn.start` over the same WebSocket RPC the
// browser uses, then re-subscribes to the thread every few seconds and reads
// `thread.latestTurn.state` from the snapshot. Exit 0 on `completed` with the
// assistant's reply printed (first 80 chars), 5 on `failed`, 3 on timeout.
const [base, threadId, prompt = "Reply with exactly: RESUME-OK"] = process.argv.slice(2);
const token = process.env.T3_TOKEN;
if (!base || !token || !threadId) {
  console.error("usage: T3_TOKEN=... resume_smoke.mjs <base-url> <threadId> [prompt]");
  process.exit(2);
}
const headers = { authorization: "Bearer " + token, "content-type": "application/json" };
const ticket = async () => {
  const r = await fetch(base + "/api/auth/websocket-ticket", { method: "POST", headers, body: "{}" });
  if (!r.ok) throw new Error("ticket " + r.status + " " + await r.text());
  return (await r.json()).ticket;
};
const open = async () => {
  const ws = new WebSocket(base.replace(/^http/, "ws") + "/ws?wsTicket=" + encodeURIComponent(await ticket()));
  await new Promise((ok, ko) => { ws.onopen = ok; ws.onerror = (e) => ko(new Error("ws " + (e.message ?? "error"))); });
  return ws;
};
const request = (ws, tag, payload) => new Promise((ok, ko) => {
  const id = crypto.randomUUID();
  ws.send(JSON.stringify({ _tag: "Request", id, tag, payload, headers: [] }));
  const chunks = [];
  ws.onmessage = (m) => {
    const msg = JSON.parse(m.data);
    if (msg.requestId !== id) return;
    if (msg._tag === "Chunk") chunks.push(...msg.values);
    if (msg._tag === "Exit") msg.exit._tag === "Success" ? ok(chunks.length ? chunks : msg.exit.value) : ko(new Error(JSON.stringify(msg.exit).slice(0, 400)));
    if (msg._tag === "Defect") ko(new Error(JSON.stringify(msg).slice(0, 400)));
  };
  // subscribeThread streams forever; the first Chunk carries the snapshot.
  if (tag === "orchestration.subscribeThread") {
    const first = ws.onmessage;
    ws.onmessage = (m) => { first(m); if (chunks.length) ok(chunks); };
  }
});

const started = new Date().toISOString();
const ws = await open();
const messageId = crypto.randomUUID();
await request(ws, "orchestration.dispatchCommand", {
  type: "thread.turn.start", commandId: crypto.randomUUID(), threadId,
  message: { messageId, role: "user", text: prompt, attachments: [] },
  runtimeMode: "full-access", interactionMode: "default", createdAt: started,
});
ws.close();
console.log(threadId, "turn dispatched");

const deadline = Date.now() + 240_000;
let lastState = "";
while (Date.now() < deadline) {
  await new Promise((r) => setTimeout(r, 5000));
  const s = await open();
  const [snap] = await request(s, "orchestration.subscribeThread", { threadId });
  s.close();
  const turn = snap?.snapshot?.thread?.latestTurn;
  const state = turn?.state ?? "none";
  if (state !== lastState) { lastState = state; console.log(threadId, "latestTurn", state); }
  if (turn && turn.requestedAt >= started && (state === "completed" || state === "failed" || state === "interrupted")) {
    const msgs = snap.snapshot.thread.messages ?? [];
    const reply = msgs.filter((m) => m.role === "assistant" && m.createdAt >= started).map((m) => m.text).join(" ").trim();
    console.log(threadId, "result", state, "|", reply.replace(/\s+/g, " ").slice(0, 80));
    process.exit(state === "completed" && reply ? 0 : 5);
  }
}
console.error(threadId, "timeout in state", lastState);
process.exit(3);
