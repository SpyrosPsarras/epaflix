import { mkdir, readFile, rename, writeFile } from "node:fs/promises"
import { homedir } from "node:os"
import { join } from "node:path"

const RETIRE_AFTER_MS = 14 * 24 * 60 * 60 * 1000
const EFFORTS = ["low", "medium", "high"]
const codexName = id => id.startsWith("codex-") ? id : `codex-${id.replace(/^gpt-/, "")}`

// The cache keeps the version 5 catalog shape; OpenCode 2 model entries are derived from it.
const discover = async (url, key) => {
  const cacheDir = join(process.env.XDG_CACHE_HOME || join(homedir(), ".cache"), "opencode")
  const cachePath = join(cacheDir, "cliproxy-models.json")
  const cached = await readFile(cachePath, "utf8").then(JSON.parse).catch(() => null)
  const usable = cached?.version === 5 && cached.url === url && Object.keys(cached.models ?? {}).length > 0
  // Update old display names even when a credential is cooling down or the API is offline.
  if (usable) {
    for (const model of Object.values(cached.models)) {
      if (model.id?.startsWith("codex/")) model.name = codexName(model.id.slice(6))
    }
  }
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
      let name, npm
      if (subscription === "openrouter") {
        name = id
        npm = "@ai-sdk/openai-compatible"
      } else if (subscription === "claude") {
        name = `anthropic-${id.replace(/(\d)-(\d)(?=-|$)/g, "$1.$2")}`
        npm = "@ai-sdk/anthropic"
      } else if (subscription === "codex" && !id.startsWith("gpt-image-")) {
        name = codexName(id)
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
  await mkdir(cacheDir, { recursive: true })
  const temporary = `${cachePath}.${process.pid}.${crypto.randomUUID()}`
  await writeFile(temporary, JSON.stringify({ version: 5, url, updatedAt: new Date(now).toISOString(), models, seen }), { mode: 0o600 })
  await rename(temporary, cachePath)
  console.error(`[cliproxy-models] discovered ${Object.keys(models).length} models`)
  return models
}

// Codex and Claude models (except Haiku, which takes no effort) get the effort
// variants that T3's picker and jev-auto's subagent routes select.
const toModel = (id, model) => {
  const codex = model.id.startsWith("codex/")
  const effort = codex || (model.id.startsWith("claude/") && !id.includes("haiku"))
  return {
    id,
    modelID: model.id,
    providerID: "cliproxy",
    name: model.name,
    package: `aisdk:${model.provider.npm}`,
    capabilities: { tools: true, input: model.modalities.input, output: ["text"] },
    variants: effort ? EFFORTS.map(level => ({ id: level, settings: codex ? { reasoningEffort: level } : { effort: level } })) : [],
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
    const load = async () => Object.entries(await discover(url, key)).map(([id, model]) => toModel(id, model))
    let models = await load()
    await ctx.provider.transform(editor => editor.add({
      info: { id: "cliproxy", name: "CLIProxyAPI", activation: "enabled", package: "aisdk:@ai-sdk/openai-compatible",
        settings: { baseURL: url, apiKey: key } },
      models,
    }))
    // OpenCode 1 read the catalog at every config load; a T3-managed server
    // lives for days, so new and retired models are picked up on a timer.
    const timer = setInterval(async () => {
      try {
        const next = await load()
        if (JSON.stringify(next) === JSON.stringify(models)) return
        models = next
        await ctx.provider.reload()
      } catch (error) {
        console.error(`[cliproxy-models] refresh failed: ${error.message}`)
      }
    }, REFRESH_MS)
    timer.unref?.()
    return () => clearInterval(timer)
  },
}
