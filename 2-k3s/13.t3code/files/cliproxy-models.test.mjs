import assert from "node:assert/strict"
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import plugin from "./cliproxy-models.js"

const cache = await mkdtemp(join(tmpdir(), "cliproxy-models-test-"))
const previousCache = process.env.XDG_CACHE_HOME
const originalFetch = globalThis.fetch
process.env.XDG_CACHE_HOME = cache
const config = { provider: { cliproxy: { options: { baseURL: "https://proxy.invalid/v1", apiKey: "test-secret" } } } }
let data = [
  { id: "claude-fable-5-1", owned_by: "anthropic" },
  { id: "gpt-6-astra", owned_by: "OpenAI" },
  { id: "or-glm-5.3-flash", owned_by: "openrouter" },
  { id: "or-gcp-a-model-name", owned_by: "openrouter" },
  { id: "or-minimax-m3:free", owned_by: "openrouter" },
  { id: "gpt-image-2", owned_by: "openai" },
  { id: "text-embedding-3-small", owned_by: "Azure OpenAI" },
]
try {
  const hook = await plugin()
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "https://proxy.invalid/v1/models")
    assert.equal(options.headers.Authorization, "Bearer test-secret")
    return Response.json({ data })
  }
  await hook.config(config)
  const models = config.provider.cliproxy.models
  assert.equal(models["claude-fable-5-1"].name, "anthropic-claude-fable-5.1")
  assert.equal(models["claude-fable-5-1"].provider.npm, "@ai-sdk/anthropic")
  assert.equal(models["gpt-6-astra"].name, "codex-astra")
  // CLIProxyAPI's Responses stream changes item IDs and crashes the Responses parser.
  assert.equal(models["gpt-6-astra"].provider.npm, "@ai-sdk/openai-compatible")
  assert.equal(models["gpt-6-astra"].reasoning, true)
  assert.equal(models["gpt-6-astra"].options?.store, undefined)
  assert.equal(models["or-glm-5.3-flash"].name, "or-glm-5.3-flash")
  assert.equal(models["or-gcp-a-model-name"].name, "or-gcp-a-model-name")
  assert.equal(models["or-minimax-m3:free"].provider.npm, "@ai-sdk/openai-compatible")
  assert.equal(Object.keys(models).length, 5)
  const saved = await readFile(join(cache, "opencode/cliproxy-models.json"), "utf8")
  assert.ok(!saved.includes("test-secret"))
  data = [{ id: "or-new-model", owned_by: "openrouter" }]
  await hook.config(config)
  assert.deepEqual(Object.keys(config.provider.cliproxy.models), ["or-new-model"])
  globalThis.fetch = async () => new Response("unavailable", { status: 503 })
  await hook.config(config)
  assert.deepEqual(Object.keys(config.provider.cliproxy.models), ["or-new-model"])
  config.provider.cliproxy.options.baseURL = "https://different.invalid/v1"
  await assert.rejects(hook.config(config), /HTTP 503/)
  config.provider.cliproxy.options.baseURL = "https://proxy.invalid/v1"
  globalThis.fetch = async () => Response.json({ data: [{ id: 42 }] })
  await hook.config(config)
  assert.deepEqual(Object.keys(config.provider.cliproxy.models), ["or-new-model"])
  console.log("PASS: names, Codex Chat Completions routing, catalog updates, safe cache fallback, no cached secrets")
} finally {
  globalThis.fetch = originalFetch
  if (previousCache === undefined) delete process.env.XDG_CACHE_HOME
  else process.env.XDG_CACHE_HOME = previousCache
  await rm(cache, { recursive: true, force: true })
}
