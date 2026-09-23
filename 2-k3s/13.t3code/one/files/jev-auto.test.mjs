import assert from "node:assert/strict"
import { mkdtemp, writeFile, rm, readFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import plugin from "./jev-auto.js"

const dir = await mkdtemp(join(tmpdir(), "jev-auto-"))
const saved = { fetch: globalThis.fetch, state: process.env.XDG_STATE_HOME, key: process.env.JEV_OPENROUTER_KEY_FILE }
process.env.XDG_STATE_HOME = dir
process.env.JEV_OPENROUTER_KEY_FILE = join(dir, "key")
await writeFile(process.env.JEV_OPENROUTER_KEY_FILE, "test-key", { mode: 0o600 })
try {
  let parentID, history = [], calls = 0
  const client = { session: {
    get: async () => ({ data: { parentID } }),
    messages: async () => ({ data: history }),
  } }
  const hooks = await plugin({ client, directory: "/test", project: { id: "test" } })
  const config = { provider: { cliproxy: { options: { baseURL: "http://proxy/v1" }, models: { "gpt-6-luna": {}, "gpt-6-astra": { id: "codex/gpt-6-astra", limit: { context: 872000, output: 128000 } } } } }, enabled_providers: ["cliproxy"] }
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
  console.log("PASS: Auto catalog, high-confidence routing, fallback, manual selections, context gates, safe logs")
} finally {
  globalThis.fetch = saved.fetch
  for (const [key, value] of [["XDG_STATE_HOME", saved.state], ["JEV_OPENROUTER_KEY_FILE", saved.key]]) {
    if (value === undefined) delete process.env[key]
    else process.env[key] = value
  }
  await rm(dir, { recursive: true, force: true })
}
