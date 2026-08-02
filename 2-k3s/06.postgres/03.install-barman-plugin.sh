#!/bin/bash
set -e

# Bootstrap-only. Day-to-day Barman Cloud Plugin lifecycle is now owned by
# ArgoCD Application "cnpg-operator" (2-k3s/11.argocd/apps/app-cnpg-operator.yaml),
# which renders operator-kustomization/barman-manifest.yaml via
# operator-kustomization/kustomization.yaml (issue #93). Run this script ONLY
# for the very first install on a fresh cluster before ArgoCD is up.

echo "======================================"
echo "Installing Barman Cloud Plugin (v0.14.0)"
echo "======================================"

# Requires: CNPG operator >= 1.26 (we run 1.30.0) and cert-manager
# (plugin uses a cert-manager Issuer/Certificate for its CNPG-i gRPC TLS).
echo "Applying vendored plugin manifest into cnpg-system..."
kubectl apply --server-side --force-conflicts \
  -f operator-kustomization/barman-manifest.yaml

echo "Waiting for plugin deployment to be ready..."
kubectl wait --for=condition=available --timeout=300s \
  deployment/barman-cloud -n cnpg-system

echo ""
echo "Verify:"
echo "  kubectl get pods -n cnpg-system | grep barman"
echo "  kubectl get crd | grep barmancloud"
