#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
if [[ ${1:-} != --inner ]]; then
  image=${T3_RUNTIME_IMAGE:-t3-runtime:homelab-dev}
  if [[ -z ${T3_RUNTIME_IMAGE:-} ]]; then
    docker build --build-arg T3_RUNTIME_VARIANT=homelab -f "$root/env/Dockerfile" -t "$image" "$root"
  fi
  name=t3-homelab-smoke-$$
  volume=$name-home
  trap 'docker rm -f "$name" >/dev/null 2>&1 || :; docker volume rm "$volume" >/dev/null 2>&1 || :' EXIT
  docker volume create "$volume" >/dev/null
  docker run --rm --network none --user 0:0 -v "$volume:/home/t3" "$image" chown 1000:1000 /home/t3
  for round in first restart; do
    timeout 1800 docker run --rm --name "$name" --user 1000:1000 \
      --cap-drop ALL --security-opt no-new-privileges \
      --add-host mcp-hub.mcp-hub-homelab.svc.cluster.local:127.0.0.1 \
      --tmpfs /scripts:uid=1000,gid=1000,exec --tmpfs /tmp:uid=1000,gid=1000,exec \
      --tmpfs /run/t3-inputs:uid=1000,gid=1000 --tmpfs /run/t3-login:uid=1000,gid=1000 \
      -v "$root:/src:ro" -v "$volume:/home/t3" -e HOME=/home/t3 \
      -e T3CODE_HOME=/home/t3/.t3 -e MCP_HUB_INSTANCE=homelab \
      "$image" bash /src/env/tools/homelab-smoke.sh --inner "$round"
  done
  exit
fi
round=${2:?first or restart}
fail() { echo "homelab smoke: $*" >&2; tail -80 /tmp/entrypoint.log /tmp/sshd.log 2>/dev/null || :; exit 1; }
pids=()
cleanup() { if (( ${#pids[@]} )); then kill -TERM "${pids[@]}" 2>/dev/null || :; fi; }
trap cleanup EXIT
python3 - <<'PY'
from pathlib import Path
import yaml
root = Path('/src')
config = yaml.safe_load((root / 'kustomization.yaml').read_text())
files = next(item['files'] for item in config['configMapGenerator'] if item['name'] == 't3code-scripts')
for spec in files:
    name, source = spec.split('=', 1) if '=' in spec else (Path(spec).name, spec)
    destination = Path('/scripts') / name
    destination.write_bytes((root / source).read_bytes())
    destination.chmod(0o555)
PY
mkdir -p /tmp/inventory-home
HOME=/tmp/inventory-home bash /src/env/tools/inventory.sh
mkdir -p /tmp/forbidden-bin
for tool in az bw kubelogin; do
  printf '#!/bin/sh\nexit 0\n' >"/tmp/forbidden-bin/$tool"
  chmod 0755 "/tmp/forbidden-bin/$tool"
  if HOME=/tmp/inventory-home PATH=/tmp/forbidden-bin:$PATH bash /src/env/tools/inventory.sh >/tmp/rejected-inventory.log 2>&1; then
    fail "inventory accepted $tool in the homelab image"
  fi
  rm "/tmp/forbidden-bin/$tool"
done
python3 /src/env/tools/isolated-runtime.test.py fixture /run/t3-inputs
mkdir -m 0700 /tmp/laptop
ssh-keygen -q -t ed25519 -N '' -C synthetic-laptop -f /tmp/laptop/id_ed25519
cp /tmp/laptop/id_ed25519.pub /run/t3-inputs/authorized_keys
python3 - <<'PY'
import hashlib, json
from pathlib import Path
path = Path('/run/t3-inputs/runtime.json')
config = json.loads(path.read_text())
config['sha256']['authorized_keys'] = hashlib.sha256(Path('/run/t3-inputs/authorized_keys').read_bytes()).hexdigest()
path.write_text(json.dumps(config))
PY
cp /run/t3-inputs/authorized_keys /run/t3-login/authorized_keys
export MCP_HUB_TOKEN=homelab.hhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhh
export ANTHROPIC_AUTH_TOKEN=synthetic-model-grant ANTHROPIC_BASE_URL=http://127.0.0.1:18317
unset GITHUB_TOKEN GH_TOKEN SSH_AUTH_SOCK
if [[ $round == first ]]; then
  if ANTHROPIC_AUTH_TOKEN='' MCP_HUB_TOKEN='' bash /scripts/entrypoint.sh >/tmp/rejected-startup.log 2>&1; then
    fail 'startup accepted missing grants'
  fi
  grep -q 'independent environment hub grant is required' /tmp/rejected-startup.log || fail 'startup did not fail at the grant boundary'
  [[ ! -e $HOME/.config/t3-runtime/environment ]] || fail 'rejected startup wrote runtime state'
  python3 /scripts/isolated-config.py prepare /run/t3-inputs "$HOME" homelab
  mkdir -p "$HOME/projects/homelab"
  git -C "$HOME/projects/homelab" init -q --initial-branch=main
  git -C "$HOME/projects/homelab" -c user.name=fixture -c user.email=fixture@localhost commit -q --allow-empty -m fixture
  git -C "$HOME/projects/homelab" remote add origin https://github.com/homelab/fixture.git
fi
node -e 'require("node:http").createServer((q,s)=>{s.writeHead(200,{"content-type":"application/json"});
s.end(JSON.stringify({data:["codex/gpt-6-sol","claude/claude-opus-5-5"].map(id=>({id,context_window:200000}))}));}).listen(18317,"127.0.0.1")' &
pids+=("$!")
bash /scripts/entrypoint.sh >/tmp/entrypoint.log 2>&1 &
sup=$!; pids+=("$sup")
for ((i=0; i<600; i++)); do
  curl -fsS --max-time 2 http://127.0.0.1:3773/health >/dev/null 2>&1 && break
  kill -0 "$sup" 2>/dev/null || fail 'entrypoint exited'
  sleep 1
done
curl -fsS --max-time 5 http://127.0.0.1:3773/health >/dev/null || fail 'health did not pass'
grep -q '^pi-setup: all registered packages listed$' /tmp/entrypoint.log || fail 'package registration check'
codex mcp list --json >/tmp/codex-mcp.json 2>/tmp/codex-mcp.log || fail 'Codex rejected the generated TOML'
python3 - <<'PY'
import json
from pathlib import Path
servers = json.loads(Path('/tmp/codex-mcp.json').read_text())
assert {server['name'] for server in servers} == {'searxng'}
print('homelab smoke: pinned Codex parses managed MCP configuration PASS')
PY
python3 /src/env/tools/isolated-runtime.test.py codex-projects
timeout 90 node /src/env/tools/pi-parity.mjs /scripts/pi.sh "$HOME" >/tmp/pi-parity.log 2>&1 || { tail -80 /tmp/pi-parity.log; fail 'Pi parity and extension loading'; }
grep '^parity:' /tmp/pi-parity.log
node /src/env/tools/pi-models.mjs /scripts/pi.sh
python3 - <<'PY'
import json, os, subprocess
from pathlib import Path
home = Path.home()
cli = home / '.local/share/pi/cc-safety-net/2.6.4/dist/bin/cc-safety-net.js'
cases = [('git status', 'allowed'), ('git push --force origin main', 'blocked'),
         ('cat /fixture/restricted', 'blocked'), ('cat /run/t3-inputs/kubeconfig', 'blocked'),
         (f'cat {home}/.config/t3-sshd/ssh_host_ed25519_key', 'blocked')]
for command, expected in cases:
    result = subprocess.run(['node', str(cli), 'explain', '--json', command], capture_output=True, text=True,
                            check=True, env={**os.environ, 'CC_SAFETY_NET_HOME': str(home / '.cc-safety-net')})
    assert json.loads(result.stdout)['result'] == expected, command
print('homelab smoke: retained organizational deny and runtime secret protections PASS')
PY
/usr/bin/env -i HOME="$HOME" PATH=/tools/node_modules/.bin:/usr/local/bin:/usr/bin:/bin \
  bash /scripts/sshd.sh >/tmp/sshd.log 2>&1 &
pids+=("$!")
for ((i=0; i<30; i++)); do
  if [[ -s $HOME/.config/t3-sshd/ssh_host_ed25519_key.pub ]] && (exec 3<>/dev/tcp/127.0.0.1/2222) 2>/dev/null; then break; fi
  sleep 1
done
python3 /src/env/tools/prepare-laptop.py --directory /tmp/laptop --host 127.0.0.1 --port 2222 \
  --host-key-file "$HOME/.config/t3-sshd/ssh_host_ed25519_key.pub"
ssh-agent -a /tmp/synthetic-agent.sock -D >/tmp/agent.log 2>&1 &
pids+=("$!")
for ((i=0; i<10; i++)); do [[ -S /tmp/synthetic-agent.sock ]] && break; sleep 1; done
SSH_AUTH_SOCK=/tmp/synthetic-agent.sock ssh-add /tmp/laptop/id_ed25519 >/tmp/agent-add.log 2>&1
SSH_AUTH_SOCK=/tmp/synthetic-agent.sock ssh-add -l >/dev/null
ssh -F /tmp/laptop/ssh_config -o IdentityAgent=/tmp/synthetic-agent.sock -A t3-homelab \
  'test -z "${SSH_AUTH_SOCK:-}"' || fail 'SSH login or agent forwarding restriction'
ssh -F /tmp/laptop/ssh_config t3-homelab 'sh -l -s -- 0123456789abcdef' > /tmp/discovery.json <<'SH'
cat "$HOME/.t3/userdata/server-runtime.json"
SH
node -e 'const j=require("/tmp/discovery.json");if(j.port!==3773||j.origin!=="http://127.0.0.1:3773")process.exit(1)' || fail 'SSH launcher discovery'
bash /src/env/tools/connect-laptop.sh /tmp/laptop/ssh_config 13773 >/tmp/tunnel.log 2>&1 &
pids+=("$!")
for ((i=0; i<30; i++)); do
  curl -fsS --max-time 2 http://127.0.0.1:13773/health >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS --max-time 5 http://127.0.0.1:13773/.well-known/t3/environment > /tmp/environment.json || fail 'SSH tunnel descriptor'
node - "$round" <<'JS'
const fs=require('node:fs'),assert=require('node:assert/strict'),{DatabaseSync}=require('node:sqlite');
const home=process.env.HOME, descriptor=JSON.parse(fs.readFileSync('/tmp/environment.json'));
assert.ok(descriptor.environmentId);
const marker=home+'/.config/t3-runtime/smoke-environment.json';
const settings=JSON.parse(fs.readFileSync(home+'/.t3/userdata/settings.json'));
assert.deepEqual(Object.keys(settings.providerInstances).sort(),['claudeAgent','codex','pi']);
assert.ok(fs.existsSync(home+'/.t3/userdata/statev2.sqlite'));
const db=new DatabaseSync(home+'/.t3/userdata/statev2.sqlite');
assert.ok(db.prepare('SELECT workspace_root FROM projection_projects WHERE deleted_at IS NULL').all()
  .some(row=>row.workspace_root===home+'/projects/homelab'));
if(process.argv[2]==='first') {
  fs.writeFileSync(marker, JSON.stringify({environmentId:descriptor.environmentId,
    hostKey:fs.readFileSync(home+'/.config/t3-sshd/ssh_host_ed25519_key.pub','utf8')}));
  db.exec("CREATE TABLE smoke_history (message TEXT); INSERT INTO smoke_history VALUES ('homelab-only')");
} else {
  const previous=JSON.parse(fs.readFileSync(marker));
  assert.equal(descriptor.environmentId,previous.environmentId);
  assert.equal(fs.readFileSync(home+'/.config/t3-sshd/ssh_host_ed25519_key.pub','utf8'),previous.hostKey);
  assert.equal(db.prepare('SELECT message FROM smoke_history').get().message,'homelab-only');
  assert.ok(fs.existsSync(home+'/projects/homelab/.git'));
}
console.log('homelab smoke: '+process.argv[2]+' health, SSH discovery, policy enforcement and independent state PASS');
JS
idle=$(python3 /src/one/files/t3-idle.py)
[[ $idle =~ ^[0-9]+$ ]] || fail 'rollout idle check cannot read this backend state'
ssh -F /tmp/laptop/ssh_config -N -L 127.0.0.1:13774:127.0.0.1:18317 t3-homelab >/tmp/denied-tunnel.log 2>&1 &
pids+=("$!")
sleep 1
! curl -fsS --max-time 3 http://127.0.0.1:13774 >/dev/null 2>&1 || fail 'sshd allowed an unrelated port'
grep -q 'administratively prohibited' /tmp/denied-tunnel.log || fail 'sshd forwarding denial was not exercised'
kill -TERM "$sup"
set +e
wait "$sup"; rc=$?
set -e
[[ $rc == 130 || $rc == 143 ]] || fail "unexpected shutdown $rc"
python3 - "$round" <<'PY'
import importlib, json, sys, tomllib
from pathlib import Path
sys.path.insert(0, '/scripts')
runtime = importlib.import_module('isolated-config')
home = Path.home()
settings_path = home / '.t3/userdata/settings.json'
codex_path = home / '.codex/config.toml'
settings = json.loads(settings_path.read_text())
codex = tomllib.loads(codex_path.read_text())
project = str(home / 'projects/homelab')
if sys.argv[1] == 'first':
    settings['providerInstances']['pi']['displayName'] = 'Homelab preference fixture'
    settings['providerInstances']['codex']['enabled'] = False
    settings['providerInstances']['codex']['config']['customModels'] = ['codex/fixture']
    settings['providerInstances']['work'] = {'driver': 'codex', 'enabled': True}
    settings_path.write_text(json.dumps(settings))
    codex['model_reasoning_effort'] = 'high'
    codex['projects'] = {project: {'trust_level': 'trusted'}}
    codex['mcp_servers']['work'] = {'url': 'https://mcp.work.example.test'}
    codex_path.write_text(''.join(json.dumps(key) + ' = ' + runtime.toml_value(value) + '\n' for key, value in codex.items()))
else:
    assert settings['providerInstances']['pi']['displayName'] == 'Homelab preference fixture'
    assert settings['providerInstances']['codex']['enabled'] is False
    assert settings['providerInstances']['codex']['config']['customModels'] == ['codex/fixture']
    assert 'work' not in settings['providerInstances']
    assert codex['model_reasoning_effort'] == 'high'
    assert codex['projects'][project]['trust_level'] == 'trusted'
    assert set(codex['mcp_servers']) == {'searxng'}
    print('homelab smoke: safe provider preferences and Codex project trust survive restart PASS')
PY
echo "homelab smoke: $round clean shutdown PASS"
