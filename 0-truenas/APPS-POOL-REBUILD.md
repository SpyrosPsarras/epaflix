# apps Pool Rebuild: Backup, Recreate, and Restore

## Overview

This guide covers the complete process of backing up the entire apps dataset (including Docker containers and iSCSI zvols), recreating the apps pool, and restoring everything. Use this when you need to:

- Replace disks in the apps pool
- Reconfigure RAIDZ settings
- Fix pool corruption issues
- Migrate to different disk configuration

## Prerequisites

### Space Requirements

**apps Pool Current Usage:**
```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>
sudo zfs list -o name,used,avail apps
```

**pool1 Available Space:**
```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>
sudo zfs list -o name,used,avail pool1
```

✅ **Verify pool1 has at least 2x the used space in apps** (for snapshots + some overhead)

### Services to Stop

Before migration, you'll need to stop:
1. **Docker containers** on TrueNAS
2. **K3s VMs** on Proxmox (if using iSCSI from apps)
3. **Any apps** using apps pool datasets

---

## Phase 1: Pre-Backup Checks

### Step 1: Document Current Configuration

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>

# 1. List all datasets in apps pool
sudo zfs list -r apps > ~/apps-datasets.txt

# 2. List all zvols (iSCSI targets)
sudo zfs list -t volume -r apps > ~/apps-zvols.txt

# 3. Check pool status
sudo zpool status apps > ~/apps-pool-status.txt

# 4. List iSCSI extents
sudo midclt call iscsi.extent.query | jq '.' > ~/iscsi-extents.json

# 5. List Docker containers
sudo docker ps -a > ~/docker-containers.txt

# 6. Get pool properties
sudo zpool get all apps > ~/apps-pool-properties.txt

# Download these files for reference
cat ~/apps-datasets.txt
cat ~/apps-zvols.txt
```

### Step 2: Stop All Services Using apps Pool

#### Stop Docker Containers

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>

# List all running containers
sudo docker ps

# Stop all containers (if Docker is using apps pool)
sudo docker stop $(sudo docker ps -q)

# Verify all stopped
sudo docker ps
```

#### Stop K3s VMs (if using iSCSI from apps)

```bash
# On Proxmox hosts (if iSCSI zvols are on apps pool)

# Stop all K3s VMs
for VMID in 1051 1052 1053 1061 1062 1063 1065; do
  qm stop $VMID
done

# Logout from iSCSI sessions
sudo iscsiadm -m node --logout

# Verify no active sessions
sudo iscsiadm -m session
```

### Step 3: Create Backup Dataset on pool1

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>

# Create backup dataset
sudo zfs create pool1/apps-backup

# Verify creation
sudo zfs list pool1/apps-backup
```

---

## Phase 2: Backup apps Pool to pool1

### Method 1: Full Recursive Snapshot and Send (Recommended)

This method preserves all datasets, zvols, and properties.

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>

# 1. Create a recursive snapshot of entire apps pool
SNAPSHOT_NAME="apps-backup-$(date +%Y%m%d-%H%M%S)"
sudo zfs snapshot -r apps@${SNAPSHOT_NAME}

# 2. Verify snapshot was created
sudo zfs list -t snapshot -r apps

# 3. Clean up any existing backup destination (if it exists)
# Check if destination exists
if sudo zfs list pool1/apps-backup &>/dev/null; then
  echo "Destroying existing pool1/apps-backup..."
  sudo zfs destroy -r pool1/apps-backup
fi

# 4. Send the entire apps pool to pool1 (this may take a while)
sudo zfs send -R apps@${SNAPSHOT_NAME} | sudo zfs receive -F pool1/apps-backup

# Monitor progress (in another SSH session)
watch -n 5 'sudo zfs list -r pool1/apps-backup'

# 5. Verify backup completed successfully
sudo zfs list -r pool1/apps-backup

# 6. Compare used space
echo "Original apps pool:"
sudo zfs list apps
echo ""
echo "Backup in pool1:"
sudo zfs list pool1/apps-backup
```

**Expected Duration:**
- 250GB data: 30-60 minutes depending on disk speed
- Progress is silent, use `watch` command above to monitor

### Method 2: Individual Dataset Backup (Alternative)

Use this if you want more control or selective backup.

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>

SNAPSHOT_NAME="backup-$(date +%Y%m%d-%H%M%S)"

# Get list of all datasets and zvols
DATASETS=$(sudo zfs list -H -o name -r apps | grep -v "^apps$")

# Backup each dataset individually
for dataset in $DATASETS; do
  echo "Backing up $dataset..."

  # Create snapshot
  sudo zfs snapshot ${dataset}@${SNAPSHOT_NAME}

  # Extract dataset name (remove apps/ prefix)
  relative_name=${dataset#apps/}

  # Send to pool1
  sudo zfs send ${dataset}@${SNAPSHOT_NAME} | \
    sudo zfs receive pool1/apps-backup/${relative_name}

  echo "Completed: $dataset"
done

# Verify all backups
sudo zfs list -r pool1/apps-backup
```

---

## Phase 3: Verify Backup Integrity

### Critical Verification Steps

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>

# 1. Compare dataset list
echo "Original apps pool datasets:"
sudo zfs list -r apps | wc -l

echo "Backed up datasets in pool1:"
sudo zfs list -r pool1/apps-backup | wc -l

# 2. Compare total used space
echo "apps pool used space:"
sudo zfs get -H -o value used apps

echo "Backup used space:"
sudo zfs get -H -o value used pool1/apps-backup

# 3. List all backed up datasets to understand structure
echo "Listing all backed up datasets:"
sudo zfs list -r pool1/apps-backup

# 4. Verify specific important datasets exist
# First, check which datasets exist in the source
echo "Important datasets in source:"
sudo zfs list -r apps | grep -E '(ix-applications|iscsi-|docker)' | head -10

# Then check if they exist in backup (adjust names based on your actual datasets)
echo "Checking backup structure..."
# Example verification - adjust dataset names to match YOUR actual datasets:
if sudo zfs list -r pool1/apps-backup | grep -q "ix-applications\|iscsi-\|docker"; then
  echo "✅ Key datasets found in backup"
  sudo zfs list -r pool1/apps-backup | grep -E '(ix-applications|iscsi-|docker)'
else
  echo "❌ WARNING: Key datasets not found - verify manually"
fi

# 6. Check for any snapshots that should be preserved
echo "Snapshots in backup:"
sudo zfs list -t snapshot -r pool1/apps-backup | head -20

# 7. Verify the backup snapshot exists
sudo zfs list -t snapshot pool1/apps-backup@${SNAPSHOT_NAME}
if [ $? -eq 0 ]; then
  echo "✅ Backup snapshot exists"
else
  echo "❌ Backup snapshot missing"
fi
```

**✅ If dataset counts match and space is similar, backup is successful!**

**🛑 STOP HERE IF:**
- Dataset counts don't match (65 vs different number)
- Used space differs by more than 20% (compression can cause differences)
- The backup snapshot doesn't exist

Do not proceed to destroy apps pool until backup is verified!

---

## Phase 4: Destroy and Recreate apps Pool

### ⚠️ WARNING: Point of No Return

Once you destroy the apps pool, the only way to recover is from the pool1 backup.

### Step 1: Export apps Pool

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>

# 1. Unmount all datasets (if mounted)
sudo zfs unmount -a

# 2. Export the apps pool
sudo zpool export apps

# 3. Verify pool is exported
sudo zpool list
# apps should not appear in the list
```

### Step 2: Recreate apps Pool

#### Option A: Same Configuration (RAIDZ1 with 3 disks)

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>

# 1. Identify disk IDs (replace with your actual disk IDs)
# Use: ls -l /dev/disk/by-id/ | grep -v part
DISK1="/dev/disk/by-id/ata-SAMSUNG_SSD_XXXXX"
DISK2="/dev/disk/by-id/ata-SAMSUNG_SSD_YYYYY"
DISK3="/dev/disk/by-id/ata-SAMSUNG_SSD_ZZZZZ"

# 2. Create new apps pool with RAIDZ1
sudo zpool create -f apps raidz1 $DISK1 $DISK2 $DISK3

# 3. Set pool properties (adjust as needed)
sudo zfs set compression=lz4 apps
sudo zfs set atime=off apps

# 4. Verify pool creation
sudo zpool status apps
sudo zpool list apps
```

#### Option B: Different Configuration (Mirror, RAIDZ2, etc.)

```bash
# For mirror (2 disks):
sudo zpool create -f apps mirror $DISK1 $DISK2

# For RAIDZ2 (4+ disks):
sudo zpool create -f apps raidz2 $DISK1 $DISK2 $DISK3 $DISK4

# For stripe (not recommended):
sudo zpool create -f apps $DISK1 $DISK2
```

### Step 3: Verify New Pool

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>

# Check pool health
sudo zpool status apps

# Check pool properties
sudo zpool get all apps

# Verify mountpoint
sudo zfs get mountpoint apps
# Should be: /mnt/apps
```

---

## Phase 5: Restore Data from pool1 Backup

### Method 1: Full Restore (Recommended)

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>

# 1. Get the snapshot name used during backup
SNAPSHOT_NAME=$(sudo zfs list -t snapshot -r pool1/apps-backup | grep apps-backup@ | head -1 | awk '{print $1}' | cut -d@ -f2)

echo "Using snapshot: $SNAPSHOT_NAME"

# 2. Send backup from pool1 back to apps pool
sudo zfs send -R pool1/apps-backup@${SNAPSHOT_NAME} | sudo zfs receive -F apps

# Monitor progress in another terminal
watch -n 5 'zfs list -r apps'

# 3. Verify restoration
sudo zfs list -r apps

# 4. Check that data is accessible
ls -la /mnt/apps/
```

**Expected Duration:**
- 250GB data: 30-60 minutes
- Same speed as backup phase

### Method 2: Selective Restore (Alternative)

Restore only specific datasets if needed.

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>

SNAPSHOT_NAME="backup-YYYYMMDD-HHMMSS"  # Use your actual snapshot name

# List available datasets in backup
sudo zfs list -r pool1/apps-backup

# Restore specific datasets
for dataset in \
  "pool1/apps-backup/ix-applications" \
  "pool1/apps-backup/iscsi-master-51" \
  "pool1/apps-backup/iscsi-worker-61"; do

  # Extract relative name
  relative_name=${dataset#pool1/apps-backup/}

  echo "Restoring $relative_name..."

  sudo zfs send ${dataset}@${SNAPSHOT_NAME} | \
    sudo zfs receive apps/${relative_name}
done

# Verify
sudo zfs list -r apps
```

---

## Phase 6: Restore Services and Verify

### Step 1: Verify Data Integrity

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>

# 1. Compare dataset count
echo "Original backup:"
sudo zfs list -r pool1/apps-backup | wc -l

echo "Restored apps pool:"
sudo zfs list -r apps | wc -l

# 2. Check specific important paths
ls -la /mnt/apps/ix-applications/
ls -la /mnt/apps/

# 3. Verify zvols exist for iSCSI
sudo zfs list -t volume -r apps

# 4. Check file permissions
sudo ls -la /mnt/apps/ix-applications/docker/
```

### Step 2: Restore iSCSI Configuration

If you have iSCSI zvols, update extent paths in TrueNAS:

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>

# 1. List all extents
sudo midclt call iscsi.extent.query | jq -r '.[] | "\(.id) \(.name) \(.path)"'

# 2. Update extent paths (if needed)
# Example: Update extent ID 1 to point to new zvol path
# sudo midclt call iscsi.extent.update <EXTENT_ID> '{"path": "/dev/zvol/apps/iscsi-master-51"}'

# Most likely, if you used zfs receive, paths should already be correct
# Just verify extents are pointing to apps pool:
sudo midclt call iscsi.extent.query | jq -r '.[] | .path' | grep apps
```

**Via TrueNAS Web UI:**
1. Go to **Sharing → Block Shares (iSCSI)**
2. Click **Extents**
3. Verify each extent points to correct zvol path in apps pool
4. Should be: `/dev/zvol/apps/iscsi-*`

### Step 3: Restart Docker (if applicable)

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>

# 1. Check Docker service status
sudo service docker status

# 2. Start Docker if not running
sudo service docker start

# 3. Verify containers
sudo docker ps -a

# 4. Start containers if needed
sudo docker start $(sudo docker ps -aq)

# 5. Check container logs
sudo docker logs <container_name>
```

### Step 4: Restart K3s VMs (if applicable)

```bash
# On Proxmox hosts

# 1. Re-enable iSCSI storage
for NODE in master-51 master-52 master-53 worker-61 worker-62 worker-63 worker-65; do
  pvesm set iscsi-${NODE} --disable 0
done

# 2. Rescan iSCSI targets
iscsiadm -m discovery -t st -p <TRUENAS_IP>:3260

# 3. Login to targets
iscsiadm -m node --login

# 4. Verify sessions
iscsiadm -m session

# 5. Start VMs
for VMID in 1051 1052 1053 1061 1062 1063 1065; do
  qm start $VMID
  sleep 30  # Wait between starts
done

# 6. Verify VMs are running
for VMID in 1051 1052 1053 1061 1062 1063 1065; do
  qm status $VMID
done

# 7. Check K3s cluster health
ssh <K3S_USER>@<MASTER_IP>
kubectl get nodes
kubectl get pods -A
```

---

## Phase 7: Cleanup and Final Verification

### Step 1: Test All Services

```bash
# Test iSCSI connectivity from Proxmox
ssh <PROXMOX_IP>
lsblk | grep iscsi

# Test Docker apps
ssh <TRUENAS_USER>@<TRUENAS_IP>
sudo docker ps

# Test K3s cluster (if applicable)
ssh <K3S_USER>@<MASTER_IP>
kubectl cluster-info
kubectl get all -A
```

### Step 2: Monitor Performance

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>

# Monitor pool IO
sudo zpool iostat apps 5

# Check for errors
sudo zpool status apps

# Check disk health
sudo smartctl -a /dev/sdX  # Replace with actual disk
```

### Step 3: Clean Up Backup (Optional)

**⚠️ Only delete backup after confirming everything works for at least 24-48 hours**

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>

# Review backup size
sudo zfs list pool1/apps-backup

# Delete backup dataset (ONLY when confirmed apps pool is working)
# sudo zfs destroy -r pool1/apps-backup

# Or keep the backup snapshot for safety
# Delete only after 1-2 weeks of confirmed operation
```

---

## Rollback Procedure (If Something Goes Wrong)

If the restore fails or data is corrupted, you can restore from pool1 backup:

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP>

# 1. Export the corrupted apps pool
sudo zpool export apps

# 2. Recreate apps pool (see Phase 4, Step 2)

# 3. Restore from pool1 backup again (see Phase 5)

# 4. If that fails, you can temporarily use pool1/apps-backup directly:
# Rename backup to apps (emergency only)
sudo zfs rename pool1/apps-backup apps

# This will make your backup the live pool
# Then create new backup location and try again
```

---

## Troubleshooting

### Issue: "Pool is busy" during export

```bash
# Find what's using the pool
sudo lsof | grep /mnt/apps
sudo fuser -vm /mnt/apps

# Kill processes if needed
sudo fuser -km /mnt/apps

# Try export again
sudo zpool export apps
```

### Issue: "Dataset is busy" during zfs send/receive

```bash
# Stop all services using the dataset
sudo systemctl stop docker
sudo zfs unmount -a

# Try again
```

### Issue: "destination has snapshots" error during receive

```bash
# Error: cannot receive new filesystem stream: destination has snapshots
# Solution: Destroy existing destination dataset

sudo zfs destroy -r pool1/apps-backup

# Then retry the send/receive
sudo zfs send -R apps@${SNAPSHOT_NAME} | sudo zfs receive -F pool1/apps-backup
```

### Issue: Snapshots not transferring

```bash
# Use -R flag for recursive send including snapshots
sudo zfs send -R apps@snapshot | sudo zfs receive pool1/apps-backup

# Or send individual snapshots
sudo zfs send apps@snapshot1 apps@snapshot2 | sudo zfs receive pool1/apps-backup
```

### Issue: Not enough space in pool1

```bash
# Check actual space needed
sudo zfs list -r apps

# Check available space
sudo zfs list pool1

# Clean up old snapshots if needed
sudo zfs list -t snapshot -r pool1
sudo zfs destroy pool1/dataset@old-snapshot

# Or use compression during send
sudo zfs send -R apps@snapshot | gzip | pv | gunzip | sudo zfs receive pool1/apps-backup
```

### Issue: iSCSI extents not working after restore

```bash
# Verify zvol paths
sudo zfs list -t volume -r apps

# Should show zvols like: apps/iscsi-master-51

# Update extent paths in TrueNAS UI
# Go to: Sharing → Block Shares (iSCSI) → Extents
# Update Device path to: /dev/zvol/apps/iscsi-master-51

# Restart iSCSI service
sudo midclt call service.restart iscsi
```

---

## Estimated Timeline

| Phase | Duration | Notes |
|-------|----------|-------|
| Pre-backup checks | 15 minutes | Documentation and planning |
| Stop services | 10 minutes | Docker, VMs, etc. |
| Backup to pool1 | 30-90 minutes | Depends on data size (250GB) |
| Verify backup | 15 minutes | Critical step |
| Destroy and recreate pool | 5 minutes | Fast operation |
| Restore from pool1 | 30-90 minutes | Same as backup duration |
| Restore services | 20 minutes | Docker, iSCSI, VMs |
| Testing and verification | 30 minutes | Comprehensive testing |
| **Total** | **2.5-4 hours** | With 250GB of data |

---

## Summary Checklist

- [ ] Document current apps pool configuration
- [ ] Stop all services (Docker, VMs, apps)
- [ ] Create backup dataset on pool1
- [ ] Create recursive snapshot of apps pool
- [ ] Send snapshot to pool1/apps-backup
- [ ] Verify backup integrity (critical!)
- [ ] Export apps pool
- [ ] Recreate apps pool with desired configuration
- [ ] Restore data from pool1 backup
- [ ] Verify restored data integrity
- [ ] Update iSCSI extent paths (if needed)
- [ ] Restart Docker containers
- [ ] Restart K3s VMs and verify cluster
- [ ] Monitor for 24-48 hours
- [ ] Clean up backup (after confirmation)

---

## References

- [ZFS send/receive](https://docs.oracle.com/cd/E19253-01/819-5461/gbchx/index.html)
- [TrueNAS Documentation](https://www.truenas.com/docs/)
- [Proxmox iSCSI Storage](https://pve.proxmox.com/wiki/Storage:_iSCSI)

## Related Documentation

- [MIGRATION-TO-SSD.md](MIGRATION-TO-SSD.md) - For moving iSCSI targets between pools
- [MIGRATION-QUICK-START.md](MIGRATION-QUICK-START.md) - Quick migration guide
- [README.md](README.md) - TrueNAS setup overview
