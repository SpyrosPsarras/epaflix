#!/usr/bin/env bash
set -euo pipefail

# Configure DNS for K3s Cluster
#
# This script:
# 1. Configures systemd-resolved on all nodes to listen on node IPs
# 2. Updates CoreDNS to forward queries to nodes instead of DNS server
# 3. Updates custom epaflix domains configuration
# 4. Restarts CoreDNS
#
# Why this is needed:
# The DNS server at 192.168.10.30 (Pi-hole) only accepts queries from the
# 192.168.10.0/24 network, not from the pod network (10.42.x.x). To work
# around this, we configure pods to query DNS via the nodes, which then
# forward to the DNS server.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Node IPs
MASTER_IPS=(51 52 53)
WORKER_IPS=(61 62 63 65)
ALL_IPS=("${MASTER_IPS[@]}" "${WORKER_IPS[@]}")

log_info "Starting DNS configuration for K3s cluster"

# Step 1: Configure systemd-resolved on all nodes
log_info "Step 1/4: Configuring systemd-resolved on all nodes..."
for ip in "${ALL_IPS[@]}"; do
    log_info "Configuring DNS on 192.168.10.$ip"
    ssh ubuntu@192.168.10.$ip "sudo mkdir -p /etc/systemd/resolved.conf.d/ && sudo tee /etc/systemd/resolved.conf.d/listen.conf > /dev/null << 'EOL'
[Resolve]
DNSStubListenerExtra=192.168.10.$ip
DNSStubListenerExtra=10.0.0.$ip
EOL
sudo systemctl restart systemd-resolved"
done

# Verify systemd-resolved is listening
log_info "Verifying systemd-resolved is listening on node IPs..."
for ip in "${MASTER_IPS[@]}"; do
    log_info "Checking 192.168.10.$ip..."
    if ssh ubuntu@192.168.10.$ip "sudo ss -tulpn | grep -q '192.168.10.$ip:53'"; then
        log_info "  ✓ systemd-resolved is listening on 192.168.10.$ip:53"
    else
        log_error "  ✗ systemd-resolved is NOT listening on 192.168.10.$ip:53"
        exit 1
    fi
done

# Step 2: Update CoreDNS main configuration
log_info "Step 2/4: Updating CoreDNS main configuration..."
kubectl patch configmap coredns -n kube-system --type=json -p='[{"op":"replace","path":"/data/Corefile","value":".:53 {\n    errors\n    health\n    ready\n    kubernetes cluster.local in-addr.arpa ip6.arpa {\n      pods insecure\n      fallthrough in-addr.arpa ip6.arpa\n    }\n    hosts /etc/coredns/NodeHosts {\n      ttl 60\n      reload 15s\n      fallthrough\n    }\n    prometheus :9153\n    cache 30\n    loop\n    reload\n    loadbalance\n    import /etc/coredns/custom/*.override\n    forward . 192.168.10.51 192.168.10.52 192.168.10.53\n}\nimport /etc/coredns/custom/*.server\n"}]'
log_info "  ✓ CoreDNS main configuration updated"

# Step 3: Update custom epaflix domains configuration
log_info "Step 3/4: Updating custom epaflix domains configuration..."
if kubectl get configmap coredns-custom -n kube-system &> /dev/null; then
    kubectl patch configmap coredns-custom -n kube-system --type=json -p='[{"op":"replace","path":"/data/epaflix.server","value":"epaflix.com:53 internal.epaflix.com:53 {\n    errors\n    cache 30\n    forward . 192.168.10.51 192.168.10.52 192.168.10.53\n    log\n}\n"}]'
    log_info "  ✓ Custom epaflix domains configuration updated"
else
    log_warn "  coredns-custom ConfigMap not found, applying from file..."
    kubectl apply -f "$SCRIPT_DIR/coredns-epaflix-domains.yaml"
    log_info "  ✓ Custom epaflix domains configuration applied"
fi

# Step 4: Restart CoreDNS
log_info "Step 4/4: Restarting CoreDNS..."
kubectl rollout restart deployment/coredns -n kube-system
kubectl rollout status deployment/coredns -n kube-system --timeout=60s
log_info "  ✓ CoreDNS restarted successfully"

# Verification
log_info "Verifying DNS resolution from pods..."

# Test public domain
log_info "Testing auth.epaflix.com..."
if kubectl run test-dns-verify-1 --image=busybox --restart=Never --rm -it -- \
    nslookup auth.epaflix.com | grep -q "192.168.10.101"; then
    log_info "  ✓ auth.epaflix.com resolves correctly"
else
    log_error "  ✗ auth.epaflix.com resolution failed"
fi

# Test internal domain
log_info "Testing sonarr.internal.epaflix.com..."
if kubectl run test-dns-verify-2 --image=busybox --restart=Never --rm -it -- \
    nslookup sonarr.internal.epaflix.com | grep -q "192.168.10.101"; then
    log_info "  ✓ sonarr.internal.epaflix.com resolves correctly"
else
    log_warn "  ⚠ sonarr.internal.epaflix.com resolution may have issues (check manually)"
fi

log_info "DNS configuration complete!"
log_info ""
log_info "To verify DNS is working:"
log_info "  kubectl run test-dns --image=busybox --restart=Never --rm -it -- nslookup auth.epaflix.com"
log_info "  kubectl run test-dns --image=busybox --restart=Never --rm -it -- nslookup sonarr.internal.epaflix.com"
