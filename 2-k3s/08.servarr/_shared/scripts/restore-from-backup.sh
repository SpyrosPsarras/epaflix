#!/bin/bash
# Automated restore script for *arr applications using PostgreSQL
# Based on Servarr Wiki: https://wiki.servarr.com/prowlarr/faq#using-file-system-backup

set -e

APP_NAME="$1"
BACKUP_ZIP="$2"

if [ -z "$APP_NAME" ] || [ -z "$BACKUP_ZIP" ]; then
    echo "Usage: $0 <app-name> <backup-zip-file>"
    echo "Example: $0 prowlarr /tmp/prowlarr_backup.zip"
    exit 1
fi

if [ ! -f "$BACKUP_ZIP" ]; then
    echo "Error: Backup file not found: $BACKUP_ZIP"
    exit 1
fi

echo "=== Restoring $APP_NAME from $BACKUP_ZIP ==="

# Step 1: Scale deployment to 0
echo "[1/6] Stopping $APP_NAME..."
kubectl -n servarr scale deployment "$APP_NAME" --replicas=0
sleep 5

# Step 2: Find the hostPath for the app's config
echo "[2/6] Finding config volume path..."
PVC_NAME="${APP_NAME}-config"
PV_NAME=$(kubectl -n servarr get pvc "$PVC_NAME" -o jsonpath='{.spec.volumeName}')
CONFIG_PATH=$(kubectl get pv "$PV_NAME" -o jsonpath='{.spec.hostPath.path}')

if [ -z "$CONFIG_PATH" ]; then
    echo "Error: Could not find hostPath for $APP_NAME config"
    exit 1
fi

echo "   Config path: $CONFIG_PATH"

# Step 3: Find which node has the volume first
echo "[3/6] Finding node with volume..."
NODE=""
for N in $(kubectl get nodes -o jsonpath='{.items[*].metadata.name}'); do
    kubectl debug node/"$N" -it --image=busybox:latest -- sh -c "[ -d /host$CONFIG_PATH ]" 2>/dev/null && {
        NODE="$N"
        echo "   Found on node: $NODE"
        break
    }
done

if [ -z "$NODE" ]; then
    echo "Error: Could not find node with volume"
    exit 1
fi

# Step 4: Extract backup and copy to node's /tmp
echo "[4/6] Extracting and uploading backup to node..."
TEMP_DIR="/tmp/${APP_NAME}_restore_$$"
NODE_TEMP="/tmp/${APP_NAME}_restore_$$"
mkdir -p "$TEMP_DIR"
unzip -q "$BACKUP_ZIP" -d "$TEMP_DIR"

# Copy extracted files to node via kubectl cp
TAR_FILE="/tmp/${APP_NAME}_backup_$$.tar"
tar -czf "$TAR_FILE" -C "$TEMP_DIR" .

# Use a temporary pod to receive the tar file
cat > /tmp/upload-pod-$$.yaml <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: upload-helper-$$
  namespace: servarr
spec:
  nodeName: $NODE
  containers:
  - name: uploader
    image: busybox:latest
    command: ['sleep', '600']
    volumeMounts:
    - name: tmp
      mountPath: /tmp-host
  volumes:
  - name: tmp
    hostPath:
      path: /tmp
  restartPolicy: Never
EOF

kubectl apply -f /tmp/upload-pod-$$.yaml >/dev/null
kubectl -n servarr wait --for=condition=Ready pod/upload-helper-$$ --timeout=60s >/dev/null 2>&1

# Upload and extract
kubectl -n servarr cp "$TAR_FILE" upload-helper-$$:/tmp/backup.tar.gz
kubectl -n servarr exec upload-helper-$$ -- sh -c "mkdir -p /tmp-host/${APP_NAME}_restore && cd /tmp-host/${APP_NAME}_restore && tar -xzf /tmp/backup.tar.gz"
kubectl -n servarr delete pod upload-helper-$$ >/dev/null
rm /tmp/upload-pod-$$.yaml "$TAR_FILE"

# Step 5: Clean existing data
echo "[5/6] Cleaning existing database files..."
kubectl debug node/"$NODE" -it --image=busybox:latest -- sh -c "
    rm -f /host${CONFIG_PATH}/*.db 2>/dev/null || true
    rm -f /host${CONFIG_PATH}/*.db-wal 2>/dev/null || true
    rm -f /host${CONFIG_PATH}/*.db-journal 2>/dev/null || true
    rm -f /host${CONFIG_PATH}/config.xml 2>/dev/null || true
" 2>/dev/null

# Step 6: Copy backup files to config directory
echo "[6/6] Copying backup files to config directory..."
cat > /tmp/restore-pod-$$.yaml <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: restore-helper-$$
  namespace: servarr
spec:
  nodeName: $NODE
  containers:
  - name: restore
    image: busybox:latest
    command: ['sh', '-c', 'cp -r /backup/* /config/ && sleep 10']
    volumeMounts:
    - name: config
      mountPath: /config
    - name: backup
      mountPath: /backup
  volumes:
  - name: config
    hostPath:
      path: $CONFIG_PATH
  - name: backup
    hostPath:
      path: /tmp/${APP_NAME}_restore
  restartPolicy: Never
EOF

kubectl apply -f /tmp/restore-pod-$$.yaml >/dev/null
kubectl -n servarr wait --for=condition=ContainersReady pod/restore-helper-$$ --timeout=60s 2>/dev/null || sleep 5
kubectl -n servarr delete pod restore-helper-$$ >/dev/null 2>&1 || true
rm /tmp/restore-pod-$$.yaml

# Cleanup
rm -rf "$TEMP_DIR"
kubectl debug node/"$NODE" -it --image=busybox:latest -- sh -c "rm -rf /host/tmp/${APP_NAME}_restore" 2>/dev/null || true

# Step 7: Start the application
echo "[7/7] Starting $APP_NAME..."
kubectl -n servarr scale deployment "$APP_NAME" --replicas=1

echo ""
echo "=== Restore complete! ==="
echo "Waiting for $APP_NAME to start..."
sleep 30

kubectl -n servarr get pods -l app="$APP_NAME"

echo ""
echo "Monitor logs with: kubectl -n servarr logs -l app=$APP_NAME -f"
echo ""
echo "Note: The app will migrate data to PostgreSQL on first startup."
echo "This may take a few minutes depending on backup size."
