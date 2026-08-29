#!/bin/bash
set -e

: "${KUBECTL_CONTEXT:=epaflix}"
kubectl() { command kubectl --context "$KUBECTL_CONTEXT" "$@"; }


echo "======================================"
echo "Installing CloudNativePG Operator"
echo "======================================"

echo "Creating postgres-system namespace..."
kubectl apply -f namespace.yaml

echo "Installing CloudNativePG operator v1.30.0..."
kubectl apply --server-side --force-conflicts -f operator-kustomization/cnpg-operator.yaml

echo "Waiting for operator deployment to be ready..."
kubectl wait --for=condition=available --timeout=300s \
  deployment/cnpg-controller-manager -n cnpg-system

echo ""
echo "======================================"
echo "CloudNativePG Operator installed successfully!"
echo "======================================"
echo ""
echo "Verify installation:"
echo "  kubectl get pods -n cnpg-system"
echo "  kubectl get crd | grep postgresql"
echo ""
