#!/usr/bin/env bash
# t3-run <image> [-- cmd...]: run a one-shot pod in the t3-build namespace with
# stdin and stdout attached; the pod is deleted when the command exits.
set -euo pipefail
kubeconfig=${T3_BUILD_KUBECONFIG:-/run/t3-build/kubeconfig}
if [[ ! -f $kubeconfig ]]; then
  echo "t3-run: no $kubeconfig; the owner runs 2-k3s/29.t3-build/tools/build-kubeconfig.py and restarts the pod (see 2-k3s/29.t3-build/README.md)." >&2
  exit 1
fi
usage() { echo 'usage: t3-run <image> [-- cmd...]' >&2; exit 2; }
[[ $# -ge 1 && $1 != -* ]] || usage
image=$1
shift
command=()
if [[ $# -gt 0 ]]; then
  [[ $1 == -- ]] || usage
  shift
  command=(-- "$@")
fi
tty=()
if [[ -t 0 && -t 1 ]]; then tty=(-t); fi
exec kubectl --kubeconfig "$kubeconfig" -n t3-build run "t3-run-$(date +%s)-$RANDOM" \
  --rm -i "${tty[@]}" -q --restart=Never --image="$image" \
  --overrides='{"spec":{"automountServiceAccountToken":false,"enableServiceLinks":false}}' \
  "${command[@]}"
