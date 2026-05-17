#!/usr/bin/env bash
# Install / upgrade argocd-image-updater on the k3s cluster.
#
# Prereq:
#   - argocd installed (run ../install.sh first)
#   - git-creds Secret created (see git-creds-secret.yaml; needs real PAT)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHART_VERSION="0.14.0"   # app v0.17.0
#
# NOTE: We deliberately pin to the pre-operator (annotation-based) release.
# Chart 1.x ships argocd-image-updater v1.x which switched to a CRD-driven
# `ImageUpdater` operator model and at v1.1.x/1.2.x has a registry-prefix
# normalization bug that prevents matching `lscr.io/linuxserver/*` against
# live application images. v0.17.0 reads the legacy
# `argocd-image-updater.argoproj.io/*` annotations on the ArgoCD Application
# itself (see ../apps/app-servarr.yaml).

if ! kubectl -n argocd get secret git-creds >/dev/null 2>&1; then
  echo "!!  argocd/git-creds secret not found (or repo not registered as" >&2
  echo "    ArgoCD repository with credentials)." >&2
  echo "    Image-updater discovers Git creds from ArgoCD's repository" >&2
  echo "    registration — create a Secret labelled" >&2
  echo "    argocd.argoproj.io/secret-type=repository with type=git, url=,"  >&2
  echo "    username=git, password=<PAT>. See QUICKSTART step 6." >&2
fi

if ! kubectl -n argocd get secret argocd-image-updater-secret >/dev/null 2>&1; then
  echo "!!  argocd-image-updater-secret not found." >&2
  echo "    Image-updater needs an ArgoCD API token. Create one with:" >&2
  echo "      ADMIN_PW=\$(kubectl -n argocd get secret argocd-initial-admin-secret \\" >&2
  echo "                  -o jsonpath='{.data.password}' | base64 -d)" >&2
  echo "      TOKEN=\$(kubectl -n argocd exec deploy/argocd-server -- sh -c \\" >&2
  echo "        \"argocd login argocd-server.argocd.svc.cluster.local:443 \\" >&2
  echo "         --username admin --password '\$ADMIN_PW' --insecure --plaintext \\" >&2
  echo "         --grpc-web >/dev/null && argocd account generate-token \\" >&2
  echo "         --account image-updater\")" >&2
  echo "      kubectl -n argocd create secret generic argocd-image-updater-secret \\" >&2
  echo "        --from-literal=argocd.token=\"\$TOKEN\"" >&2
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
