#!/bin/bash
set -e

echo "======================================"
echo "Deploying PostgreSQL HA Cluster"
echo "======================================"

# Create NFS storage for backups (dynamic provisioning via CSI)
echo "Creating NFS backup storage..."
kubectl apply -f storage/backup-nfs-storage.yaml

# Wait for PVC to be bound
echo "Waiting for backup PVC to be bound..."
kubectl wait --for=jsonpath='{.status.phase}'=Bound --timeout=60s \
  pvc/postgres-backup-pvc -n postgres-system

# Create secrets
echo "Creating PostgreSQL secrets..."
kubectl apply -f cluster/postgres-secret.yaml

# Deploy PostgreSQL cluster
echo "Deploying PostgreSQL cluster (3 instances with PostgreSQL 16)..."
kubectl apply -f cluster/postgres-cluster.yaml

# Wait for cluster to be ready
echo "Waiting for PostgreSQL cluster to be ready (this may take a few minutes)..."
kubectl wait --for=condition=Ready --timeout=600s \
  cluster/postgres-cluster -n postgres-system

# Deploy PgBouncer pooler
echo "Deploying PgBouncer connection pooler..."
kubectl apply -f cluster/postgres-pooler.yaml

# Wait for pooler to be ready
echo "Waiting for pooler to be ready..."
sleep 10
kubectl wait --for=condition=Ready --timeout=300s \
  pod -l cnpg.io/poolerName=postgres-pooler -n postgres-system

# Create LoadBalancer services
echo "Creating LoadBalancer services..."
kubectl apply -f services/postgres-lb-rw.yaml
kubectl apply -f services/postgres-lb-ro.yaml
kubectl apply -f services/postgres-lb-r.yaml
kubectl apply -f services/postgres-pooler-lb.yaml

# Deploy scheduled backup
echo "Configuring daily backup schedule..."
kubectl apply -f backup/backup-schedule.yaml

echo ""
echo "======================================"
echo "PostgreSQL HA Cluster deployed successfully!"
echo "======================================"
echo ""
echo "Cluster Status:"
kubectl get cluster -n postgres-system
echo ""
echo "Pods:"
kubectl get pods -n postgres-system
echo ""
echo "Services:"
kubectl get svc -n postgres-system
echo ""
echo "Connection Information:"
echo "  Primary (RW):    192.168.10.105:5432"
echo "  Replicas (RO):   192.168.10.106:5432"
echo "  Any Instance:    192.168.10.107:5432"
echo "  Pooler (RW):     192.168.10.108:5432"
echo ""
echo "Credentials:"
echo "  Database:        authentik"
echo "  App User:        authentik"
echo "  Superuser:       postgres"
echo ""
echo "Connection string (via pooler):"
echo "  postgresql://authentik:<AUTHENTIK_DB_PASSWORD>@192.168.10.108:5432/authentik"
echo ""
