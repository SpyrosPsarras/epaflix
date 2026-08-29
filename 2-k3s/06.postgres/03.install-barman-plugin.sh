#!/bin/bash
set -e

: "${KUBECTL_CONTEXT:=epaflix}"
kubectl() { command kubectl --context "$KUBECTL_CONTEXT" "$@"; }


echo "======================================"
echo "Installing Barman Cloud Plugin (v0.14.0)"
echo "======================================"

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
