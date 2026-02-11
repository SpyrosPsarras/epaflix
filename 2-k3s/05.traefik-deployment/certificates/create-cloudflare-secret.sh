#!/bin/bash
# Create Cloudflare API token secret for Traefik DNS challenge

kubectl create secret generic cloudflare-api-token \
  --namespace=traefik-system \
  --from-literal=api-token=<CLOUDFLARE_API_TOKEN>

echo "Cloudflare API token secret created successfully!"
echo "Verify with: kubectl -n traefik-system get secret cloudflare-api-token"
