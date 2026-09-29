// Per-step routing (jev-auto.md "Every step"): the plugin's local proxy between
// OpenCode and CLIProxyAPI. A fake CLIProxy records what arrives upstream; Jev
// answers are faked by URL.
import assert from "node:assert/strict"
import http from "node:http"
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
const test = globalThis.Bun ? "bun" : "node"

const seen = []
const sleep = (ms) => new Promise(r => setTimeout(r, ms))
let bunUp // Bun-only upstream for the abort check; stopped in finally so the process exits
// Behaviour per request, set by the x-test header (forwarded like any other header).
const upstream = http.createServer(async (req, res) => {
  const chunks = []
  for await (const c of req) chunks.push(c)
  const bytes = Buffer.concat(chunks)
  let body = null
  try { body = bytes.length ? JSON.parse(bytes.toString("utf8")) : null } catch {}
  const entry = { url: req.url, method: req.method, headers: req.headers, bytes, body, closedEarly: false }
  seen.push(entry)
  res.on("close", () => { if (!res.writableFinished) entry.closedEarly = true })
  const mode = req.headers["x-test"]
  if (mode === "429") {
    res.writeHead(429, { "content-type": "application/json", "retry-after": "7" })
    return res.end('{"type":"error","error":{"type":"rate_limit_error"}}')
  }
  res.writeHead(200, { "content-type": "text/event-stream" })
  res.write("data: one\n\n")
  if (mode === "slow" || mode === "abort") { await sleep(mode === "abort" ? 2000 : 400); if (res.destroyed) return }
  if (mode === "fail") return setTimeout(() => res.socket.destroy(), 50)
  res.end("data: two\n\n")
})
await new Promise(r => upstream.listen(0, "127.0.0.1", r))

let jevCalls = 0, jevBody, answer
globalThis.fetch = async (url, init) => {
  if (String(url).startsWith("https://openrouter.ai/")) {
    jevCalls++
    jevBody = JSON.parse(init.body)
    if (answer instanceof Error) throw answer
    return Response.json({ id: "req", answers: answer, usage: { cost: 0.00003 } })
  }
  return saved.fetch(url, init)
}
const step = (tier, effort, tierConfidence = 0.9, effortConfidence = 0.9, dialogue = 0) =>
  ({ tier: { score: tier, confidence: tierConfidence }, effort: { score: effort, confidence: effortConfidence }, dialogue: { noul: dialogue } })

try {
  let parentID
  const client = { session: { get: async () => ({ data: { parentID } }), messages: async () => ({ data: [] }) } }
  const hooks = await plugin({ client, directory: "/test", project: { id: "test" } })
  const models = {
    "gpt-6-luna": { id: "codex/gpt-6-luna" }, "gpt-6-astra": { id: "codex/gpt-6-astra" }, "gpt-6-sol": { id: "codex/gpt-6-sol" },
    "claude-haiku-4-5-20251001": { id: "claude/claude-haiku-4-5-20251001" }, "claude-sonnet-5": { id: "claude/claude-sonnet-5" },
    "claude-sonnet-5-5": { id: "claude/claude-sonnet-5-5" }, "claude-opus-5-5": { id: "claude/claude-opus-5-5" },
    "claude-fable-5-1": { id: "claude/claude-fable-5-1" }, "or-glm-5.3-flash": { id: "openrouter/or-glm-5.3-flash" },
  }
  const config = { provider: { cliproxy: { options: { baseURL: `http://127.0.0.1:${upstream.address().port}/v1` }, models } } }
  await hooks.config(config)
  const local = config.provider.cliproxy.options.baseURL
  assert.match(local, /^http:\/\/127\.0\.0\.1:\d+\/v1$/, "the provider now points at the local proxy")
  assert.notEqual(local, `http://127.0.0.1:${upstream.address().port}/v1`)
  assert.equal(config.provider["jev-auto"].options.baseURL, local, "Jev Auto goes through the proxy too")
  await hooks.config(config)
  assert.equal(config.provider.cliproxy.options.baseURL, local, "a second config call keeps the real upstream")

  const headers = async (sessionID) => {
    const out = { headers: {} }
    await hooks["chat.headers"]({ sessionID, agent: "build", model: { providerID: "cliproxy" }, provider: {}, message: {} }, out)
    return out.headers
  }
  const post = async (path, body, sessionID = "s1", extra = {}) => {
    const r = await saved.fetch(local.replace(/\/v1$/, "") + path, { method: "POST",
      headers: { "content-type": "application/json", authorization: "Bearer k", ...(await headers(sessionID)), ...extra },
      body: typeof body === "string" ? body : JSON.stringify(body) })
    return { status: r.status, headers: r.headers, text: await r.text(), up: seen.at(-1) }
  }
  const out = { headers: {} }
  await hooks["chat.headers"]({ sessionID: "x", model: { providerID: "openrouter" } }, out)
  assert.deepEqual(out.headers, {}, "only CLIProxy requests carry the session tag")
  const tools = [{ name: "bash", description: "run", input_schema: { type: "object" } }]
  const claude = (model, extra = {}) => ({ model, max_tokens: 128000, stream: true, tools,
    thinking: { type: "adaptive" }, output_config: { effort: "medium" },
    messages: [
      { role: "user", content: [{ type: "text", text: "List the files, then fix the failing test in src/app.ts" }] },
      { role: "assistant", content: [{ type: "thinking", thinking: "plan", signature: "sig" }, { type: "tool_use", id: "t1", name: "bash", input: { command: "ls" } }] },
      { role: "user", content: [{ type: "tool_result", tool_use_id: "t1", content: "app.ts\napp.test.ts" }] },
    ], ...extra })

  // Small step on an Opus thread: newest Haiku, no thinking or effort, output capped.
  answer = step(0.1, 0.2)
  let r = await post("/v1/messages", claude("claude/claude-opus-5-5"))
  assert.equal(r.status, 200)
  assert.equal(r.text, "data: one\n\ndata: two\n\n", "the stream reaches OpenCode unchanged")
  assert.equal(r.up.body.model, "claude/claude-haiku-4-5-20251001")
  assert.equal(r.up.body.thinking, undefined)
  assert.equal(r.up.body.output_config, undefined)
  assert.equal(r.up.body.max_tokens, 64000)
  assert.equal(r.up.headers.authorization, "Bearer k", "credentials pass through")
  assert.ok(!Object.keys(r.up.headers).some(h => h.startsWith("x-jev-")), "plugin headers stay local")
  assert.match(jevBody.state.task, /fix the failing test/)
  assert.match(jevBody.state.last, /bash[\s\S]*ls[\s\S]*app\.test\.ts/)
  assert.ok(!JSON.stringify(jevBody.state).includes("sig"), "thinking is not sent to Jev")
  assert.equal(r.up.body.messages.length, 3, "history is forwarded as sent")

  // Medium and large steps: newest Sonnet and Opus, with Jev's effort.
  answer = step(1.1, 1.9)
  r = await post("/v1/messages", claude("claude/claude-haiku-4-5-20251001", { thinking: undefined, output_config: undefined }))
  assert.equal(r.up.body.model, "claude/claude-sonnet-5-5", "newest Sonnet, not Sonnet 5")
  assert.deepEqual(r.up.body.output_config, { effort: "high" })
  answer = step(2, 0)
  r = await post("/v1/messages", claude("claude/claude-sonnet-5"))
  assert.equal(r.up.body.model, "claude/claude-opus-5-5")
  assert.deepEqual(r.up.body.output_config, { effort: "low" })
  assert.equal(r.up.body.max_tokens, 128000)

  // User decisions stay on Opus even when the capability tier is medium.
  answer = step(1, 1, 0.9, 0.9, 0.9)
  r = await post("/v1/messages", claude("claude/claude-opus-5-5"))
  assert.equal(r.up.body.model, "claude/claude-opus-5-5")
  assert.deepEqual(r.up.body.output_config, { effort: "medium" })

  // Too long for Haiku's 200K window: Sonnet instead.
  answer = step(0, 0)
  const long = claude("claude/claude-opus-5-5")
  long.messages[2].content[0].content = "x".repeat(700000)
  r = await post("/v1/messages", long)
  assert.equal(r.up.body.model, "claude/claude-sonnet-5-5")
  assert.ok(jevBody.state.last.length < 6000, "Jev gets a bounded excerpt")
  const mid = claude("claude/claude-opus-5-5")
  mid.messages[2].content[0].content = "x".repeat(350000)
  assert.equal((await post("/v1/messages", mid)).up.body.model, "claude/claude-sonnet-5-5", "350K characters is already too long for Haiku")

  // Left alone: unsure, Jev down, malformed, title calls (no tools), OpenRouter, pinned subagents.
  let before = seen.length
  answer = step(0, 0, 0.3)
  r = await post("/v1/messages", claude("claude/claude-opus-5-5"))
  assert.equal(r.up.body.model, "claude/claude-opus-5-5")
  assert.deepEqual(r.up.body.output_config, { effort: "medium" })
  answer = new Error("down")
  assert.equal((await post("/v1/messages", claude("claude/claude-opus-5-5"))).up.body.model, "claude/claude-opus-5-5")
  answer = { tier: { score: "x" } }
  assert.equal((await post("/v1/messages", claude("claude/claude-opus-5-5"))).up.body.model, "claude/claude-opus-5-5")
  let calls = jevCalls
  answer = step(0, 0)
  assert.equal((await post("/v1/messages", claude("claude/claude-opus-5-5", { tools: undefined }))).up.body.model, "claude/claude-opus-5-5")
  assert.equal((await post("/v1/chat/completions", { model: "openrouter/or-glm-5.3-flash", tools: [{}], messages: [{ role: "user", content: "hi" }] })).up.body.model, "openrouter/or-glm-5.3-flash")
  parentID = "p"
  await hooks["chat.message"]({ sessionID: "pinned", agent: "general" }, { message: { id: "m", model: { providerID: "cliproxy", modelID: "claude-opus-5-5" } }, parts: [{ type: "text", text: "Review it.\nroute: sonnet_high" }] })
  parentID = undefined
  r = await post("/v1/messages", claude("claude/claude-sonnet-5-5"), "pinned")
  assert.equal(r.up.body.model, "claude/claude-sonnet-5-5")
  assert.deepEqual(r.up.body.output_config, { effort: "medium" }, "a pinned subagent is left as OpenCode sent it")
  assert.equal(jevCalls, calls, "no Jev call for title calls, OpenRouter or pinned subagents")
  assert.equal(seen.length - before, 6, "every request still reaches CLIProxy")

  // Codex: effort only, the model stays.
  answer = step(0, 1.8)
  r = await post("/v1/chat/completions", { model: "codex/gpt-6-sol", reasoning_effort: "low", stream: true,
    tools: [{ type: "function", function: { name: "bash" } }],
    messages: [{ role: "user", content: "Refactor the parser" },
      { role: "assistant", content: null, tool_calls: [{ id: "c1", type: "function", function: { name: "bash", arguments: "{\"command\":\"rg parse\"}" } }] },
      { role: "tool", tool_call_id: "c1", content: "src/parse.ts:1" }] })
  assert.equal(r.up.body.model, "codex/gpt-6-sol")
  assert.equal(r.up.body.reasoning_effort, "high")
  assert.match(jevBody.state.last, /rg parse[\s\S]*src\/parse\.ts/)
  answer = step(0, 1.8, 0.9, 0.2)
  r = await post("/v1/chat/completions", { model: "codex/gpt-6-sol", reasoning_effort: "low", tools: [{}], messages: [{ role: "user", content: "x" }] })
  assert.equal(r.up.body.reasoning_effort, "high", "effort is applied even when Jev is unsure of it")

  // Bytes: non-ASCII text survives, split across chunks or not, routed or not.
  const greek = "Καλημέρα κόσμε 🚀 ".repeat(20000)
  answer = step(2, 1)
  const g1 = claude("claude/claude-opus-5-5")
  g1.messages[2].content[0].content = greek
  r = await post("/v1/messages", g1)
  assert.equal(r.up.body.messages[2].content[0].content, greek, "a routed body keeps its Greek text")
  const g2 = JSON.stringify({ model: "claude/claude-opus-5-5", note: greek })
  r = await post("/v1/messages", g2)
  assert.equal(r.up.bytes.toString("utf8"), g2, "an untouched body arrives byte for byte")
  // Output config: only effort is removed for Haiku.
  answer = step(0, 0)
  r = await post("/v1/messages", claude("claude/claude-opus-5-5", { output_config: { effort: "high", format: { type: "json" } } }))
  assert.deepEqual(r.up.body.output_config, { format: { type: "json" } })
  // Query strings still route; a null body passes untouched.
  answer = step(0, 0)
  assert.equal((await post("/v1/messages?beta=true", claude("claude/claude-opus-5-5"))).up.body.model, "claude/claude-haiku-4-5-20251001")
  assert.equal(seen.at(-1).url, "/v1/messages?beta=true")
  assert.equal((await post("/v1/messages", "null")).status, 200)

  // Upstream errors keep status, body and headers.
  r = await post("/v1/messages", claude("claude/claude-opus-5-5", { tools: undefined }), "s1", { "x-test": "429" })
  assert.equal(r.status, 429)
  assert.equal(r.headers.get("retry-after"), "7")
  assert.match(r.text, /rate_limit_error/)
  // Streams are not buffered: the first chunk arrives before the upstream finishes.
  const t0 = Date.now()
  const streamed = await saved.fetch(local + "/messages", { method: "POST", headers: { "x-test": "slow", ...(await headers("s1")) }, body: "{}" })
  const reader = streamed.body.getReader()
  const first = new TextDecoder().decode((await reader.read()).value)
  assert.equal(first, "data: one\n\n")
  assert.ok(Date.now() - t0 < 300, "first chunk before the 400 ms pause")
  while (!(await reader.read()).done);
  // A stream that fails midway is an error for OpenCode, not a clean end.
  for (const [path, shape] of [["/messages", /^data: one\n\nevent: error\ndata: \{"type":"error","error":\{"type":"api_error"/], ["/chat/completions", /^data: one\n\ndata: \{"error":\{"type":"api_error"/]]) {
    const failing = await saved.fetch(local + path, { method: "POST", headers: { "x-test": "fail" }, body: "{}" })
    assert.match(await failing.text(), shape, `a mid-stream failure on ${path} ends with an error event`)
  }
  // A client abort aborts the upstream request. Bun's node:http never reports a
  // disconnect, so under Bun the upstream for this check is Bun.serve.
  let upstreamClosed, abortVia = local
  if (globalThis.Bun) {
    let aborted = false
    bunUp = globalThis.Bun.serve({ hostname: "127.0.0.1", port: 0, idleTimeout: 0, fetch: (request) => {
      request.signal.addEventListener("abort", () => { aborted = true })
      return new Response(new ReadableStream({ async start(c) { c.enqueue(new TextEncoder().encode("data: one\n\n")); await sleep(2000); try { c.close() } catch {} } }), { headers: { "content-type": "text/event-stream" } })
    } })
    const viaBun = await plugin({ client, directory: "/test", project: { id: "test" } })
    const bunCfg = { provider: { cliproxy: { options: { baseURL: `http://127.0.0.1:${bunUp.port}/v1` }, models } } }
    await viaBun.config(bunCfg)
    abortVia = bunCfg.provider.cliproxy.options.baseURL
    upstreamClosed = () => aborted
  }
  const ac = new AbortController()
  const aborting = await saved.fetch(abortVia + "/messages", { method: "POST", headers: { "x-test": "abort" }, body: "{}", signal: ac.signal })
  if (!globalThis.Bun) { const entry = seen.at(-1); upstreamClosed = () => entry.closedEarly }
  await aborting.body.getReader().read()
  ac.abort()
  await sleep(300)
  assert.ok(upstreamClosed(), "the upstream request was closed when OpenCode aborted")
  // Upstream down: an Anthropic-shaped 502.
  const dead = await plugin({ client, directory: "/test", project: { id: "test" } })
  const deadCfg = { provider: { cliproxy: { options: { baseURL: "http://127.0.0.1:9/api" }, models } } }
  await dead.config(deadCfg)
  assert.match(deadCfg.provider.cliproxy.options.baseURL, /^http:\/\/127\.0\.0\.1:\d+\/api$/, "a base path other than /v1 is kept")
  const d = await saved.fetch(deadCfg.provider.cliproxy.options.baseURL + "/messages", { method: "POST", body: "{}" })
  assert.equal(d.status, 502)
  assert.equal((await d.json()).type, "error")
  const unresolved = { provider: { cliproxy: { options: { baseURL: "{env:ANTHROPIC_BASE_URL}/v1" }, models } } }
  await dead.config(unresolved)
  assert.equal(unresolved.provider.cliproxy.options.baseURL, "{env:ANTHROPIC_BASE_URL}/v1", "an unresolved URL is left alone")

  // GET and upstream errors pass through.
  const g = await saved.fetch(local + "/models", { headers: { authorization: "Bearer k" } })
  assert.equal(g.status, 200)
  assert.equal(seen.at(-1).method, "GET")

  const steps = (await readFile(join(dir, "opencode/jev-auto.jsonl"), "utf8")).trim().split("\n").map(l => JSON.parse(l)).filter(l => l.mode === "step")
  assert.deepEqual(steps.map(s => s.status), ["routed", "routed", "routed", "routed", "routed", "routed", "kept", "kept", "kept", "routed", "routed", "routed", "routed", "routed"])
  assert.deepEqual(steps.slice(0, 3).map(s => `${s.requestedModel}>${s.actualModel}:${s.effort ?? "none"}`), [
    "claude/claude-opus-5-5>claude/claude-haiku-4-5-20251001:none",
    "claude/claude-haiku-4-5-20251001>claude/claude-sonnet-5-5:high",
    "claude/claude-sonnet-5>claude/claude-opus-5-5:low"])
  assert.equal(steps[0].sessionId, "s1")
  assert.ok(steps.every(s => Number.isFinite(s.latencyMs)))
  assert.ok(!JSON.stringify(steps).includes("failing test"), "no prompt text in the log")
  console.log(`jev-steps: all checks passed (${test})`)
} finally {
  globalThis.fetch = saved.fetch
  upstream.close()
  bunUp?.stop(true)
  for (const [k, v] of [["XDG_STATE_HOME", saved.state], ["JEV_OPENROUTER_KEY_FILE", saved.key], ["XDG_CONFIG_HOME", saved.config]]) v === undefined ? delete process.env[k] : process.env[k] = v
  await rm(dir, { recursive: true, force: true })
}
