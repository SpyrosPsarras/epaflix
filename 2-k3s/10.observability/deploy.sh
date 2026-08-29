#!/bin/bash
set -e

: "${KUBECTL_CONTEXT:=epaflix}"
kubectl() { command kubectl --context "$KUBECTL_CONTEXT" "$@"; }


echo "=========================================="
echo "Observability Stack Deployment"
echo "=========================================="
echo ""

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${YELLOW}Checking prerequisites...${NC}"

command -v kubectl >/dev/null 2>&1 || { echo -e "${RED}kubectl not found!${NC}"; exit 1; }
command -v helm >/dev/null 2>&1 || { echo -e "${RED}helm not found!${NC}"; exit 1; }

echo -e "${GREEN}✓ Prerequisites OK${NC}"
echo ""

echo -e "${YELLOW}Step 1: Creating observability namespace and PostgreSQL database...${NC}"
kubectl apply -f namespace.yaml
kubectl apply -f grafana-db-secret.yaml
kubectl apply -f postgres-setup-job.yaml
echo "Waiting for PostgreSQL setup to complete..."
kubectl wait --for=condition=complete --timeout=300s job/postgres-setup-observability -n observability
echo -e "${GREEN}✓ Namespace and database ready${NC}"
echo ""

echo -e "${YELLOW}Step 2: Creating PersistentVolumeClaims...${NC}"
echo "Note: Storage is provisioned from node-local paths backed by iSCSI targets"
kubectl apply -f storage/prometheus-pvc.yaml
kubectl apply -f storage/loki-pvc.yaml
kubectl get pvc -n observability
echo -e "${GREEN}✓ Storage resources created${NC}"
echo ""

echo -e "${YELLOW}Step 3: Adding Helm repositories...${NC}"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update
echo -e "${GREEN}✓ Helm repos added${NC}"
echo ""

echo -e "${YELLOW}Step 4: Installing kube-prometheus-stack (Prometheus + Grafana + AlertManager)...${NC}"
echo "This may take 5-10 minutes..."
helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n observability \
  -f prometheus-values.yaml \
  --wait --timeout=15m
echo -e "${GREEN}✓ kube-prometheus-stack installed${NC}"
echo ""

echo -e "${YELLOW}Step 5: Installing Loki...${NC}"
helm install loki grafana/loki \
  -n observability \
  -f loki-values.yaml \
  --wait --timeout=10m
echo -e "${GREEN}✓ Loki installed${NC}"
echo ""

echo -e "${YELLOW}Step 6: Installing Promtail...${NC}"
helm install promtail grafana/promtail \
  -n observability \
  -f promtail-values.yaml \
  --wait --timeout=5m
echo -e "${GREEN}✓ Promtail installed${NC}"
echo ""

echo -e "${YELLOW}Step 7: Deploying Proxmox VE Exporter...${NC}"
SECRETS_ENC="${SECRETS_ENC:-../../.github/instructions/secrets.enc.yaml}"
if [[ ! -f "$SECRETS_ENC" ]]; then
  echo -e "${RED}secrets.enc.yaml not found at $SECRETS_ENC — skipping pve-exporter${NC}"
else
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

echo -e "${YELLOW}Step 8: Applying custom alert rules...${NC}"
kubectl apply -f alertmanager-config/custom-alerts.yaml
echo -e "${GREEN}✓ Custom alerts applied${NC}"
echo ""

echo -e "${YELLOW}Step 9: Creating ingress routes...${NC}"
kubectl apply -f ingress/grafana-ingressroute.yaml
echo -e "${GREEN}✓ Ingress routes created${NC}"
echo ""

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
echo "  2. Install Cilium CNI"
echo ""
echo "  3. Install Istio service mesh"
echo ""
echo "  4. Test email alerts:"
echo "     kubectl exec -n observability alertmanager-kube-prometheus-stack-alertmanager-0 -- amtool alert add test_alert"
echo ""
echo -e "${YELLOW}To view pods:${NC}"
echo "  kubectl get pods -n observability"
echo ""
echo -e "${YELLOW}To view logs:${NC}"
echo "  kubectl logs -n observability -l app.kubernetes.io/name=grafana -f"
