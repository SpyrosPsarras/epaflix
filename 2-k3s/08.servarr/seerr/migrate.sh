#!/usr/bin/env bash
set -euo pipefail

# Migrate Jellyseerr to Seerr
# This script automates the migration process from Jellyseerr to Seerr
# Based on: https://docs.seerr.dev/migration-guide

NAMESPACE="servarr"
JELLYSEERR_DIR="../jellyseerr"
SLEEP_TIME=5

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_info() { echo -e "${BLUE}ℹ️  $1${NC}"; }
print_success() { echo -e "${GREEN}✅ $1${NC}"; }
print_warning() { echo -e "${YELLOW}⚠️  $1${NC}"; }
print_error() { echo -e "${RED}❌ $1${NC}"; }

# Function to wait for user confirmation
confirm() {
    read -p "$(echo -e ${YELLOW}$1 [y/N]: ${NC})" -r
    echo
    [[ $REPLY =~ ^[Yy]$ ]]
}

# Check prerequisites
check_prerequisites() {
    print_info "Checking prerequisites..."

    if ! command -v kubectl &> /dev/null; then
        print_error "kubectl not found. Please install kubectl first."
        exit 1
    fi

    if ! kubectl get namespace ${NAMESPACE} &> /dev/null; then
        print_error "Namespace ${NAMESPACE} not found."
        exit 1
    fi

    if ! kubectl get deployment jellyseerr -n ${NAMESPACE} &> /dev/null; then
        print_error "Jellyseerr deployment not found in namespace ${NAMESPACE}."
        exit 1
    fi

    print_success "Prerequisites check passed"
}

# Backup Jellyseerr
backup_jellyseerr() {
    print_info "Step 1: Backing up Jellyseerr..."

    if [ ! -f "${JELLYSEERR_DIR}/backup-jellyseerr-db.sh" ]; then
        print_error "Backup script not found at ${JELLYSEERR_DIR}/backup-jellyseerr-db.sh"
        exit 1
    fi

    cd "${JELLYSEERR_DIR}"
    chmod +x backup-jellyseerr-db.sh

    print_info "Running backup script..."
    if ./backup-jellyseerr-db.sh; then
        print_success "Backup completed successfully"
    else
        print_error "Backup failed. Please check the error and try again."
        exit 1
    fi

    cd - > /dev/null

    # List backup files
    print_info "Backup files created:"
    ls -lh "${JELLYSEERR_DIR}/backups/" | tail -n 2
    echo
}

# Scale down Jellyseerr
scale_down_jellyseerr() {
    print_info "Step 2: Scaling down Jellyseerr..."

    if ! confirm "Are you ready to stop Jellyseerr?"; then
        print_error "Migration cancelled by user."
        exit 1
    fi

    kubectl scale deployment jellyseerr -n ${NAMESPACE} --replicas=0

    print_info "Waiting for pod to terminate..."
    kubectl wait --for=delete pod -l app=jellyseerr -n ${NAMESPACE} --timeout=60s 2>/dev/null || true

    # Verify no pods are running
    if kubectl get pods -n ${NAMESPACE} -l app=jellyseerr 2>/dev/null | grep -q Running; then
        print_warning "Jellyseerr pods still running. Waiting additional time..."
        sleep 10
    fi

    print_success "Jellyseerr scaled down"
}

# Deploy Seerr
deploy_seerr() {
    print_info "Step 3: Deploying Seerr..."

    if [ ! -f "seerr.yaml" ]; then
        print_error "seerr.yaml not found in current directory"
        exit 1
    fi

    print_info "Applying Seerr deployment..."
    kubectl apply -f seerr.yaml

    print_success "Seerr deployment created"
}

# Wait for Seerr to be ready
wait_for_seerr() {
    print_info "Step 4: Waiting for Seerr to start..."

    print_info "Waiting for pod to be created..."
    local max_wait=60
    local waited=0
    while ! kubectl get pods -n ${NAMESPACE} -l app=seerr 2>/dev/null | grep -q seerr; do
        if [ $waited -ge $max_wait ]; then
            print_error "Timeout waiting for Seerr pod to be created"
            exit 1
        fi
        sleep 2
        waited=$((waited + 2))
    done

    print_info "Pod created. Waiting for it to be ready..."
    print_warning "This may take several minutes during first startup (automatic migration)..."

    # Watch logs in background
    print_info "Following pod logs (press Ctrl+C to stop watching, migration will continue)..."
    kubectl logs -n ${NAMESPACE} -l app=seerr --follow 2>&1 &
    local log_pid=$!

    # Wait for ready status (with longer timeout for migration)
    if kubectl wait --for=condition=ready pod -l app=seerr -n ${NAMESPACE} --timeout=600s; then
        kill $log_pid 2>/dev/null || true
        print_success "Seerr pod is ready!"
    else
        kill $log_pid 2>/dev/null || true
        print_error "Timeout waiting for Seerr to be ready. Check logs for details:"
        kubectl logs -n ${NAMESPACE} -l app=seerr --tail=50
        exit 1
    fi
}

# Verify Seerr
verify_seerr() {
    print_info "Step 5: Verifying Seerr..."

    # Check service
    print_info "Checking service..."
    kubectl get service seerr -n ${NAMESPACE}

    # Test API endpoint
    print_info "Testing API endpoint..."
    if kubectl run test-seerr-api --image=busybox --restart=Never -n ${NAMESPACE} --rm -i --quiet -- \
        wget -O- http://seerr.${NAMESPACE}.svc.cluster.local:5055/api/v1/status 2>/dev/null | grep -q "version"; then
        print_success "Seerr API is responding correctly"
    else
        print_warning "API test inconclusive, but service is running"
    fi

    print_success "Seerr verification completed"
}

# Deploy Ingress
deploy_ingress() {
    print_info "Step 6: Deploying Ingress..."

    if [ ! -f "ingress.yaml" ]; then
        print_warning "ingress.yaml not found, skipping ingress deployment"
        return
    fi

    if confirm "Do you want to deploy the new ingress (seerr.epaflix.com)?"; then
        kubectl apply -f ingress.yaml
        print_success "Ingress deployed"

        print_info "Certificate request status:"
        sleep 5
        kubectl get certificate seerr-tls -n ${NAMESPACE} 2>/dev/null || print_warning "Certificate not yet created"
    else
        print_info "Skipping ingress deployment. You can deploy it later with: kubectl apply -f ingress.yaml"
    fi
}

# Update old ingress
update_old_ingress() {
    print_info "Step 7: Handling old Jellyseerr ingress..."

    if ! kubectl get ingress jellyseerr -n ${NAMESPACE} &> /dev/null; then
        print_info "No existing jellyseerr ingress found, skipping..."
        return
    fi

    echo
    print_warning "Old Jellyseerr ingress detected (jellyseerr.epaflix.com)"
    echo "Options:"
    echo "  1) Keep both domains (point jellyseerr.epaflix.com to Seerr)"
    echo "  2) Delete old ingress (use only seerr.epaflix.com)"
    echo "  3) Keep old ingress as-is (manual update later)"
    echo

    read -p "Choose option [1-3]: " -r option

    case $option in
        1)
            print_info "Updating jellyseerr ingress to point to seerr service..."
            kubectl patch ingress jellyseerr -n ${NAMESPACE} --type='json' \
                -p='[{"op": "replace", "path": "/spec/rules/0/http/paths/0/backend/service/name", "value":"seerr"}]'
            print_success "Jellyseerr ingress now points to Seerr"
            ;;
        2)
            if confirm "Are you sure you want to delete the jellyseerr ingress?"; then
                kubectl delete ingress jellyseerr -n ${NAMESPACE}
                print_success "Jellyseerr ingress deleted"
            else
                print_info "Keeping jellyseerr ingress"
            fi
            ;;
        3)
            print_info "Keeping jellyseerr ingress as-is"
            ;;
        *)
            print_warning "Invalid option, keeping jellyseerr ingress as-is"
            ;;
    esac
}

# Summary and next steps
show_summary() {
    echo
    print_success "🎉 Migration from Jellyseerr to Seerr completed successfully!"
    echo
    print_info "Summary:"
    echo "  ✅ Jellyseerr backup created"
    echo "  ✅ Jellyseerr scaled down"
    echo "  ✅ Seerr deployed and running"
    echo "  ✅ Automatic migration completed"
    echo
    print_info "Access Seerr:"
    echo "  Internal: http://seerr.${NAMESPACE}.svc.cluster.local:5055"

    if kubectl get ingress seerr -n ${NAMESPACE} &> /dev/null; then
        echo "  External: https://seerr.epaflix.com"
    fi

    if kubectl get ingress jellyseerr -n ${NAMESPACE} &> /dev/null; then
        BACKEND=$(kubectl get ingress jellyseerr -n ${NAMESPACE} -o jsonpath='{.spec.rules[0].http.paths[0].backend.service.name}')
        if [ "$BACKEND" = "seerr" ]; then
            echo "  Legacy:   https://jellyseerr.epaflix.com (pointing to Seerr)"
        else
            echo "  Legacy:   https://jellyseerr.epaflix.com (still pointing to old service)"
        fi
    fi

    echo
    print_warning "Next steps:"
    echo "  1. Test Seerr functionality thoroughly"
    echo "  2. Verify all your settings and integrations"
    echo "  3. Monitor for 24-48 hours before cleanup"
    echo "  4. After verification, clean up old deployment:"
    echo "     kubectl delete deployment jellyseerr -n ${NAMESPACE}"
    echo "     kubectl delete service jellyseerr -n ${NAMESPACE}"
    echo
    print_info "Backup location: ${JELLYSEERR_DIR}/backups/"
    echo
    print_warning "Keep backups safe in case you need to rollback!"
    echo
}

# Main execution
main() {
    echo
    print_info "============================================"
    print_info "  Jellyseerr to Seerr Migration Script"
    print_info "============================================"
    echo

    check_prerequisites
    echo

    print_warning "This script will:"
    echo "  1. Backup Jellyseerr database and config"
    echo "  2. Stop Jellyseerr"
    echo "  3. Deploy Seerr"
    echo "  4. Automatically migrate your data"
    echo "  5. Optionally update ingress"
    echo

    if ! confirm "Do you want to proceed with the migration?"; then
        print_error "Migration cancelled by user."
        exit 0
    fi

    echo
    backup_jellyseerr
    echo
    scale_down_jellyseerr
    echo
    deploy_seerr
    echo
    wait_for_seerr
    echo
    verify_seerr
    echo
    deploy_ingress
    echo
    update_old_ingress
    echo
    show_summary
}

# Run main function
main "$@"
