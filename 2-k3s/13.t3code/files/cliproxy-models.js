import { mkdir, readFile, rename, writeFile } from "node:fs/promises"
import { homedir } from "node:os"
import { join } from "node:path"

// OpenCode calls this hook before building the provider inventory used by T3.
export default async () => ({
  async config(config) {
    const provider = config.provider?.cliproxy
    if (!provider) return
    const url = provider.options?.baseURL
    const key = provider.options?.apiKey
    if (!url || !key) throw new Error("CLIProxyAPI URL or API key is missing")
    const cacheDir = join(process.env.XDG_CACHE_HOME || join(homedir(), ".cache"), "opencode")
    const cachePath = join(cacheDir, "cliproxy-models.json")
    let models
    try {
      const response = await fetch(`${url.replace(/\/$/, "")}/models`, {
        headers: { Authorization: `Bearer ${key}` },
        signal: AbortSignal.timeout(10000),
      })
      if (!response.ok) throw new Error(`catalog HTTP ${response.status}`)
      const catalog = await response.json()
      if (!Array.isArray(catalog.data) || !catalog.data.length) throw new Error("empty or invalid catalog")
      models = {}
      for (const model of catalog.data) {
        if (typeof model.id !== "string" || !model.id.trim() || typeof model.owned_by !== "string") {
          throw new Error("invalid catalog model")
        }
        // Only prefixed catalog entries bind a request to one subscription.
        const route = /^(codex|claude|openrouter)\/(.+)$/.exec(model.id)
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
          name = id.startsWith("codex-") ? id : `codex-${id.replace(/^gpt-(\d+(?:\.\d+)?)-([a-z]+)$/, (_, version, suffix) =>
            ["mini", "nano", "codex"].includes(suffix) ? `${version}-${suffix}` : suffix).replace(/^gpt-/, "")}`
          // Responses item IDs change between events in CLIProxyAPI's stream.
          // Chat Completions uses indexed deltas and avoids that broken parser path.
          npm = "@ai-sdk/openai-compatible"
        } else {
          continue
        }
        models[id] = {
          // Preserve saved OpenCode selections while sending the scoped API ID.
          id: model.id,
          name,
          provider: { npm },
          // ponytail: conservative budgets until /v1/models supplies token limits.
          limit: { context: model.context_length || 32768, output: model.max_output_tokens || 8192 },
          ...(subscription === "codex" ? { reasoning: true } : {}),
        }
      }
      if (!Object.keys(models).length) throw new Error("catalog has no supported models")
    } catch (error) {
      const cached = JSON.parse(await readFile(cachePath, "utf8").catch(() => "null"))
      if (cached?.version !== 2 || cached.url !== url || !cached.models || !Object.keys(cached.models).length) throw error
      provider.models = cached.models
      console.error(`[cliproxy-models] ${error.message}; using catalog saved at ${cached.updatedAt}`)
      return
    }
    provider.models = models
    await mkdir(cacheDir, { recursive: true })
    const temporary = `${cachePath}.${process.pid}.${crypto.randomUUID()}`
    await writeFile(temporary, JSON.stringify({ version: 2, url, updatedAt: new Date().toISOString(), models }), { mode: 0o600 })
    await rename(temporary, cachePath)
    console.error(`[cliproxy-models] discovered ${Object.keys(models).length} models`)
  },
})
