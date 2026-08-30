#!/bin/bash

: "${KUBECTL_CONTEXT:=epaflix}"
kubectl() { command kubectl --context "$KUBECTL_CONTEXT" "$@"; }

set -e

cd "$(dirname "$0")"

kubectl apply -f namespace.yaml
sops -d cloudflared-credentials.enc.yaml | kubectl apply -f -
kubectl apply -f configmap.yaml
kubectl apply -f deployment.yaml
kubectl -n cloudflare-tunnel rollout status deployment/cloudflared --timeout=120s
kubectl -n cloudflare-tunnel get pods -o wide
