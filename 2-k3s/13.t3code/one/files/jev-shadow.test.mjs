import assert from "node:assert/strict"
import { mkdtemp, readFile, writeFile, rm } from "node:fs/promises"
import { join } from "node:path"
import { tmpdir } from "node:os"
import plugin from "./jev-shadow.js"

const dir = await mkdtemp(join(tmpdir(), "jev-test-"))
const oldFetch = globalThis.fetch
const oldConfig = process.env.XDG_CONFIG_HOME
const oldState = process.env.XDG_STATE_HOME
const oldKeyFile = process.env.JEV_OPENROUTER_KEY_FILE
delete process.env.JEV_OPENROUTER_KEY_FILE
process.env.XDG_CONFIG_HOME = dir
process.env.XDG_STATE_HOME = dir
try {
  const hooks = await plugin({ directory: "/project", project: { id: "project" } })
  const input = { sessionID: "session" }
  const output = { message: { id: "message", model: { providerID: "cliproxy", modelID: "gpt-6-astra" } }, parts: [
    { type: "text", text: "Fix README spelling" },
    { type: "text", text: "private attached content", synthetic: true },
  ] }
  const original = structuredClone(output)
  let calls = 0
  globalThis.fetch = async (url, options) => {
    calls++
    assert.equal(url, "https://openrouter.ai/api/v1/systemone")
    assert.equal(JSON.parse(options.body).state, "Fix README spelling")
    assert.equal(options.headers.Authorization, "Bearer test-key")
    assert.ok(options.signal instanceof AbortSignal)
    return Response.json({ id: "request", model: "typesafe/jev", answers: { route: { choice: "routine", confidence: 0.9 } }, usage: { cost: 0.00001, input_tokens: 100, output_tokens: 10 } })
  }
  await hooks["chat.message"](input, output)
  assert.equal(calls, 0, "disabled without credential")
  const { mkdir } = await import("node:fs/promises")
  await mkdir(join(dir, "opencode"))
  await writeFile(join(dir, "opencode/jev-openrouter-key"), "test-key\n", { mode: 0o600 })
  await hooks["chat.message"](input, output)
  assert.equal(calls, 1)
  process.env.JEV_OPENROUTER_KEY_FILE = join(dir, "mounted-key")
  await writeFile(process.env.JEV_OPENROUTER_KEY_FILE, "test-key", { mode: 0o600 })
  await hooks["chat.message"](input, output)
  assert.equal(calls, 2, "mounted credential used")
  assert.deepEqual(output, original, "selected model and message must remain unchanged")
  await hooks["chat.message"](input, { ...output, parts: [{ type: "text", text: "resume", synthetic: true }] })
  await hooks["chat.message"](input, { ...output, parts: [...output.parts, { type: "file" }] })
  await hooks["chat.message"](input, { ...output, parts: [{ type: "text", text: "x".repeat(12001) }] })
  assert.equal(calls, 2, "synthetic, attachment and oversized inputs must not call API")
  for (const response of [Response.json({}, { status: 429 }), Response.json({ answers: { route: { choice: "invented" } } })]) {
    globalThis.fetch = async () => response
    await hooks["chat.message"](input, output)
  }
  globalThis.fetch = async () => { throw new Error("private upstream body test-key") }
  await hooks["chat.message"](input, output)
  const log = await readFile(join(dir, "opencode/jev-shadow.jsonl"), "utf8")
  const rows = log.trim().split("\n").map(JSON.parse)
  assert.deepEqual(rows.map(r => r.status), ["ok", "ok", "skipped_context", "skipped_context", "error", "error", "error"])
  assert.deepEqual(rows.slice(-3).map(r => r.error), ["http_429", "invalid_response", "request_failed"])
  assert.equal(rows[0].costUsd, 0.00001)
  assert.ok(!/test-key|Fix README|private/.test(log), "logs must omit prompts and credentials")
  assert.deepEqual(output, original)
  // Logging failure also must not interrupt a user message.
  await rm(join(dir, "opencode/jev-shadow.jsonl"))
  await mkdir(join(dir, "opencode/jev-shadow.jsonl"))
  await hooks["chat.message"](input, output)
  console.log("PASS: opt-in, classification, unchanged model, context skips, API failures, safe logs, log failure")
} finally {
  globalThis.fetch = oldFetch
  for (const [key, value] of [["XDG_CONFIG_HOME", oldConfig], ["XDG_STATE_HOME", oldState], ["JEV_OPENROUTER_KEY_FILE", oldKeyFile]]) {
    if (value === undefined) delete process.env[key]
    else process.env[key] = value
  }
  await rm(dir, { recursive: true, force: true })
}
