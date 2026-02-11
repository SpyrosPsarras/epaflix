#!/bin/bash
# Main Deployment Script for Servarr Ecosystem
# This script deploys the complete stack in the correct order

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SHARED_DIR="$(dirname "$SCRIPT_DIR")"
BASE_DIR="$(dirname "$SHARED_DIR")"

echo "========================================"
echo "Servarr Ecosystem Deployment"
echo "========================================"
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Helper functions
info() {
    echo -e "${GREEN}✓${NC} $1"
}

warn() {
    echo -e "${YELLOW}⚠${NC} $1"
}

error() {
    echo -e "${RED}✗${NC} $1"
}

wait_for_pods() {
    local namespace=$1
    local label=$2
    local timeout=${3:-300}

    echo "Waiting for pods with label $label to be ready..."
    kubectl wait --for=condition=ready pod -l "$label" -n "$namespace" --timeout="${timeout}s" || true
}

# Check prerequisites
echo "Checking prerequisites..."
echo ""

if ! kubectl cluster-info &> /dev/null; then
    error "kubectl not configured or cluster not reachable"
    exit 1
fi
info "kubectl configured"

if ! kubectl get storageclass local-path &> /dev/null; then
    warn "local-path storage class not found"
fi

if ! kubectl get storageclass manual-nfs &> /dev/null; then
    warn "manual-nfs storage class not found - you may need to create it"
fi

echo ""

# Step 1: Create namespace
echo "Step 1: Creating namespace..."
kubectl apply -f "$BASE_DIR/namespace.yaml"
info "Namespace created"
echo ""

# Step 2: Create secrets
echo "Step 2: Creating secrets..."
echo ""

if [ -f "$SHARED_DIR/secrets/postgres-secret-generated.yaml" ]; then
    kubectl apply -f "$SHARED_DIR/secrets/postgres-secret-generated.yaml"
    info "PostgreSQL secret applied"
else
    warn "PostgreSQL secret not found. Run _shared/scripts/01-setup-postgres.sh first"
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

if [ -f "$SHARED_DIR/secrets/wireguard-secret-generated.yaml" ]; then
    kubectl apply -f "$SHARED_DIR/secrets/wireguard-secret-generated.yaml"
    info "WireGuard secret applied"
else
    warn "WireGuard secret not found. Create it from secrets.yml values using _shared/secrets/wireguard-secret.yaml"
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

echo ""

# Step 3: Create storage
echo "Step 3: Creating storage (PV/PVC)..."
kubectl apply -f "$SHARED_DIR/storage/"
info "Storage resources created"

echo "Waiting for PVCs to bind..."
sleep 5
kubectl get pvc -n servarr
echo ""

read -p "Continue with app deployment? (Y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Nn]$ ]]; then
    exit 0
fi
echo ""

# Step 4: Deploy core infrastructure apps
echo "Step 4: Deploying core infrastructure..."
kubectl apply -f "$BASE_DIR/prowlarr/prowlarr.yaml"
kubectl apply -f "$BASE_DIR/flaresolverr/flaresolverr.yaml"
info "Prowlarr and FlareSolverr deployed"

wait_for_pods servarr "app=prowlarr" 180
wait_for_pods servarr "app=flaresolverr" 180
echo ""

# Step 5: Deploy download client
echo "Step 5: Deploying qBittorrent with VPN..."
kubectl apply -f "$BASE_DIR/qbittorrent/qbittorrent.yaml"
info "qBittorrent deployed"

warn "Verifying VPN connection..."
echo "Waiting for qBittorrent to be ready..."
sleep 30
wait_for_pods servarr "app=qbittorrent" 180

echo "Checking VPN IP..."
VPN_IP=$(kubectl exec -n servarr deployment/qbittorrent -- curl -s ifconfig.me 2>/dev/null || echo "FAILED")
if [[ $VPN_IP == 192.168.* ]]; then
    error "VPN NOT WORKING! IP is $VPN_IP (local network)"
    warn "Check WireGuard configuration"
else
    info "VPN working! External IP: $VPN_IP"
fi
echo ""

# Step 6: Deploy *arr apps
echo "Step 6: Deploying *arr applications..."
kubectl apply -f "$BASE_DIR/sonarr/sonarr.yaml"
kubectl apply -f "$BASE_DIR/sonarr2/sonarr2.yaml"
kubectl apply -f "$BASE_DIR/radarr/radarr.yaml"
kubectl apply -f "$BASE_DIR/bazarr/bazarr.yaml"
info "*arr apps deployed"

wait_for_pods servarr "app in (sonarr,sonarr2,radarr,bazarr)" 300
echo ""

# Step 7: Deploy media apps
echo "Step 7: Deploying media applications..."
kubectl apply -f "$BASE_DIR/jellyfin/jellyfin.yaml"
# Apply TrueNAS redirect (routes jellyfin.epaflix.com to 192.168.10.200:30013)
kubectl apply -f "$BASE_DIR/jellyfin/jellyfin-truenas-redirect.yaml"
kubectl apply -f "$BASE_DIR/jellyseerr/jellyseerr.yaml"
info "Jellyfin (TrueNAS redirect) and Jellyseerr deployed"

wait_for_pods servarr "app in (jellyfin,jellyseerr)" 300
echo ""

# Step 8: Deploy utility apps
echo "Step 8: Deploying utility applications..."
kubectl apply -f "$BASE_DIR/homarr/"
kubectl apply -f "$BASE_DIR/wizarr/"
info "Homarr and Wizarr deployed"

wait_for_pods servarr "app in (homarr,wizarr)" 300
echo ""

# Step 9: Deploy ingress routes
echo "Step 9: Deploying Traefik IngressRoutes..."
kubectl apply -f "$SHARED_DIR/ingress/"
info "IngressRoutes deployed"
echo ""

# Summary
echo "========================================"
echo "Deployment Complete!"
echo "========================================"
echo ""

kubectl get pods -n servarr
echo ""

echo "Access URLs:"
echo ""
echo "Public (Internet):"
echo "  Jellyfin:    https://jellyfin.epaflix.com"
echo "  Jellyseerr:  https://jellyseerr.epaflix.com"
echo ""
echo "Internal (LAN - *.epaflix.com):"
echo "  Sonarr:      http://sonarr.epaflix.com"
echo "  Sonarr2:     http://sonarr2.epaflix.com"
echo "  Radarr:      http://radarr.epaflix.com"
echo "  Prowlarr:    http://prowlarr.epaflix.com"
echo "  Bazarr:      http://bazarr.epaflix.com"
echo "  qBittorrent: http://qbittorrent.epaflix.com"
echo "  Tdarr:       http://tdarr.epaflix.com"
echo "  Homarr:      http://homarr.epaflix.com"
echo "  Wizarr:      http://wizarr.epaflix.com"
echo ""
echo "Next steps:"
echo "1. Configure download clients in *arr apps"
echo "2. Configure Prowlarr sync with Sonarr/Radarr"
echo "3. Configure Jellyfin GPU transcoding"
echo "4. Verify hardlinks are working"
echo ""
echo "See README.md for detailed post-deployment configuration"
echo ""
