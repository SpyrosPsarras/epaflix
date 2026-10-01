import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const home = await mkdtemp(join(tmpdir(), 't3-v2-container-probe-'));
const env = { PATH: process.env.PATH, HOME: home, T3CODE_HOME: join(home, '.t3') };
const executable = '/runtime/t3';
const origin = 'http://127.0.0.1:13773';
const server = spawn(executable, ['start', '--no-browser', '--host', '127.0.0.1', '--port', '13773', '--base-dir', env.T3CODE_HOME, home], { env, stdio: ['ignore', 'pipe', 'pipe'] });
let log = '';
server.stdout.on('data', chunk => { log = (log + chunk).slice(-12000); });
server.stderr.on('data', chunk => { log = (log + chunk).slice(-12000); });
try {
  let descriptor;
  for (let attempt = 0; attempt < 120; attempt++) {
    if (server.exitCode !== null) throw new Error(`Server exited: ${log}`);
    try {
      const response = await fetch(`${origin}/.well-known/t3/environment`, { signal: AbortSignal.timeout(2000) });
      if (response.ok) { descriptor = await response.json(); break; }
    } catch {}
    await delay(1000);
  }
  assert.equal(descriptor?.orchestrationProtocolVersion, 2);
  assert.ok(descriptor.environmentId);
  for (const path of ['/health', '/']) {
    const response = await fetch(origin + path, { signal: AbortSignal.timeout(5000) });
    assert.equal(response.status, 200);
  }
  const token = execFileSync(executable, ['auth', 'session', 'issue', '--base-dir', env.T3CODE_HOME, '--ttl', '5m', '--token-only'], { env, encoding: 'utf8' }).trim();
  const count = async () => {
    const response = await fetch(`${origin}/api/auth/clients`, { headers: { Authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(5000) });
    assert.equal(response.status, 200);
    const body = await response.json();
    const clients = Array.isArray(body) ? body : body.clients;
    assert.ok(Array.isArray(clients));
    return clients.filter(client => client.connected).length;
  };
  assert.equal(await count(), 0);
  const socket = new WebSocket(`${origin.replace('http:', 'ws:')}/ws?orchestrationProtocol=2`, { headers: { Authorization: `Bearer ${token}` } });
  try {
    await Promise.race([
      new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once: true }); socket.addEventListener('error', reject, { once: true }); }),
      delay(10000).then(() => { throw new Error('WebSocket open timed out'); }),
    ]);
    await delay(500);
    assert.equal(await count(), 1);
  } finally {
    socket.close();
  }
  for (let attempt = 0; attempt < 20 && await count() !== 0; attempt++) await delay(100);
  assert.equal(await count(), 0);
  console.log('PASS: runtime protocol 2, fresh identity, web/health and live client 0/1/0');
} catch (error) {
  console.error(log);
  throw error;
} finally {
  if (server.exitCode === null) {
    const exited = new Promise(resolve => server.once('exit', resolve));
    server.kill('SIGTERM');
    const timer = setTimeout(() => server.kill('SIGKILL'), 30000);
    await exited;
    clearTimeout(timer);
  }
  await rm(home, { recursive: true, force: true });
}
