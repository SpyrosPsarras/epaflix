#!/bin/bash
set -e

# K3s Embedded Registry Mirror Enablement
# Enables Spegel-based P2P image sharing across all cluster nodes
#
# NOTE: This script has been updated with the working commands that were
# manually validated on February 3, 2026. The original sed command didn't
# work and was replaced with the head/cat approach that succeeded.
#
# To re-run: This script is idempotent - it checks if flags already exist

echo "=========================================="
echo "K3s Embedded Registry Mirror Setup"
echo "=========================================="
echo ""

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Node IPs
MASTERS=(192.168.10.51 192.168.10.52 192.168.10.53)
WORKERS=(192.168.10.61 192.168.10.62 192.168.10.63 192.168.10.65)
ALL_NODES=("${MASTERS[@]}" "${WORKERS[@]}")

echo -e "${YELLOW}Configuration:${NC}"
echo "  Masters: ${MASTERS[@]}"
echo "  Workers: ${WORKERS[@]}"
echo "  P2P Network: 10.0.0.0/24 (eth1) - 2.5G dedicated"
echo "  P2P Port: 5001"
echo "  Registry Port: 6443"
echo ""

# Step 1: Create registries.yaml
echo -e "${YELLOW}Step 1: Creating registries.yaml${NC}"
cat > /tmp/registries.yaml << 'EOF'
# K3s Embedded Registry Mirror Configuration
# Enables peer-to-peer image sharing across all cluster nodes
# Using wildcard to mirror ALL registries

mirrors:
  "*":
    # Empty endpoint list enables embedded mirror without external mirrors
EOF

echo -e "${GREEN}✓ registries.yaml created${NC}"
cat /tmp/registries.yaml
echo ""

# Step 2: Distribute registries.yaml to all nodes
echo -e "${YELLOW}Step 2: Distributing registries.yaml to all nodes${NC}"
for node in "${ALL_NODES[@]}"; do
    echo -n "  Copying to $node... "
    scp -q /tmp/registries.yaml ubuntu@$node:/tmp/
    ssh ubuntu@$node 'sudo mkdir -p /etc/rancher/k3s && sudo mv /tmp/registries.yaml /etc/rancher/k3s/ && sudo chmod 644 /etc/rancher/k3s/registries.yaml'
    echo -e "${GREEN}✓${NC}"
done
echo ""

# Step 3: Update master nodes with --embedded-registry flag
echo -e "${YELLOW}Step 3: Adding --embedded-registry flag to master nodes${NC}"
for master in "${MASTERS[@]}"; do
    master_num="${master##*.}"
    echo "  Updating master-$master_num ($master)..."

    # Check if flag already exists
    if ssh ubuntu@$master 'sudo grep -q "embedded-registry" /etc/systemd/system/k3s.service'; then
        echo -e "    ${YELLOW}⚠ Flag already exists, skipping${NC}"
        continue
    fi

    # Rebuild the last 3 lines of k3s.service with the flag
    ssh ubuntu@$master 'sudo bash -c "head -n -3 /etc/systemd/system/k3s.service > /tmp/k3s.service.new && cat >> /tmp/k3s.service.new << '\''EOF'\''
        '\''--tls-san'\'' \\
        '\''10.0.0.53'\'' \\
        '\''--embedded-registry'\''
EOF
sudo mv /tmp/k3s.service.new /etc/systemd/system/k3s.service"'
    ssh ubuntu@$master 'sudo systemctl daemon-reload && sudo systemctl restart k3s'

    # Wait for node to be Ready
    echo -n "    Waiting for node to be Ready... "
    for i in {1..60}; do
        if kubectl get node k3s-master-$master_num -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null | grep -q "True"; then
            echo -e "${GREEN}✓${NC}"
            break
        fi
        sleep 2
        echo -n "."
    done

    # Wait additional 10 seconds for stability
    echo "    Waiting 10s for stability..."
    sleep 10

    echo -e "  ${GREEN}✓ master-$master_num ready${NC}"
done
echo ""

# Step 5: Rolling restart - Workers (one at a time)
echo -e "${YELLOW}Step 5: Rolling restart of worker nodes (one at a time)${NC}"
for worker in "${WORKERS[@]}"; do
    worker_num="${worker##*.}"
    echo -e "  ${YELLOW}Restarting worker-$worker_num ($worker)...${NC}"

    # Cordon node
    kubectl cordon k3s-worker-$worker_num
    echo "    Node cordoned"

    # Restart k3s-agent
    ssh ubuntu@$worker 'sudo systemctl daemon-reload && sudo systemctl restart k3s-agent'

    # Wait for node to be Ready
    echo -n "    Waiting for node to be Ready... "
    for i in {1..60}; do
        if kubectl get node k3s-worker-$worker_num -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null | grep -q "True"; then
            echo -e "${GREEN}✓${NC}"
            break
        fi
        sleep 2
        echo -n "."
    done

    # Uncordon node
    kubectl uncordon k3s-worker-$worker_num
    echo "    Node uncordoned"

    # Wait additional 10 seconds for stability
    echo "    Waiting 10s for stability..."
    sleep 10

    echo -e "  ${GREEN}✓ worker-$worker_num ready${NC}"
done
echo ""

# Step 6: Verify embedded registry is running
echo -e "${YELLOW}Step 6: Verifying embedded registry pods${NC}"
kubectl get pods -n kube-system | grep spegel || echo -e "${YELLOW}⚠ Spegel pods not found (may take a moment to start)${NC}"
echo ""

# Step 7: Test image pull from peer
echo -e "${YELLOW}Step 7: Testing P2P image sharing${NC}"
echo "  You can test by pulling an image on one node and checking if others can get it from peer"
echo "  Example:"
echo "    # On node 1: docker pull nginx:alpine"
echo "    # On node 2: docker pull nginx:alpine (should be faster, pulled from peer)"
echo ""

echo -e "${GREEN}=========================================="
echo "Embedded Registry Mirror Setup Complete!"
echo "==========================================${NC}"
echo ""
echo "Next steps:"
echo "1. Verify all nodes are Ready: kubectl get nodes"
echo "2. Check spegel pods: kubectl get pods -n kube-system | grep spegel"
echo "3. Test P2P sharing by pulling an image on multiple nodes"
echo "4. Remove pre-pull CronJob: kubectl delete cronjob -n kube-system image-prepull-weekly"
echo ""
