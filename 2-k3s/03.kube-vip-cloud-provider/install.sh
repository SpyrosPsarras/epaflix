#!/usr/bin/env bash
# kube-vip Cloud Provider — bootstrap helper.
#
# Since 2026-05-25 (Issue #16) this stack is GitOps-managed by ArgoCD via
# `2-k3s/11.argocd/apps/app-kube-vip-cloud-provider.yaml`. The cloud-controller
# manifest and the `kubevip` IP-pool ConfigMap both live in this directory
# (kustomization.yaml). Day-to-day changes go through git → ArgoCD sync.
#
# This script remains only for fresh-cluster bootstrap (before ArgoCD itself
# is up). It applies the same kustomization that the Application later adopts,
# so the first sync is a no-op (only the tracking-id annotation is added).
#
# Bumping kube-vip-cloud-provider: replace `cloud-controller.yaml` with the
# upstream file at the new tag, render with `kubectl kustomize .`, confirm
# `kubectl diff` is empty against any unrelated drift, commit, let ArgoCD sync.
#
# Pinned version: v0.0.12 (see cloud-controller.yaml header).

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
