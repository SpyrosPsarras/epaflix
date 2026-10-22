#!/usr/bin/env bash
# Install and boot the real runtime, unprivileged and without production secrets.
# Outer mode uses Docker; --inner expects /src, /tools, /scripts and writable /tmp.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
if [[ ${T3_RUNTIME_VARIANT:-mixed} != mixed ]]; then
  exec bash "$ROOT/env/tools/isolated-smoke.sh" "$@"
fi
if [[ ${1:-} != --inner ]]; then
  image=${T3_RUNTIME_IMAGE:-t3-runtime:dev}
  if [[ -z ${T3_RUNTIME_IMAGE:-} ]]; then
    docker build -f "$ROOT/env/Dockerfile" -t "$image" "$ROOT"
  fi
  name=t3env-smoke-$$
  trap 'docker rm -f "$name" >/dev/null 2>&1 || :' EXIT
  timeout 1800 docker run --rm --network none --name "$name" --user 1000:1000 \
    --tmpfs /scripts:uid=1000,gid=1000,exec --tmpfs /tmp:uid=1000,gid=1000,exec \
    --tmpfs /private-agent-config:uid=1000,gid=1000 \
    -v "$ROOT:/src:ro" -e HOME=/home/t3 \
    "$image" bash /src/env/tools/smoke.sh --inner
  exit
fi
fail() {
  echo "FAIL: $*" >&2
  for log in /tmp/entrypoint.log /tmp/pi-parity.log; do
    if [[ -f $log ]]; then echo "--- $log"; cat "$log"; fi
  done
  exit 1
}
pids=()
cleanup() {
  if (( ${#pids[@]} )); then
    kill -TERM "${pids[@]}" 2>/dev/null || :
    sleep 1
    kill -KILL "${pids[@]}" 2>/dev/null || :
    wait 2>/dev/null || :
  fi
}
trap cleanup EXIT
mkdir -p "$HOME"
python3 - <<'PY'
from pathlib import Path
import yaml
root = Path('/src/one')
config = yaml.safe_load((root / 'kustomization.yaml').read_text())
files = next(item['files'] for item in config['configMapGenerator'] if item['name'] == 't3code-scripts')
for spec in files:
    name, source = spec.split('=', 1) if '=' in spec else (Path(spec).name, spec)
    destination = Path('/scripts') / name
    destination.write_bytes((root / source).read_bytes())
    destination.chmod(0o555)
PY
python3 /src/one/tools/test-cc-safety-net-install.py || fail 'Safety Net installer checks'
[[ ! -e $HOME/.local/share/pi/cc-safety-net/2.6.4 ]] || fail 'Safety Net home is not fresh'
python3 /src/env/tools/private-config.test.py fixture /private-agent-config/bundle.json
bash /src/env/tools/inventory.sh || fail 'runtime inventory'
node -e 'require("node:http").createServer((q,s)=>{s.writeHead(200,{"content-type":"application/json"});
s.end(JSON.stringify({data:["codex/gpt-6-sol","claude/claude-opus-5-5"].map(id=>({id,context_window:200000}))}));}).listen(18317,"127.0.0.1")' &
pids+=("$!")
git init -q --bare --initial-branch=main /tmp/remote.git
git init -q --initial-branch=main /tmp/seed
(cd /tmp/seed && git -c user.name=smoke -c user.email=smoke@localhost commit -q --allow-empty -m seed && git push -q /tmp/remote.git HEAD:main)
export ANTHROPIC_BASE_URL=http://127.0.0.1:18317 ANTHROPIC_AUTH_TOKEN=smoke-not-a-secret
export T3_PROJECT_REPO=/tmp/remote.git T3CODE_HOME=$HOME/.t3
export MCP_HUB_TOKEN=synthetic-smoke-hub-token MCP_HUB_URL=http://127.0.0.1:18318
unset GITHUB_TOKEN GH_TOKEN MCP_HUB_INSTANCE MCP_HUB_SERVICES_FILE
mkdir -p "$T3CODE_HOME/userdata"
cat >"$T3CODE_HOME/userdata/settings.json" <<'JSON'
{"providerInstances":{"opencode":{"driver":"opencode"},"codex":{"driver":"codex","enabled":true},"claudeAgent":{"driver":"claudeAgent","enabled":false}},"defaultModelSelection":{"instanceId":"opencode","model":"old","options":[{"id":"variant","value":"high"}]},"textGenerationModelSelection":{"instanceId":"opencode","model":"old","options":[{"id":"variant","value":"low"}]}}
JSON
bash /scripts/entrypoint.sh >/tmp/entrypoint.log 2>&1 &
sup=$!; pids+=("$sup")
wait_url() {
  for ((i=0; i<$2; i++)); do
    curl -fsS --max-time 2 "$1" >/dev/null 2>&1 && return 0
    kill -0 "$sup" 2>/dev/null || fail 'entrypoint exited early'
    sleep 1
  done
  fail "$1 did not become healthy"
}
wait_url http://127.0.0.1:3773/health 600
grep -q '^pi-setup: all registered packages listed$' /tmp/entrypoint.log || fail 'package check did not pass'
mkdir -p "$HOME/.agents/skills/ponytail"
printf '%s\n' '---' 'name: ponytail' 'description: Private duplicate must not win' '---' 'Duplicate skill fixture' >"$HOME/.agents/skills/ponytail/SKILL.md"
timeout 90 node /src/env/tools/pi-parity.mjs /scripts/pi.sh "$HOME" > /tmp/pi-parity.log 2>&1 || fail 'Pi extension loading failed'
grep '^parity:' /tmp/pi-parity.log
node /src/env/tools/pi-models.mjs /scripts/pi.sh || fail 'Pi model listing failed'
curl -fsS --max-time 5 http://127.0.0.1:3773/.well-known/t3/environment | node -e '
let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{
const j=JSON.parse(s),p=require("/tools/package.json");
if(j.serverVersion!==p.dependencies.t3 || !j.environmentId)process.exit(1);
console.log("smoke: descriptor and server version verified");});'
ready=false
for ((i=0; i<60; i++)); do
  if node -e '
const open=p=>{try{return new(require("node:sqlite").DatabaseSync)(p,{readOnly:true})}catch{return null}};
const db=open(process.argv[1])||open(process.argv[2]);
if(!db)process.exit(3);
const rows=db.prepare("select workspace_root from projection_projects where deleted_at is null").all();
process.exit(rows.some(r=>r.workspace_root===process.argv[3])?0:3);' "$T3CODE_HOME/userdata/statev2.sqlite" "$T3CODE_HOME/userdata/state.sqlite" "$HOME/projects/remote" 2>/dev/null; then
    ready=true; break
  fi
  sleep 2
done
[[ $ready == true ]] || fail 'project was not bootstrapped'
check_settings() {
node -e '
const d=require(process.argv[1]);
if(Object.keys(d.providerInstances).sort().join(",")!=="claudeAgent,codex,pi")throw Error("provider instances");
if(d.providerInstances.pi.driver!=="pi" || d.providerInstances.pi.config?.binaryPath!=="/scripts/pi.sh")throw Error("Pi launcher");
for(const [key,model,level] of [["defaultModelSelection","cliproxy/claude/claude-opus-5-5","high"],["textGenerationModelSelection","cliproxy-textgen/claude/claude-haiku-5-5","low"]]) {
const selection=d[key];
if(selection.instanceId!=="pi" || selection.model!==model || JSON.stringify(selection.options)!==JSON.stringify([{id:"thinking",value:level}]))throw Error("migration selection");
}
const fs=require("node:fs"),backup=process.argv[1]+".before-pi";
if((fs.statSync(backup).mode&0o777)!==0o600 || !JSON.parse(fs.readFileSync(backup)).providerInstances.opencode)throw Error("migration backup");
console.log("smoke: project bootstrap, migrated provider selections and private rollback backup verified");' "$T3CODE_HOME/userdata/settings.json"
python3 - <<'PY'
import json, os, sys, tomllib
from pathlib import Path
sys.path.insert(0, '/scripts')
import hub_services
home = Path.home()
hub = os.environ['MCP_HUB_URL']
for name in ('.pi/agent/mcp.json', '.claude/.claude.json', '.claude.json', '.codex/config.toml'):
    text = (home / name).read_text()
    assert os.environ['MCP_HUB_TOKEN'] not in text, name
    codex = name.endswith('.toml')
    config = tomllib.loads(text) if codex else json.loads(text)
    servers = config['mcp_servers' if codex else 'mcpServers']
    for service, path in hub_services.client_servers().items():
        entry = servers[service]
        assert entry['url'] == hub + path, name
        if codex:
            assert entry['bearer_token_env_var'] == 'MCP_HUB_TOKEN', name
        else:
            assert entry['headers'] == {'Authorization': 'Bearer ${MCP_HUB_TOKEN}'}, name
print('smoke: Pi, Claude and Codex synthetic MCP hub env references PASS')
PY
}
check_settings
stop_runtime() {
kill -TERM "$sup"
timeout 30 tail --pid="$sup" -f /dev/null || fail 'supervisor did not stop in 30s'
set +e; wait "$sup"; rc=$?; set -e
# JS and native T3 launchers report TERM as 130 and 143 respectively.
[[ $rc == 130 || $rc == 143 ]] || fail "TERM exit was $rc, expected 130 or 143"
node -e 'const s=require("node:net").connect(3773,"127.0.0.1");s.on("connect",()=>process.exit(1));s.on("error",()=>process.exit(0));s.setTimeout(1000,()=>process.exit(1));' || fail 'port 3773 still open'
pids=("${pids[0]}")
}
stop_runtime
node -e 'const fs=require("node:fs"),p=process.argv[1],s=JSON.parse(fs.readFileSync(p));s.providerInstances.pi.displayName="Retained smoke preference";fs.writeFileSync(p,JSON.stringify(s));' "$T3CODE_HOME/userdata/settings.json"
safety=$HOME/.local/share/pi/cc-safety-net/2.6.4/dist/pi/index.js
cp "$safety" /tmp/reviewed-safety-entry.js
printf '\ntampered\n' >>"$safety"
if timeout 90 bash /scripts/entrypoint.sh >/tmp/rejected-startup.log 2>&1; then
  fail 'startup accepted a changed Safety Net artifact'
else
  rc=$?
  [[ $rc != 124 ]] || fail 'rejected startup did not exit'
fi
grep -q 'Hash mismatch: dist/pi/index.js' /tmp/rejected-startup.log || fail 'startup failed before the artifact check'
node -e 'const s=require("node:net").connect(3773,"127.0.0.1");s.on("connect",()=>process.exit(1));s.on("error",()=>process.exit(0));s.setTimeout(1000,()=>process.exit(1));' || fail 'rejected startup opened port 3773'
cp /tmp/reviewed-safety-entry.js "$safety"
echo 'smoke: changed Safety Net fails before final launch PASS'
bash /scripts/entrypoint.sh >/tmp/entrypoint.log 2>&1 &
sup=$!; pids+=("$sup")
wait_url http://127.0.0.1:3773/health 600
grep -q '^pi-setup: all registered packages listed$' /tmp/entrypoint.log || fail 'restart package check did not pass'
check_settings
node -e 'if(require(process.argv[1]).providerInstances.pi.displayName!=="Retained smoke preference")throw Error("user preference lost");' "$T3CODE_HOME/userdata/settings.json"
echo 'smoke: offline first boot, restart, retained user edits and provider reconciliation PASS'
stop_runtime
echo 'smoke: clean shutdown, PASS'
