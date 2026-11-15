#!/usr/bin/env bash

: "${KUBECTL_CONTEXT:=epaflix}"
kubectl() { command kubectl --context "$KUBECTL_CONTEXT" "$@"; }

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
KEY_FILE="${KEY_FILE:-$HOME/.config/sops/age/k3s-cluster.txt}"
CHART_VERSION="$(sed -n 's/^ *version: "\(.*\)"$/\1/p' kustomization.yaml)"
[[ -n $CHART_VERSION ]] || { echo "no chart version in kustomization.yaml" >&2; exit 1; }

command -v openssl >/dev/null
kubectl apply -f namespace.yaml

kubectl -n argocd get secret sops-age >/dev/null 2>&1 ||
  kubectl -n argocd create secret generic sops-age --from-file=keys.txt="$KEY_FILE"
kubectl -n argocd get secret argocd-redis >/dev/null 2>&1 ||
  kubectl -n argocd create secret generic argocd-redis --from-file=auth=<(openssl rand -hex 32 | tr -d '\n')

# The kustomization renders no CRDs (crds.install: false), so they come from the same chart here.
helm template argocd argo-cd --repo https://argoproj.github.io/argo-helm \
  --version "$CHART_VERSION" --namespace argocd --set crds.install=true \
  -s templates/crds/crd-application.yaml \
  -s templates/crds/crd-applicationset.yaml \
  -s templates/crds/crd-appproject.yaml |
  kubectl apply --server-side -f -
kubectl wait --for=condition=Established --timeout=60s \
  crd/applications.argoproj.io crd/applicationsets.argoproj.io crd/appprojects.argoproj.io

kustomize build --enable-helm . | kubectl apply -f -
kubectl -n argocd rollout status deploy/argocd-repo-server --timeout=5m

kubectl apply -k apps
