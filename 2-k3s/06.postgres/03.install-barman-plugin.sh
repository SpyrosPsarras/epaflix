#!/bin/bash
set -e

echo "======================================"
echo "Installing Barman Cloud Plugin (v0.12.0)"
echo "======================================"

# Requires: CNPG operator >= 1.26 (we run 1.28.0) and cert-manager
# (plugin uses a cert-manager Issuer/Certificate for its CNPG-i gRPC TLS).
echo "Applying vendored plugin manifest into cnpg-system..."
kubectl apply --server-side --force-conflicts \
  -f barman-cloud-plugin/manifest.yaml

echo "Waiting for plugin deployment to be ready..."
kubectl wait --for=condition=available --timeout=300s \
  deployment/barman-cloud -n cnpg-system

echo ""
echo "Verify:"
echo "  kubectl get pods -n cnpg-system | grep barman"
echo "  kubectl get crd | grep barmancloud"
