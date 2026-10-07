import { mkdir, readFile, rename, writeFile } from "node:fs/promises"
import { homedir } from "node:os"
import { dirname, join } from "node:path"

const RETIRE_AFTER_MS = 14 * 24 * 60 * 60 * 1000
const title = words => words.filter(Boolean).map(word => word[0].toUpperCase() + word.slice(1)).join(" ")
// claude-opus-5-5 reads Claude Opus 5.5, gpt-6.1-sol reads GPT-6.1 Sol;
// OpenRouter IDs follow no common pattern and stay as they are.
const displayName = slug => {
  const [, subscription, id = slug] = /^([^/]+)\/(.+)$/.exec(slug) ?? []
  if (subscription === "claude") return title(id.replace(/-\d{8}$/, "").replace(/(\d)-(\d)(?=-|$)/g, "$1.$2").split("-"))
  if (subscription !== "codex") return id
  const [, version, rest] = /^gpt-([\d.]+)-?(.*)$/.exec(id) ?? []
  return version ? [`GPT-${version}`, ...(rest ? [title(rest.split("-"))] : [])].join(" ") : title(id.split("-"))
}

const cachePath = () => join(process.env.XDG_CACHE_HOME || join(homedir(), ".cache"), "opencode", "cliproxy-models.json")

// The cache keeps the version 5 catalog shape; OpenCode 2 model entries are derived from it.
const readCache = async url => {
  const cached = await readFile(cachePath(), "utf8").then(JSON.parse).catch(() => null)
  if (cached?.version !== 5 || cached.url !== url || !Object.keys(cached.models ?? {}).length) return null
  // Update old display names even when a credential is cooling down or the API is offline.
  for (const model of Object.values(cached.models)) {
    if (model.id?.includes("/")) model.name = displayName(model.id)
  }
  return cached
}

const discover = async (url, key) => {
  const cached = await readCache(url)
  const usable = cached !== null
  const now = Date.now()
  let models
  const seen = {}
  try {
    // The plain listing strips every field but id. The Codex client listing on
    // the same route carries context_window and max_tokens for every model.
    // CLIProxyAPI only compares the version against 0.144.0 to pick reasoning
    // level names; a high value keeps the newest shape and never touches limits.
    const response = await fetch(`${url.replace(/\/$/, "")}/models?client_version=99.0.0`, {
      headers: { Authorization: `Bearer ${key}` },
      signal: AbortSignal.timeout(10000),
    })
    if (!response.ok) throw new Error(`catalog HTTP ${response.status}`)
    const catalog = await response.json()
    if (!Array.isArray(catalog.models) || !catalog.models.length) throw new Error("empty or invalid catalog")
    models = {}
    for (const model of catalog.models) {
      if (typeof model.slug !== "string" || !model.slug.trim()) throw new Error("invalid catalog model")
      // Only prefixed catalog entries bind a request to one subscription.
      const route = /^(codex|claude|openrouter)\/(.+)$/.exec(model.slug)
      if (!route) continue
      const [, subscription, id] = route
      const name = displayName(model.slug)
      let npm
      if (subscription === "openrouter") {
        npm = "@ai-sdk/openai-compatible"
      } else if (subscription === "claude") {
        npm = "@ai-sdk/anthropic"
      } else if (subscription === "codex" && !id.startsWith("gpt-image-")) {
        // Responses item IDs change between events in CLIProxyAPI's stream.
        // Chat Completions uses indexed deltas and avoids that broken parser path.
        npm = "@ai-sdk/openai-compatible"
      } else {
        continue
      }
      const input = Array.isArray(model.input_modalities)
        ? model.input_modalities.filter(value => ["text", "image", "audio", "video", "pdf"].includes(value))
        : ["text"]
      models[id] = {
        // Preserve saved OpenCode selections while sending the scoped API ID.
        id: model.slug,
        name,
        provider: { npm },
        modalities: { input, output: ["text"] },
        attachment: input.some(value => value !== "text"),
        // Codex advertises separate default and maximum windows. Use the
        // subscription maximum, not the smaller CLI default.
        // OpenCode compacts at context minus output, so a wrong small context
        // compacts every few turns. CLIProxyAPI omits max_tokens for models it
        // has no metadata for; 8192 keeps that fallback conservative.
        limit: { context: model.max_context_window || model.context_window || 32768, output: model.max_tokens || 8192 },
        ...(subscription === "codex" ? { reasoning: true } : {}),
        efforts: (Array.isArray(model.supported_reasoning_levels) ? model.supported_reasoning_levels : [])
          .map(level => level?.effort).filter(effort => typeof effort === "string"),
      }
    }
    if (!Object.keys(models).length) throw new Error("catalog has no supported models")
    // CLIProxyAPI lists only models of credentials active right now. A model
    // seen once stays selectable while its credential is in quota cooldown.
    // Codex's longest cooldown is the 7-day window; a model unlisted for
    // longer than 14 days is treated as retired.
    for (const id of Object.keys(models)) seen[id] = now
    if (usable) {
      for (const [id, at] of Object.entries(cached.seen)) {
        if (!models[id] && now - at <= RETIRE_AFTER_MS) {
          models[id] = cached.models[id]
          seen[id] = at
        }
      }
    }
  } catch (error) {
    if (!usable) throw error
    console.error(`[cliproxy-models] ${error.message}; using catalog saved at ${cached.updatedAt}`)
    return cached.models
  }
  const path = cachePath()
  await mkdir(dirname(path), { recursive: true })
  const temporary = `${path}.${process.pid}.${crypto.randomUUID()}`
  await writeFile(temporary, JSON.stringify({ version: 5, url, updatedAt: new Date(now).toISOString(), models, seen }), { mode: 0o600 })
  await rename(temporary, path)
  console.error(`[cliproxy-models] discovered ${Object.keys(models).length} models`)
  return models
}

// Each model gets exactly the effort levels CLIProxyAPI advertises for it;
// T3's picker and jev-auto's subagent routes select them.
const toModel = (id, model) => {
  const anthropic = model.provider.npm === "@ai-sdk/anthropic"
  return {
    id,
    modelID: model.id,
    providerID: "cliproxy",
    name: model.name,
    package: `aisdk:${model.provider.npm}`,
    capabilities: { tools: true, input: model.modalities.input, output: ["text"] },
    variants: (model.efforts ?? []).map(level => ({ id: level, settings: anthropic ? { effort: level } : { reasoningEffort: level } })),
    time: { released: 0 },
    cost: [],
    status: "active",
    enabled: true,
    limit: model.limit,
  }
}

const REFRESH_MS = 15 * 60 * 1000

// The provider transform must be synchronous, so the catalog is fetched outside it.
export default {
  id: "cliproxy-models",
  async setup(ctx) {
    const base = process.env.ANTHROPIC_BASE_URL
    const key = process.env.ANTHROPIC_AUTH_TOKEN
    if (!base && !key) return
    if (!base || !key) throw new Error("CLIProxyAPI URL or API key is missing")
    const url = `${base.replace(/\/$/, "")}/v1`
    const toModels = catalog => Object.entries(catalog).map(([id, model]) => toModel(id, model))
    const load = async () => toModels(await discover(url, key))
    // T3 waits 5 s for the model list and keeps an empty answer for 5 minutes.
    // The saved catalog is served right away; the fresh one replaces it after
    // setup instead of holding an empty list for up to 10 s.
    const cached = await readCache(url)
    let models = cached ? toModels(cached.models) : await load()
    await ctx.provider.transform(editor => editor.add({
      info: { id: "cliproxy", name: "CLIProxyAPI", activation: "enabled", package: "aisdk:@ai-sdk/openai-compatible",
        settings: { baseURL: url, apiKey: key } },
      models,
    }))
    // OpenCode 1 read the catalog at every config load; a T3-managed server
    // lives for days, so new and retired models are picked up on a timer.
    const refresh = async () => {
      try {
        const next = await load()
        if (JSON.stringify(next) === JSON.stringify(models)) return
        models = next
        await ctx.provider.reload()
      } catch (error) {
        console.error(`[cliproxy-models] refresh failed: ${error.message}`)
      }
    }
    if (cached) setTimeout(refresh)
    const timer = setInterval(refresh, REFRESH_MS)
    timer.unref?.()
    return () => clearInterval(timer)
  },
}
