#!/usr/bin/env bash
# Read cliproxy request payloads - the prompts and completions that stdout and
# Loki deliberately do NOT carry. See ../../README.md "Logs - two surfaces".
#
# Grafana answers "which call, when, status, how long". This answers "what was
# actually sent and returned", by reading the per-request files that
# `request-log: true` writes on the pod (#1007).
#
# Files live on an emptyDir, so a pod restart wipes them and the 256MB cap
# (#1011) trims oldest-first. This is a debugging surface, not an audit log.
#
#   list [N]        newest N payloads: request id, time, size, first user line
#   show <id>       readable transcript for one request id
#   raw <id> [out]  the whole payload file, unparsed
#
# The request id is the bracketed token in the Grafana access line:
#   [2026-08-17 20:53:21] [91def450] [info] ... 200 | POST "/v1/messages"
set -euo pipefail

CTX="${CLIPROXY_CONTEXT:-epaflix}"
NS="${CLIPROXY_NAMESPACE:-remote-pi}"
LOGS_DIR=/var/lib/cliproxy/logs

pod() {
  kubectl --context "$CTX" -n "$NS" get pod \
    -l app.kubernetes.io/name=cliproxy \
    -o jsonpath='{.items[0].metadata.name}'
}

# Read a file off the pod. `kubectl exec cat` rather than `kubectl cp`, which
# needs tar in the image - this one is distroless-ish and has no tar.
pod_cat() {
  kubectl --context "$CTX" -n "$NS" exec "$(pod)" -c cliproxy -- cat "$1"
}

resolve() {
  local id="$1" match
  case "$id" in */*|*..*) echo "refusing suspicious id: $id" >&2; exit 2 ;; esac
  match=$(kubectl --context "$CTX" -n "$NS" exec "$(pod)" -c cliproxy -- \
            sh -c "ls -t $LOGS_DIR/*-${id}.log 2>/dev/null | head -1")
  if [ -z "$match" ]; then
    echo "no payload for request id '$id'." >&2
    echo "It may have been trimmed by the 256MB cap, or lost to a pod restart." >&2
    exit 1
  fi
  printf '%s\n' "$match"
}

cmd_list() {
  local n="${1:-10}"
  kubectl --context "$CTX" -n "$NS" exec "$(pod)" -c cliproxy -- sh -c "
    for f in \$(ls -t $LOGS_DIR/*.log 2>/dev/null | head -$n); do
      id=\$(basename \"\$f\" .log); id=\${id##*-}
      size=\$(wc -c < \"\$f\")
      ts=\$(sed -n 's/^Timestamp: //p' \"\$f\" | head -1)
      printf '%s\t%s\t%sB\t%s\n' \"\$id\" \"\$ts\" \"\$size\" \"\$f\"
    done" | while IFS=$'\t' read -r id ts size path; do
      printf '%s  %s  %10s\n' "$id" "${ts:0:19}" "$size"
    done
}

cmd_show() {
  local id="${1:?usage: $0 show <request-id>}" path
  # Resolve first, on its own line. `pod_cat "$(resolve ...)"` would swallow
  # resolve's non-zero exit inside the substitution and cat an empty path.
  path=$(resolve "$id") || exit 1
  pod_cat "$path" | python3 "$(dirname "$0")/render-payload.py"
}

cmd_raw() {
  local id="${1:?usage: $0 raw <request-id> [outfile]}" out="${2:-}"
  local path; path=$(resolve "$id") || exit 1
  if [ -n "$out" ]; then
    pod_cat "$path" > "$out"
    echo "wrote $out ($(wc -c < "$out") bytes) from $path" >&2
  else
    pod_cat "$path"
  fi
}

case "${1:-list}" in
  list) shift || true; cmd_list "${1:-10}" ;;
  show) shift; cmd_show "$@" ;;
  raw)  shift; cmd_raw "$@" ;;
  *)    sed -n '2,20p' "$0"; exit 1 ;;
esac
