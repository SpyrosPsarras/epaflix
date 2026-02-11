#!/bin/bash

# kube-vip Cloud Provider Installation Script
# This script installs and configures kube-vip cloud provider for k3s

set -e

echo "==================================="
echo "kube-vip Cloud Provider Installation"
echo "==================================="
echo ""

# Configuration
CLOUD_PROVIDER_URL="https://raw.githubusercontent.com/kube-vip/kube-vip-cloud-provider/main/manifest/kube-vip-cloud-controller.yaml"
NAMESPACE="kube-system"
CONFIGMAP_NAME="kubevip"

# Default IP pool configuration (adjust these values)
DEFAULT_CIDR="192.168.10.100/26"
DEFAULT_RANGE="192.168.10.100-192.168.10.199"

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored messages
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if kubectl is available
if ! command -v kubectl &> /dev/null; then
    print_error "kubectl not found. Please install kubectl first."
    exit 1
fi

# Check if cluster is accessible
if ! kubectl cluster-info &> /dev/null; then
    print_error "Cannot connect to Kubernetes cluster. Please check your kubeconfig."
    exit 1
fi

print_info "Connected to Kubernetes cluster"
kubectl cluster-info | head -n 1

echo ""
print_info "Step 1: Installing kube-vip Cloud Provider..."
kubectl apply -f "$CLOUD_PROVIDER_URL"

echo ""
print_info "Waiting for cloud provider to be ready..."
kubectl wait --for=condition=ready pod -l app=kube-vip-cloud-provider -n "$NAMESPACE" --timeout=60s || true

echo ""
print_info "Step 2: Creating IP address pool ConfigMap..."

# Check if ConfigMap already exists
if kubectl get configmap "$CONFIGMAP_NAME" -n "$NAMESPACE" &> /dev/null; then
    print_warning "ConfigMap '$CONFIGMAP_NAME' already exists in namespace '$NAMESPACE'"
    read -p "Do you want to update it? (y/n): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        print_info "Updating ConfigMap..."
        kubectl delete configmap "$CONFIGMAP_NAME" -n "$NAMESPACE"
    else
        print_info "Skipping ConfigMap creation"
        echo ""
        print_info "Installation complete!"
        exit 0
    fi
fi

# Ask user for IP pool configuration
echo ""
echo "Choose IP pool configuration method:"
echo "1) CIDR notation (e.g., 192.168.10.100/26)"
echo "2) IP range (e.g., 192.168.10.100-192.168.10.199) [Recommended]"
echo "3) Use default range (192.168.10.100-192.168.10.199)"
echo ""
read -p "Enter choice (1-3): " choice

case $choice in
    1)
        read -p "Enter CIDR (e.g., 192.168.10.100/26): " cidr
        print_info "Creating ConfigMap with CIDR: $cidr"
        kubectl create configmap "$CONFIGMAP_NAME" -n "$NAMESPACE" --from-literal cidr-global="$cidr"
        ;;
    2)
        read -p "Enter IP range (e.g., 192.168.10.100-192.168.10.199): " range
        print_info "Creating ConfigMap with range: $range"
        kubectl create configmap "$CONFIGMAP_NAME" -n "$NAMESPACE" --from-literal range-global="$range"
        ;;
    3)
        print_info "Creating ConfigMap with default range: $DEFAULT_RANGE"
        kubectl create configmap "$CONFIGMAP_NAME" -n "$NAMESPACE" --from-literal range-global="$DEFAULT_RANGE"
        ;;
    *)
        print_error "Invalid choice. Exiting."
        exit 1
        ;;
esac

echo ""
print_info "Verifying ConfigMap creation..."
kubectl get configmap "$CONFIGMAP_NAME" -n "$NAMESPACE" -o yaml

echo ""
echo "==================================="
print_info "Installation completed successfully!"
echo "==================================="
echo ""
echo "Next steps:"
echo "1. Create a LoadBalancer service:"
echo "   kubectl expose deployment <name> --port=80 --type=LoadBalancer"
echo ""
echo "2. Check the assigned IP:"
echo "   kubectl get svc"
echo ""
echo "3. View cloud provider logs:"
echo "   kubectl logs -n kube-system -l app=kube-vip-cloud-provider"
echo ""
echo "For more examples, see the example YAML files in this directory."
echo ""
