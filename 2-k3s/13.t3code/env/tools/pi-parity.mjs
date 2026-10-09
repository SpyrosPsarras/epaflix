import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { readFileSync, realpathSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { createInterface } from 'node:readline'
import { checkEnforcement } from './pi-enforcement.mjs'

const [binary, home, modules] = process.argv.slice(2)
assert.ok(binary && home, 'usage: pi-parity.mjs <pi launcher> <home>')
const settings = JSON.parse(readFileSync(join(home, '.pi/agent/settings.json')))
await checkEnforcement(home, modules)
const child = spawn(resolve(binary), ['--mode', 'rpc', '--no-session'], {
  cwd: home, env: { ...process.env, HOME: home }, stdio: ['pipe', 'pipe', 'pipe'],
})
child.on('error', error => { throw error })
let stderr = ''
child.stderr.setEncoding('utf8').on('data', data => { stderr += data })
const lines = createInterface({ input: child.stdout })
const closed = new Promise(resolve => child.once('close', resolve))
const timer = setTimeout(() => child.kill('SIGKILL'), 60000)
try {
  child.stdin.write('{"id":"1","type":"get_commands"}\n')
  let response
  for await (const line of lines) {
    const message = JSON.parse(line)
    assert.notEqual(message.type, 'extension_error', JSON.stringify(message))
    if (message.id === '1') { response = message; break }
  }
  assert.ok(response?.success, JSON.stringify(response) + '\n' + stderr)
  const commands = response.data.commands
  assert.ok(Array.isArray(commands), 'get_commands response missing commands')
  for (const name of ['cliproxyapi', 'ponytail', 'permission-system', 'todos', 'redact-stats', 'cc-safety-net', 'skill:using-superpowers']) {
    assert.ok(commands.some(command => command.name === name), `missing ${name}: ${JSON.stringify(commands)}`)
  }
  const ponytail = commands.find(command => command.name === 'skill:ponytail')
  const pkg = settings.packages.find(path => path.endsWith('/@dietrichgebert/ponytail'))
  assert.ok(ponytail && pkg, 'package ponytail skill missing')
  assert.equal(realpathSync(ponytail.sourceInfo.path), realpathSync(join(pkg, 'skills/ponytail/SKILL.md')))
  assert.ok(!/failed to load|extension.*error|error.*extension/i.test(stderr), stderr)
  const jev = settings.packages.find(path => path.endsWith('/jev-guard'))
  assert.ok(jev, 'jev-guard package missing')
  assert.ok(readFileSync(join(jev, 'extensions/jev-guard.ts')).length, 'jev-guard extension missing')
  console.log('parity: Pi commands, package skills, permission policy and extension loading PASS')
} finally {
  clearTimeout(timer)
  lines.close()
  child.kill('SIGTERM')
  await closed
  if (stderr) process.stderr.write(stderr)
}
