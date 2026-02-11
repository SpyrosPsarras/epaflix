#!/usr/bin/env bash
set -euo pipefail

# Install cert-manager via Helm (latest stable release)
# Provides automatic certificate management for Kubernetes

cd "$(dirname "$0")"

echo "Installing cert-manager via Helm..."

# Add Jetstack Helm repository
helm repo add jetstack https://charts.jetstack.io --force-update
helm repo update

# Install cert-manager with CRDs
# --replace flag allows overwriting stuck/failed installations
helm install cert-manager jetstack/cert-manager \
  --namespace cert-manager \
  --create-namespace \
  --set installCRDs=true \
  --replace

echo "Waiting for cert-manager to be ready..."
kubectl wait --for=condition=Available --timeout=300s \
  deployment/cert-manager \
  deployment/cert-manager-cainjector \
  deployment/cert-manager-webhook \
  -n cert-manager

echo ""
echo "✅ cert-manager installed successfully"
echo ""
echo "Applying issuers..."
kubectl apply -f issuers/

echo ""
echo "Waiting for CA certificate to be ready..."
kubectl wait --for=condition=Ready certificate/epavli-ca -n cert-manager --timeout=60s

echo ""
echo "Applying certificates..."
kubectl apply -f certificates/

echo ""
echo "Waiting for wildcard certificate to be ready..."
kubectl wait --for=condition=Ready certificate/epavli-wildcard-cert -n traefik-system --timeout=60s

echo ""
echo "✅ All certificates deployed successfully"
echo ""
echo "Verify with:"
echo "  kubectl get certificate -A"
echo "  kubectl get secret epavli-tls -n traefik-system"
echo ""
echo "To upgrade in the future:"
echo "  helm upgrade cert-manager jetstack/cert-manager -n cert-manager"
