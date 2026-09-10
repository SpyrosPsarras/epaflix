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
  { id: "claude/claude-fable-5-1", owned_by: "anthropic" },
  { id: "codex/gpt-6-astra", owned_by: "OpenAI" },
  { id: "openrouter/or-glm-5.3-flash", owned_by: "openrouter" },
  { id: "openrouter/or-gcp-a-model-name", owned_by: "openrouter" },
  { id: "openrouter/or-minimax-m3:free", owned_by: "openrouter" },
  // Unscoped or differently scoped entries must never override a pinned route.
  { id: "gpt-6-astra", owned_by: "OpenAI" },
  { id: "copilot/gpt-6-astra", owned_by: "OpenAI" },
  { id: "claude-fable-5-1", owned_by: "anthropic" },
  { id: "or-minimax-m3:free", owned_by: "ollama" },
  { id: "ollama/or-minimax-m3:free", owned_by: "ollama" },
  { id: "codex/gpt-image-2", owned_by: "openai" },
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
  assert.equal(models["claude-fable-5-1"].id, "claude/claude-fable-5-1")
  assert.equal(models["claude-fable-5-1"].provider.npm, "@ai-sdk/anthropic")
  assert.equal(models["gpt-6-astra"].name, "codex-astra")
  assert.equal(models["gpt-6-astra"].id, "codex/gpt-6-astra")
  // CLIProxyAPI's Responses stream changes item IDs and crashes the Responses parser.
  assert.equal(models["gpt-6-astra"].provider.npm, "@ai-sdk/openai-compatible")
  assert.equal(models["gpt-6-astra"].reasoning, true)
  assert.equal(models["gpt-6-astra"].options?.store, undefined)
  assert.equal(models["or-glm-5.3-flash"].name, "or-glm-5.3-flash")
  assert.equal(models["or-glm-5.3-flash"].id, "openrouter/or-glm-5.3-flash")
  assert.equal(models["or-gcp-a-model-name"].name, "or-gcp-a-model-name")
  assert.equal(models["or-minimax-m3:free"].provider.npm, "@ai-sdk/openai-compatible")
  assert.equal(models["or-minimax-m3:free"].id, "openrouter/or-minimax-m3:free")
  assert.equal(Object.keys(models).length, 5)
  const saved = await readFile(join(cache, "opencode/cliproxy-models.json"), "utf8")
  assert.ok(!saved.includes("test-secret"))
  data = [{ id: "openrouter/or-new-model", owned_by: "openrouter" }]
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
  // An old cache would silently restore the cross-provider routing bug.
  const legacy = JSON.parse(saved)
  delete legacy.version
  await writeFile(join(cache, "opencode/cliproxy-models.json"), JSON.stringify(legacy))
  globalThis.fetch = async () => new Response("unavailable", { status: 503 })
  await assert.rejects(hook.config(config), /HTTP 503/)
  globalThis.fetch = async () => Response.json({ data: [{ id: "gpt-6-astra", owned_by: "OpenAI" }] })
  await assert.rejects(hook.config(config), /no supported models/)
  console.log("PASS: subscription routing, collision exclusion, stable selections, names, SDK routing, catalog updates, safe cache fallback, legacy cache rejection, no cached secrets")
} finally {
  globalThis.fetch = originalFetch
  if (previousCache === undefined) delete process.env.XDG_CACHE_HOME
  else process.env.XDG_CACHE_HOME = previousCache
  await rm(cache, { recursive: true, force: true })
}
