#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

target=".kube/epaflix.kubeconfig"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "ERROR: kubectl is not on PATH, so there is no source kubeconfig to minify." >&2
  exit 1
fi

if ! kubectl config get-contexts -o name | grep -qx epaflix; then
  echo "ERROR: context epaflix not found in the source kubeconfig." >&2
  echo "       Any other context names it does hold are not printed (public repo, #856)." >&2
  exit 1
fi

mkdir -p .kube
umask 077
kubectl config view --minify --flatten --context epaflix >"$target"
chmod 600 "$target"

contexts="$(KUBECONFIG="$target" kubectl config get-contexts -o name 2>/dev/null || true)"
context_count="$(printf '%s\n' "$contexts" | grep -c . || true)"
cluster_count="$(KUBECONFIG="$target" kubectl config view \
  -o jsonpath='{range .clusters[*]}{.name}{"\n"}{end}' 2>/dev/null | grep -c . || true)"
user_count="$(KUBECONFIG="$target" kubectl config view \
  -o jsonpath='{range .users[*]}{.name}{"\n"}{end}' 2>/dev/null | grep -c . || true)"

bad=0
[ "$context_count" = "1" ] || bad=1
printf '%s\n' "$contexts" | grep -qx epaflix || bad=1
[ "$cluster_count" = "1" ] || bad=1
[ "$user_count" = "1" ] || bad=1

if [ "$bad" -ne 0 ]; then
  rm -f "$target"
  echo "ERROR: the generated kubeconfig failed verification and was deleted." >&2
  echo "       contexts=$context_count (expected exactly 1, named epaflix)," \
       "clusters=$cluster_count (expected 1), users=$user_count (expected 1)." >&2
  exit 1
fi

echo "Wrote $target (mode 600, git-ignored)."
echo "  verified: contexts=1 (epaflix), clusters=1, users=1"
echo
echo "Activate it. Interactive shells, with direnv installed (it is not installed"
echo "by default on this workstation):"
echo "  direnv allow    # .envrc exports KUBECONFIG to the file above"
echo
echo "Non-interactive shells (tool- and agent-emitted commands never run a direnv"
echo "hook) - add the prefix match to ~/.zshenv:"
echo '  case "$PWD" in /home/spy/Documents/Epaflix/k3s-swarm-proxmox*)'
echo '    export KUBECONFIG="/home/spy/Documents/Epaflix/k3s-swarm-proxmox/.kube/epaflix.kubeconfig" ;;'
echo '  esac'
echo
echo "Residual gap, stated not hidden: a shell where neither activation ran still"
echo "sees the full kubeconfig, so --context epaflix on the command itself stays"
echo "mandatory. check-kube-context.sh refuses the next commit from such a shell."
