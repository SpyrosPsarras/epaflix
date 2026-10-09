import assert from 'node:assert/strict'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

export async function checkEnforcement(home, modules = '/tools/node_modules') {
  const { createJiti } = await import(pathToFileURL(join(modules, 'jiti/lib/jiti.mjs')))
  const jiti = createJiti(import.meta.url)
  const source = join(modules, '@gotgenes/pi-permission-system/src')
  const load = path => jiti.import(join(source, path))
  const { PermissionManager } = await load('policy/permission-manager.ts')
  const { PermissionResolver } = await load('policy/permission-resolver.ts')
  const { PathNormalizer } = await load('path/path-normalizer.ts')
  const { posixPathFlavor } = await load('path/path-flavor.ts')
  const { ToolCallGatePipeline } = await load('handlers/gates/tool-call-gate-pipeline.ts')
  const { preResolvedCheckOf, isGateBypass } = await load('handlers/gates/descriptor.ts')
  const manager = new PermissionManager({ agentDir: join(home, '.pi/agent'), flavor: posixPathFlavor })
  manager.configureForCwd(home)
  assert.deepEqual(manager.getPolicyIssues(), [])
  const resolver = new PermissionResolver(manager, { getRuleset: () => [] })
  const normalizer = new PathNormalizer(posixPathFlavor, home)
  const pipeline = new ToolCallGatePipeline(resolver, {
    getActiveSkillEntries: () => [],
    getInfrastructureReadScope: () => ({ dirs: [], excludedDirs: [] }),
    getToolPreviewLimits: () => ({}),
    getPathNormalizer: () => normalizer,
    getShellToolAliases: () => undefined,
  })
  const cases = [
    ['bash', { command: 'printenv HOME' }, 'deny'],
    ['bash', { command: 'cat /proc/1/environ' }, 'deny'],
    ...['ps e', 'ps auxe', 'ps eww'].map(command => ['bash', { command }, 'deny']),
    ...['git status', 'kubectl -n x get pods', 'ps aux', 'ps -ef', 'ps -o pid,cmd']
      .map(command => ['bash', { command }, 'allow']),
    ['mcp__gmail__gmail_send', {}, 'ask'],
  ]
  const rank = { allow: 0, ask: 1, deny: 2 }
  for (const [toolName, input, expected] of cases) {
    let actual = 'allow'
    await pipeline.evaluate({ toolName, agentName: null, input, toolCallId: 'smoke', cwd: home }, {
      run: async gate => {
        if (gate && !isGateBypass(gate)) {
          const result = preResolvedCheckOf(gate) ?? resolver.resolve({ kind: 'tool', surface: gate.surface, input: gate.input })
          if (rank[result.state] > rank[actual]) actual = result.state
        }
        return { action: 'allow' }
      },
    })
    assert.equal(actual, expected, `${toolName} ${JSON.stringify(input)}`)
    console.log(`enforcement: ${toolName} ${JSON.stringify(input)} -> ${actual}`)
  }
  console.log(`parity: shipped permission pipeline ${cases.length}/${cases.length} PASS`)
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  await checkEnforcement(process.argv[2], process.argv[3])
}
