#!/usr/bin/env bash

: "${KUBECTL_CONTEXT:=epaflix}"
kubectl() { command kubectl --context "$KUBECTL_CONTEXT" "$@"; }

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v kubectl >/dev/null; then
  echo "kubectl not found." >&2
  exit 1
fi

if ! kubectl cluster-info >/dev/null 2>&1; then
  echo "Cannot reach the cluster. Check your kubeconfig." >&2
  exit 1
fi

echo "Applying kube-vip cloud provider (v0.0.12) + IP-pool ConfigMap from $SCRIPT_DIR ..."
kubectl apply -k "$SCRIPT_DIR"

echo ""
echo "Verify:"
echo "  kubectl get deploy kube-vip-cloud-provider -n kube-system"
echo "  kubectl get cm kubevip -n kube-system -o yaml"
echo ""
echo "Once ArgoCD is bootstrapped, apply 2-k3s/11.argocd/apps/app-kube-vip-cloud-provider.yaml"
echo "to bring this stack under GitOps (first sync is annotation-only)."
