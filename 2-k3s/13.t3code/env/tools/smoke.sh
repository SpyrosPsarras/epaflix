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
  timeout 900 docker run --rm --name "$name" --user 1000:1000 \
    --tmpfs /scripts:uid=1000,gid=1000,exec --tmpfs /tmp:uid=1000,gid=1000,exec \
    --tmpfs /private-agent-config:uid=1000,gid=1000 \
    -v "$ROOT:/src:ro" -e HOME=/tmp/home \
    "$image" bash /src/env/tools/smoke.sh --inner
  exit
fi
fail() { echo "FAIL: $*" >&2; if [[ -f /tmp/entrypoint.log ]]; then cat /tmp/entrypoint.log; fi; exit 1; }
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
install -m 0555 /src/env/files/keepass-remote.sh /scripts/
python3 /src/env/tools/private-config.test.py fixture /private-agent-config/bundle.json
for tool in tree kubectl az gh helm kustomize argocd sops git curl ssh python3; do
  command -v "$tool" >/dev/null || fail "missing $tool"
done
kubectl version --client >/dev/null
az version >/dev/null
gh --version >/dev/null
pin() { node -p "require('/tools/package.json').dependencies['$1']"; }
ver() { timeout 60 "$1" --version 2>/dev/null | grep -oE '[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?' | head -1; }
for p in t3:t3 @anthropic-ai/claude-code:claude @openai/codex:codex opencode-ai:opencode; do
  want=$(pin "${p%%:*}"); got=$(ver "/tools/node_modules/.bin/${p##*:}")
  [[ $got == "$want" ]] || fail "${p##*:} reports $got; expected $want"
done
echo 'smoke: all four CLI versions match'
node -e 'require("node:http").createServer((q,s)=>{s.writeHead(200,{"content-type":"application/json"});s.end("{}");}).listen(18317,"127.0.0.1")' &
pids+=("$!")
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
wait_url http://127.0.0.1:3773/health 180
# Probe command discovery separately; T3 must not be configured to use this server.
/tools/node_modules/.bin/opencode serve --hostname 127.0.0.1 --port 4097 >/tmp/opencode-probe.log 2>&1 &
probe=$!; pids+=("$probe")
wait_url http://127.0.0.1:4097/global/health 120
curl -fsS --get --data-urlencode "directory=$HOME/projects/remote" http://127.0.0.1:4097/command | node -e '
let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{if(!JSON.parse(s).some(c=>c.name==="implement"))process.exit(1);console.log("smoke: implement command discovered");});'
kill -TERM "$probe"
wait "$probe" || :
curl -fsS --max-time 5 http://127.0.0.1:3773/.well-known/t3/environment | node -e '
let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{
const j=JSON.parse(s),p=require("/tools/package.json");
if(j.serverVersion!==p.dependencies.t3 || !j.environmentId)process.exit(1);
console.log("smoke: descriptor and server version verified");});'
ready=false
for ((i=0; i<60; i++)); do
  if node -e '
const db=new(require("node:sqlite").DatabaseSync)(process.argv[1],{readOnly:true});
const rows=db.prepare("select workspace_root from projection_projects where deleted_at is null").all();
process.exit(rows.some(r=>r.workspace_root===process.argv[2])?0:3);' "$T3CODE_HOME/userdata/state.sqlite" "$HOME/projects/remote" 2>/dev/null; then
    ready=true; break
  fi
  sleep 2
done
[[ $ready == true ]] || fail 'project was not bootstrapped'
node -e '
const d=require(process.argv[1]);
if(Object.keys(d.providerInstances).sort().join(",")!=="claudeAgent,codex,opencode")throw Error("provider instances");
if(d.providers.opencode.serverUrl || d.providerInstances.opencode.config?.serverUrl)throw Error("OpenCode must be T3-managed");
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
