#!/usr/bin/env bash
set -euo pipefail

if ! command -v kubectl >/dev/null 2>&1; then
  echo "SKIP: kubectl is not on PATH, so this shell cannot reach any cluster."
  exit 0
fi

contexts="$(kubectl config get-contexts -o name 2>/dev/null || true)"
count="$(printf '%s\n' "$contexts" | grep -c . || true)"

if [ "$count" = "1" ] && printf '%s\n' "$contexts" | grep -qx epaflix; then
  echo "kube context guard: resolved kubeconfig exposes 1 context (epaflix)."
  exit 0
fi

echo "ERROR: resolved kubeconfig exposes $count context(s); expected exactly 1 (epaflix)." >&2
if [ "$count" = "1" ]; then
  echo "       The one context it exposes is not epaflix (name withheld: public repo, #856)." >&2
fi
echo "       A wrong context here reaches clusters owned by another team, so this" >&2
echo "       commit is refused rather than made from a shell that can hit them." >&2
echo "       Generate and export the homelab-only kubeconfig:" >&2
echo '         ./.github/hooks/install-kubeconfig-epaflix.sh' >&2
echo '         export KUBECONFIG="$(git rev-parse --show-toplevel)/.kube/epaflix.kubeconfig"' >&2
exit 1
