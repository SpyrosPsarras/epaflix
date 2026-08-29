#!/bin/bash

# Every kubectl call in this script runs against the homelab cluster, whatever
# the ambient kubeconfig says (issue #971). Override with KUBECTL_CONTEXT=... .
: "${KUBECTL_CONTEXT:=epaflix}"
kubectl() { command kubectl --context "$KUBECTL_CONTEXT" "$@"; }
# Create Cloudflare API token secret for Traefik DNS challenge

kubectl create secret generic cloudflare-api-token \
  --namespace=traefik-system \
  --from-literal=api-token=<CLOUDFLARE_API_TOKEN>

echo "Cloudflare API token secret created successfully!"
echo "Verify with: kubectl -n traefik-system get secret cloudflare-api-token"
