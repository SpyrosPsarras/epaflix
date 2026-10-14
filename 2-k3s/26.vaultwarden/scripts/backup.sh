#!/usr/bin/env bash
# Runs in the scheduled Job. Only this job may scale the writer during capture.
set -euo pipefail
umask 077
export PATH="/tools:$PATH"
# fsGroup makes the mount writable, but it remains root-owned. Only chmod our
# own child, never the emptyDir mount itself.
WORK_DIR=${WORK_ROOT:-/work}/private
DATA_DIR=${DATA_DIR:-/data}
mkdir -p "$WORK_DIR"
chmod 700 "$WORK_DIR"
export HOME="$WORK_DIR" TMPDIR="$WORK_DIR" RESTIC_CACHE_DIR="$WORK_DIR/cache"
cd "$WORK_DIR"
# Explicit in-cluster connection, with tokenFile so credentials never enter argv.
# Do not depend on kubectl's implicit fallback, which selected localhost here.
export KUBECONFIG="$WORK_DIR/kubeconfig"
cat > "$KUBECONFIG" <<EOF
apiVersion: v1
kind: Config
clusters:
- name: local
  cluster:
    server: https://${KUBERNETES_SERVICE_HOST:?}:${KUBERNETES_SERVICE_PORT:?}
    certificate-authority: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
users:
- name: backup
  user:
    tokenFile: /var/run/secrets/kubernetes.io/serviceaccount/token
contexts:
- name: backup
  context:
    cluster: local
    user: backup
    namespace: vaultwarden
current-context: backup
EOF
paused=false
step=preflight
child=
bounded() {
  local limit=$1 result
  shift
  timeout --kill-after=1s "$limit" "$@" &
  child=$!
  if wait "$child"; then result=0; else result=$?; fi
  child=
  return "$result"
}
kube() { bounded 10s kubectl --request-timeout=5s -n vaultwarden "$@"; }
terminate() {
  if [[ -n $child ]]; then
    kill -TERM "$child" 2>/dev/null || true
    wait "$child" || true
  fi
  exit 143
}
resume() {
  local attempt
  # At most 3 * 2 * (10s + 1s kill grace) + 2 * 2s = 70s.
  # This fits the pod's 120s termination grace even after cancelling capture.
  for attempt in 1 2 3; do
    if kube scale deployment/vaultwarden --replicas=1 >/dev/null &&
       kube annotate deployment/vaultwarden vaultwarden.epaflix.com/backup-paused- >/dev/null; then
      return 0
    fi
    if (( attempt < 3 )); then sleep 2; fi
  done
  return 1
}
cleanup() {
  result=$?
  trap - EXIT
  trap '' TERM INT
  if "$paused"; then
    if ! resume; then
      printf 'ERROR: writer resume failed; operator must resume deployment/vaultwarden\n' >&2
      result=1
    fi
  fi
  rm -rf -- backup-set
  if (( result != 0 )); then
    printf 'ERROR: backup failed at %s; no retention performed after capture/upload failure\n' "$step" >&2
  fi
  exit "$result"
}
trap cleanup EXIT
trap terminate TERM INT
# The CronJob has concurrencyPolicy=Forbid. Do not run manual jobs concurrently.
# A stale marker needs operator review. Automatically resuming might interrupt
# another manually started capture, or hide the cause of a killed job.
marker=$(kube get deployment/vaultwarden -o 'jsonpath={.metadata.annotations.vaultwarden\.epaflix\.com/backup-paused}')
if [[ -n $marker ]]; then
  printf 'ERROR: unresolved backup pause; inspect running Jobs before resuming writer\n' >&2
  exit 1
fi
replicas=$(kube get deployment/vaultwarden -o 'jsonpath={.spec.replicas}')
[[ $replicas == 1 ]] || { printf 'ERROR: expected exactly one writer\n' >&2; exit 1; }
rm -rf -- backup-set
mkdir -m 700 backup-set
step=pause
kube annotate deployment/vaultwarden "vaultwarden.epaflix.com/backup-paused=$(date -u +%s)" --overwrite >/dev/null
paused=true
kube scale deployment/vaultwarden --replicas=0 >/dev/null
# Wait for deletion, not readiness: a terminating server can still finish writes.
stop_deadline=$((SECONDS + 240))
while (( SECONDS < stop_deadline )); do
  pods=$(kube get pods -l app=vaultwarden -o name)
  [[ -z $pods ]] && break
  sleep 2
done
[[ -z $pods ]] || { printf 'ERROR: writer did not stop\n' >&2; exit 1; }
date -u +%FT%TZ > backup-set/recovery-point.txt
step=database-dump
# Stderr can include connection details. Report only a constant stage on failure.
bounded "${CAPTURE_TIMEOUT_SECONDS:-300}s" pg_dump --format=custom --no-owner --no-privileges --file=backup-set/database.dump 2>/dev/null
step=file-copy
bounded "${CAPTURE_TIMEOUT_SECONDS:-300}s" cp -a "$DATA_DIR" backup-set/data
printf '%s\n' "${SERVER_IMAGE:?SERVER_IMAGE required}" > backup-set/server-image.txt
step=resume
if ! resume; then
  # The bounded retry budget is exhausted; do not repeat it in the EXIT trap.
  paused=false
  printf 'ERROR: writer resume failed; operator must resume deployment/vaultwarden\n' >&2
  exit 1
fi
paused=false
# All state, including RSA keys, attachments, Sends and config.json, is included.
step=upload
restic backup "$WORK_DIR/backup-set" --host vaultwarden --tag matched-set --quiet
printf 'Matched backup uploaded; recovery point %s\n' "$(< backup-set/recovery-point.txt)"
restic snapshots --host vaultwarden --tag matched-set --latest 1 --compact
# One job owns backup AND weekly maintenance, so repository operations serialize.
if [[ $(date -u +%u) == 7 ]]; then
  step=integrity-check
  restic check --read-data
  step=retention
  restic forget --host vaultwarden --tag matched-set --group-by host,paths,tags \
    --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune --quiet
fi
