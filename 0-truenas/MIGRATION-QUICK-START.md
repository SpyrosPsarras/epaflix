# Quick Migration Guide: HDD to SSD (No Local Snapshot Required)

## Why No Local Snapshot Needed

Your Proxmox hosts have limited local storage, but you have plenty of space on TrueNAS Pool1. This guide shows how to **move existing iSCSI zvols** from HDD to SSD using ZFS rename - no cloning or snapshots needed!

## Migration Approach

Instead of creating new iSCSI targets and cloning VMs, we:
1. **Stop VMs and disconnect iSCSI**
2. **Move zvols** from Pool1 to Apps pool using `zfs rename`
3. **Update iSCSI extents** to point to new location
4. **Reconnect and start VMs**

**Advantages:**
- No disk space needed for clones
- Fast operation (just metadata change)
- Same VM, same disk, just different pool
- Minimal downtime (~5-10 minutes)

## Backup Strategy

### Option 1: NFS Backup to Pool1 (Safest)
- Backup VMs to Pool1 via NFS before migration
- Uses: 30-70GB on Pool1 (plenty available)
- Recovery time: 10-30 minutes

### Option 2: No Backup (Fast)
- VMs are cloud-init based, can recreate quickly
- Just backup K8s configs and databases
- Recovery time: 5-15 minutes

---

## Quick Start: Move zvols Method (Recommended)

This method moves existing zvols from Pool1 to Apps pool - no cloning needed!

### Step 1: Backup VMs (Optional but Recommended)

```bash
# Setup NFS backup storage on Pool1
# On TrueNAS
ssh <TRUENAS_ADMIN>@<TRUENAS_IP>
sudo zfs create Pool1/proxmox-vm-backups
sudo chmod 755 /mnt/Pool1/proxmox-vm-backups

# Create NFS share via TrueNAS UI:
# Sharing → Unix Shares (NFS)
# Path: /mnt/Pool1/proxmox-vm-backups

# On both Proxmox hosts
pvesm add nfs backup-nfs \
  --server <TRUENAS_IP> \
  --export /mnt/Pool1/proxmox-vm-backups \
  --content backup,vztmpl

# Backup all VMs to NFS (uses Pool1 space)
for VMID in 1051 1052 1053 1061 1062 1063 1065; do
  vzdump $VMID --mode snapshot --compress zstd --storage backup-nfs
done
```

### Step 2: Stop VMs and Disconnect iSCSI

```bash
# On both Proxmox hosts

# Stop all VMs
for VMID in 1051 1052 1053 1061 1062 1063 1065; do
  qm stop $VMID
done

# Disable iSCSI storage
for NODE in master-51 master-52 master-53 worker-61 worker-62 worker-63 worker-65; do
  pvesm set iscsi-${NODE} --disable 1
done

# Logout from iSCSI targets
iscsiadm -m node --logout
```

### Step 3: Move zvols to Apps Pool

```bash
# On TrueNAS
ssh <TRUENAS_ADMIN>@<TRUENAS_IP>

# Move all zvols from Pool1 to Apps
sudo zfs rename Pool1/iscsi-master-51 Apps/iscsi-master-51
sudo zfs rename Pool1/iscsi-master-52 Apps/iscsi-master-52
sudo zfs rename Pool1/iscsi-master-53 Apps/iscsi-master-53
sudo zfs rename Pool1/iscsi-worker-61 Apps/iscsi-worker-61
sudo zfs rename Pool1/iscsi-worker-62 Apps/iscsi-worker-62
sudo zfs rename Pool1/iscsi-worker-63 Apps/iscsi-worker-63
sudo zfs rename Pool1/iscsi-worker-65 Apps/iscsi-worker-65

# Verify
sudo zfs list Apps | grep iscsi
```

### Step 4: Update iSCSI Extents

**In TrueNAS UI:**
1. **Sharing** → **Block Shares (iSCSI)** → **Extents**
2. For each extent:
   - Edit extent
   - Update **Device** to: `zvol/Apps/iscsi-master-XX` (or worker-XX)
   - Save
3. Verify all 7 extents point to Apps pool

### Step 5: Reconnect and Start VMs

```bash
# On both Proxmox hosts

# Login to iSCSI targets (same targets, now backed by SSD)
iscsiadm -m discovery -t st -p <TRUENAS_IP>:3260
iscsiadm -m node --login

# Re-enable storage
for NODE in master-51 master-52 master-53 worker-61 worker-62 worker-63 worker-65; do
  pvesm set iscsi-${NODE} --disable 0
done

# Start all VMs
for VMID in 1051 1052 1053 1061 1062 1063 1065; do
  qm start $VMID
done
```

### Step 6: Verify

```bash
# Check K3s cluster
ssh ubuntu@192.168.10.51 "sudo kubectl get nodes -o wide"

# Check IO pressure on Proxmox UI
# Should drop from 10-20% to <3%

# Verify zvols are on Apps pool
ssh <TRUENAS_ADMIN>@<TRUENAS_IP> "sudo zfs list Apps | grep iscsi"
```

**Total downtime: 5-10 minutes**

## Alternative: Gradual One-by-One Migration

If you prefer to migrate VMs one at a time with testing between each:

### For Each VM

```bash
# Example for master-51

# 1. Stop VM
qm stop 1051

# 2. Disconnect iSCSI
pvesm set iscsi-master-51 --disable 1
iscsiadm -m node --targetname iqn.2024-01.local.truenas:master-51 --logout

# 3. Move zvol on TrueNAS
ssh <TRUENAS_ADMIN>@<TRUENAS_IP> "sudo zfs rename Pool1/iscsi-master-51 Apps/iscsi-master-51"

# 4. Update iSCSI extent in TrueNAS UI
# Sharing → iSCSI → Extents → Edit master-51
# Device: zvol/Apps/iscsi-master-51

# 5. Reconnect iSCSI
iscsiadm -m node --targetname iqn.2024-01.local.truenas:master-51 --login
pvesm set iscsi-master-51 --disable 0

# 6. Start VM
qm start 1051

# 7. Verify
ssh ubuntu@192.168.10.51 "sudo kubectl get nodes"
```

**Wait 24-48 hours between VMs to ensure stability**

---

## Optional: NFS Backup to Pool1

If you want full VM backups:

### Setup (One-Time)

```bash
# On TrueNAS - Create backup dataset
ssh <TRUENAS_ADMIN>@<TRUENAS_IP>
sudo zfs create Pool1/proxmox-vm-backups
sudo chmod 755 /mnt/Pool1/proxmox-vm-backups

# Create NFS share in TrueNAS UI
# Sharing → Unix Shares (NFS)
# Path: /mnt/Pool1/proxmox-vm-backups

# On Proxmox - Add NFS storage
pvesm add nfs backup-nfs \
  --server <TRUENAS_IP> \
  --export /mnt/Pool1/proxmox-vm-backups \
  --content backup \
  --maxfiles 5
```

### Backup VMs

```bash
# Backup to NFS (stored on Pool1 HDD)
for VMID in 1051 1052 1053 1061 1062 1063 1065; do
  vzdump $VMID --mode snapshot --compress zstd --storage backup-nfs
done

# Space used: ~30-70GB on Pool1 (you have TB available)
```

### Restore if Needed

```bash
# List backups
pvesm list backup-nfs

# Restore
qmrestore backup-nfs:vzdump-qemu-1051-*.vma.zst 1051 \
  --storage iscsi-master-51-ssd-lvm
```

---

## Space Usage Summary

**Apps Pool (SSD) - Before migration:**
- Used: ~200GB (varies)
- Available: ~300GB

**Apps Pool (SSD) - After migration:**
- K3s VMs: ~250GB
- Available: ~250GB
- Status: ✅ Sufficient

**Pool1 (HDD) - Always plenty of space:**
- Total: ~24TB (10TB + 14TB stripe)
- Available: ~20TB
- Can easily hold VM backups if needed

**Proxmox Local Storage:**
- Not used for snapshots ✅
- Only used for Proxmox OS

---

## Verification After Migration

```bash
# Check IO Pressure on Proxmox UI
# VM → Summary → IO Pressure Should
# Before: 10-20%
# After: <3%

# Check from VM
ssh ubuntu@192.168.10.51
iostat -x 5 3
# Look for: low await, low %util

# Benchmark (optional)
sudo apt install fio -y
fio --name=test --ioengine=libaio --iodepth=16 --rw=randread \
    --bs=4k --direct=1 --size=1G --runtime=30
# Expect: 10,000+ IOPS (vs 200-500 before)
```

---

## Rollback Scenarios

### If Using Clone-as-Backup Method

```bash
# Old VM still exists on HDD
qm stop 2051  # Stop new VM
qm start 1051 # Start old VM
# Done! Back to working state
```

### If Using No-Backup Method

```bash
# Recreate from template
qm destroy 1051
qm clone 9000 1051 --name master-51 --full --storage iscsi-master-51-hdd-lvm
# (Use old HDD storage)
# Reconfigure cloud-init
qm set 1051 --ipconfig0 ip=192.168.10.51/24,gw=192.168.10.1
qm start 1051
```

### If Using NFS Backup Method

```bash
# Restore from backup
qmrestore backup-nfs:vzdump-qemu-1051-*.vma.zst 1051 \
  --storage iscsi-master-51-hdd-lvm
```

---

## Why This Works

1. **ZFS Rename is Fast**: Just metadata change, no data copy
2. **Same iSCSI Targets**: No need to reconfigure Proxmox storage pools
3. **VMs are Intact**: Same VM, same disk, just different backing pool
4. **Minimal Downtime**: 5-10 minutes total
5. **Pool1 has Space**: TB available for NFS backups if wanted

**Result:** Fast, safe migration with minimal downtime and no local snapshot space needed!

---

## Questions?

- **Q: What if something goes wrong during zvol move?**
  A: Restore VM from NFS backup (if taken), or rename zvol back to Pool1

- **Q: Will this cause data loss?**
  A: No, zfs rename just moves the dataset, data stays intact

- **Q: How long does zvol move take?**
  A: Instant - it's just a metadata operation

- **Q: What about PVC data?**
  A: PVCs are inside the zvols being moved, they automatically move too

- **Q: Can I test one VM first?**
  A: Yes! Use gradual migration method, test one VM before doing all

---

## Recommended Approach

✅ **Use zvol Move Method:**
1. Zero data copy needed (just metadata)
2. Fast operation (5-10 min downtime)
3. Can backup to NFS on Pool1 first (optional)
4. Lowest risk, fastest method

**Timeline:**
- Backup VMs to NFS: 1-2 hours (optional)
- Stop VMs and disconnect: 2 minutes
- Move zvols: 1 minute
- Update extents: 2 minutes
- Reconnect and start: 5 minutes

**Total time:** 10-15 minutes (including optional backup)
