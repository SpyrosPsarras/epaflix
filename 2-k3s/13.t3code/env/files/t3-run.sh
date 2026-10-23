#!/usr/bin/env bash
# t3-run <image> [-- cmd...]: run a one-shot pod in this pod's builder namespace
# (the kubeconfig's context) with stdin and stdout attached. The pod is deleted
# when the command exits or t3-run is interrupted.
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
name=t3-run-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
child=
cleanup() {
  [[ -z $child ]] || kill "$child" 2>/dev/null || true
  kubectl --kubeconfig "$kubeconfig" delete pod "$name" --ignore-not-found --wait=false >/dev/null 2>&1 ||
    echo "t3-run: could not delete pod $name" >&2
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# In the background so the traps fire while kubectl waits; <&0 keeps stdin attached.
kubectl --kubeconfig "$kubeconfig" run "$name" \
  --rm -i "${tty[@]}" -q --restart=Never --image="$image" \
  --overrides='{"spec":{"automountServiceAccountToken":false,"enableServiceLinks":false}}' \
  "${command[@]}" <&0 &
child=$!
status=0
wait "$child" || status=$?
child=
exit "$status"
