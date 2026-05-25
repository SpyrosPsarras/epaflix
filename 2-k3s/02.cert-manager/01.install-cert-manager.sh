#!/usr/bin/env bash
set -euo pipefail

# Bootstrap-only install for cert-manager (jetstack chart).
#
# Day-to-day cert-manager lifecycle is owned by ArgoCD Application "cert-manager"
# (2-k3s/11.argocd/apps/app-cert-manager.yaml), which kustomize-with-helm renders
# the same jetstack chart from this directory's `kustomization.yaml` +
# `values/cert-manager-values.yaml`.
#
# Run this script ONLY for the very first install (fresh cluster, before ArgoCD
# is up) — it installs the CRDs and the operator imperatively so ArgoCD can
# then adopt the ClusterIssuers without a chicken-and-egg.

cd "$(dirname "$0")"

echo "Installing cert-manager via Helm (bootstrap-only)..."

helm repo add jetstack https://charts.jetstack.io --force-update
helm repo update

helm upgrade --install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --version v1.19.3 \
  --set crds.enabled=true \
  --set crds.keep=true

echo "Waiting for cert-manager to be ready..."
kubectl wait --for=condition=Available --timeout=300s \
  deployment/cert-manager \
  deployment/cert-manager-cainjector \
  deployment/cert-manager-webhook \
  -n cert-manager

echo ""
echo "✅ cert-manager installed."
echo ""
echo "Next: create the Cloudflare API token and ACME account-key Secrets:"
echo "  kubectl -n cert-manager create secret generic cloudflare-api-token \\"
echo "    --from-literal=api-token='<CLOUDFLARE_API_TOKEN>'"
echo ""
echo "Then either:"
echo "  - Apply issuers/* directly:  kubectl apply -f issuers/"
echo "  - Or let ArgoCD adopt them:  kubectl apply -f ../11.argocd/apps/app-cert-manager.yaml"
echo ""
echo "Verify:"
echo "  kubectl get clusterissuer"
echo "  kubectl get certificate -A"
