import assert from "node:assert/strict"
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises"
import { tmpdir } from "node:os"
import { join } from "node:path"
import plugin from "./cliproxy-models.js"

const cache = await mkdtemp(join(tmpdir(), "cliproxy-models-test-"))
const saved = { cache: process.env.XDG_CACHE_HOME, base: process.env.ANTHROPIC_BASE_URL, key: process.env.ANTHROPIC_AUTH_TOKEN }
const originalFetch = globalThis.fetch
process.env.XDG_CACHE_HOME = cache
process.env.ANTHROPIC_BASE_URL = "https://proxy.invalid"
process.env.ANTHROPIC_AUTH_TOKEN = "test-secret"
// OpenCode 2 runs setup once, then replays the synchronous provider transform on every reload.
const timers = { setInterval: globalThis.setInterval }
let added, runs = 0, transform, reloads = 0, refresh
globalThis.setInterval = (fn) => { refresh = fn; return { unref() {} } }
const run = () => { const out = []; transform({ add: (x) => out.push(x) }); runs++; return out }
const load = async () => {
  added = transform = undefined
  const cleanup = await plugin.setup({ provider: {
    transform: async (callback) => { transform = callback; return { dispose: async () => {} } },
    reload: async () => { reloads++ },
  } })
  if (!transform) return undefined
  assert.equal(typeof cleanup, "function")
  const first = run()
  assert.deepEqual(run(), first, "a provider reload replays the same catalog")
  assert.equal(first.length, 1)
  added = first[0]
  return Object.fromEntries(added.models.map(m => [m.id, m]))
}
let data = [
  { slug: "claude/claude-fable-5-1", context_window: 1000000, max_tokens: 128000, input_modalities: ["text", "image"] },
  { slug: "codex/gpt-6-astra", context_window: 272000, max_tokens: 128000, input_modalities: ["text", "image"] },
  { slug: "codex/gpt-5.3-codex-spark", input_modalities: ["text"] },
  // CLIProxyAPI advertises 272000 and no max_tokens for models it has no metadata for.
  { slug: "openrouter/or-glm-5.3-flash", context_window: 272000 },
  { slug: "openrouter/or-gcp-a-model-name", context_window: 272000 },
  { slug: "openrouter/or-minimax-m3:free" },
  { slug: "claude/claude-haiku-4-5-20251001", context_window: 200000, max_tokens: 64000 },
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
  globalThis.fetch = async (url, options) => {
    assert.equal(url, "https://proxy.invalid/v1/models?client_version=99.0.0")
    assert.equal(options.headers.Authorization, "Bearer test-secret")
    return Response.json({ models: data })
  }
  let models = await load()
  assert.equal(added.info.id, "cliproxy")
  assert.equal(added.info.activation, "enabled")
  assert.deepEqual(added.info.settings, { baseURL: "https://proxy.invalid/v1", apiKey: "test-secret" })
  for (const id of ["claude-fable-5-1", "gpt-6-astra"]) {
    assert.deepEqual(models[id].capabilities, { tools: true, input: ["text", "image"], output: ["text"] })
  }
  for (const id of ["gpt-5.3-codex-spark", "or-glm-5.3-flash"]) {
    assert.deepEqual(models[id].capabilities, { tools: true, input: ["text"], output: ["text"] })
  }
  assert.ok(Object.values(models).every(m => m.providerID === "cliproxy" && m.enabled && m.status === "active"))
  assert.equal(models["claude-fable-5-1"].name, "anthropic-claude-fable-5.1")
  assert.equal(models["claude-fable-5-1"].modelID, "claude/claude-fable-5-1")
  assert.equal(models["claude-fable-5-1"].package, "aisdk:@ai-sdk/anthropic")
  assert.equal(models["gpt-6-astra"].name, "codex-6-astra")
  assert.equal(models["gpt-6-astra"].modelID, "codex/gpt-6-astra")
  // CLIProxyAPI's Responses stream changes item IDs and crashes the Responses parser.
  assert.equal(models["gpt-6-astra"].package, "aisdk:@ai-sdk/openai-compatible")
  // Effort variants: reasoningEffort for Codex, effort for Claude, none for Haiku or OpenRouter.
  assert.deepEqual(models["gpt-6-astra"].variants, ["low", "medium", "high"].map(id => ({ id, settings: { reasoningEffort: id } })))
  assert.deepEqual(models["claude-fable-5-1"].variants.map(v => v.settings), [{ effort: "low" }, { effort: "medium" }, { effort: "high" }])
  assert.deepEqual(models["claude-haiku-4-5-20251001"].variants, [])
  assert.deepEqual(models["or-glm-5.3-flash"].variants, [])
  // OpenCode compacts at context minus output; 32k here compacted every few turns.
  assert.deepEqual(models["gpt-6-astra"].limit, { context: 272000, output: 128000 })
  assert.deepEqual(models["claude-fable-5-1"].limit, { context: 1000000, output: 128000 })
  assert.deepEqual(models["or-glm-5.3-flash"].limit, { context: 272000, output: 8192 })
  assert.deepEqual(models["or-minimax-m3:free"].limit, { context: 32768, output: 8192 })
  assert.equal(models["or-glm-5.3-flash"].name, "or-glm-5.3-flash")
  assert.equal(models["or-glm-5.3-flash"].modelID, "openrouter/or-glm-5.3-flash")
  assert.equal(models["or-gcp-a-model-name"].name, "or-gcp-a-model-name")
  assert.equal(models["or-minimax-m3:free"].package, "aisdk:@ai-sdk/openai-compatible")
  assert.equal(models["or-minimax-m3:free"].modelID, "openrouter/or-minimax-m3:free")
  assert.equal(Object.keys(models).length, 7)
  const savedCache = await readFile(join(cache, "opencode/cliproxy-models.json"), "utf8")
  assert.ok(!savedCache.includes("test-secret"))
  assert.equal(JSON.parse(savedCache).version, 5, "the cache shape stays version 5 across the OpenCode 2 upgrade")
  // The timer refetches outside the transform; only a changed catalog reloads the provider.
  const beforeRefresh = await readFile(join(cache, "opencode/cliproxy-models.json"), "utf8")
  await refresh()
  assert.equal(reloads, 0, "an unchanged catalog does not reload")
  data = [...data, { slug: "codex/gpt-6-terra", context_window: 272000, max_tokens: 128000 }]
  await refresh()
  assert.equal(reloads, 1)
  const reloaded = Object.fromEntries(run()[0].models.map(m => [m.id, m]))
  assert.equal(reloaded["gpt-6-terra"].modelID, "codex/gpt-6-terra", "the reloaded provider lists the new model")
  assert.equal(Object.keys(reloaded).length, 8)
  const online = globalThis.fetch
  globalThis.fetch = async () => new Response("unavailable", { status: 503 })
  await refresh()
  assert.equal(reloads, 1, "offline: the saved catalog is the same, so nothing reloads")
  globalThis.fetch = online
  data = data.filter(m => m.slug !== "codex/gpt-6-terra")
  await writeFile(join(cache, "opencode/cliproxy-models.json"), beforeRefresh)
  // A partial listing (credential in cooldown) must not drop models seen before.
  data = [{ slug: "openrouter/or-new-model", context_window: 272000, input_modalities: ["text", "image"] }]
  models = await load()
  assert.deepEqual(Object.keys(models).sort(), ["claude-fable-5-1", "claude-haiku-4-5-20251001", "gpt-5.3-codex-spark", "gpt-6-astra", "or-gcp-a-model-name", "or-glm-5.3-flash", "or-minimax-m3:free", "or-new-model"])
  assert.equal(models["gpt-6-astra"].modelID, "codex/gpt-6-astra")
  data = [{ slug: "openrouter/or-new-model", context_window: 300000, input_modalities: ["text"] }]
  models = await load()
  assert.deepEqual(models["or-new-model"].limit, { context: 300000, output: 8192 })
  globalThis.fetch = async () => new Response("unavailable", { status: 503 })
  models = await load()
  assert.equal(Object.keys(models).length, 8)
  // A model unlisted for over 14 days is retired; a recently unlisted one is kept with its original timestamp.
  const cachePath = join(cache, "opencode/cliproxy-models.json")
  const aged = JSON.parse(await readFile(cachePath, "utf8"))
  const fifteenDaysAgo = Date.now() - 15 * 24 * 60 * 60 * 1000
  aged.seen["gpt-5.3-codex-spark"] = fifteenDaysAgo
  aged.seen["gpt-6-astra"] = fifteenDaysAgo + 2 * 24 * 60 * 60 * 1000
  await writeFile(cachePath, JSON.stringify(aged))
  globalThis.fetch = async () => Response.json({ models: data })
  models = await load()
  assert.equal(models["gpt-5.3-codex-spark"], undefined)
  assert.equal(models["gpt-6-astra"].modelID, "codex/gpt-6-astra")
  assert.equal(JSON.parse(await readFile(cachePath, "utf8")).seen["gpt-6-astra"], aged.seen["gpt-6-astra"])
  // A corrupt cache must not block a successful fetch from replacing it.
  await writeFile(cachePath, "{not json")
  globalThis.fetch = async () => Response.json({ models: [{ slug: "openrouter/or-new-model", context_window: 272000, input_modalities: ["text", "image"] }] })
  models = await load()
  assert.deepEqual(Object.keys(models), ["or-new-model"])
  globalThis.fetch = async () => new Response("unavailable", { status: 503 })
  models = await load()
  assert.deepEqual(Object.keys(models), ["or-new-model"])
  assert.deepEqual(models["or-new-model"].capabilities.input, ["text", "image"])
  // Distinct versions stay distinguishable, including old names in an offline cache.
  const versions = ["gpt-6.1-sol", "gpt-6-sol", "gpt-5.6-sol", "gpt-6-luna", "gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.5", "codex-auto-review"]
  globalThis.fetch = async () => Response.json({ models: versions.map(id => ({ slug: `codex/${id}` })) })
  models = await load()
  for (const id of versions) {
    assert.equal(models[id].name, id.startsWith("codex-") ? id : `codex-${id.slice(4)}`)
    assert.equal(models[id].modelID, `codex/${id}`)
  }
  const oldNames = JSON.parse(await readFile(cachePath, "utf8"))
  for (const id of versions) oldNames.models[id].name = "old-label"
  await writeFile(cachePath, JSON.stringify(oldNames))
  globalThis.fetch = async () => new Response("unavailable", { status: 503 })
  models = await load()
  assert.equal(models["gpt-6.1-sol"].name, "codex-6.1-sol")
  globalThis.fetch = async () => Response.json({ models: [{ slug: "openrouter/or-new-model" }] })
  models = await load()
  assert.equal(models["gpt-5.6-luna"].name, "codex-5.6-luna")
  // Restore the single-model cache used by the remaining fallback checks.
  await writeFile(cachePath, "{not json")
  await load()
  globalThis.fetch = async () => new Response("unavailable", { status: 503 })
  process.env.ANTHROPIC_BASE_URL = "https://different.invalid"
  await assert.rejects(load(), /HTTP 503/)
  process.env.ANTHROPIC_BASE_URL = "https://proxy.invalid"
  globalThis.fetch = async () => Response.json({ models: [{ slug: 42 }] })
  models = await load()
  assert.deepEqual(Object.keys(models), ["or-new-model"])
  // A cache without per-model timestamps could never retire anything.
  const legacy = JSON.parse(savedCache)
  legacy.version = 4
  await writeFile(cachePath, JSON.stringify(legacy))
  globalThis.fetch = async () => new Response("unavailable", { status: 503 })
  await assert.rejects(load(), /HTTP 503/)
  globalThis.fetch = async () => Response.json({ models: [{ slug: "gpt-6-astra" }] })
  await assert.rejects(load(), /no supported models/)
  // Credentials come from the pod environment; half a configuration is an error, none is a no-op.
  delete process.env.ANTHROPIC_AUTH_TOKEN
  await assert.rejects(load(), /URL or API key is missing/)
  delete process.env.ANTHROPIC_BASE_URL
  assert.equal(await load(), undefined)
  assert.ok(runs > 0)
  console.log("PASS: subscription routing, token limits, effort variants, collision exclusion, stable selections, names, SDK routing, catalog merge over cache, retirement after 14 days, safe cache fallback, legacy cache rejection, reload replay, timed refresh and reload, no cached secrets")
} finally {
  globalThis.fetch = originalFetch
  Object.assign(globalThis, timers)
  for (const [k, v] of [["XDG_CACHE_HOME", saved.cache], ["ANTHROPIC_BASE_URL", saved.base], ["ANTHROPIC_AUTH_TOKEN", saved.key]]) v === undefined ? delete process.env[k] : process.env[k] = v
  await rm(cache, { recursive: true, force: true })
}
