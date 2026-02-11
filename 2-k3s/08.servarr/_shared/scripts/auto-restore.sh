#!/bin/bash
# Simplified automated restore for *arr apps with PostgreSQL
# Places backup in Backups folder for auto-restore on startup

set -e

APP_NAME="$1"
BACKUP_ZIP="$2"

if [ -z "$APP_NAME" ] || [ -z "$BACKUP_ZIP" ]; then
    echo "Usage: $0 <app-name> <backup-zip-file>"
    echo "Example: $0 prowlarr /tmp/prowlarr_backup_2026.01.24.zip"
    exit 1
fi

if [ ! -f "$BACKUP_ZIP" ]; then
    echo "Error: Backup file '$BACKUP_ZIP' not found"
    exit 1
fi

echo "=== Auto-Restore for $APP_NAME ==="
echo "Backup: $(basename $BACKUP_ZIP)"
echo ""

# Scale down
echo "[1/5] Stopping $APP_NAME..."
kubectl -n servarr scale deployment "$APP_NAME" --replicas=0
sleep 5

# Find config path
echo "[2/5] Locating config directory..."
PV_NAME=$(kubectl -n servarr get pvc "${APP_NAME}-config" -o jsonpath='{.spec.volumeName}')
CONFIG_PATH=$(kubectl get pv "$PV_NAME" -o jsonpath='{.spec.hostPath.path}')
echo "   Path: $CONFIG_PATH"

# Find node with volume
echo "[3/5] Finding node..."
NODE=""
for N in $(kubectl get nodes -o jsonpath='{.items[*].metadata.name}'); do
    kubectl debug node/"$N" -it --image=busybox:latest -- sh -c "[ -d /host$CONFIG_PATH ]" 2>/dev/null && {
        NODE="$N"
        echo "   Node: $NODE"
        break
    }
done

if [ -z "$NODE" ]; then
    echo "Error: Could not find node with volume"
    exit 1
fi

# Place backup file for auto-restore
echo "[4/5] Placing backup file for auto-restore..."

# Copy backup to node's /tmp
BACKUP_NAME=$(basename "$BACKUP_ZIP")
kubectl debug node/"$NODE" -it --image=busybox:latest -- sh -c "
    mkdir -p /host/tmp
    cat > /host/tmp/$BACKUP_NAME
" < "$BACKUP_ZIP" 2>/dev/null

# Use temp pod to move backup to Backups folder
cat > /tmp/restore-mover-$$.yaml <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: restore-mover
  namespace: servarr
spec:
  nodeName: $NODE
  containers:
  - name: mover
    image: busybox:latest
    command: ['sh', '-c', 'mkdir -p /config/Backups/scheduled && mv /tmp-host/$BACKUP_NAME /config/Backups/ && sleep 10']
    volumeMounts:
    - name: config
      mountPath: /config
    - name: tmphost
      mountPath: /tmp-host
  volumes:
  - name: config
    hostPath:
      path: $CONFIG_PATH
  - name: tmphost
    hostPath:
      path: /tmp
  restartPolicy: Never
EOF

kubectl apply -f /tmp/restore-mover-$$.yaml
kubectl -n servarr wait --for=condition=ContainersReady pod/restore-mover --timeout=30s 2>/dev/null || true
sleep 2
kubectl -n servarr delete pod restore-mover
rm /tmp/restore-mover-$$.yaml

# Start app - it will auto-restore from the Backups folder
echo "[5/5] Starting $APP_NAME (will auto-restore on startup)..."
kubectl -n servarr scale deployment "$APP_NAME" --replicas=1

echo ""
echo "✓ Backup placed in Backups folder"
echo "✓ $APP_NAME starting..."
echo ""
echo "The app will automatically:"
echo "  1. Detect the backup file"
echo "  2. Restore to PostgreSQL database"
echo "  3. Start normally"
echo ""
echo "Monitor with: kubectl -n servarr logs -l app=$APP_NAME -f --tail=100"
echo ""
echo "Wait 60-90 seconds, then check if restore completed successfully."
