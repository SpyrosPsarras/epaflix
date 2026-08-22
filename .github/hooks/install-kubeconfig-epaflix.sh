#!/usr/bin/env bash
# One-shot generator for a homelab-only KUBECONFIG (#856, option 1).
#
# ~/.kube/config is Syncthing-synced and carries work AKS contexts, including
# production ones, next to `epaflix`. A sync from another machine can change
# the active context under a running session, so `kubectl config use-context
# epaflix` is a value something else can overwrite, not a guard. This writes a
# kubeconfig that can see ONLY the `epaflix` context, so from a shell that
# exports it the other clusters are simply unreachable - which is why #856
# calls option 1 "the least code and the most protection".
#
# Product: .kube/epaflix.kubeconfig, mode 600, git-ignored. `kubectl config
# view --flatten` EMBEDS client-certificate-data and client-key-data, i.e. live
# cluster credentials, so this file must never be committed (.gitignore has
# /.kube/, which also enlists check-no-force-added-ignored.sh and the ci.yml
# "No tracked file matches .gitignore" step).
#
# This repo is public. Every diagnostic below is a COUNT plus the literal
# allowlisted name `epaflix`. Do not "helpfully" print the names of the
# contexts that were filtered out - #856 withholds them on purpose, and a
# denylist would have to name them.
#
# Activation is deliberately a separate, documented step: .envrc for
# interactive shells, a ~/.zshenv export for non-interactive ones (direnv hooks
# never fire in non-interactive shells, which is where tool-emitted kubectl
# runs happen). check-kube-context.sh is the forcing function that makes a
# missing activation loud at the next commit instead of silent.
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

# Verify the PRODUCT, not the exit status of the command that made it. A
# half-good kubeconfig is worse than none: it would pass activation and still
# expose another cluster. Anything unexpected here deletes the file.
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
