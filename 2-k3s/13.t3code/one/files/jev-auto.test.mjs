import assert from "node:assert/strict"
import { mkdir, mkdtemp, writeFile, rm, readFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import plugin from "./jev-auto.js"

const dir = await mkdtemp(join(tmpdir(), "jev-auto-"))
const saved = { fetch: globalThis.fetch, state: process.env.XDG_STATE_HOME, key: process.env.JEV_OPENROUTER_KEY_FILE, config: process.env.XDG_CONFIG_HOME }
process.env.XDG_STATE_HOME = dir
process.env.XDG_CONFIG_HOME = dir
await mkdir(join(dir, "opencode"))
await writeFile(join(dir, "opencode/jev-checks.md"), "JEV CHECKS RULE")
process.env.JEV_OPENROUTER_KEY_FILE = join(dir, "key")
await writeFile(process.env.JEV_OPENROUTER_KEY_FILE, "test-key", { mode: 0o600 })
try {
  let parentID, history = [], calls = 0
  const client = { session: {
    get: async () => ({ data: { parentID } }),
    messages: async () => ({ data: history }),
  } }
  const hooks = await plugin({ client, directory: "/test", project: { id: "test" } })
  const catalog = () => ({ "gpt-6-luna": { id: "codex/gpt-6-luna" }, "gpt-6-astra": { id: "codex/gpt-6-astra", limit: { context: 872000, output: 128000 } },
    "or-glm-5.3-flash": { id: "openrouter/or-glm-5.3-flash" }, "claude-opus-5-5": { id: "claude/claude-opus-5-5" } })
  const config = { provider: { cliproxy: { options: { baseURL: "http://proxy/v1" }, models: catalog() } }, enabled_providers: ["cliproxy"] }
  await hooks.config(config)
  assert.equal(config.provider["jev-auto"].models.auto.id, "codex/gpt-6-astra")
  assert.ok(config.enabled_providers.includes("jev-auto"))
  const fresh = () => ({ message: { id: "test", model: { providerID: "jev-auto", modelID: "auto" }, system: "original" }, parts: [{ type: "text", text: "Fix the typo: deploymnet" }] })
  const input = { sessionID: "test", model: { providerID: "jev-auto", modelID: "auto" } }
  const response = (choice, confidence) => async () => {
    calls++
    return Response.json({ id: "request", answers: { route: { choice, confidence } }, usage: { cost: 0.00002 } })
  }
  globalThis.fetch = response("trivial", 0.95)
  let output = fresh()
  await hooks["chat.message"](input, output)
  assert.equal(output.message.model.modelID, "gpt-6-luna")
  assert.ok(output.message.system.startsWith("original"))
  assert.deepEqual(output.parts, fresh().parts)
  for (const [route, confidence] of [["trivial", 0.89], ["strong", 1], ["invalid", 1], ["trivial", 2]]) {
    globalThis.fetch = response(route, confidence)
    output = fresh()
    await hooks["chat.message"](input, output)
    assert.equal(output.message.model.modelID, "gpt-6-astra")
  }
  globalThis.fetch = response("trivial", 1)
  const before = calls
  history = [{ info: { role: "user" } }]
  output = fresh()
  await hooks["chat.message"](input, output)
  assert.equal(output.message.model.modelID, "gpt-6-astra")
  history = []; parentID = "parent"
  await hooks["chat.message"](input, fresh())
  parentID = undefined
  output = fresh(); output.parts.push({ type: "file" })
  await hooks["chat.message"](input, output)
  assert.equal(calls, before, "followups, child sessions and files must not invoke Jev")
  output = fresh(); output.message.model = { providerID: "cliproxy", modelID: "gpt-6-astra" }
  const copy = structuredClone(output)
  await hooks["chat.message"](input, output)
  assert.deepEqual(output, copy, "manual selections unchanged")
  globalThis.fetch = async () => { throw new Error("secret error") }
  output = fresh()
  await hooks["chat.message"](input, output)
  assert.equal(output.message.model.modelID, "gpt-6-astra")
  const log = await readFile(join(dir, "opencode/jev-auto.jsonl"), "utf8")
  assert.ok(!/test-key|deploymnet|secret error/.test(log))
  assert.equal(JSON.parse(log.split("\n")[0]).actualModel, "cliproxy/gpt-6-luna")

  // Auto is never registered unless both targets are OpenAI or Anthropic routes.
  const offRoute = { provider: { cliproxy: { options: {}, models: { ...catalog(), "gpt-6-astra": { id: "openrouter/gpt-6-astra" } } } } }
  await (await plugin({ client, directory: "/test", project: { id: "test" } })).config(offRoute)
  assert.equal(offRoute.provider["jev-auto"], undefined, "Auto must not target a non-OpenAI/Anthropic route")

  // Jev MCP tools run only in sessions whose current turn is an OpenAI or Anthropic model.
  const blocked = (sessionID, tool = "jev_jev_screen") => hooks["tool.execute.before"]({ tool, sessionID, callID: "c" })
    .then(() => false, e => { assert.match(e.message, /only on OpenAI and Anthropic/); return true })
  const turn = async (sessionID, providerID, modelID) => {
    const out = { message: { id: "m", model: { providerID, modelID }, system: "s" }, parts: [{ type: "text", text: "hi" }] }
    const copy = structuredClone(out)
    await hooks["chat.message"]({ sessionID }, out)
    assert.deepEqual(out, copy, "non-Auto turns are never modified")
  }
  // Fresh Auto sessions keep Jev on every path: classifier failure and a Luna route.
  globalThis.fetch = async () => { throw new Error("down") }
  await hooks["chat.message"]({ sessionID: "auto-fail" }, fresh())
  assert.equal(await blocked("auto-fail"), false, "Auto turn whose classifier failed keeps Jev")
  globalThis.fetch = response("trivial", 0.95)
  output = fresh()
  await hooks["chat.message"]({ sessionID: "auto-luna" }, output)
  assert.equal(output.message.model.modelID, "gpt-6-luna")
  assert.equal(await blocked("auto-luna"), false, "Auto turn routed to Luna keeps Jev")
  const jevCalls = calls
  await turn("glm", "cliproxy", "or-glm-5.3-flash")
  assert.equal(await blocked("glm"), true, "OpenRouter model loses Jev")
  assert.equal(await blocked("glm", "bash"), false, "other tools are untouched")
  await turn("claude", "cliproxy", "claude-opus-5-5")
  assert.equal(await blocked("claude"), false, "Anthropic via CLIProxyAPI keeps Jev")
  await turn("claude", "cliproxy", "or-glm-5.3-flash")
  assert.equal(await blocked("claude"), true, "switching a thread to OpenRouter removes Jev")
  await turn("direct", "anthropic", "claude-sonnet-5")
  assert.equal(await blocked("direct"), false, "direct Anthropic provider keeps Jev")
  await turn("direct-openai", "openai", "gpt-5")
  assert.equal(await blocked("direct-openai"), false, "direct OpenAI provider keeps Jev")
  await turn("unknown-model", "cliproxy", "not-in-catalog")
  assert.equal(await blocked("unknown-model"), true, "unknown catalog entry fails closed")
  assert.equal(await blocked("never-seen"), true, "unseen session fails closed")
  assert.equal(calls, jevCalls, "manual turns never call the Jev classifier")

  // The jev-checks rule reaches OpenAI and Anthropic models only.
  const prompt = async (providerID, apiId) => {
    const out = { system: ["base"] }
    await hooks["experimental.chat.system.transform"]({ sessionID: "x", model: { providerID, api: { id: apiId } } }, out)
    return out.system
  }
  assert.deepEqual(await prompt("cliproxy", "openrouter/or-glm-5.3-flash"), ["base"])
  assert.deepEqual(await prompt("cliproxy", "codex/gpt-6-luna"), ["base", "JEV CHECKS RULE"])
  assert.deepEqual(await prompt("cliproxy", "claude/claude-opus-5-5"), ["base", "JEV CHECKS RULE"])
  assert.deepEqual(await prompt("openai", "gpt-5"), ["base", "JEV CHECKS RULE"])
  console.log("PASS: Auto catalog, high-confidence routing, fallback, manual selections, context gates, safe logs, OpenAI/Anthropic-only Jev")
} finally {
  globalThis.fetch = saved.fetch
  for (const [key, value] of [["XDG_STATE_HOME", saved.state], ["JEV_OPENROUTER_KEY_FILE", saved.key], ["XDG_CONFIG_HOME", saved.config]]) {
    if (value === undefined) delete process.env[key]
    else process.env[key] = value
  }
  await rm(dir, { recursive: true, force: true })
}
