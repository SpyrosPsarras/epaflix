#!/bin/bash
set -e

# Observability Stack Deployment Script
# Deploys: Cilium, Hubble, Istio, Prometheus, Grafana, Loki, Promtail, PVE-Exporter

echo "=========================================="
echo "Observability Stack Deployment"
echo "=========================================="
echo ""

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Check prerequisites
echo -e "${YELLOW}Checking prerequisites...${NC}"

command -v kubectl >/dev/null 2>&1 || { echo -e "${RED}kubectl not found!${NC}"; exit 1; }
command -v helm >/dev/null 2>&1 || { echo -e "${RED}helm not found!${NC}"; exit 1; }

echo -e "${GREEN}✓ Prerequisites OK${NC}"
echo ""

# Step 1: Apply namespace and PostgreSQL setup
echo -e "${YELLOW}Step 1: Creating observability namespace and PostgreSQL database...${NC}"
kubectl apply -f namespace.yaml
kubectl apply -f grafana-db-secret.yaml
kubectl apply -f postgres-setup-job.yaml
echo "Waiting for PostgreSQL setup to complete..."
kubectl wait --for=condition=complete --timeout=300s job/postgres-setup-observability -n observability
echo -e "${GREEN}✓ Namespace and database ready${NC}"
echo ""

# Step 2: Create PersistentVolumeClaims (uses K3s local-path provisioner)
echo -e "${YELLOW}Step 2: Creating PersistentVolumeClaims...${NC}"
echo "Note: Storage is provisioned from node-local paths backed by iSCSI targets"
kubectl apply -f storage/prometheus-pvc.yaml
kubectl apply -f storage/loki-pvc.yaml
kubectl get pvc -n observability
echo -e "${GREEN}✓ Storage resources created${NC}"
echo ""

# Step 3: Add Helm repositories
echo -e "${YELLOW}Step 3: Adding Helm repositories...${NC}"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update
echo -e "${GREEN}✓ Helm repos added${NC}"
echo ""

# Step 4: Install kube-prometheus-stack
echo -e "${YELLOW}Step 4: Installing kube-prometheus-stack (Prometheus + Grafana + AlertManager)...${NC}"
echo "This may take 5-10 minutes..."
helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n observability \
  -f prometheus-values.yaml \
  --wait --timeout=15m
echo -e "${GREEN}✓ kube-prometheus-stack installed${NC}"
echo ""

# Step 5: Install Loki
echo -e "${YELLOW}Step 5: Installing Loki...${NC}"
helm install loki grafana/loki \
  -n observability \
  -f loki-values.yaml \
  --wait --timeout=10m
echo -e "${GREEN}✓ Loki installed${NC}"
echo ""

# Step 6: Install Promtail
echo -e "${YELLOW}Step 6: Installing Promtail...${NC}"
helm install promtail grafana/promtail \
  -n observability \
  -f promtail-values.yaml \
  --wait --timeout=5m
echo -e "${GREEN}✓ Promtail installed${NC}"
echo ""

# Step 7: Deploy Proxmox VE Exporter
echo -e "${YELLOW}Step 7: Deploying Proxmox VE Exporter...${NC}"
# Real token comes from the SOPS-encrypted credential store
# .github/instructions/secrets.enc.yaml. The Secret in git carries
# `<PROXMOX_API_TOKEN_VALUE>` as a placeholder — sed substitutes it in-flight
# before kubectl apply so the real value never lands on disk in the repo path.
# `sops -d --extract` returns the YAML-parsed value, so no quote-stripping is
# needed (that was the #293 trap with the old plaintext grep read).
SECRETS_ENC="${SECRETS_ENC:-../../.github/instructions/secrets.enc.yaml}"
if [[ ! -f "$SECRETS_ENC" ]]; then
  echo -e "${RED}secrets.enc.yaml not found at $SECRETS_ENC — skipping pve-exporter${NC}"
else
  # The token is stored as `root@pam!grafana=<uuid>`; pve-exporter wants only
  # the secret half, hence the trailing `=`-strip.
  PROXMOX_API_TOKEN_VALUE=$(sops -d --extract '["proxmox_grafana_api_token"]' "$SECRETS_ENC" \
    | sed -E 's/^.*=//')
  if [[ -z "$PROXMOX_API_TOKEN_VALUE" ]]; then
    echo -e "${RED}Could not decrypt proxmox_grafana_api_token from $SECRETS_ENC${NC}"
    echo -e "${RED}  Is KeePassXC unlocked? The age key lives there now (entry sops-age-k3s-cluster),${NC}"
    echo -e "${RED}  not in ~/.config/sops/age/. See .github/instructions/sops.instructions.md.${NC}"
    exit 1
  fi
  sed "s|<PROXMOX_API_TOKEN_VALUE>|$PROXMOX_API_TOKEN_VALUE|g" pve-exporter/secret.yaml \
    | kubectl apply -f -
  kubectl apply -f pve-exporter/deployment.yaml
  kubectl apply -f pve-exporter/service.yaml
  kubectl apply -f pve-exporter/servicemonitor.yaml
  echo -e "${GREEN}✓ PVE Exporter deployed${NC}"
fi
echo ""

# Step 8: Apply custom alerts
echo -e "${YELLOW}Step 8: Applying custom alert rules...${NC}"
kubectl apply -f alertmanager-config/custom-alerts.yaml
echo -e "${GREEN}✓ Custom alerts applied${NC}"
echo ""

# Step 9: Apply ingress routes
echo -e "${YELLOW}Step 9: Creating ingress routes...${NC}"
kubectl apply -f ingress/grafana-ingressroute.yaml
echo -e "${GREEN}✓ Ingress routes created${NC}"
echo ""

# Step 10: Display access information
echo -e "${GREEN}=========================================="
echo "Deployment Complete!"
echo "==========================================${NC}"
echo ""
echo -e "${GREEN}Access URLs:${NC}"
echo "  Grafana:   https://grafana.epaflix.com"
echo "  Kiali:     https://kiali.monitor.epaflix.com (after Istio installation)"
echo "  Hubble UI: https://hubble.monitor.epaflix.com (after Cilium installation)"
echo ""
echo -e "${GREEN}Grafana Credentials:${NC}"
echo "  Username: admin"
echo "  Password: <POSTGRES_PASSWORD>"
echo ""
echo -e "${YELLOW}Next Steps:${NC}"
echo "  1. Configure Grafana OAuth with Authentik:"
echo "     - Create OAuth2/OIDC provider in Authentik"
echo "     - Update grafana-config/oauth-secret.yaml"
echo "     - Run: helm upgrade kube-prometheus-stack prometheus-community/kube-prometheus-stack -n observability -f prometheus-values.yaml"
echo ""
echo "  2. Install Cilium CNI (see README.md for detailed steps)"
echo ""
echo "  3. Install Istio service mesh (see README.md for detailed steps)"
echo ""
echo "  4. Test email alerts:"
echo "     kubectl exec -n observability alertmanager-kube-prometheus-stack-alertmanager-0 -- amtool alert add test_alert"
echo ""
echo -e "${YELLOW}To view pods:${NC}"
echo "  kubectl get pods -n observability"
echo ""
echo -e "${YELLOW}To view logs:${NC}"
echo "  kubectl logs -n observability -l app.kubernetes.io/name=grafana -f"
