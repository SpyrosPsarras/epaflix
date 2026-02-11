# Migration Plan: iSCSI Targets from HDD to SSD Pool

## Overview

This document provides a step-by-step plan to migrate K3s VM iSCSI targets from Pool1 (HDD stripe) to Apps pool (SSD RAIDZ1) for dramatic performance improvement.

## Current State

**Pool1 (HDD - Slow):**
- 2x HDDs: 10TB + 14TB in stripe mode
- Performance: ~100-200 IOPS, 100-150 MB/s
- Current use: iSCSI targets for K3s VMs
- IO Pressure: 10-20%

**Apps Pool (SSD - Fast):**
- 3x 250GB SSDs in RAIDZ1
- Performance: ~10,000+ IOPS, 500+ MB/s
- Usable capacity: ~500GB
- Current use: Apps and other storage

## Space Requirements

### Per-VM Disk Size
- **Master nodes** (3): ~30GB each = 90GB total
- **Worker nodes** (4): ~40GB each = 160GB total
- **Total needed**: 250GB
- **Available in Apps pool**: ~500GB usable

✅ **Sufficient space available**

## Migration Approach: Move Instead of Create

This migration uses **ZFS rename** to move existing iSCSI zvols from Pool1 (HDD) to Apps pool (SSD). This is superior to creating new targets because:

1. **No data copy**: ZFS rename is instant (just metadata change)
2. **No extra space**: Don't need double the space for cloning
3. **Minimal downtime**: 5-10 minutes total
4. **Same targets**: iSCSI targets/IQNs remain the same
5. **Fast operation**: Move 7 VMs in minutes, not hours

**Process:**
1. Stop VMs and disconnect iSCSI on Proxmox
2. Use `zfs rename` to move zvols from Pool1 to Apps
3. Update iSCSI extents in TrueNAS to point to new location
4. Reconnect iSCSI and start VMs

**Backup Strategy:**
- Optional: Backup VMs to NFS on Pool1 (uses /mnt/Pool1/proxmox-vm-backups)
- Alternative: Just backup application data (PostgreSQL, Authentik configs)

### What Stays on Pool1 vs Moves to Apps

**Moving to Apps Pool (SSD):**
- ✅ iSCSI zvols for K3s VMs (high IOPS workload)
- ✅ VM operating systems and K3s binaries
- ✅ PVCs with databases and application configs

**Staying on Pool1 (HDD):**
- ✅ Media files NFS shares (animes, movies, tvshows, downloads)
- ✅ VM backups (proxmox-vm-backups)
- ✅ ISO files and other large, infrequently accessed data

**Reason:** Media streaming and backups don't need SSD performance, keeping them on HDD saves SSD space for high-IOPS workloads.

---

## Migration Strategy

### Option A: Gradual Migration (Recommended for Production)

Migrate VMs one-by-one with minimal downtime to the cluster.

**Pros:**
- Cluster remains operational during migration
- Can roll back if issues occur
- Less risky

**Cons:**
- Takes longer (several hours to days)
- Requires careful coordination

**Steps**: See [Gradual Migration](#gradual-migration-procedure) below

---

### Option B: Full Cluster Rebuild (Fastest)

Recreate entire cluster from cloud-init templates on new SSD storage.

**Pros:**
- Clean slate, no migration complexity
- Faster overall process
- Can test before switching over

**Cons:**
- Full cluster downtime
- Need to restore application state
- Requires good backups

**Steps**: See [Full Rebuild](#full-cluster-rebuild-procedure) below

---

## Gradual Migration Procedure

### Prerequisites

#### Setup NFS Backup Storage (One-Time)

**On TrueNAS:**
```bash
# Create backup dataset on Pool1 (not using deprecated k3s-containers)
ssh <TRUENAS_ADMIN>@<TRUENAS_IP>
sudo zfs create Pool1/proxmox-vm-backups
sudo chmod 755 /mnt/Pool1/proxmox-vm-backups

# Create NFS share for backups
# Via TrueNAS UI: Sharing → Unix Shares (NFS)
# Path: /mnt/Pool1/proxmox-vm-backups
# Authorized Networks: <PROXMOX_NETWORK>/24
```

**On both Proxmox hosts:**
```bash
# Add NFS backup storage to Proxmox
pvesm add nfs backup-nfs \
  --server <TRUENAS_IP> \
  --export /mnt/Pool1/proxmox-vm-backups \
  --content backup,vztmpl \
  --maxfiles 5

# Verify
pvesm status | grep backup-nfs

# Test mount
mount | grep backup-nfs
```

#### Verify Space Availability

```bash
# 1. SSH into TrueNAS
ssh <TRUENAS_ADMIN>@<TRUENAS_IP>

# 2. Verify Apps pool has space for new VMs
zfs list -o name,used,avail Apps
# Should show ~500GB available

# 3. Verify Pool1 has space for backups (if using backups)
zfs list -o name,used,avail Pool1
# Should show several TB available
```

#### Backup Important Data

```bash
# Export Kubernetes configs
kubectl get all -A -o yaml > k8s-backup-$(date +%F).yaml

# Backup PostgreSQL databases (if using)
cd 2-k3s/06.postgres/backup
./backup-all-databases.sh

# Backup Authentik (if using)
cd 2-k3s/07.authentik-deployment
./backup.sh
```

### Phase 1: Migrate First Master (master-51)

#### 1.1 Move Existing iSCSI zvol to Apps Pool

**On TrueNAS:**

```bash
# SSH into TrueNAS
ssh <TRUENAS_ADMIN>@<TRUENAS_IP>

# Check current zvol location
sudo zfs list | grep iscsi-master-51
# Example output: Pool1/iscsi-master-51

# IMPORTANT: Disconnect iSCSI sessions from Proxmox FIRST
# (Do this from Proxmox hosts - see step 1.1.1)

# After Proxmox disconnects, rename/move zvol to Apps pool
sudo zfs rename Pool1/iscsi-master-51 Apps/iscsi-master-51

# Verify new location
sudo zfs list Apps/iscsi-master-51
# Should show: Apps/iscsi-master-51  30G  ...
```

**Step 1.1.1: Disconnect iSCSI from Proxmox (Do BEFORE zvol move)**

**On both Proxmox hosts:**
```bash
# Stop VM using this storage
qm stop 1051

# Disable storage in Proxmox
pvesm set iscsi-master-51 --disable 1

# Logout from iSCSI target
iscsiadm -m node --targetname iqn.2024-01.local.truenas:master-51 --logout

# Verify disconnected
iscsiadm -m session | grep master-51
# Should return nothing
```

**Step 1.1.2: Update iSCSI Extent in TrueNAS**

**Via TrueNAS UI:**
1. **Sharing** → **Block Shares (iSCSI)** → **Extents**
2. Find extent for **master-51**
3. **Edit** the extent
4. Update **Device** path to: `zvol/Apps/iscsi-master-51`
5. **Save**

**Via CLI (alternative):**
```bash
# On TrueNAS
sudo midclt call iscsi.extent.update <extent_id> '{"path": "zvol/Apps/iscsi-master-51"}'
```

#### 1.2 Reconnect iSCSI Storage to Proxmox

**On both Proxmox hosts:**

```bash
# Login to target (same target, now pointing to SSD zvol)
iscsiadm -m node --targetname iqn.2024-01.local.truenas:master-51 --portal <TRUENAS_IP>:3260 --login

# Re-enable storage in Proxmox
pvesm set iscsi-master-51 --disable 0

# Rescan storage
pvesm set iscsi-master-51 --content images

# Verify - should now be backed by Apps pool SSD
pvesm status | grep iscsi-master-51
iscsiadm -m session | grep master-51
```

#### 1.3 Backup Current VM (Optional but Recommended)

**Choose one of these backup strategies:**

**Option A: Backup to NFS (Recommended - Uses Pool1 HDD space)**

**On Proxmox host where VM 1051 runs:**
```bash
# Backup to NFS storage on TrueNAS Pool1
vzdump 1051 --mode snapshot --compress zstd --storage backup-nfs

# Verify backup
pvesm list backup-nfs | grep 1051

# Note: This uses Pool1 space, not local Proxmox storage
```

**Option B: Skip Backup (Acceptable for cloud-init VMs)**

Since K3s VMs are created from cloud-init templates and can be recreated:
```bash
# No backup needed if you:
# 1. Have cloud-init template available
# 2. Have application data backed up (PostgreSQL, Authentik)
# 3. Can tolerate brief downtime to recreate VM

# Just document current VM config
qm config 1051 > /tmp/vm-1051-config.txt
```

**Option C: Export VM Config Only (Minimal backup)**
```bash
# Just export the VM configuration
qm config 1051 > /root/vm-configs/1051.conf

# This allows you to recreate VM with same settings
# Application data persists in K3s PVCs on other nodes
```

#### 1.4 Start VM on Migrated Storage

**The VM disk is now on SSD - just start it:**

```bash
# Start VM (same VMID, now backed by SSD storage)
qm start 1051

# Wait for boot
sleep 30

# Verify functionality
ssh ubuntu@192.168.10.51
sudo systemctl status k3s
sudo kubectl get nodes

# Check IO performance improvement
iostat -x 5 3
```

**That's it!** The zvol move was transparent to Proxmox. Same VM, same disk, now on SSD.

#### 1.5 Verify Migration Success

```bash
# Check storage is active
pvesm status | grep iscsi-master-51

# Check iSCSI session
iscsiadm -m session | grep master-51

# Verify zvol is on Apps pool
ssh <TRUENAS_ADMIN>@<TRUENAS_IP> "zfs list | grep iscsi-master-51"
# Should show: Apps/iscsi-master-51
```

#### 1.6 Verify and Monitor

**Check IO metrics:**

```bash
# From Proxmox UI: VM Summary → IO Pressure
# Expected: Drop from 10-20% to <5%

# From VM
ssh ubuntu@192.168.10.51
iostat -x 5 3
# Look for improved await times and %util
```

**Kubernetes health:**

```bash
kubectl get nodes
kubectl get pods -A | grep -v Running
```

### Phase 2: Migrate Remaining Masters (master-52, master-53)

Repeat Phase 1 steps for master-52 (VMID 1052) and master-53 (VMID 1053).

**Important:** Wait 24-48 hours between migrations to ensure stability.

### Phase 3: Migrate Worker Nodes (worker-61, 62, 63, 65)

Repeat Phase 1 steps for each worker node:
- worker-61 (VMID 1061) - 40GB
- worker-62 (VMID 1062) - 40GB
- worker-63 (VMID 1063) - 40GB
- worker-65 (VMID 1065) - 40GB

**Drain nodes before migration:**

```bash
# Drain worker before shutdown
kubectl drain worker-61 --ignore-daemonsets --delete-emptydir-data

# After migration, uncordon
kubectl uncordon worker-61
```

### Phase 4: Cleanup

**On TrueNAS:**

```bash
# After all VMs are migrated and verified (wait 1 week)
# Pool1 should now be free of iSCSI zvols

# Verify no iSCSI zvols remain on Pool1
ssh <TRUENAS_ADMIN>@<TRUENAS_IP>
sudo zfs list Pool1 | grep iscsi
# Should return nothing (zvols were moved, not copied)

# Clean up deprecated backup locations (if they exist)
# Note: /mnt/pool1/k3s-containers-backup is deprecated
# New location: /mnt/Pool1/proxmox-vm-backups

# Optional: Remove old backup data after migration
# ls -la /mnt/pool1/k3s-containers-backup
# rm -rf /mnt/pool1/k3s-containers-backup (BE CAREFUL!)
```

**NFS Exports - Verify correct paths:**

The following NFS exports should remain on Pool1 (HDD) for media:
- `/mnt/pool1/dataset01/animes`
- `/mnt/pool1/dataset01/downloads`
- `/mnt/pool1/dataset01/movies`
- `/mnt/pool1/dataset01/tvshows`

New NFS export for VM backups:
- `/mnt/Pool1/proxmox-vm-backups`

**On Proxmox hosts:**

```bash
# Verify NFS backup storage points to correct location
pvesm status | grep backup-nfs
# Should show: /mnt/Pool1/proxmox-vm-backups

# Optional: Update old backup storage if it exists
# pvesm set backup-nfs --export /mnt/Pool1/proxmox-vm-backups
```

---

## Full Cluster Rebuild Procedure

This is faster but requires full cluster downtime.

### Prerequisites

#### 1. Setup NFS Backup Storage

**On TrueNAS:**
```bash
# Create backup dataset on Pool1
ssh <TRUENAS_ADMIN>@<TRUENAS_IP>
sudo zfs create Pool1/proxmox-vm-backups
sudo chmod 755 /mnt/Pool1/proxmox-vm-backups

# Create NFS share via TrueNAS UI:
# Sharing → Unix Shares (NFS)
# Path: /mnt/Pool1/proxmox-vm-backups
```

**On both Proxmox hosts:**
```bash
# Add NFS storage for VM backups (uses Pool1 HDD space)
pvesm add nfs backup-nfs \
  --server <TRUENAS_IP> \
  --export /mnt/Pool1/proxmox-vm-backups \
  --content backup,vztmpl \
  --maxfiles 10

pvesm status | grep backup-nfs
```

#### 2. Backup Application Data

```bash
# Kubernetes resources
kubectl get all -A -o yaml > k8s-backup-$(date +%F).yaml
kubectl get pv,pvc -A -o yaml > k8s-storage-$(date +%F).yaml

# PostgreSQL databases (if using)
cd 2-k3s/06.postgres/backup
./backup-all-databases.sh

# Authentik (if using)
cd 2-k3s/07.authentik-deployment
./backup.sh

# Document cluster state
kubectl get nodes -o wide > nodes-backup.txt
kubectl get svc,ing -A > services-backup.txt
```

#### 3. Backup VMs to NFS (Optional)

**Only if you want full VM backups:**
```bash
# Backup all VMs to NFS on Pool1 (uses HDD space, not local SSD)
for VMID in 1051 1052 1053 1061 1062 1063 1065; do
  vzdump $VMID --mode snapshot --compress zstd --storage backup-nfs
done

# This will take time but uses Pool1 space
# Each VM backup: ~3-10GB compressed
# Total: ~30-70GB on Pool1 (plenty of space available)
```

**Alternative: No VM backups needed**
```bash
# If you can recreate VMs from cloud-init templates:
# 1. Save VM configs only
mkdir -p /root/vm-configs-backup
for VMID in 1051 1052 1053 1061 1062 1063 1065; do
  qm config $VMID > /root/vm-configs-backup/$VMID.conf
done

# 2. Copy configs to safe location
scp -r /root/vm-configs-backup/ <YOUR_BACKUP_LOCATION>

# That's it! VMs can be recreated from templates in <5 minutes each
```

### Phase 1: Move Existing iSCSI zvols to Apps Pool

**On TrueNAS**, move all zvols from Pool1 to Apps:

```bash
# SSH into TrueNAS
ssh <TRUENAS_ADMIN>@<TRUENAS_IP>

# List current zvols
sudo zfs list | grep iscsi
# Should show all on Pool1

# Move all zvols to Apps pool (do this AFTER disconnecting Proxmox)
# See Phase 2 for Proxmox disconnect steps

sudo zfs rename Pool1/iscsi-master-51 Apps/iscsi-master-51
sudo zfs rename Pool1/iscsi-master-52 Apps/iscsi-master-52
sudo zfs rename Pool1/iscsi-master-53 Apps/iscsi-master-53
sudo zfs rename Pool1/iscsi-worker-61 Apps/iscsi-worker-61
sudo zfs rename Pool1/iscsi-worker-62 Apps/iscsi-worker-62
sudo zfs rename Pool1/iscsi-worker-63 Apps/iscsi-worker-63
sudo zfs rename Pool1/iscsi-worker-65 Apps/iscsi-worker-65

# Verify new location
sudo zfs list Apps | grep iscsi
# Should show all 7 zvols under Apps pool
```

**Update iSCSI extents in TrueNAS UI:**
1. **Sharing** → **Block Shares (iSCSI)** → **Extents**
2. For each extent (master-51, master-52, etc.):
   - Edit the extent
   - Update **Device** path to: `zvol/Apps/iscsi-master-XX` (or worker-XX)
   - Save
3. Verify all extents point to Apps pool

### Phase 2: Disconnect and Reconnect iSCSI on Proxmox

**On both Proxmox hosts:**

```bash
# Stop all VMs
for VMID in 1051 1052 1053 1061 1062 1063 1065; do
  qm stop $VMID
done

# Disable all iSCSI storage
for NODE in master-51 master-52 master-53 worker-61 worker-62 worker-63 worker-65; do
  pvesm set iscsi-${NODE} --disable 1
done

# Logout from all iSCSI targets
iscsiadm -m node --logout

# Wait for TrueNAS zvol move and extent update (Phase 1)
echo "Waiting for TrueNAS zvol migration..."
sleep 10

# Rescan and login to targets (same targets, now backed by SSD)
iscsiadm -m discovery -t st -p <TRUENAS_IP>:3260
iscsiadm -m node --login

# Re-enable all storage
for NODE in master-51 master-52 master-53 worker-61 worker-62 worker-63 worker-65; do
  pvesm set iscsi-${NODE} --disable 0
done

# Rescan storage
pvesm set iscsi-master-51 --content images
pvesm set iscsi-master-52 --content images
pvesm set iscsi-master-53 --content images
pvesm set iscsi-worker-61 --content images
pvesm set iscsi-worker-62 --content images
pvesm set iscsi-worker-63 --content images
pvesm set iscsi-worker-65 --content images

# Verify all reconnected
pvesm status | grep iscsi
iscsiadm -m session
```

### Phase 3: Restart VMs on SSD Storage

```bash
# VMs are already stopped from Phase 2
# Storage is now backed by SSD pool
# Just start VMs

for VMID in 1051 1052 1053 1061 1062 1063 1065; do
  qm start $VMID
  echo "Started VM $VMID"
  sleep 5
done

# Wait for boot
sleep 60
```

### Phase 4: Verify Migration

### Phase 5: Verify Cluster Health

```bash
# Wait for VMs to boot
sleep 60

# Check K3s cluster status
ssh ubuntu@192.168.10.51 "sudo kubectl get nodes -o wide"

# Check all pods
ssh ubuntu@192.168.10.51 "sudo kubectl get pods -A"

# Verify applications
ssh ubuntu@192.168.10.51 "sudo kubectl get svc -A"
```

### Phase 6: Monitor Performance

```bash
# Check IO pressure on Proxmox UI for each VM
# Should drop from 10-20% to <3%

# From any K3s node
ssh ubuntu@192.168.10.51
iostat -x 5 3
# Look for: low await times, low %util

# Benchmark (optional)
sudo apt install fio -y
fio --name=test --ioengine=libaio --iodepth=16 --rw=randread \
    --bs=4k --direct=1 --size=1G --runtime=30
# Expected: 10,000+ IOPS (vs 200-500 on HDD)
```

---

## Performance Verification

After migration, measure improvements:

### IO Pressure Metrics

**From Proxmox UI:**
- Navigate to each VM → Summary
- Check **IO Pressure Stall**: Should drop from 10-20% to <3%

### Disk Performance

**Inside each VM:**

```bash
# Install fio if not present
sudo apt install -y fio

# Test random read IOPS
fio --name=randread --ioengine=libaio --iodepth=16 --rw=randread \
    --bs=4k --direct=1 --size=1G --numjobs=4 --runtime=60 --group_reporting

# Expected results:
# HDD: ~200-500 IOPS
# SSD: ~10,000-20,000 IOPS
```

### Application Performance

**Measure container start times:**

```bash
# Before migration
time kubectl delete pod <pod-name> -n <namespace>
# Measure time until Running status

# After migration
# Should be 2-3x faster
```

**Database query performance:**

```bash
# PostgreSQL query latency
kubectl exec -it -n database-postgres postgres-cluster-1 -- \
  psql -U postgres -c "EXPLAIN ANALYZE SELECT * FROM large_table LIMIT 100;"

# Check for reduced IO wait times
```

---

## Rollback Plan

If issues occur during migration:

1. **Before zvol move (still on Pool1)**:
   ```bash
   # Just reconnect and start VMs
   iscsiadm -m node --login
   pvesm set iscsi-master-<X> --disable 0
   qm start <VMID>
   ```

2. **After zvol move to Apps (issues detected)**:
   ```bash
   # Stop VMs and disconnect iSCSI
   qm stop <VMID>
   iscsiadm -m node --logout

   # Move zvols back to Pool1 on TrueNAS
   ssh <TRUENAS_ADMIN>@<TRUENAS_IP>
   sudo zfs rename Apps/iscsi-master-51 Pool1/iscsi-master-51

   # Update iSCSI extents back to Pool1
   # TrueNAS UI: Sharing → iSCSI → Extents
   # Device: zvol/Pool1/iscsi-master-51

   # Reconnect and start
   iscsiadm -m node --login
   pvesm set iscsi-master-51 --disable 0
   qm start 1051
   ```

3. **Restore from NFS backup (if taken)**:
   ```bash
   pvesm list backup-nfs
   qmrestore backup-nfs:vzdump-qemu-1051-*.vma.zst 1051 \
     --storage iscsi-master-51  # Can restore to either pool
   ```

---

## Expected Benefits

After migrating to SSD storage:

| Metric | Before (HDD) | After (SSD) | Improvement |
|--------|--------------|-------------|-------------|
| Random IOPS | 200-500 | 10,000+ | 20-50x |
| Sequential Read | 100 MB/s | 500 MB/s | 5x |
| IO Pressure Stall | 10-20% | <3% | 70-85% reduction |
| Container Start Time | 30-60s | 5-15s | 3-4x faster |
| Pod Scheduling Time | 15-30s | 5-10s | 2-3x faster |
| Database Latency | High | Low | Significant |

---

## Post-Migration Optimization

After migration, you can remove some HDD-specific optimizations:

1. **Revert PostgreSQL checkpoint settings** to more aggressive values
2. **Enable Traefik access logs** if needed
3. **Increase log retention** for applications

---

## Additional Considerations

### Space Management

Monitor Apps pool usage:

```bash
# On TrueNAS
zfs list -o name,used,avail,refer Apps

# Set up alerts for >80% usage
```

### Backup Strategy

SSD pool should also have backups:

```bash
# Replicate to Pool1 for backup
zfs snapshot -r Apps@migration-complete
zfs send -R Apps@migration-complete | zfs recv Pool1/apps-backup
```

### Future Expansion

If Apps pool runs out of space:
- Add more SSDs to expand RAIDZ1
- Consider migrating less IO-intensive workloads back to HDD
- Use tiered storage (hot data on SSD, cold data on HDD)

---

## Questions or Issues?

Document any problems in `.history/migration-to-ssd.log` for troubleshooting.
