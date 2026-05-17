#!/usr/bin/env bash
# Install / upgrade argocd-image-updater on the k3s cluster.
#
# Prereq:
#   - argocd installed (run ../install.sh first)
#   - git-creds Secret created (see git-creds-secret.yaml; needs real PAT)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_VERSION="1.2.1"   # app v1.2.0

if ! kubectl -n argocd get secret git-creds >/dev/null 2>&1; then
  echo "!!  argocd/git-creds secret not found." >&2
  echo "    Create it from git-creds-secret.yaml (with a real GitHub PAT)" >&2
  echo "    BEFORE running this script. Example:" >&2
  echo "        kubectl -n argocd create secret generic git-creds \\" >&2
  echo "          --from-literal=username=git \\" >&2
  echo "          --from-literal=password=<GITHUB_PAT_FROM_secrets.yml>" >&2
  exit 1
fi

echo ">>> helm upgrade --install argocd-image-updater (chart ${CHART_VERSION})"
helm repo add argo https://argoproj.github.io/argo-helm 2>/dev/null || true
helm repo update argo
helm upgrade --install argocd-image-updater argo/argocd-image-updater \
  --namespace argocd \
  --version "${CHART_VERSION}" \
  -f "${SCRIPT_DIR}/values.yaml" \
  --wait --timeout 3m

echo ">>> done. Tail logs with:"
echo "      kubectl -n argocd logs -f deploy/argocd-image-updater"
