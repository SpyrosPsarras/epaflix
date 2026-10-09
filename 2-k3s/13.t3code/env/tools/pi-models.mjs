import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

const modules = process.argv[3] ?? '/tools/node_modules'
const { createJiti } = await import(pathToFileURL(join(modules, 'jiti/lib/jiti.mjs')))
const jiti = createJiti(import.meta.url)
const provider = join(modules, 'pi-cliproxyapi-provider')
const { loadConfig } = await jiti.import(join(provider, 'src/config.ts'))
const { ProviderCatalog } = await jiti.import(join(provider, 'src/catalog.ts'))
const catalog = new ProviderCatalog({
  config: loadConfig(process.cwd()),
  bundledModelsDevPath: join(provider, 'data/models-dev-fallback.json'),
  getApiKey: async () => process.env.ANTHROPIC_AUTH_TOKEN,
})
// Pi --list-models reads the provider snapshot without refreshing it from the network.
const refreshed = await catalog.refresh('models', 'manual')
assert.ok(refreshed.models.updated, String(refreshed.models.error))

const result = spawnSync(process.argv[2], ['--list-models'], { encoding: 'utf8', timeout: 60000 })
assert.equal(result.status, 0, result.stderr)
const rows = result.stdout.replace(/\u001b\[[0-9;]*m/g, '').split('\n').map(line => line.trim().split(/\s+/))
  .filter(row => row.length > 1 && row[0] !== 'provider')
assert.ok(rows.length, result.stdout)
assert.ok(rows.every(row => row[0] === 'cliproxy'), result.stdout)
for (const model of ['codex/gpt-6-sol', 'claude/claude-opus-5-5']) {
  assert.ok(rows.some(row => row[1] === model), `missing ${model}: ${result.stdout}`)
}
console.log('smoke: --list-models lists only cliproxy and both mock models PASS')
