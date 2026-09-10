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
  { slug: "claude/claude-fable-5-1", context_window: 1000000, max_tokens: 128000 },
  { slug: "codex/gpt-6-astra", context_window: 272000, max_tokens: 128000 },
  // CLIProxyAPI advertises 272000 and no max_tokens for models it has no metadata for.
  { slug: "openrouter/or-glm-5.3-flash", context_window: 272000 },
  { slug: "openrouter/or-gcp-a-model-name", context_window: 272000 },
  { slug: "openrouter/or-minimax-m3:free" },
  // Unscoped or differently scoped entries must never override a pinned route.
  { slug: "gpt-6-astra", context_window: 272000 },
  { slug: "copilot/gpt-6-astra", context_window: 272000 },
  { slug: "claude-fable-5-1", context_window: 1000000 },
  { slug: "or-minimax-m3:free", context_window: 272000 },
  { slug: "ollama/or-minimax-m3:free", context_window: 272000 },
  { slug: "codex/gpt-image-2", context_window: 272000 },
  { slug: "text-embedding-3-small", context_window: 272000 },
]
try {
  const hook = await plugin()
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "https://proxy.invalid/v1/models?client_version=99.0.0")
    assert.equal(options.headers.Authorization, "Bearer test-secret")
    return Response.json({ models: data })
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
  // OpenCode compacts at context minus output; 32k here compacted every few turns.
  assert.deepEqual(models["gpt-6-astra"].limit, { context: 272000, output: 128000 })
  assert.deepEqual(models["claude-fable-5-1"].limit, { context: 1000000, output: 128000 })
  assert.deepEqual(models["or-glm-5.3-flash"].limit, { context: 272000, output: 8192 })
  assert.deepEqual(models["or-minimax-m3:free"].limit, { context: 32768, output: 8192 })
  assert.equal(models["gpt-6-astra"].options?.store, undefined)
  assert.equal(models["or-glm-5.3-flash"].name, "or-glm-5.3-flash")
  assert.equal(models["or-glm-5.3-flash"].id, "openrouter/or-glm-5.3-flash")
  assert.equal(models["or-gcp-a-model-name"].name, "or-gcp-a-model-name")
  assert.equal(models["or-minimax-m3:free"].provider.npm, "@ai-sdk/openai-compatible")
  assert.equal(models["or-minimax-m3:free"].id, "openrouter/or-minimax-m3:free")
  assert.equal(Object.keys(models).length, 5)
  const saved = await readFile(join(cache, "opencode/cliproxy-models.json"), "utf8")
  assert.ok(!saved.includes("test-secret"))
  data = [{ slug: "openrouter/or-new-model", context_window: 272000 }]
  await hook.config(config)
  assert.deepEqual(Object.keys(config.provider.cliproxy.models), ["or-new-model"])
  globalThis.fetch = async () => new Response("unavailable", { status: 503 })
  await hook.config(config)
  assert.deepEqual(Object.keys(config.provider.cliproxy.models), ["or-new-model"])
  config.provider.cliproxy.options.baseURL = "https://different.invalid/v1"
  await assert.rejects(hook.config(config), /HTTP 503/)
  config.provider.cliproxy.options.baseURL = "https://proxy.invalid/v1"
  globalThis.fetch = async () => Response.json({ models: [{ slug: 42 }] })
  await hook.config(config)
  assert.deepEqual(Object.keys(config.provider.cliproxy.models), ["or-new-model"])
  // An old cache would silently restore the cross-provider routing bug or the 32k limits.
  const legacy = JSON.parse(saved)
  legacy.version = 2
  await writeFile(join(cache, "opencode/cliproxy-models.json"), JSON.stringify(legacy))
  globalThis.fetch = async () => new Response("unavailable", { status: 503 })
  await assert.rejects(hook.config(config), /HTTP 503/)
  globalThis.fetch = async () => Response.json({ models: [{ slug: "gpt-6-astra" }] })
  await assert.rejects(hook.config(config), /no supported models/)
  console.log("PASS: subscription routing, token limits, collision exclusion, stable selections, names, SDK routing, catalog updates, safe cache fallback, legacy cache rejection, no cached secrets")
} finally {
  globalThis.fetch = originalFetch
  if (previousCache === undefined) delete process.env.XDG_CACHE_HOME
  else process.env.XDG_CACHE_HOME = previousCache
  await rm(cache, { recursive: true, force: true })
}
