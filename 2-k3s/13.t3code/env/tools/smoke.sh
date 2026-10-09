#!/usr/bin/env bash
# Install and boot the real runtime, unprivileged and without production secrets.
# Outer mode uses Docker; --inner expects /src, /tools, /scripts and writable /tmp.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
if [[ ${1:-} != --inner ]]; then
  image=${T3_RUNTIME_IMAGE:-t3-runtime:dev}
  if [[ -z ${T3_RUNTIME_IMAGE:-} ]]; then
    docker build -f "$ROOT/env/Dockerfile" -t "$image" "$ROOT"
  fi
  name=t3env-smoke-$$
  trap 'docker rm -f "$name" >/dev/null 2>&1 || :' EXIT
  timeout 1800 docker run --rm --name "$name" --user 1000:1000 \
    --tmpfs /scripts:uid=1000,gid=1000,exec --tmpfs /tmp:uid=1000,gid=1000,exec \
    --tmpfs /private-agent-config:uid=1000,gid=1000 \
    -v "$ROOT:/src:ro" -e HOME=/home/t3 \
    "$image" bash /src/env/tools/smoke.sh --inner
  exit
fi
fail() {
  echo "FAIL: $*" >&2
  for log in /tmp/entrypoint.log /tmp/opencode-parity.log; do
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
install -m 0555 /src/env/files/entrypoint.sh /src/env/files/git-credential-github.sh /src/files/cliproxy-models.js /scripts/
install -m 0555 /src/env/files/private-config.py /scripts/
install -m 0755 /src/one/files/pi.sh /scripts/
install -m 0555 /src/one/files/pi-setup.py /src/one/files/hub_clients.py \
  /src/one/files/t3-pi-settings.py /src/one/files/homelab-ssh.md /scripts/
# The pod's OpenCode plugins and reviewed cc-safety-net installer (HOME is outside
# /tmp because the installer refuses to build there).
install -m 0555 /src/one/files/jev-auto.js /src/one/files/jev-guard.js /src/one/files/opencode-compat.js \
  /src/one/files/jev-checks.md /src/one/files/cc-safety-net-install.py /src/one/files/cc-safety-net.patch \
  /src/one/files/cc-safety-net-hashes.json /src/one/files/opencode-v1-backup.py \
  /src/one/files/opencode-plugin-check.mjs /scripts/
python3 /src/env/tools/private-config.test.py fixture /private-agent-config/bundle.json
for tool in tree kubectl az gh helm kustomize argocd sops git curl ssh python3; do
  command -v "$tool" >/dev/null || fail "missing $tool"
done
[[ -x /usr/sbin/sshd ]] || fail 'missing sshd'
! compgen -G '/etc/ssh/ssh_host_*' >/dev/null || fail 'image ships SSH host keys'
kubectl version --client >/dev/null
az version >/dev/null
gh_want=$(source /src/versions.env && echo "$GH_VERSION")
[[ $(gh --version | head -1) == "gh version $gh_want "* ]] || fail "gh is not the pinned $gh_want"
pin() { node -p "require('/tools/package.json').dependencies['$1']"; }
ver() { timeout 60 "$1" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?' | head -1; }
for p in t3:t3 @anthropic-ai/claude-code:claude @openai/codex:codex @opencode/cli:opencode; do
  want=$(pin "${p%%:*}"); got=$(ver "/tools/node_modules/.bin/${p##*:}")
  [[ $got == "$want" ]] || fail "${p##*:} reports $got; expected $want"
done
echo 'smoke: all four CLI versions match'
node -e 'require("node:http").createServer((q,s)=>{s.writeHead(200,{"content-type":"application/json"});
s.end(JSON.stringify({models:["codex/gpt-6-sol","claude/claude-opus-5-5"].map(slug=>({slug,context_window:200000}))}));}).listen(18317,"127.0.0.1")' &
pids+=("$!")
# A synthetic OpenCode 1 store whose rows are only in the WAL: _exit skips the checkpoint on close.
python3 - "$HOME/.local/share/opencode/opencode.db" <<'PY'
import os, sqlite3, sys
os.makedirs(os.path.dirname(sys.argv[1]), exist_ok=True)
db = sqlite3.connect(sys.argv[1])
db.execute("pragma journal_mode=wal")
db.execute("pragma wal_autocheckpoint=0")
db.execute("create table session (id text primary key)")
db.executemany("insert into session values (?)", [("ses_smoke_1",), ("ses_smoke_2",)])
db.commit()
os._exit(0)
PY
[[ -s $HOME/.local/share/opencode/opencode.db-wal ]] || fail 'the OpenCode 1 fixture has an empty WAL'
git init -q --bare --initial-branch=main /tmp/remote.git
git init -q --initial-branch=main /tmp/seed
(cd /tmp/seed && git -c user.name=smoke -c user.email=smoke@localhost commit -q --allow-empty -m seed && git push -q /tmp/remote.git HEAD:main)
export ANTHROPIC_BASE_URL=http://127.0.0.1:18317 ANTHROPIC_AUTH_TOKEN=smoke-not-a-secret
export T3_PROJECT_REPO=/tmp/remote.git T3CODE_HOME=$HOME/.t3
unset GITHUB_TOKEN GH_TOKEN
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
grep -q '^opencode-plugin-check: .* active$' /tmp/entrypoint.log || fail 'the entrypoint did not run the plugin check'
python3 - "$HOME/.local/share/opencode" <<'PY' || fail 'no verified OpenCode 1 backup'
import json, sqlite3, sys
marker = json.load(open(f"{sys.argv[1]}/opencode.db.v1-backup.marker"))
db = sqlite3.connect(f"file:{sys.argv[1]}/opencode.db.v1-backup?mode=ro", uri=True)
assert marker["state"] == "v1-backup" and marker["sessions"] == 2 and marker["walBytes"] > 0, marker
assert db.execute("select count(*) from session").fetchone()[0] == 2
print("smoke: the OpenCode 1 store, WAL rows included, was backed up before OpenCode started")
PY
rm -rf /tmp/broken-config
cp -r "$HOME/.config/opencode" /tmp/broken-config
echo 'export default {' >/tmp/broken-config/plugins/jev-guard.js
if node /scripts/opencode-plugin-check.mjs /tools/node_modules/@opencode/cli/bin/opencode.exe /tmp/broken-config \
  cliproxy-models jev-auto jev-guard opencode-compat cc-safety-net superpowers >/tmp/opencode-parity.log 2>&1; then
  fail 'the plugin check passed a broken jev-guard'
fi
echo 'smoke: the plugin check stops startup on a broken plugin'
# OpenCode 2 parity on the plugin set the entrypoint provisioned, against local
# mocks in a private HOME; T3 is not configured to use it.
timeout 600 node /src/env/tools/opencode-parity.mjs /tools/node_modules/@opencode/cli/bin/opencode.exe \
  "$HOME/.config/opencode" "$HOME" >>/tmp/opencode-parity.log 2>&1 || fail 'OpenCode 2 parity failed'
grep '^parity:' /tmp/opencode-parity.log
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
node -e '
const d=require(process.argv[1]);
if(Object.keys(d.providerInstances).sort().join(",")!=="claudeAgent,codex,opencode")throw Error("provider instances");
if(d.providerInstances.opencode.config?.serverUrl)throw Error("OpenCode must be T3-managed");
console.log("smoke: project bootstrap and provider settings verified");' "$T3CODE_HOME/userdata/settings.json"
kill -TERM "$sup"
timeout 30 tail --pid="$sup" -f /dev/null || fail 'supervisor did not stop in 30s'
set +e; wait "$sup"; rc=$?; set -e
# JS and native T3 launchers report TERM as 130 and 143 respectively.
[[ $rc == 130 || $rc == 143 ]] || fail "TERM exit was $rc, expected 130 or 143"
for port in 3773 4096; do
  node -e 'const s=require("node:net").connect(+process.argv[1],"127.0.0.1");s.on("connect",()=>process.exit(1));s.on("error",()=>process.exit(0));s.setTimeout(1000,()=>process.exit(1));' "$port" || fail "port $port still open"
done
pids=("${pids[0]}")
echo 'smoke: clean shutdown, PASS'
