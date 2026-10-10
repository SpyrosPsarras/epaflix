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
    ...['mkfs.ext4 /dev/sda', 'dd if=x of=/dev/sda', 'kubectl delete pod x',
      'kubectl drain node', 'kubectl cordon node', 'kubectl -n a delete pod x',
      'kubectl --context prod drain node', 'kubectl -n a cordon node',
      'helm uninstall release', 'helm -n a uninstall release', 'reboot', 'shutdown now',
      'poweroff', 'qm stop 100', 'qm shutdown 100', 'qm --skiplock stop 100',
      'qm --skiplock shutdown 100', 'env', 'env HOME', 'printenv HOME', 'set', 'set -o',
      'set +o', 'export', 'export -p', 'declare -p', 'typeset -p', 'cat /proc/1/environ',
      'ps x /proc/1/environ']
      .map(command => ['bash', { command }, 'deny']),
    ...['git status', 'mktemp', 'dd if=/dev/zero of=/tmp/x', 'kubectl get pods',
      'kubectl -n x get pods', 'helm list', 'qm status 100', 'set -euo pipefail',
      'set -- arg', 'export NAME=value', 'envsubst', 'printf hello', 'type ps',
      'cat /proc/1/status', 'ls /run', 'uptime', 'systemctl status']
      .map(command => ['bash', { command }, 'allow']),
    ['mcp__gmail__gmail_send', {}, 'allow'],
  ]
  for (const prefix of ['ps', '/bin/ps', '/usr/bin/ps']) {
    for (let length = 1; length <= 8; length++) {
      cases.push(['bash', { command: `${prefix} ${'x'.repeat(length)} -o pid,etime` }, 'allow'])
      for (let position = 0; position < length; position++) {
        const word = 'x'.repeat(position) + 'e' + 'x'.repeat(length - position - 1)
        cases.push(['bash', { command: `${prefix} ${word}` }, 'deny'])
      }
    }
    cases.push(
      ...['e', 'auxe', 'eww', 'axe', 'auxwwe', 'auxwwwwwe', 'e -o pid',
        'ewwwwwwww', 'eauxwwwww']
        .map(args => ['bash', { command: `${prefix} ${args}` }, 'deny']),
      ['bash', { command: `ls && ${prefix} auxe` }, 'deny'],
      ...['', ' aux', ' -ef', ' -eo pid,cmd', ' -o pid,cmd', ' auxf', ' axo pid,comm',
        ' aux --sort=-%mem', ' x -o pid,etime', ' -A e', ' -ef e']
        .map(args => ['bash', { command: prefix + args }, 'allow']),
    )
  }
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
