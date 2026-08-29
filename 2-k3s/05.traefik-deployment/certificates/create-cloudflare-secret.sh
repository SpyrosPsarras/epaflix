#!/bin/bash

: "${KUBECTL_CONTEXT:=epaflix}"
kubectl() { command kubectl --context "$KUBECTL_CONTEXT" "$@"; }

kubectl create secret generic cloudflare-api-token \
  --namespace=traefik-system \
  --from-literal=api-token=<CLOUDFLARE_API_TOKEN>

echo "Cloudflare API token secret created successfully!"
echo "Verify with: kubectl -n traefik-system get secret cloudflare-api-token"
