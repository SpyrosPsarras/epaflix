#!/bin/bash
set -e

echo "=========================================="
echo "Authentik Helm Upgrade"
echo "=========================================="
echo ""

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Check current version
echo -e "${YELLOW}Current deployment:${NC}"
helm list -n app-authentik
echo ""

# Show current image version
CURRENT_IMAGE=$(kubectl -n app-authentik get deployment authentik-server -o jsonpath='{.spec.template.spec.containers[0].image}')
echo "Current image: $CURRENT_IMAGE"
echo ""

# Update Helm repository
echo -e "${YELLOW}Step 1: Updating Helm repository...${NC}"
helm repo update authentik
echo -e "${GREEN}✓ Repository updated${NC}"
echo ""

# Show available versions
echo "Available chart versions:"
helm search repo authentik/authentik --versions | head -n 10
echo ""

# Option to specify version
read -p "Enter chart version to upgrade to (or press Enter for latest): " CHART_VERSION
echo ""

# Prepare upgrade command
UPGRADE_CMD="helm upgrade authentik authentik/authentik --namespace app-authentik --values helm-values.yaml --wait --timeout 10m"
if [ -n "$CHART_VERSION" ]; then
    UPGRADE_CMD="$UPGRADE_CMD --version $CHART_VERSION"
fi

# Confirm upgrade
echo -e "${YELLOW}Upgrade command:${NC}"
echo "$UPGRADE_CMD"
echo ""
read -p "Proceed with upgrade? (y/N): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "Upgrade cancelled."
    exit 0
fi
echo ""

# Backup before upgrade
echo -e "${YELLOW}Step 2: Creating quick backup...${NC}"
BACKUP_DIR="/tmp/authentik-pre-upgrade-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
kubectl -n app-authentik get all -o yaml > "$BACKUP_DIR/resources.yaml"
echo "Backup saved to: $BACKUP_DIR"
echo -e "${GREEN}✓ Backup created${NC}"
echo ""

# Perform upgrade
echo -e "${YELLOW}Step 3: Upgrading Authentik...${NC}"
eval "$UPGRADE_CMD"
echo -e "${GREEN}✓ Upgrade complete${NC}"
echo ""

# Wait for rollout
echo -e "${YELLOW}Step 4: Waiting for rollout to complete...${NC}"
kubectl rollout status deployment/authentik-server -n app-authentik --timeout=300s
kubectl rollout status deployment/authentik-worker -n app-authentik --timeout=300s
echo -e "${GREEN}✓ Rollout complete${NC}"
echo ""

# Show new version
echo -e "${YELLOW}Step 5: Verifying upgrade...${NC}"
NEW_IMAGE=$(kubectl -n app-authentik get deployment authentik-server -o jsonpath='{.spec.template.spec.containers[0].image}')
echo "New image: $NEW_IMAGE"
echo ""

# Display status
echo -e "${GREEN}=========================================="
echo "Upgrade Complete!"
echo "==========================================${NC}"
echo ""
echo "Deployment status:"
kubectl -n app-authentik get pods
echo ""
echo "Helm release:"
helm list -n app-authentik
echo ""
echo -e "${GREEN}Verify the upgrade:${NC}"
echo "1. Check logs: kubectl -n app-authentik logs -l app.kubernetes.io/name=authentik"
echo "2. Test login: https://auth.epaflix.com"
echo "3. Verify functionality"
echo ""
echo "To rollback if needed:"
echo "  helm rollback authentik -n app-authentik"
echo ""
echo "Rollback history:"
helm history authentik -n app-authentik
