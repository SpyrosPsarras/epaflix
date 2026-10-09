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
install -m 0555 /src/env/files/entrypoint.sh /src/env/files/git-credential-github.sh /scripts/
install -m 0555 /src/env/files/private-config.py /scripts/
install -m 0755 /src/one/files/pi.sh /scripts/
install -m 0555 /src/one/files/pi-setup.py /src/one/files/hub_clients.py \
  /src/one/files/t3-pi-settings.py /src/one/files/homelab-ssh.md /scripts/
install -m 0555 /src/one/files/cc-safety-net-install.py /src/one/files/cc-safety-net.patch \
  /src/one/files/cc-safety-net-hashes.json /scripts/
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
for p in t3:t3 @anthropic-ai/claude-code:claude @openai/codex:codex @earendil-works/pi-coding-agent:pi; do
  want=$(pin "${p%%:*}"); got=$(ver "/tools/node_modules/.bin/${p##*:}")
  [[ $got == "$want" ]] || fail "${p##*:} reports $got; expected $want"
done
echo 'smoke: all four CLI versions match'
node -e 'require("node:http").createServer((q,s)=>{s.writeHead(200,{"content-type":"application/json"});
s.end(JSON.stringify({data:["codex/gpt-6-sol","claude/claude-opus-5-5"].map(id=>({id,context_window:200000}))}));}).listen(18317,"127.0.0.1")' &
pids+=("$!")
git init -q --bare --initial-branch=main /tmp/remote.git
git init -q --initial-branch=main /tmp/seed
(cd /tmp/seed && git -c user.name=smoke -c user.email=smoke@localhost commit -q --allow-empty -m seed && git push -q /tmp/remote.git HEAD:main)
export ANTHROPIC_BASE_URL=http://127.0.0.1:18317 ANTHROPIC_AUTH_TOKEN=smoke-not-a-secret
export T3_PROJECT_REPO=/tmp/remote.git T3CODE_HOME=$HOME/.t3
unset GITHUB_TOKEN GH_TOKEN
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
node -e '
const d=require(process.argv[1]);
if(Object.keys(d.providerInstances).sort().join(",")!=="claudeAgent,codex,pi")throw Error("provider instances");
if(d.providerInstances.pi.driver!=="pi" || d.providerInstances.pi.config?.binaryPath!=="/scripts/pi.sh")throw Error("Pi launcher");
for(const [key,level] of [["defaultModelSelection","high"],["textGenerationModelSelection","low"]]) {
const selection=d[key];
if(selection.instanceId!=="pi" || selection.model!=="cliproxy/claude/claude-opus-5-5" || JSON.stringify(selection.options)!==JSON.stringify([{id:"thinking",value:level}]))throw Error("migration selection");
}
const fs=require("node:fs"),backup=process.argv[1]+".before-pi";
if((fs.statSync(backup).mode&0o777)!==0o600 || !JSON.parse(fs.readFileSync(backup)).providerInstances.opencode)throw Error("migration backup");
console.log("smoke: project bootstrap, migrated provider selections and private rollback backup verified");' "$T3CODE_HOME/userdata/settings.json"
kill -TERM "$sup"
timeout 30 tail --pid="$sup" -f /dev/null || fail 'supervisor did not stop in 30s'
set +e; wait "$sup"; rc=$?; set -e
# JS and native T3 launchers report TERM as 130 and 143 respectively.
[[ $rc == 130 || $rc == 143 ]] || fail "TERM exit was $rc, expected 130 or 143"
node -e 'const s=require("node:net").connect(3773,"127.0.0.1");s.on("connect",()=>process.exit(1));s.on("error",()=>process.exit(0));s.setTimeout(1000,()=>process.exit(1));' || fail 'port 3773 still open'
pids=("${pids[0]}")
echo 'smoke: clean shutdown, PASS'
