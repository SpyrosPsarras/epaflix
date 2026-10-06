// Per-step routing (jev-auto.md "Every step"): the plugin's http.request hook
// rewrites the request OpenCode is about to send to CLIProxyAPI. Jev answers
// are faked by URL; the hook gets real Request objects.
import assert from "node:assert/strict"
import { mkdtemp, writeFile, rm, readFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"

const dir = await mkdtemp(join(tmpdir(), "jev-steps-"))
const saved = { fetch: globalThis.fetch, state: process.env.XDG_STATE_HOME, key: process.env.JEV_OPENROUTER_KEY_FILE, config: process.env.XDG_CONFIG_HOME }
process.env.XDG_STATE_HOME = dir
process.env.XDG_CONFIG_HOME = dir
process.env.JEV_OPENROUTER_KEY_FILE = join(dir, "key")
await writeFile(process.env.JEV_OPENROUTER_KEY_FILE, "test-key")
const { default: plugin } = await import("./jev-auto.js")

let jevCalls = 0, jevBody, answer
globalThis.fetch = async (url, init) => {
  jevCalls++
  jevBody = JSON.parse(init.body)
  if (answer instanceof Error) throw answer
  return Response.json({ id: "req", answers: answer, usage: { cost: 0.00003 } })
}
const step = (tier, effort, tierConfidence = 0.9, effortConfidence = 0.9, dialogue = 0) =>
  ({ tier: { score: tier, confidence: tierConfidence }, effort: { score: effort, confidence: effortConfidence }, dialogue: { noul: dialogue } })

try {
  const models = {
    "gpt-6-luna": { id: "codex/gpt-6-luna" }, "gpt-6-astra": { id: "codex/gpt-6-astra" }, "gpt-6-sol": { id: "codex/gpt-6-sol" },
    "claude-haiku-4-5-20251001": { id: "claude/claude-haiku-4-5-20251001" }, "claude-sonnet-5": { id: "claude/claude-sonnet-5" },
    "claude-sonnet-5-5": { id: "claude/claude-sonnet-5-5" }, "claude-opus-5-5": { id: "claude/claude-opus-5-5" },
    "claude-fable-5-1": { id: "claude/claude-fable-5-1" }, "or-glm-5.3-flash": { id: "openrouter/or-glm-5.3-flash" },
  }
  const list = Object.entries(models).map(([id, m]) => ({ id, modelID: m.id, providerID: "cliproxy" }))
  const sessions = new Map()
  let hook
  const ctx = {
    location: { directory: "/test", project: { id: "test" } },
    model: { list: async () => ({ data: list }) },
    skill: { list: async () => ({ data: [] }) },
    provider: { transform: async () => {} },
    session: {
      hook: async (name, cb) => { if (name === "http.request") hook = cb; if (name === "prompt") sessions.prompt = cb },
      get: async ({ sessionID }) => sessions.get(sessionID),
      context: async () => [], switchModel: async () => {},
    },
    tool: { hook: async () => {} },
  }
  await plugin.setup(ctx)

  const URL_BASE = "http://cliproxy.invalid/v1"
  const tools = [{ name: "shell", description: "run", input_schema: { type: "object" } }]
  const claude = (model, extra = {}) => ({ model, max_tokens: 128000, stream: true, tools,
    thinking: { type: "adaptive" }, output_config: { effort: "medium" },
    messages: [
      { role: "user", content: [{ type: "text", text: "List the files, then fix the failing test in src/app.ts" }] },
      { role: "assistant", content: [{ type: "thinking", thinking: "plan", signature: "sig" }, { type: "tool_use", id: "t1", name: "shell", input: { command: "ls" } }] },
      { role: "user", content: [{ type: "tool_result", tool_use_id: "t1", content: "app.ts\napp.test.ts" }] },
    ], ...extra })
  const chat = (model, extra = {}) => ({ model, stream: true, max_tokens: 128000, reasoning_effort: "low", tools: [{ type: "function", function: { name: "shell" } }],
    messages: [{ role: "user", content: "Refactor the parser" },
      { role: "assistant", content: null, tool_calls: [{ id: "c1", type: "function", function: { name: "shell", arguments: "{\"command\":\"rg parse\"}" } }] },
      { role: "tool", tool_call_id: "c1", content: "src/parse.ts:1" }], ...extra })
  // Runs the hook on a POST and returns the request OpenCode would now send.
  const send = async (path, body, { providerID = "cliproxy", kind = "primary", sessionID = "s1", signal, headers = {} } = {}) => {
    const original = new Request(URL_BASE + path, { method: "POST", signal,
      headers: { "content-type": "application/json", authorization: "Bearer k", "x-keep": "1", ...headers }, body: typeof body === "string" ? body : JSON.stringify(body) })
    const event = { sessionID, agent: "build", model: { providerID, id: "x" }, kind, request: original }
    await hook(event)
    return { original, request: event.request, body: await event.request.clone().json().catch(() => null) }
  }

  // Small step on an Opus thread: newest Haiku, no thinking or effort, output capped.
  answer = step(0.1, 0.2)
  let r = await send("/messages", claude("claude/claude-opus-5-5"))
  assert.equal(r.body.model, "claude/claude-haiku-4-5-20251001")
  assert.equal(r.body.thinking, undefined)
  assert.equal(r.body.output_config, undefined)
  assert.equal(r.body.max_tokens, 64000)
  assert.equal(r.request.headers.get("authorization"), "Bearer k", "credentials pass through")
  assert.equal(r.request.headers.get("x-keep"), "1")
  assert.equal(r.request.url, URL_BASE + "/messages")
  assert.equal(r.request.method, "POST")
  assert.match(jevBody.state.task, /fix the failing test/)
  assert.match(jevBody.state.last, /shell[\s\S]*ls[\s\S]*app\.test\.ts/)
  assert.ok(!JSON.stringify(jevBody.state).includes("sig"), "thinking is not sent to Jev")
  assert.equal(r.body.messages.length, 3, "history is forwarded as sent")

  // Medium and large steps: newest Sonnet and Opus, with Jev's effort.
  answer = step(1.1, 1.9)
  r = await send("/messages", claude("claude/claude-haiku-4-5-20251001", { thinking: undefined, output_config: undefined }))
  assert.equal(r.body.model, "claude/claude-sonnet-5-5", "newest Sonnet, not Sonnet 5")
  assert.deepEqual(r.body.output_config, { effort: "high" })
  answer = step(2, 0)
  r = await send("/messages", claude("claude/claude-sonnet-5"))
  assert.equal(r.body.model, "claude/claude-opus-5-5")
  assert.deepEqual(r.body.output_config, { effort: "low" })
  assert.equal(r.body.max_tokens, 128000)

  // User decisions stay on Opus even when the capability tier is medium.
  answer = step(1, 1, 0.9, 0.9, 0.9)
  r = await send("/messages", claude("claude/claude-opus-5-5"))
  assert.equal(r.body.model, "claude/claude-opus-5-5")
  assert.deepEqual(r.body.output_config, { effort: "medium" })

  // Too long for Haiku's 200K window: Sonnet instead.
  answer = step(0, 0)
  const long = claude("claude/claude-opus-5-5")
  long.messages[2].content[0].content = "x".repeat(700000)
  r = await send("/messages", long)
  assert.equal(r.body.model, "claude/claude-sonnet-5-5")
  assert.ok(jevBody.state.last.length < 6000, "Jev gets a bounded excerpt")
  const mid = claude("claude/claude-opus-5-5")
  mid.messages[2].content[0].content = "x".repeat(350000)
  assert.equal((await send("/messages", mid)).body.model, "claude/claude-sonnet-5-5", "350K characters is already too long for Haiku")

  // Left alone, and not rebuilt: unsure, Jev down, malformed, title calls (no tools), OpenRouter, other kinds and providers, pinned subagents.
  const untouched = async (p, what) => { const x = await p; assert.equal(x.request, x.original, what); return x }
  answer = step(0, 0, 0.3)
  r = await untouched(send("/messages", claude("claude/claude-opus-5-5")), "unsure about the tier keeps the request")
  assert.equal(r.body.model, "claude/claude-opus-5-5")
  assert.deepEqual(r.body.output_config, { effort: "medium" })
  answer = new Error("down")
  await untouched(send("/messages", claude("claude/claude-opus-5-5")), "Jev down keeps the request")
  answer = { tier: { score: "x" } }
  await untouched(send("/messages", claude("claude/claude-opus-5-5")), "a malformed answer keeps the request")
  const calls = jevCalls
  answer = step(0, 0)
  await untouched(send("/messages", claude("claude/claude-opus-5-5", { tools: undefined })), "no tools: a title call")
  await untouched(send("/chat/completions", { model: "openrouter/or-glm-5.3-flash", tools: [{}], messages: [{ role: "user", content: "hi" }] }), "OpenRouter")
  await untouched(send("/messages", claude("claude/claude-opus-5-5"), { kind: "title" }), "auxiliary kinds")
  await untouched(send("/messages", claude("claude/claude-opus-5-5"), { providerID: "anthropic" }), "foreign providers")
  await untouched(send("/messages", "null"), "a null body")
  await untouched(send("/messages", "{not json"), "a body that is not JSON")
  await untouched(send("/models", {}), "other paths")
  sessions.set("pinned", { model: { providerID: "cliproxy", id: "claude-opus-5-5" }, parentID: "p", agent: "general" })
  await sessions.prompt({ sessionID: "pinned", messageID: "m", prompt: { text: "Review it.\nroute: sonnet_high" } })
  r = await untouched(send("/messages", claude("claude/claude-sonnet-5-5"), { sessionID: "pinned" }), "a pinned subagent is left as OpenCode sent it")
  assert.deepEqual(r.body.output_config, { effort: "medium" })
  assert.equal(jevCalls, calls, "no Jev call for title calls, OpenRouter or pinned subagents")

  // Codex on Chat Completions: effort only, the model stays.
  answer = step(0, 1.8)
  r = await send("/chat/completions", chat("codex/gpt-6-sol"))
  assert.equal(r.body.model, "codex/gpt-6-sol")
  assert.equal(r.body.reasoning_effort, "high")
  assert.match(jevBody.state.last, /rg parse[\s\S]*src\/parse\.ts/)
  answer = step(0, 1.8, 0.9, 0.2)
  r = await send("/chat/completions", chat("codex/gpt-6-sol", { messages: [{ role: "user", content: "x" }] }))
  assert.equal(r.body.reasoning_effort, "high", "effort is applied even when Jev is unsure of it")

  // Claude on Chat Completions (what Auto sends when it picks Opus): the family decides, not the path.
  answer = step(1.1, 1.9)
  r = await send("/chat/completions", chat("claude/claude-opus-5-5"))
  assert.equal(r.body.model, "claude/claude-sonnet-5-5", "a Claude step can move between Claude models on this wire too")
  assert.equal(r.body.reasoning_effort, "high", "CLIProxyAPI takes reasoning_effort, not output_config")
  assert.equal(r.body.output_config, undefined)
  answer = step(0, 0)
  r = await send("/chat/completions", chat("claude/claude-opus-5-5"))
  assert.equal(r.body.model, "claude/claude-haiku-4-5-20251001")
  assert.equal(r.body.reasoning_effort, undefined, "Haiku takes no effort")
  assert.equal(r.body.max_tokens, 64000)
  r = await send("/chat/completions", chat("claude/claude-opus-5-5", { max_tokens: undefined, max_completion_tokens: 100000 }))
  assert.equal(r.body.max_completion_tokens, 64000)
  answer = step(2, 1, 0.3)
  r = await untouched(send("/chat/completions", chat("claude/claude-opus-5-5")), "an unsure Claude step on Chat Completions is left alone")

  // Auto's virtual provider: its requests carry the real scoped model; effort goes by that model's family.
  sessions.set("auto", { model: { providerID: "jev-auto", id: "auto" } })
  answer = step(0, 0.2)
  r = await send("/chat/completions", chat("codex/gpt-6-sol"), { providerID: "jev-auto", sessionID: "auto" })
  assert.equal(r.body.model, "codex/gpt-6-sol")
  assert.equal(r.body.reasoning_effort, "low")
  r = await untouched(send("/chat/completions", chat("codex/gpt-6-sol"), { kind: "title", providerID: "jev-auto", sessionID: "auto" }), "Auto's title call")

  // Bytes: non-ASCII text survives, routed or not.
  const greek = "Καλημέρα κόσμε 🚀 ".repeat(20000)
  answer = step(2, 1)
  const g1 = claude("claude/claude-opus-5-5")
  g1.messages[2].content[0].content = greek
  r = await send("/messages", g1)
  assert.equal(r.body.messages[2].content[0].content, greek, "a routed body keeps its Greek text")
  // Output config: only effort is removed for Haiku.
  answer = step(0, 0)
  r = await send("/messages", claude("claude/claude-opus-5-5", { output_config: { effort: "high", format: { type: "json" } } }))
  assert.deepEqual(r.body.output_config, { format: { type: "json" } })
  // Query strings still route; headers and a stale content-length do not leak into the rebuilt request.
  r = await send("/messages?beta=true", claude("claude/claude-opus-5-5"), { headers: { "content-length": "1" } })
  assert.equal(r.body.model, "claude/claude-haiku-4-5-20251001")
  assert.equal(r.request.url, URL_BASE + "/messages?beta=true")
  assert.notEqual(r.request.headers.get("content-length"), "1")

  // An abort of OpenCode's request still aborts the rebuilt one, so the upstream call is cancelled.
  const ac = new AbortController()
  r = await send("/messages", claude("claude/claude-opus-5-5"), { signal: ac.signal })
  assert.notEqual(r.request, r.original)
  assert.equal(r.request.signal.aborted, false)
  ac.abort()
  assert.equal(r.request.signal.aborted, true, "the abort reaches the rewritten request")

  const steps = (await readFile(join(dir, "opencode/jev-auto.jsonl"), "utf8")).trim().split("\n").map(l => JSON.parse(l)).filter(l => l.mode === "step")
  assert.deepEqual(steps.slice(0, 3).map(s => `${s.requestedModel}>${s.actualModel}:${s.effort ?? "none"}`), [
    "claude/claude-opus-5-5>claude/claude-haiku-4-5-20251001:none",
    "claude/claude-haiku-4-5-20251001>claude/claude-sonnet-5-5:high",
    "claude/claude-sonnet-5>claude/claude-opus-5-5:low"])
  assert.equal(steps[0].sessionId, "s1")
  assert.ok(steps.every(s => Number.isFinite(s.latencyMs)))
  assert.ok(steps.some(s => s.status === "kept" && s.reason === "routing_unavailable"), "a Jev outage is logged and the request goes out unchanged")
  assert.ok(!JSON.stringify(steps).includes("failing test"), "no prompt text in the log")
  console.log("jev-steps: all checks passed")
} finally {
  globalThis.fetch = saved.fetch
  for (const [k, v] of [["XDG_STATE_HOME", saved.state], ["JEV_OPENROUTER_KEY_FILE", saved.key], ["XDG_CONFIG_HOME", saved.config]]) v === undefined ? delete process.env[k] : process.env[k] = v
  await rm(dir, { recursive: true, force: true })
}
