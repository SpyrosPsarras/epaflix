#!/usr/bin/env bash
set -euo pipefail

# Backup Jellyseerr PostgreSQL Database
# This script creates a backup of the jellyseerr database before migration to seerr

BACKUP_DIR="./backups"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/jellyseerr-db-backup-${TIMESTAMP}.sql"
NAMESPACE="servarr"
SECRET_NAME="servarr-postgres"

echo "🔄 Starting Jellyseerr database backup..."

# Create backup directory if it doesn't exist
mkdir -p "${BACKUP_DIR}"

# Get database credentials from Kubernetes secret
echo "📝 Retrieving database credentials from secret..."
DB_HOST=$(kubectl get secret -n ${NAMESPACE} ${SECRET_NAME} -o jsonpath='{.data.jellyseerr-host}' | base64 -d)
DB_PORT=$(kubectl get secret -n ${NAMESPACE} ${SECRET_NAME} -o jsonpath='{.data.jellyseerr-port}' | base64 -d)
DB_USER=$(kubectl get secret -n ${NAMESPACE} ${SECRET_NAME} -o jsonpath='{.data.jellyseerr-user}' | base64 -d)
DB_PASS=$(kubectl get secret -n ${NAMESPACE} ${SECRET_NAME} -o jsonpath='{.data.jellyseerr-password}' | base64 -d)
DB_NAME=$(kubectl get secret -n ${NAMESPACE} ${SECRET_NAME} -o jsonpath='{.data.jellyseerr-database}' | base64 -d)

echo "📊 Database: ${DB_NAME} on ${DB_HOST}:${DB_PORT}"

# Create a temporary pod to run pg_dump
echo "🚀 Creating backup pod..."

# Delete any existing backup pod from previous failed runs
kubectl delete pod jellyseerr-backup-pod -n ${NAMESPACE} 2>/dev/null || true

# Create the pod (without --rm and --attach for more reliability)
kubectl run jellyseerr-backup-pod \
  --namespace=${NAMESPACE} \
  --image=postgres:16-alpine \
  --restart=Never \
  --env="PGPASSWORD=${DB_PASS}" \
  --command -- pg_dump \
  -h "${DB_HOST}" \
  -p "${DB_PORT}" \
  -U "${DB_USER}" \
  -d "${DB_NAME}" \
  --clean \
  --if-exists

echo "⏳ Waiting for backup to complete..."
# Wait for pod to complete (max 5 minutes)
kubectl wait --for=condition=Ready pod/jellyseerr-backup-pod -n ${NAMESPACE} --timeout=10s 2>/dev/null || true
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/jellyseerr-backup-pod -n ${NAMESPACE} --timeout=300s

# Get the logs (which contain the SQL dump)
echo "📥 Retrieving backup data..."
kubectl logs jellyseerr-backup-pod -n ${NAMESPACE} > "${BACKUP_FILE}"

# Clean up the pod
kubectl delete pod jellyseerr-backup-pod -n ${NAMESPACE}

echo "✅ Database backup completed: ${BACKUP_FILE}"

# Compress the backup
echo "🗜️  Compressing backup..."
gzip "${BACKUP_FILE}"
COMPRESSED_FILE="${BACKUP_FILE}.gz"

echo "✅ Backup compressed: ${COMPRESSED_FILE}"

# Get backup size
BACKUP_SIZE=$(du -h "${COMPRESSED_FILE}" | cut -f1)
echo "📦 Backup size: ${BACKUP_SIZE}"

# Also backup the Jellyseerr config volume
echo ""
echo "📁 Backing up Jellyseerr config volume..."
CONFIG_BACKUP="${BACKUP_DIR}/jellyseerr-config-${TIMESTAMP}.tar.gz"

# Get the pod name
POD_NAME=$(kubectl get pods -n ${NAMESPACE} -l app=jellyseerr -o jsonpath='{.items[0].metadata.name}')

if [ -n "${POD_NAME}" ]; then
  echo "📦 Found pod: ${POD_NAME}"
  kubectl exec -n ${NAMESPACE} ${POD_NAME} -- tar czf - -C /app config > "${CONFIG_BACKUP}"
  CONFIG_SIZE=$(du -h "${CONFIG_BACKUP}" | cut -f1)
  echo "✅ Config backup completed: ${CONFIG_BACKUP} (${CONFIG_SIZE})"
else
  echo "⚠️  No running jellyseerr pod found, skipping config backup"
fi

echo ""
echo "🎉 Backup process completed successfully!"
echo ""
echo "Backup files:"
echo "  Database: ${COMPRESSED_FILE}"
if [ -f "${CONFIG_BACKUP}" ]; then
  echo "  Config:   ${CONFIG_BACKUP}"
fi
echo ""
echo "⚠️  IMPORTANT: Keep these backups safe in case you need to rollback!"
