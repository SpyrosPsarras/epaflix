#!/bin/bash
set -euo pipefail

echo "======================================"
echo "  FileBrowser Quantum Deployment"
echo "======================================"
echo ""

# Check if we're in the right directory
if [[ ! -f "namespace.yaml" ]]; then
    echo "Error: Run this script from the 09.filebrowser directory"
    exit 1
fi

# Step 1: Create namespace
echo "[1/7] Creating namespace..."
kubectl apply -f namespace.yaml

# Step 2: Verify NFS mount exists on nodes
echo "[2/7] Verifying NFS mounts on nodes..."
echo "FileBrowser requires these NFS mounts (should already exist from Servarr setup):"
echo "  - /mnt/k3s-animes (uid:gid 568:568)"
echo "  - /mnt/k3s-movies (uid:gid 568:568)"
echo "  - /mnt/k3s-tvshows (uid:gid 568:568)"
echo "  - /mnt/k3s-downloads (uid:gid 568:568)"
echo ""
echo "Press Enter when ready to continue..."
read -r

# Step 3: Create storage resources
echo "[3/7] Creating storage resources..."
kubectl apply -f storage/

# Step 4: Create ConfigMap
echo "[4/7] Creating FileBrowser ConfigMap..."
kubectl apply -f configmap.yaml

# Step 5: Create OIDC secret
echo "[5/7] Setting up OIDC secret..."
echo ""
echo "Before continuing, create an OAuth2/OIDC provider in Authentik:"
echo "  1. Go to https://auth.epaflix.com/if/admin/#/core/providers"
echo "  2. Create new OAuth2/OpenID Provider"
echo "  3. Name: FileBrowser"
echo "  4. Set a Client ID (recommended: filebrowser)"
echo "  5. Redirect URIs: https://filebrowser.epaflix.com/api/auth/oidc/callback"
echo "  6. Signing Key: Select authentik Self-signed Certificate"
echo "  7. Copy the Client ID and Client Secret"
echo ""
read -rp "Enter Authentik Client ID [filebrowser]: " CLIENT_ID
CLIENT_ID=${CLIENT_ID:-filebrowser}

read -rp "Enter Authentik Client Secret: " CLIENT_SECRET

if [[ -z "$CLIENT_SECRET" ]]; then
    echo "Error: Client secret cannot be empty"
    exit 1
fi

kubectl create secret generic filebrowser-oidc \
    -n filebrowser \
    --from-literal=client-id="$CLIENT_ID" \
    --from-literal=client-secret="$CLIENT_SECRET" \
    --dry-run=client -o yaml | kubectl apply -f -

echo "Secret created successfully"

# Step 6: Deploy FileBrowser
echo "[6/7] Deploying FileBrowser..."
kubectl apply -f deployment.yaml
kubectl apply -f service.yaml
kubectl apply -f ingress.yaml

# Step 7: Wait for pods to be ready
echo "[7/7] Waiting for FileBrowser to be ready..."
kubectl wait --for=condition=available --timeout=300s deployment/filebrowser -n filebrowser

echo ""
echo "======================================"
echo "  Deployment Complete!"
echo "======================================"
echo ""
echo "FileBrowser is now available at: https://filebrowser.epaflix.com"
echo ""
echo "Next steps:"
echo "  1. Create 'filebrowser-admins' group in Authentik if not exists"
echo "  2. Add users to the group for admin access"
echo "  3. Configure access rules in FileBrowser for group-based permissions"
echo ""
echo "Useful commands:"
echo "  kubectl get pods -n filebrowser"
echo "  kubectl logs -f deployment/filebrowser -n filebrowser"
echo "  kubectl describe ingress filebrowser -n filebrowser"
echo ""
