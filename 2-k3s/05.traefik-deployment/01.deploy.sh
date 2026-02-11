#!/bin/bash
# Complete Traefik deployment script with Cloudflare DNS challenge and local k3s storage

set -e

echo "========================================"
echo "Traefik Deployment for *.epaflix.com"
echo "========================================"
echo ""

# Navigate to deployment directory
cd "$(dirname "$0")"

# Step 1: Create namespace
echo "[1/5] Creating traefik namespace..."
kubectl apply -f namespace.yaml

# Step 2: Create Cloudflare API token secret
echo "[2/5] Creating Cloudflare API token secret..."
kubectl create secret generic cloudflare-api-token \
  --namespace=traefik-system \
  --from-literal=api-token=<CLOUDFLARE_API_TOKEN> \
  --dry-run=client -o yaml | kubectl apply -f -

# Step 3: Deploy Traefik via Helmfile
echo "[3/5] Deploying Traefik (initially with 1 replica)..."
# Note: Helm chart requires 1 replica for initial ACME setup
# We'll scale to 2 after deployment
if command -v helmfile &> /dev/null; then
    helmfile sync
else
    echo "Helmfile not found, using Helm directly..."
    helm repo add traefik https://traefik.github.io/charts
    helm repo update
    helm upgrade --install traefik traefik/traefik \
      -n traefik-system \
      -f values/traefik-values.yaml \
      --wait
fi

# Step 4: Wait for LoadBalancer IP
echo "[4/5] Waiting for LoadBalancer IP assignment..."
kubectl -n traefik-system wait --for=condition=ready pod -l app.kubernetes.io/name=traefik --timeout=120s
TRAEFIK_IP=$(kubectl -n traefik-system get svc traefik -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
echo "Traefik LoadBalancer IP: $TRAEFIK_IP"

# Step 5: Apply middleware
echo "[5/5] Applying middleware..."
kubectl apply -f middleware/

echo ""
echo "========================================"
echo "Deployment Complete!"
echo "========================================"
echo ""
echo "Traefik is now running at: $TRAEFIK_IP"
echo "Current replicas: 1 (for initial ACME certificate setup)"
echo "Storage: Local k3s storage (local-path StorageClass)"
echo ""
echo "Next steps:"
echo "1. Configure your router to forward ports 80/443 to $TRAEFIK_IP"
echo "2. Add DNS record in Pi-hole: *.epaflix.com → $TRAEFIK_IP (or router public IP)"
echo "3. Wait ~2 minutes for Let's Encrypt certificate issuance"
echo "4. Once certificates are obtained, scale to 2 replicas:"
echo "   ./scale-to-2-replicas.sh"
echo ""
echo "To check certificate status:"
echo "  kubectl -n traefik-system logs -l app.kubernetes.io/name=traefik | grep -i acme"
echo ""
echo "To test:"
echo "  curl https://whoami.epaflix.com"
echo "  curl https://traefik.epaflix.com/dashboard/"
echo ""
