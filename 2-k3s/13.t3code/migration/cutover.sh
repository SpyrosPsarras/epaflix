#!/usr/bin/env bash
# Cutover: three T3 servers -> one pod in namespace t3code.
#
# Run from a machine that is NOT one of the three servers (your laptop) with:
#   - kubectl context epaflix
#   - ssh proxmox-takaros (root) reaching LXC 100 via pct
#   - ssh spyros@t3code (192.168.10.240) for the host home copy
#   - python3 >= 3.11
#
# Phases are idempotent and can be re-run; each one checks its own marker.
#   freeze    stop host t3 + scale t3env to 0. Nothing writes after this.
#   snapshot  SQLite backups + settings from all three, into $WORK/snapshots
#   merge     migrate.py merge -> $WORK/merged
#   load      create pod with replicas=0, copy homes into the three PVCs via a
#             helper pod, drop merged state.sqlite/settings.json into place
#   start     scale t3code to 1, wait ready, run smoke checks
#   route     point t3code.epaflix.com at the pod (cross-namespace IngressRoute)
#   all       freeze snapshot merge load start   (route stays manual)
#   rollback  scale t3code to 0, start host t3, scale t3env back to 2
#
# Sources are never modified. Old PVCs and the LXC home stay intact.
set -euo pipefail

WORK=${WORK:-$HOME/t3-cutover}
REPO=${REPO:-$(cd "$(dirname "$0")/../../.." && pwd)}
MIG=$REPO/2-k3s/13.t3code/migration
HOST_SSH=${HOST_SSH:-spyros@192.168.10.240}
PVE_SSH=${PVE_SSH:-proxmox-takaros}
LXC_ID=${LXC_ID:-100}
NS=t3code
OLD_NS=remote-pi
NODE=${NODE:-k3s-worker-65}

log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die() { log "FAIL: $*"; exit 1; }
k() { kubectl "$@"; }
marker() { [[ -f $WORK/.done-$1 ]]; }
mark() { touch "$WORK/.done-$1"; }

# Paths inside each source home that carry state worth moving. Caches,
# runtimes, package trees and terraform providers are rebuilt on demand.
# migration/private holds the rehearsal snapshots and must not ride along.
EXCLUDES=(
  --exclude=./.cache --exclude=./.npm --exclude=./.bun --exclude=./.nuget
  --exclude=./.dotnet --exclude=./.azure/cliextensions --exclude=./go
  --exclude=./.t3/runtime --exclude=./.t3/caches --exclude=./.t3/tools
  --exclude=./.t3/userdata/logs --exclude=./.local/share/opencode/snapshot
  --exclude=./.local/share/t3-private-config --exclude=./.zed_server
  --exclude=./pentect-v0.0.83.zip --exclude=node_modules --exclude=.terraform
  --exclude=__pycache__ --exclude='13.t3code/migration/private'
)

phase_freeze() {
  marker freeze && { log "freeze: already done"; return; }
  mkdir -p "$WORK"
  log "freeze: stopping host t3 (LXC $LXC_ID via $PVE_SSH)"
  ssh "$PVE_SSH" "pct exec $LXC_ID -- su - spyros -c 'systemctl --user stop t3code.service && systemctl --user disable t3code.service'"
  ssh "$PVE_SSH" "pct exec $LXC_ID -- systemctl disable --now t3code-update.timer t3code-update.service" || true
  ssh "$HOST_SSH" 'pgrep -fa "t3 (serve|__service-launcher)" && exit 1 || true' || die "host t3 still running"
  log "freeze: scaling $OLD_NS/t3env to 0"
  k -n argocd patch application t3code-env --type merge -p '{"spec":{"syncPolicy":{"automated":null}}}' || true
  k -n "$OLD_NS" scale statefulset t3env --replicas=0
  k -n "$OLD_NS" wait --for=delete pod/t3env-0 pod/t3env-1 --timeout=180s
  mark freeze
}

# Read a source's userdata via SQLite online backup so WAL content is included.
# The pods are gone after freeze, so env PVCs are read through a helper pod.
helper_pod() { # name pvc
  cat <<EOF | k -n "$OLD_NS" apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: $1, namespace: $OLD_NS, labels: {app.kubernetes.io/name: t3-cutover}}
spec:
  restartPolicy: Never
  nodeSelector: {kubernetes.io/hostname: "$3"}
  securityContext: {runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000}
  containers:
    - name: h
      image: docker.io/library/python:3.13-slim
      command: [sleep, "7200"]
      volumeMounts: [{name: home, mountPath: /home/t3}]
  volumes: [{name: home, persistentVolumeClaim: {claimName: $2}}]
EOF
  k -n "$OLD_NS" wait --for=condition=Ready "pod/$1" --timeout=180s >/dev/null
}

pvc_node() { k -n "$1" get pv "$(k -n "$1" get pvc "$2" -o jsonpath='{.spec.volumeName}')" -o jsonpath='{.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]}'; }

phase_snapshot() {
  marker snapshot && { log "snapshot: already done"; return; }
  marker freeze || die "snapshot requires freeze"
  rm -rf "$WORK/snapshots"; mkdir -p "$WORK/snapshots"
  # SQLite online backup to a file on the source (WAL included), then copy.
  # Streaming over `kubectl exec` stdout dropped mid-way on the 1.2G host DB.
  local code='import sqlite3,sys
c=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True); d=sqlite3.connect(sys.argv[2]); c.backup(d); d.close()'
  log "snapshot: host"
  mkdir -m 700 "$WORK/snapshots/host"
  ssh "$HOST_SSH" "rm -f /tmp/state.snap && python3 -c '$code' /home/spyros/.t3/userdata/state.sqlite /tmp/state.snap && sha256sum /tmp/state.snap | cut -c1-64" > "$WORK/snapshots/host/state.sha"
  scp -q "$HOST_SSH:/tmp/state.snap" "$WORK/snapshots/host/state.sqlite"
  [[ $(cat "$WORK/snapshots/host/state.sha") == $(sha256sum "$WORK/snapshots/host/state.sqlite" | cut -c1-64) ]] || die "host snapshot hash mismatch"
  ssh "$HOST_SSH" "rm -f /tmp/state.snap; cat /home/spyros/.t3/userdata/settings.json" > "$WORK/snapshots/host/settings.json"
  for i in 0 1; do
    log "snapshot: t3env-$i"
    helper_pod "cutover-src-$i" "home-t3env-$i" "$(pvc_node "$OLD_NS" "home-t3env-$i")"
    mkdir -m 700 "$WORK/snapshots/t3env-$i"
    k -n "$OLD_NS" exec "cutover-src-$i" -- python3 -c "$code" /home/t3/.t3/userdata/state.sqlite /tmp/state.snap
    k -n "$OLD_NS" cp "cutover-src-$i:/tmp/state.snap" "$WORK/snapshots/t3env-$i/state.sqlite" --retries=20
    [[ $(k -n "$OLD_NS" exec "cutover-src-$i" -- sha256sum /tmp/state.snap | cut -c1-64) == $(sha256sum "$WORK/snapshots/t3env-$i/state.sqlite" | cut -c1-64) ]] || die "t3env-$i snapshot hash mismatch"
    k -n "$OLD_NS" exec "cutover-src-$i" -- cat /home/t3/.t3/userdata/settings.json > "$WORK/snapshots/t3env-$i/settings.json"
  done
  mark snapshot
}

phase_merge() {
  marker merge && { log "merge: already done"; return; }
  marker snapshot || die "merge requires snapshot"
  rm -rf "$WORK/merged"
  (cd "$MIG" && python3 migrate.py merge "$WORK/snapshots" "$WORK/merged")
  python3 - "$WORK/merged/report.json" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
t = r["totals"]["projection_threads"]; s = sum(v["counts"]["projection_threads"] for v in r["sources"].values())
assert t == s, (t, s)
print("merge: threads", t, "events", r["totals"]["orchestration_events"])
EOF
  mark merge
}

# Move a home tree into a destination PVC. Streaming tar or `kubectl cp` of one
# multi-GB file drops the websocket mid-stream, so archive on the source, land
# 256M parts with per-part hashes, cat inside the pod, then extract there.
# `Cannot utime .` on the mount root is harmless.
STAGE=/home/spyros/.cutover-stage
land_file() { # local_file name
  local src=$1 name=$2 parts=$WORK/parts
  rm -rf "$parts"; mkdir -p "$parts"
  split -b 256M -d -a 3 "$src" "$parts/$name.part."
  k -n "$NS" exec cutover-dst -- sh -c "mkdir -p $STAGE && rm -f $STAGE/$name.part.*"
  local p want got
  for p in "$parts/$name".part.*; do
    want=$(sha256sum "$p" | cut -c1-64)
    for _ in 1 2 3 4 5 6; do
      k -n "$NS" cp "$p" "cutover-dst:$STAGE/$(basename "$p")" --retries=10 2>/dev/null || true
      got=$(k -n "$NS" exec cutover-dst -- sha256sum "$STAGE/$(basename "$p")" 2>/dev/null | cut -c1-64)
      [[ $got == "$want" ]] && break
      got=
    done
    [[ $got == "$want" ]] || die "part $(basename "$p") failed after 6 tries"
  done
  k -n "$NS" exec cutover-dst -- sh -c "cd $STAGE && cat $name.part.* > $name && rm -f $name.part.*"
  [[ $(sha256sum "$src" | cut -c1-64) == $(k -n "$NS" exec cutover-dst -- sha256sum "$STAGE/$name" | cut -c1-64) ]] || die "hash mismatch for $name"
  rm -rf "$parts"
}
land_archive() { gzip -t "$1"; land_file "$1" "$2.tgz"; }
extract_archive() { # name dest_dir
  k -n "$NS" exec cutover-dst -- sh -c "cd $2 && tar -xzpf $STAGE/$1.tgz --no-same-owner --warning=no-timestamp 2>&1 | grep -v 'Cannot utime\|Exiting with failure' || true
    tar --quoting-style=literal -tzf $STAGE/$1.tgz | grep -v '/\$' | sort > /tmp/a; find . \( -type f -o -type l \) | sort > /tmp/b
    m=\$(comm -23 /tmp/a /tmp/b | wc -l); echo \"$1: \$(wc -l < /tmp/b) entries, \$m missing\"; [ \"\$m\" = 0 ]"
}
env_home_archive() { # i -> local path
  local i=$1 out=$WORK/homes/t3env-$i.tgz
  mkdir -p "$WORK/homes"
  [[ -f $out ]] && gzip -t "$out" 2>/dev/null && { echo "$out"; return; }
  # Archive to the pod's /tmp (emptyDir) then cp; a streamed tar dropped mid-way.
  k -n "$OLD_NS" exec "cutover-src-$i" -- sh -c "cd /home/t3 && tar -czf /tmp/home.tgz $(printf ' %q' "${EXCLUDES[@]}") . 2>&1 | grep -v 'file changed' >&2 || true"
  k -n "$OLD_NS" cp "cutover-src-$i:/tmp/home.tgz" "$out" --retries=20 >&2
  [[ $(k -n "$OLD_NS" exec "cutover-src-$i" -- sha256sum /tmp/home.tgz | cut -c1-64) == $(sha256sum "$out" | cut -c1-64) ]] || die "t3env-$i archive hash mismatch"
  echo "$out"
}
host_home_archive() {
  local out=$WORK/homes/host.tgz
  mkdir -p "$WORK/homes"
  [[ -f $out ]] && gzip -t "$out" 2>/dev/null && { echo "$out"; return; }
  ssh "$HOST_SSH" "cd /home/spyros && tar -czf /tmp/home.tgz $(printf ' %q' "${EXCLUDES[@]}") . 2>&1 | grep -v 'file changed' >&2 || true; sha256sum /tmp/home.tgz | cut -c1-64" > "$WORK/homes/host.sha"
  scp -q "$HOST_SSH:/tmp/home.tgz" "$out"
  [[ $(cat "$WORK/homes/host.sha") == $(sha256sum "$out" | cut -c1-64) ]] || die "host archive hash mismatch"
  ssh "$HOST_SSH" rm -f /tmp/home.tgz
  echo "$out"
}

phase_load() {
  marker load && { log "load: already done"; return; }
  marker merge || die "load requires merge"
  log "load: applying $NS manifests with replicas=0"
  k get ns "$NS" >/dev/null 2>&1 || k apply -f "$REPO/2-k3s/13.t3code/one/namespace.yaml"
  (cd "$REPO/2-k3s/13.t3code/one" && kustomize build --enable-alpha-plugins --enable-exec . | k apply --server-side -f -)
  k -n "$NS" scale statefulset t3code --replicas=0
  # PVCs bind on first consumer; a helper pod on the target node does that.
  cat <<EOF | k -n "$NS" apply -f - >/dev/null
apiVersion: v1
kind: Pod
metadata: {name: cutover-dst, namespace: $NS, labels: {app.kubernetes.io/name: t3-cutover}}
spec:
  restartPolicy: Never
  nodeSelector: {kubernetes.io/hostname: "$NODE"}
  securityContext: {runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000}
  containers:
    - name: h
      image: docker.io/library/python:3.13-slim
      command: [sleep, "14400"]
      volumeMounts:
        - {name: s, mountPath: /home/spyros}
        - {name: e0, mountPath: /home/t3env-0}
        - {name: e1, mountPath: /home/t3env-1}
  volumes:
    - {name: s, persistentVolumeClaim: {claimName: home-spyros}}
    - {name: e0, persistentVolumeClaim: {claimName: home-t3env-0}}
    - {name: e1, persistentVolumeClaim: {claimName: home-t3env-1}}
EOF
  k -n "$NS" wait --for=condition=Ready pod/cutover-dst --timeout=300s >/dev/null
  # A populated PVC means a previous load. Re-running would bury the original
  # premerge backup under merged data. Wipe the PVCs deliberately, then retry.
  k -n "$NS" exec cutover-dst -- sh -c '[ -z "$(ls -A /home/spyros /home/t3env-0 /home/t3env-1 2>/dev/null | grep -v "^/\|^$")" ]' \
    || die "destination PVCs are not empty; wipe them (see README) before re-running load"
  for i in 0 1; do
    log "load: t3env-$i home"
    land_archive "$(env_home_archive "$i")" "t3env-$i"
    extract_archive "t3env-$i" "/home/t3env-$i"
  done
  log "load: host home (largest, several GB)"
  land_archive "$(host_home_archive)" host
  extract_archive host /home/spyros
  log "load: relocating /home/t3 plumbing in env homes"
  k -n "$NS" cp "$MIG/repair_home.py" "cutover-dst:$STAGE/repair_home.py" --retries=10
  for i in 0 1; do
    k -n "$NS" exec cutover-dst -- python3 "$STAGE/repair_home.py" "/home/t3env-$i" | tail -1
  done
  log "load: installing merged userdata"
  for h in /home/spyros /home/t3env-0 /home/t3env-1; do
    k -n "$NS" exec cutover-dst -- sh -c "cd $h/.t3/userdata && mkdir -p ../userdata-premerge && mv state.sqlite* ../userdata-premerge/ 2>/dev/null; cp -p settings.json ../userdata-premerge/; rm -f server-runtime.json"
  done
  land_file "$WORK/merged/state.sqlite" merged-state.sqlite
  land_file "$WORK/merged/settings.json" merged-settings.json
  k -n "$NS" exec cutover-dst -- sh -c "cd /home/spyros/.t3/userdata && mv $STAGE/merged-state.sqlite state.sqlite && mv $STAGE/merged-settings.json settings.json && chmod 600 state.sqlite settings.json"
  # Attachments: union by filename. Names embed the thread id, threads are distinct.
  for i in 0 1; do
    k -n "$NS" exec cutover-dst -- sh -c "cd /home/t3env-$i/.t3/userdata && for d in attachments browser-artifacts; do [ -d \$d ] && mkdir -p /home/spyros/.t3/userdata/\$d && cp -an \$d/. /home/spyros/.t3/userdata/\$d/; done; true"
  done
  # T3 Connect state is per-environment. The LXC keeps its link; the pod must
  # not claim it too (two servers on one relay identity). Park it, re-link later.
  k -n "$NS" exec cutover-dst -- sh -c 'cd /home/spyros/.t3/userdata/secrets && mkdir -p ../secrets-cloud-premerge && mv cloud-*.bin ../secrets-cloud-premerge/ 2>/dev/null; ls | wc -l'
  k -n "$NS" exec -i cutover-dst -- python3 - <<'EOF'
import sqlite3, json, os
c = sqlite3.connect("file:/home/spyros/.t3/userdata/state.sqlite?mode=ro", uri=True)
assert c.execute("PRAGMA integrity_check").fetchone() == ("ok",)
n = c.execute("SELECT count(*) FROM projection_threads").fetchone()[0]
s = json.load(open("/home/spyros/.t3/userdata/settings.json"))
ids = {r[0] for r in c.execute('SELECT json_extract(value,"$.id") FROM projection_thread_messages, json_each(coalesce(attachments_json,"[]"))')} - {None}
present = {f.rsplit(".", 1)[0] for f in os.listdir("/home/spyros/.t3/userdata/attachments")}
assert ids <= present, sorted(ids - present)[:5]
print("load: pod has", n, "threads,", len(s["providerInstances"]), "provider instances,", len(ids), "attachments all on disk")
for h in ("/home/t3env-0", "/home/t3env-1"):
    assert os.path.isdir(h + "/.codex") or os.path.isdir(h + "/.local/share/opencode"), h
    assert not os.path.exists(h + "/.t3/userdata/state.sqlite"), h
print("load: env provider homes present, no second T3 state")
EOF
  k -n "$NS" exec cutover-dst -- rm -rf "$STAGE"
  mark load
}

phase_start() {
  marker start && { log "start: already done"; return; }
  marker load || die "start requires load"
  k -n "$NS" delete pod cutover-dst --ignore-not-found --wait=true
  k -n "$NS" scale statefulset t3code --replicas=1
  k -n "$NS" rollout status statefulset/t3code --timeout=600s
  sleep 20
  log "start: smoke"
  k -n "$NS" exec t3code-0 -- sh -c 'curl -fsS -o /dev/null -w "http %{http_code}\n" http://127.0.0.1:3773/'
  k -n "$NS" cp "$WORK/merged/report.json" t3code-0:/tmp/report.json --retries=10
  k -n "$NS" exec -i t3code-0 -- python3 - /tmp/report.json <<'EOF'
import sqlite3, json, sys
c = sqlite3.connect("file:/home/spyros/.t3/userdata/state.sqlite?mode=ro", uri=True)
st = dict(c.execute("SELECT projector,last_applied_sequence FROM projection_state"))
top = c.execute("SELECT max(sequence) FROM orchestration_events").fetchone()[0]
merged = json.load(open(sys.argv[1]))["event_high_water"]
cleanup = st.pop("projection.attachment-cleanup")
assert set(st.values()) == {top}, st
assert cleanup >= merged, (cleanup, merged)
print("start: data projectors at", top, "attachment cleanup drained to", cleanup)
EOF
  k -n "$NS" exec t3code-0 -- sh -c 't3 connect status --base-dir /home/spyros/.t3 | sed -n 2,4p; curl -fsS -m 5 "$MCP_HUB_URL/healthz" >/dev/null && echo "mcp hub ok"'
  curl -sS -m 10 -k -o /dev/null -w "start: via traefik %{http_code}\n" -H "Host: t3new.epaflix.com" "https://$(k -n traefik-system get svc traefik-internal -o jsonpath='{.status.loadBalancer.ingress[0].ip}')/"
  log "start: resuming one thread per source/provider (this sends a real turn to each)"
  k -n "$NS" cp "$MIG/resume_smoke.mjs" t3code-0:/tmp/resume_smoke.mjs --retries=10
  k -n "$NS" exec t3code-0 -- bash -c '
    export T3_TOKEN=$(t3 auth session issue --base-dir /home/spyros/.t3 --ttl 30m --label cutover-smoke --token-only)
    python3 - <<EOF | while read -r inst tid; do echo "== $inst"; node /tmp/resume_smoke.mjs http://127.0.0.1:3773 "$tid" 2>&1 | tail -1; done
import sqlite3
c = sqlite3.connect("file:/home/spyros/.t3/userdata/state.sqlite?mode=ro", uri=True)
seen = {}
for tid, inst in c.execute("""SELECT t.thread_id, r.provider_instance_id FROM projection_threads t
    JOIN provider_session_runtime r ON r.thread_id=t.thread_id
    WHERE t.deleted_at IS NULL AND t.archived_at IS NULL AND r.resume_cursor_json IS NOT NULL ORDER BY t.updated_at DESC"""):
    seen.setdefault(inst, tid)
for inst, tid in seen.items(): print(inst, tid)
EOF
    rm -f /tmp/resume_smoke.mjs'
  log "start: every line above must end in 'result completed | RESUME-OK'. Then open https://t3new.epaflix.com and pair."
  mark start
}

phase_route() {
  marker route && { log "route: already done"; return; }
  marker start || die "route requires start"
  log "route: pointing t3code.epaflix.com at $NS/t3code"
  cat <<EOF | k apply -f -
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: t3code-https
  namespace: traefik-system
spec:
  entryPoints: [internal]
  routes:
    - match: Host(\`t3code.epaflix.com\`)
      kind: Rule
      services:
        - name: t3code
          namespace: $NS
          port: 3773
  tls:
    certResolver: cloudflare
    domains:
      - main: epaflix.com
        sans: ["*.epaflix.com"]
EOF
  k -n traefik-system delete endpointslice t3code service t3code --ignore-not-found
  log "route: commit the matching change to 05.traefik-deployment/ingress/t3code-proxy.yaml or ArgoCD will revert it"
  mark route
}

phase_rollback() {
  log "rollback: stopping $NS/t3code"
  k -n "$NS" scale statefulset t3code --replicas=0 || true
  log "rollback: starting host t3"
  ssh "$PVE_SSH" "pct exec $LXC_ID -- su - spyros -c 'systemctl --user enable --now t3code.service'"
  log "rollback: scaling $OLD_NS/t3env to 2"
  k -n "$OLD_NS" scale statefulset t3env --replicas=2
  k -n argocd patch application t3code-env --type merge -p '{"spec":{"syncPolicy":{"automated":{"selfHeal":true,"prune":true}}}}' || true
  rm -f "$WORK"/.done-{freeze,snapshot,merge,load,start,route}
  log "rollback: done. Merged data in $WORK and the $NS PVCs is untouched; wipe the PVCs before a new load."
}

phase_cleanup_helpers() {
  k -n "$OLD_NS" delete pod -l app.kubernetes.io/name=t3-cutover --ignore-not-found
  k -n "$NS" delete pod -l app.kubernetes.io/name=t3-cutover --ignore-not-found
}

case ${1:-} in
  freeze|snapshot|merge|load|start|route|rollback) "phase_$1" ;;
  cleanup-helpers) phase_cleanup_helpers ;;
  all) phase_freeze; phase_snapshot; phase_merge; phase_load; phase_start ;;
  *) sed -n '2,22p' "$0"; exit 2 ;;
esac
