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
// A saved catalog is served at once and refetched by a zero-delay timer after setup.
const timers = { setInterval: globalThis.setInterval, setTimeout: globalThis.setTimeout }
let added, runs = 0, transform, reloads = 0, refresh, startup
globalThis.setInterval = (fn) => { refresh = fn; return { unref() {} } }
globalThis.setTimeout = (fn) => { startup = fn }
const run = () => { const out = []; transform({ add: (x) => out.push(x) }); runs++; return out }
const load = async () => {
  added = transform = startup = undefined
  const cleanup = await plugin.setup({ provider: {
    transform: async (callback) => { transform = callback; return { dispose: async () => {} } },
    reload: async () => { reloads++ },
  } })
  if (!transform) return undefined
  await startup?.()
  assert.equal(typeof cleanup, "function")
  const first = run()
  assert.deepEqual(run(), first, "a provider reload replays the same catalog")
  assert.equal(first.length, 1)
  added = first[0]
  return Object.fromEntries(added.models.map(m => [m.id, m]))
}
const listed = [
  { slug: "claude/claude-fable-5-1", context_window: 1000000, max_tokens: 128000, input_modalities: ["text", "image"] },
  { slug: "codex/gpt-6-astra", context_window: 272000, max_tokens: 128000, input_modalities: ["text", "image"] },
  { slug: "codex/gpt-5.3-codex-spark", input_modalities: ["text"] },
  // CLIProxyAPI advertises 272000 and no max_tokens for models it has no metadata for.
  { slug: "openrouter/or-glm-5.3-flash", context_window: 272000 },
  { slug: "openrouter/or-gcp-a-model-name", context_window: 272000 },
  { slug: "openrouter/or-minimax-m3:free" },
  { slug: "claude/claude-haiku-4-5-20251001", context_window: 200000, max_tokens: 64000 },
  // Effort levels come only from the catalog; an empty or missing list means none.
  { slug: "claude/claude-opus-5-5", supported_reasoning_levels: ["low", "medium", "high", "xhigh", "max"].map(effort => ({ effort })) },
  { slug: "codex/gpt-5.6-sol", supported_reasoning_levels: ["low", "medium", "high", "xhigh", "max", "ultra"].map(effort => ({ effort })) },
  { slug: "claude/claude-haiku-5", supported_reasoning_levels: [] },
  { slug: "openrouter/or-reasoner", supported_reasoning_levels: [{ effort: "high" }] },
]
const ignored = [
  // Unscoped or differently scoped entries must never override a pinned route.
  { slug: "gpt-6-astra", context_window: 272000 },
  { slug: "copilot/gpt-6-astra", context_window: 272000 },
  { slug: "claude-fable-5-1", context_window: 1000000 },
  { slug: "or-minimax-m3:free", context_window: 272000 },
  { slug: "ollama/or-minimax-m3:free", context_window: 272000 },
  { slug: "codex/gpt-image-2", context_window: 272000 },
  { slug: "text-embedding-3-small", context_window: 272000 },
]
const ids = listed.map(m => m.slug.replace(/^[^/]+\//, ""))
let data = [...listed, ...ignored]
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
  assert.equal(models["claude-fable-5-1"].name, "Claude Fable 5.1")
  assert.equal(models["claude-haiku-4-5-20251001"].name, "Claude Haiku 4.5")
  assert.equal(models["claude-fable-5-1"].modelID, "claude/claude-fable-5-1")
  assert.equal(models["claude-fable-5-1"].package, "aisdk:@ai-sdk/anthropic")
  assert.equal(models["gpt-6-astra"].name, "GPT-6 Astra")
  assert.equal(models["gpt-5.3-codex-spark"].name, "GPT-5.3 Codex Spark")
  assert.equal(models["gpt-6-astra"].modelID, "codex/gpt-6-astra")
  // CLIProxyAPI's Responses stream changes item IDs and crashes the Responses parser.
  assert.equal(models["gpt-6-astra"].package, "aisdk:@ai-sdk/openai-compatible")
  // Effort variants mirror the catalog: effort for the Anthropic SDK, reasoningEffort otherwise.
  assert.deepEqual(models["claude-opus-5-5"].variants, ["low", "medium", "high", "xhigh", "max"].map(id => ({ id, settings: { effort: id } })))
  assert.deepEqual(models["gpt-5.6-sol"].variants, ["low", "medium", "high", "xhigh", "max", "ultra"].map(id => ({ id, settings: { reasoningEffort: id } })))
  assert.deepEqual(models["or-reasoner"].variants, [{ id: "high", settings: { reasoningEffort: "high" } }])
  assert.deepEqual(models["claude-haiku-5"].variants, [])
  assert.deepEqual(models["claude-fable-5-1"].variants, [], "no advertised levels, no variants")
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
  assert.deepEqual(Object.keys(models).sort(), [...ids].sort())
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
  assert.equal(Object.keys(reloaded).length, ids.length + 1)
  const online = globalThis.fetch
  globalThis.fetch = async () => new Response("unavailable", { status: 503 })
  await refresh()
  assert.equal(reloads, 1, "offline: the saved catalog is the same, so nothing reloads")
  globalThis.fetch = online
  // With a saved catalog, setup must not wait for the catalog request.
  let fetches = 0, release
  const gate = new Promise(resolve => { release = resolve })
  globalThis.fetch = async () => { fetches++; await gate; return Response.json({ models: data }) }
  added = transform = startup = undefined
  await plugin.setup({ provider: { transform: async (callback) => { transform = callback }, reload: async () => { reloads++ } } })
  assert.equal(fetches, 0, "setup returns before the catalog request starts")
  assert.equal(run()[0].models.length, ids.length + 1, "the saved catalog is listed at once")
  const pending = startup()
  data = [...data, { slug: "codex/gpt-6-luna", context_window: 272000, max_tokens: 128000 }]
  release()
  await pending
  assert.equal(fetches, 1)
  assert.equal(reloads, 2, "a changed fresh catalog reloads the provider")
  assert.equal(run()[0].models.length, ids.length + 2)
  globalThis.fetch = online
  data = data.filter(m => !["codex/gpt-6-terra", "codex/gpt-6-luna"].includes(m.slug))
  await writeFile(join(cache, "opencode/cliproxy-models.json"), beforeRefresh)
  // A partial listing (credential in cooldown) must not drop models seen before.
  data = [{ slug: "openrouter/or-new-model", context_window: 272000, input_modalities: ["text", "image"] }]
  models = await load()
  assert.deepEqual(Object.keys(models).sort(), [...ids, "or-new-model"].sort())
  assert.equal(models["gpt-6-astra"].modelID, "codex/gpt-6-astra")
  data = [{ slug: "openrouter/or-new-model", context_window: 300000, input_modalities: ["text"] }]
  models = await load()
  assert.deepEqual(models["or-new-model"].limit, { context: 300000, output: 8192 })
  globalThis.fetch = async () => new Response("unavailable", { status: 503 })
  models = await load()
  assert.equal(Object.keys(models).length, ids.length + 1)
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
  const versions = {
    "gpt-6.1-sol": "GPT-6.1 Sol", "gpt-6-sol": "GPT-6 Sol", "gpt-5.6-sol": "GPT-5.6 Sol", "gpt-6-luna": "GPT-6 Luna",
    "gpt-5.6-luna": "GPT-5.6 Luna", "gpt-5.6-terra": "GPT-5.6 Terra", "gpt-5.5": "GPT-5.5", "codex-auto-review": "Codex Auto Review",
  }
  globalThis.fetch = async () => Response.json({ models: Object.keys(versions).map(id => ({ slug: `codex/${id}` })) })
  models = await load()
  for (const [id, name] of Object.entries(versions)) {
    assert.equal(models[id].name, name)
    assert.equal(models[id].modelID, `codex/${id}`)
  }
  const oldNames = JSON.parse(await readFile(cachePath, "utf8"))
  for (const id of Object.keys(versions)) oldNames.models[id].name = "old-label"
  await writeFile(cachePath, JSON.stringify(oldNames))
  globalThis.fetch = async () => new Response("unavailable", { status: 503 })
  models = await load()
  assert.equal(models["gpt-6.1-sol"].name, "GPT-6.1 Sol")
  globalThis.fetch = async () => Response.json({ models: [{ slug: "openrouter/or-new-model" }] })
  models = await load()
  assert.equal(models["gpt-5.6-luna"].name, "GPT-5.6 Luna")
  // Caches written before the readable names get them too, for every subscription.
  globalThis.fetch = async () => Response.json({ models: [{ slug: "claude/claude-opus-5-5" }, { slug: "openrouter/or-glm-5.3-flash" }] })
  await load()
  const oldClaude = JSON.parse(await readFile(cachePath, "utf8"))
  oldClaude.models["claude-opus-5-5"].name = "anthropic-claude-opus-5.5"
  oldClaude.models["or-glm-5.3-flash"].name = "old-label"
  oldClaude.models["broken"] = { ...oldClaude.models["or-glm-5.3-flash"], id: "/" }
  oldClaude.seen["broken"] = Date.now()
  await writeFile(cachePath, JSON.stringify(oldClaude))
  globalThis.fetch = async () => new Response("unavailable", { status: 503 })
  models = await load()
  assert.equal(models["claude-opus-5-5"].name, "Claude Opus 5.5")
  assert.equal(models["or-glm-5.3-flash"].name, "or-glm-5.3-flash")
  assert.equal(models["broken"].name, "/", "a malformed cached id keeps startup alive")
  globalThis.fetch = async () => Response.json({ models: [{ slug: "openrouter/or-new-model" }] })
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
  console.log("PASS: subscription routing, token limits, effort variants, collision exclusion, stable selections, names, SDK routing, catalog merge over cache, retirement after 14 days, safe cache fallback, legacy cache rejection, reload replay, timed refresh and reload, saved catalog served before the fetch, no cached secrets")
} finally {
  globalThis.fetch = originalFetch
  Object.assign(globalThis, timers)
  for (const [k, v] of [["XDG_CACHE_HOME", saved.cache], ["ANTHROPIC_BASE_URL", saved.base], ["ANTHROPIC_AUTH_TOKEN", saved.key]]) v === undefined ? delete process.env[k] : process.env[k] = v
  await rm(cache, { recursive: true, force: true })
}
