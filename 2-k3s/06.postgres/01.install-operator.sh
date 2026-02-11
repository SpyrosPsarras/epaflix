#!/bin/bash
set -e

echo "======================================"
echo "Installing CloudNativePG Operator"
echo "======================================"

# Create namespace
echo "Creating postgres-system namespace..."
kubectl apply -f namespace.yaml

# Install CloudNativePG operator
echo "Installing CloudNativePG operator v1.28.0..."
kubectl apply --server-side --force-conflicts -f operator/cnpg-operator.yaml

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
