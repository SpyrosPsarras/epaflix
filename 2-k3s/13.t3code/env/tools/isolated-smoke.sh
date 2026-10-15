#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
variant=${T3_RUNTIME_VARIANT:-homelab}
[[ $variant == homelab || $variant == work ]]
if [[ ${1:-} != --inner ]]; then
  image=${T3_RUNTIME_IMAGE:-t3-runtime:$variant-dev}
  if [[ -z ${T3_RUNTIME_IMAGE:-} ]]; then
    docker build --build-arg T3_RUNTIME_VARIANT="$variant" -f "$root/env/Dockerfile" -t "$image" "$root"
  fi
  name=t3-$variant-smoke-$$
  volume=$name-home
  projection=$name-projection
  trap 'docker rm -f "$name" >/dev/null 2>&1 || :; docker volume rm "$volume" "$projection" >/dev/null 2>&1 || :' EXIT
  docker volume create "$volume" >/dev/null
  docker volume create "$projection" >/dev/null
  docker run --rm --network none --user 0:0 -v "$projection:/projection" "$image" python3 -c '
import json, os
from pathlib import Path
p=Path("/projection/services.json")
p.write_text(json.dumps({"environment": os.environ["T3_RUNTIME_VARIANT"], "services": ["searxng"]}))
os.chown(p, 0, 1000)
p.chmod(0o440)
'
  docker run --rm --network none --user 0:0 -v "$volume:/home/t3" "$image" chown 1000:1000 /home/t3
  for round in first restart; do
    timeout 1800 docker run --rm --name "$name" --user 1000:1000 \
      --cap-drop ALL --security-opt no-new-privileges \
      --add-host "mcp-hub.mcp-hub-$variant.svc.cluster.local:127.0.0.1" \
      --tmpfs /scripts:uid=1000,gid=1000,exec --tmpfs /tmp:uid=1000,gid=1000,exec \
      --tmpfs /run/t3-inputs:uid=1000,gid=1000 --tmpfs /run/t3-login:uid=1000,gid=1000 \
      --tmpfs /run/t3-vault:uid=1000,gid=1000 \
      -v "$root/../13.t3code-homelab:/homelab-src:ro" -v "$root:/src:ro" -v "$volume:/home/t3" -v "$projection:/projection:ro" -e HOME=/home/t3 \
      -e T3CODE_HOME=/home/t3/.t3 -e MCP_HUB_INSTANCE="$variant" \
      "$image" bash /src/env/tools/isolated-smoke.sh --inner "$round"
  done
  exit
fi
round=${2:?first or restart}
fail() { echo "isolated smoke: $*" >&2; tail -80 /tmp/entrypoint.log /tmp/sshd.log 2>/dev/null || :; exit 1; }
pids=()
cleanup() { if (( ${#pids[@]} )); then kill -TERM "${pids[@]}" 2>/dev/null || :; fi; }
trap cleanup EXIT
python3 - <<'PY'
from pathlib import Path
import yaml, os
root = Path('/src')
config = yaml.safe_load((root / 'kustomization.yaml').read_text())
files = next(item['files'] for item in config['configMapGenerator'] if item['name'] == 't3code-scripts')
for spec in files:
    name, source = spec.split('=', 1) if '=' in spec else (Path(spec).name, spec)
    if os.environ['MCP_HUB_INSTANCE'] == 'homelab' and name == 'homelab-ssh.md':
        continue
    destination = Path('/scripts') / name
    destination.write_bytes((root / source).read_bytes())
    destination.chmod(0o555)
if os.environ['MCP_HUB_INSTANCE'] == 'homelab':
    for source in Path('/homelab-src/files').iterdir():
        destination = Path('/scripts') / source.name
        destination.write_bytes(source.read_bytes())
        destination.chmod(0o555)
if os.environ['MCP_HUB_INSTANCE'] == 'work':
    destination = Path('/scripts/work-aks-auth.py')
    destination.write_bytes((root / 'env/files/work-aks-auth.py').read_bytes())
    destination.chmod(0o555)
PY
mkdir -p /tmp/inventory-home
HOME=/tmp/inventory-home bash /src/env/tools/inventory.sh
mkdir -p /tmp/forbidden-bin
if [[ $variant == homelab ]]; then forbidden_tools=(az kubelogin); else forbidden_tools=(kubelogin); fi
for tool in "${forbidden_tools[@]}"; do
  printf '#!/bin/sh\nexit 0\n' >"/tmp/forbidden-bin/$tool"
  chmod 0755 "/tmp/forbidden-bin/$tool"
  if HOME=/tmp/inventory-home PATH=/tmp/forbidden-bin:$PATH bash /src/env/tools/inventory.sh >/tmp/rejected-inventory.log 2>&1; then
    fail "inventory accepted $tool in the isolated image"
  fi
  rm "/tmp/forbidden-bin/$tool"
done
if [[ $variant == homelab ]]; then
  python3 /src/env/tools/isolated-runtime.test.py fixture /run/t3-inputs
else
  python3 /src/env/tools/work-runtime.test.py fixture /run/t3-inputs
  python3 /src/env/tools/work-aks-auth.test.py -v
fi
rm /run/t3-inputs/services.json
ln -s /projection/services.json /run/t3-inputs/services.json
mkdir -m 0700 /tmp/laptop
ssh-keygen -q -t ed25519 -N '' -C synthetic-laptop -f /tmp/laptop/id_ed25519
printf '%s\n' "$variant" >/tmp/laptop/environment
cp /tmp/laptop/id_ed25519.pub /run/t3-inputs/authorized_keys
python3 - <<'PY'
import hashlib, json
from pathlib import Path
path = Path('/run/t3-inputs/runtime.json')
config = json.loads(path.read_text())
config['sha256']['authorized_keys'] = hashlib.sha256(Path('/run/t3-inputs/authorized_keys').read_bytes()).hexdigest()
config['grantSha256']['model-token'] = hashlib.sha256(b'tok-fixture-model').hexdigest()
path.write_text(json.dumps(config))
PY
cp /run/t3-inputs/authorized_keys /run/t3-login/authorized_keys
if [[ $variant == homelab ]]; then
  ssh-agent -D -a /run/t3-vault/ssh-agent.sock >/tmp/outbound-agent.log 2>&1 &
  pids+=("$!")
  for ((i=0; i<10; i++)); do [[ -S /run/t3-vault/ssh-agent.sock ]] && break; sleep 1; done
  if [[ $round == restart ]]; then
    SSH_AUTH_SOCK=/run/t3-vault/ssh-agent.sock ssh-add /tmp/laptop/id_ed25519 >/tmp/outbound-add.log 2>&1
  fi
  touch /run/t3-vault/scope.json
fi
MCP_HUB_TOKEN=$variant.$(printf 'h%.0s' {1..43})
export MCP_HUB_TOKEN
export ANTHROPIC_AUTH_TOKEN=tok-fixture-model ANTHROPIC_BASE_URL=http://127.0.0.1:18317
unset GITHUB_TOKEN GH_TOKEN SSH_AUTH_SOCK
if [[ $round == first ]]; then
  if ANTHROPIC_AUTH_TOKEN='' MCP_HUB_TOKEN='' bash /scripts/entrypoint.sh >/tmp/rejected-startup.log 2>&1; then
    fail 'startup accepted missing grants'
  fi
  grep -q 'independent environment hub grant is required' /tmp/rejected-startup.log || fail 'startup did not fail at the grant boundary'
  [[ ! -e $HOME/.config/t3-runtime/environment ]] || fail 'rejected startup wrote runtime state'
  python3 /scripts/isolated-config.py prepare /run/t3-inputs "$HOME" "$variant"
  mkdir -p "$HOME/projects/$variant"
  git -C "$HOME/projects/$variant" init -q --initial-branch=main
  git -C "$HOME/projects/$variant" -c user.name=fixture -c user.email=fixture@localhost commit -q --allow-empty -m fixture
  git -C "$HOME/projects/$variant" remote add origin "https://github.com/$variant/fixture.git"
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
python3 - "$sup" <<'PY'
from pathlib import Path
import sys
names = [entry.partition(b'=')[0] for entry in Path('/proc', sys.argv[1], 'environ').read_bytes().split(b'\0')]
assert b'SSH_AUTH_SOCK' not in names
print('isolated smoke: T3 has no default SSH agent environment PASS')
PY
if [[ $variant == homelab ]]; then
  python3 - <<'PY'
import os
from pathlib import Path
source=Path('/run/t3-inputs/services.json')
target=Path.home()/'.config/t3-runtime/services.json'
assert source.stat().st_uid == 0 and source.stat().st_mode & 0o777 == 0o440
assert target.stat().st_uid == os.getuid() and target.stat().st_mode & 0o777 == 0o600
assert target.read_bytes() == source.read_bytes()
marker=target.parent/'environment'
assert marker.stat().st_uid == os.getuid() and marker.stat().st_mode & 0o777 == 0o600
print('isolated smoke: actual root-owned projected services become a reviewed owned private snapshot PASS')
PY
fi
codex mcp list --json >/tmp/codex-mcp.json 2>/tmp/codex-mcp.log || fail 'Codex rejected the generated TOML'
python3 - <<'PY'
import json
from pathlib import Path
servers = json.loads(Path('/tmp/codex-mcp.json').read_text())
assert {server['name'] for server in servers} == {'searxng'}
print('isolated smoke: pinned Codex parses managed MCP configuration PASS')
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
print('isolated smoke: retained organizational deny and runtime secret protections PASS')
PY
/usr/bin/env -i HOME="$HOME" T3_RUNTIME_VARIANT="$variant" PATH=/tools/node_modules/.bin:/usr/local/bin:/usr/bin:/bin \
  bash /scripts/sshd.sh >/tmp/sshd.log 2>&1 &
pids+=("$!")
for ((i=0; i<30; i++)); do
  if [[ -s $HOME/.config/t3-sshd/ssh_host_ed25519_key.pub ]] && (exec 3<>/dev/tcp/127.0.0.1/2222) 2>/dev/null; then break; fi
  sleep 1
done
python3 /src/env/tools/prepare-laptop.py --environment "$variant" --directory /tmp/laptop --host 127.0.0.1 --port 2222 \
  --host-key-file "$HOME/.config/t3-sshd/ssh_host_ed25519_key.pub"
ssh-agent -a /tmp/synthetic-agent.sock -D >/tmp/agent.log 2>&1 &
pids+=("$!")
for ((i=0; i<10; i++)); do [[ -S /tmp/synthetic-agent.sock ]] && break; sleep 1; done
SSH_AUTH_SOCK=/tmp/synthetic-agent.sock ssh-add /tmp/laptop/id_ed25519 >/tmp/agent-add.log 2>&1
SSH_AUTH_SOCK=/tmp/synthetic-agent.sock ssh-add -l >/dev/null
ssh -F /tmp/laptop/ssh_config -o IdentityAgent=/tmp/synthetic-agent.sock -A "t3-$variant" \
  'test -z "${SSH_AUTH_SOCK:-}"' || fail 'SSH login or agent forwarding restriction'
ssh -F /tmp/laptop/ssh_config "t3-$variant" 'sh -l -s -- 0123456789abcdef' > /tmp/discovery.json <<'SH'
cat "$HOME/.t3/userdata/server-runtime.json"
SH
node -e 'const j=require("/tmp/discovery.json");if(j.port!==3773||j.origin!=="http://127.0.0.1:3773")process.exit(1)' || fail 'SSH launcher discovery'
tunnel_port=13773
[[ $variant != work ]] || tunnel_port=23773
bash /src/env/tools/connect-laptop.sh /tmp/laptop/ssh_config "$tunnel_port" "$variant" >/tmp/tunnel.log 2>&1 &
pids+=("$!")
for ((i=0; i<30; i++)); do
  curl -fsS --max-time 2 "http://127.0.0.1:$tunnel_port/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS --max-time 5 "http://127.0.0.1:$tunnel_port/.well-known/t3/environment" > /tmp/environment.json || fail 'SSH tunnel descriptor'
node - "$round" <<'JS'
const fs=require('node:fs'),assert=require('node:assert/strict'),{DatabaseSync}=require('node:sqlite');
const home=process.env.HOME, descriptor=JSON.parse(fs.readFileSync('/tmp/environment.json'));
assert.ok(descriptor.environmentId);
const marker=home+'/.config/t3-runtime/smoke-environment.json';
const settings=JSON.parse(fs.readFileSync(home+'/.t3/userdata/settings.json'));
assert.deepEqual(Object.keys(settings.providerInstances).sort(),['claudeAgent','codex','pi']);
assert.ok(fs.existsSync(home+'/.t3/userdata/statev2.sqlite'));
const db=new DatabaseSync(home+'/.t3/userdata/statev2.sqlite');
if(process.env.MCP_HUB_INSTANCE==='work') {
  const cache=home+'/.azure/session-fixture';
  if(process.argv[2]==='first') {
    assert.ok(!fs.existsSync(home+'/.azure'));
    fs.mkdirSync(home+'/.azure', {mode:0o700});
    fs.writeFileSync(cache, 'synthetic-work-only-session', {mode:0o600});
  } else assert.equal(fs.readFileSync(cache,'utf8'), 'synthetic-work-only-session');
}
assert.ok(db.prepare('SELECT workspace_root FROM projection_projects WHERE deleted_at IS NULL').all()
  .some(row=>row.workspace_root===home+'/projects/'+process.env.MCP_HUB_INSTANCE));
if(process.argv[2]==='first') {
  fs.writeFileSync(marker, JSON.stringify({environmentId:descriptor.environmentId,
    hostKey:fs.readFileSync(home+'/.config/t3-sshd/ssh_host_ed25519_key.pub','utf8')}));
  db.exec("CREATE TABLE smoke_history (message TEXT); INSERT INTO smoke_history VALUES ('isolated-only')");
} else {
  const previous=JSON.parse(fs.readFileSync(marker));
  assert.equal(descriptor.environmentId,previous.environmentId);
  assert.equal(fs.readFileSync(home+'/.config/t3-sshd/ssh_host_ed25519_key.pub','utf8'),previous.hostKey);
  assert.equal(db.prepare('SELECT message FROM smoke_history').get().message,'isolated-only');
  assert.ok(fs.existsSync(home+'/projects/'+process.env.MCP_HUB_INSTANCE+'/.git'));
}
console.log('isolated smoke: '+process.argv[2]+' health, SSH discovery, retained safety checks and independent state PASS');
JS
idle=$(python3 /src/one/files/t3-idle.py)
[[ $idle =~ ^[0-9]+$ ]] || fail 'rollout idle check cannot read this backend state'
denied_port=$((tunnel_port + 1))
ssh -F /tmp/laptop/ssh_config -N -L "127.0.0.1:$denied_port:127.0.0.1:18317" "t3-$variant" >/tmp/denied-tunnel.log 2>&1 &
pids+=("$!")
sleep 1
! curl -fsS --max-time 3 "http://127.0.0.1:$denied_port" >/dev/null 2>&1 || fail 'sshd allowed an unrelated port'
grep -q 'administratively prohibited' /tmp/denied-tunnel.log || fail 'sshd forwarding denial was not exercised'
kill -TERM "$sup"
set +e
wait "$sup"; rc=$?
set -e
[[ $rc == 130 || $rc == 143 ]] || fail "unexpected shutdown $rc"
python3 - "$round" <<'PY'
import importlib, json, sys, tomllib, os
from pathlib import Path
sys.path.insert(0, '/scripts')
runtime = importlib.import_module('isolated-config')
home = Path.home()
settings_path = home / '.t3/userdata/settings.json'
codex_path = home / '.codex/config.toml'
settings = json.loads(settings_path.read_text())
codex = tomllib.loads(codex_path.read_text())
project = str(home / 'projects' / os.environ['MCP_HUB_INSTANCE'])
if sys.argv[1] == 'first':
    settings['providerInstances']['pi']['displayName'] = 'Isolated preference fixture'
    settings['providerInstances']['codex']['enabled'] = False
    settings['providerInstances']['codex']['config']['customModels'] = ['codex/fixture']
    settings['providerInstances']['work'] = {'driver': 'codex', 'enabled': True}
    settings_path.write_text(json.dumps(settings))
    codex['model_reasoning_effort'] = 'high'
    codex['projects'] = {project: {'trust_level': 'trusted'}}
    codex['mcp_servers']['work'] = {'url': 'https://mcp.work.example.test'}
    codex_path.write_text(''.join(json.dumps(key) + ' = ' + runtime.toml_value(value) + '\n' for key, value in codex.items()))
else:
    assert settings['providerInstances']['pi']['displayName'] == 'Isolated preference fixture'
    assert settings['providerInstances']['codex']['enabled'] is False
    assert settings['providerInstances']['codex']['config']['customModels'] == ['codex/fixture']
    assert 'work' not in settings['providerInstances']
    assert codex['model_reasoning_effort'] == 'high'
    assert codex['projects'][project]['trust_level'] == 'trusted'
    assert set(codex['mcp_servers']) == {'searxng'}
    print('isolated smoke: safe provider preferences and Codex project trust survive restart PASS')
PY
echo "isolated smoke: $round clean shutdown PASS"
