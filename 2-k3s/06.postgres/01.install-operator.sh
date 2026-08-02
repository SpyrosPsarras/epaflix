#!/bin/bash
set -e

# Bootstrap-only. Day-to-day CNPG operator lifecycle is now owned by ArgoCD
# Application "cnpg-operator" (2-k3s/11.argocd/apps/app-cnpg-operator.yaml),
# which renders operator-kustomization/cnpg-operator.yaml via
# operator-kustomization/kustomization.yaml (issue #93). Run this script ONLY
# for the very first install on a fresh cluster before ArgoCD is up.

echo "======================================"
echo "Installing CloudNativePG Operator"
echo "======================================"

# Create namespace
echo "Creating postgres-system namespace..."
kubectl apply -f namespace.yaml

# Install CloudNativePG operator
echo "Installing CloudNativePG operator v1.30.0..."
kubectl apply --server-side --force-conflicts -f operator-kustomization/cnpg-operator.yaml

# Wait for operator to be ready
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
