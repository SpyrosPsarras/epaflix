---
applyTo: "1-proxmox/**"
description: "Instructions for Proxmox VE virtualization setup"
---
# Proxmox-Specific Instructions

When working with files in the `1-proxmox/` directory, follow these Proxmox VE-specific guidelines.

**Credential Placeholders:**
All commands use placeholders for sensitive information. Replace with values from `.github/instructions/secrets.yml`:
- `<PROXMOX_HOST1_USER>` / `<PROXMOX_HOST1_IP>` → proxmox-takaros credentials
- `<PROXMOX_HOST2_USER>` / `<PROXMOX_HOST2_IP>` → proxmox-evanthoulaki credentials
- `<TRUENAS_IP>` → TrueNAS server IP address

## Quick Actions Reference

Common administrative tasks that should be executed quickly:

### Enable All iSCSI Targets (Both Hosts)
```bash
# On first Proxmox host (credentials from secrets.yml: proxmox-takaros_username, proxmox-takaros_password)
ssh <PROXMOX_HOST1_USER>@<PROXMOX_HOST1_IP> "pvesm set iscsi-master-51 --disable 0 && \
pvesm set iscsi-master-52 --disable 0 && \
pvesm set iscsi-master-53 --disable 0 && \
pvesm set iscsi-worker-61 --disable 0 && \
pvesm set iscsi-worker-62 --disable 0 && \
pvesm set iscsi-worker-63 --disable 0 && \
pvesm set iscsi-worker-65 --disable 0"

# On second Proxmox host (credentials from secrets.yml: proxmox-evanthoulaki_username, proxmox-evanthoulaki_password)
ssh <PROXMOX_HOST2_USER>@<PROXMOX_HOST2_IP> "pvesm set iscsi-master-51 --disable 0 && \
pvesm set iscsi-master-52 --disable 0 && \
pvesm set iscsi-master-53 --disable 0 && \
pvesm set iscsi-worker-61 --disable 0 && \
pvesm set iscsi-worker-62 --disable 0 && \
pvesm set iscsi-worker-63 --disable 0 && \
pvesm set iscsi-worker-65 --disable 0"

# Verify
ssh <PROXMOX_HOST1_USER>@<PROXMOX_HOST1_IP> "pvesm status | grep iscsi"
ssh <PROXMOX_HOST2_USER>@<PROXMOX_HOST2_IP> "pvesm status | grep iscsi"
```

### Check iSCSI Connection Status
```bash
# Check all active iSCSI sessions (use credentials from secrets.yml)
ssh <PROXMOX_HOST1_USER>@<PROXMOX_HOST1_IP> "iscsiadm -m session"
ssh <PROXMOX_HOST2_USER>@<PROXMOX_HOST2_IP> "iscsiadm -m session"

# Check storage status
ssh <PROXMOX_HOST1_USER>@<PROXMOX_HOST1_IP> "pvesm status"
ssh <PROXMOX_HOST2_USER>@<PROXMOX_HOST2_IP> "pvesm status"
```

### Reconnect All iSCSI Targets
```bash
# If iSCSI sessions are down, reconnect
ssh <PROXMOX_HOST1_USER>@<PROXMOX_HOST1_IP> "systemctl restart iscsid open-iscsi"
ssh <PROXMOX_HOST2_USER>@<PROXMOX_HOST2_IP> "systemctl restart iscsid open-iscsi"
```

# Proxmox VE Hardware Overview
- Two HPE Proliand DL380 Gen9 servers with very limited disk space host the Proxmox VE servers. Both servers are connected in a cluster configuration and the 3rd vote is a quorum container running on the TrueNAS Server just for the voting system. The HPE servers are connected to each other via two 10GiB network interfaces using bond mode balance-xor and a 1GiB network interface for connecting to the rest of the network. The datacenter has attached storage from iSCSI targets on the TrueNAS server but the iSCSI targets are not shared storage but they are presented as local storage to the Proxmox nodes. The Proxmox nodes use the iSCSI targets as their local storage and the k3s cluster uses the local storage of each node for its PVs. This allows us to have a single storage pool that is shared across all nodes but it is still presented as local storage to k3s. This is a common pattern for k3s clusters running on Proxmox VE.

# Proxmox - Storage backend
- VMs are stored on TrueNAS iSCSI targets. Each node has its own storage pool (e.g., iscsi-master-51 for master-51, iscsi-worker-61 for worker-61).
- The storage pool in Proxmox is called iscsi-master-51, iscsi-master-52, iscsi-master-53 for the master nodes and iscsi-worker-61, iscsi-worker-62, iscsi-worker-63, iscsi-worker-65 for the worker nodes
- The TrueNAS server (IP and credentials in secrets.yml) is already configured with NFS and iSCSI targets for each node. The NFS share is used for shared storage and the iSCSI targets are used for the local storage of each node. The local storage of each node is actually the TrueNAS iSCSI target but it is presented as local storage to k3s. This allows us to have a single storage pool that is shared across all nodes but it is still presented as local storage to k3s. This is a common pattern for k3s clusters running on Proxmox VE.

### Proxmox VM Management

**Always use QEMU CLI commands (`qm`) for VM operations:**
- List VMs: `qm list`
- Create VM: `qm create <VMID> --name <name> --memory <MB> --cores <N>`
- Clone from template: `qm clone <TEMPLATE_ID> <NEW_VMID> --name <name>`
- Start VM: `qm start <VMID>`
- Configure cloud-init: `qm set <VMID> --ciuser <user> --sshkey <path>`

**VMID Conventions:**
- Templates: 9000-9999 range
- Control plane nodes: 1051-1059 range. The last two numbers indicate the master node number (e.g., 1051 for master-1, with last two digits matching the node IP)
- Worker nodes: 1061-1069 range. The last two numbers indicate the worker node number (e.g., 1061 for worker-1, with last two digits matching the node IP)
- Three masters: 1051, 1052, 1053
- Four workers: 1061, 1062, 1063, 1065

## Core Concepts

- **Storage**: VMs are stored on TrueNAS iSCSI targets. Each node has its own storage pool (e.g., iscsi-master-51 for master-51, iscsi-worker-61 for worker-61).
- **Network Proxmox**: Two Proxmox servers (IPs and credentials in secrets.yml). SSH access is available via passwordless SSH keys.
- **Network Bridge**: VMs use two network devices. net0 is vmbr0 which has internet access with external IP range and net1 is vmbr1 which is an internal network for cluster communication with internal IP range. The internal network is used for cluster communication and the external network is used for internet access and accessing the cluster from outside. The nodes will have two IP addresses, one on each network. Prioritize inner communication via net1 for cluster operations and use net0 for external access, storage, iSCSI and internet connectivity.
- **Cloud-init**: Automated VM configuration system.
- **VM placement**: Two masters will be placed on one Proxmox node and the third master on another node for high availability. Two workers will be placed on separate nodes to ensure fault tolerance.

## High availability (HA)
- Proxmox VE supports HA for VMs, but it requires shared storage and a cluster setup. For this project, we will not be using Proxmox HA features, but instead rely on Kubernetes' built-in HA capabilities for the control plane and worker nodes. This allows us to have a more flexible and portable cluster that can run on any Proxmox setup without requiring specific HA configurations at the hypervisor level.

## Command Safety

### Destructive Operations (Require Confirmation)
- `qm template <VMID>` - Irreversible, converts VM to template
- `qm destroy <VMID>` - Permanently deletes VM
- Storage operations that affect multiple VMs

### Safe to Run Multiple Times
- `qm start/stop/shutdown`
- `qm clone` - Creates new copies
- `qm set` - Modifies existing configuration

## Common Proxmox Patterns

### VM Template Creation Workflow
```bash
# 1. Create base VM
qm create <VMID> --name <name> --memory 2048 --cores 2

# 2. Import cloud image
qm importdisk <VMID> <image> local-raid

# 3. Attach disk
qm set <VMID> --scsihw virtio-scsi-pci --scsi0 local-raid:vm-<VMID>-disk-0

# 4. Configure boot
qm set <VMID> --boot c --bootdisk scsi0

# 5. Add cloud-init drive
qm set <VMID> --ide2 local-raid:cloudinit

# 6. Enable agent
qm set <VMID> --agent enabled=1

# 7. Convert to template (WARNING: Irreversible)
qm template <VMID>
```

### VM Cloning Workflow
```bash
# Clone from template
qm clone <TEMPLATE_ID> <NEW_VMID> --name k3s-master-1 --full

# Customize networking (if static IP needed)
qm set <NEW_VMID> --ipconfig0 ip=<IP_ADDRESS>/<CIDR>,gw=<GATEWAY_IP>

# Resize disk if needed
qm resize <NEW_VMID> scsi0 +10G

# Start the VM
qm start <NEW_VMID>
```

## Networking Considerations

- **Bridge Mode**: VMs appear as physical devices on the network
- **DHCP**: Easiest option, use `--ipconfig0 ip=dhcp`
- **Static IP**: Required for K3s master nodes, use `--ipconfig0 ip=<IP>/24,gw=<GATEWAY>`
- **Firewall**: Consider Proxmox firewall rules for security

## Storage Best Practices

### iSCSI Storage Management

**Quick Reference Commands:**

```bash
# Check all storage status (including iSCSI)
pvesm status

# Enable specific iSCSI storage
pvesm set <storage-name> --disable 0

# Disable specific iSCSI storage
pvesm set <storage-name> --disable 1

# Enable ALL iSCSI targets at once (datacenter-wide)
pvesm set iscsi-master-51 --disable 0 && \
pvesm set iscsi-master-52 --disable 0 && \
pvesm set iscsi-master-53 --disable 0 && \
pvesm set iscsi-worker-61 --disable 0 && \
pvesm set iscsi-worker-62 --disable 0 && \
pvesm set iscsi-worker-63 --disable 0 && \
pvesm set iscsi-worker-65 --disable 0

# List active iSCSI sessions
iscsiadm -m session

# List configured iSCSI nodes
iscsiadm -m node

# Login to specific iSCSI target
iscsiadm -m node -T <target-iqn> -p <ip>:3260 -l

# Logout from specific iSCSI target
iscsiadm -m node -T <target-iqn> -p <ip>:3260 -u

# Rescan iSCSI sessions (detect new LUNs)
iscsiadm -m session -R

# Check iSCSI device mappings
ls -la /dev/disk/by-path/ | grep iscsi

# Trigger SCSI bus rescan (detect new devices)
echo '- - -' | tee /sys/class/scsi_host/host*/scan > /dev/null

# Check LVM status on iSCSI devices
vgscan && lvscan
pvdisplay

# Restart iSCSI services if needed
systemctl restart iscsid open-iscsi
```

**Common iSCSI Troubleshooting:**

1. **Storage shows as "disabled"**
   - Use `pvesm set <storage> --disable 0` to enable
   - This is a Proxmox storage configuration, not iSCSI connection

2. **Storage shows as "inactive"**
   - Check iSCSI session: `iscsiadm -m session | grep <target>`
   - Verify device exists: `ls /dev/disk/by-path/ | grep iscsi | grep <target>`
   - May need LVM initialization if newly connected

3. **iSCSI connection errors (error 1020)**
   - Network connectivity issue to TrueNAS (IP and port in configuration)
   - Check firewall rules
   - Restart iSCSI services: `systemctl restart iscsid open-iscsi`

4. **No such logical volume errors**
   - iSCSI device connected but LVM not initialized
   - Need to create PV, VG, and LV thin pool on the device
   - This is expected for brand new iSCSI targets

**iSCSI Target Naming Convention:**
- TrueNAS IQN format: `iqn.2005-10.org.freenas.ctl:k3s-<node-name>`
- Master nodes: `k3s-master-51`, `k3s-master-52`, `k3s-master-53`
- Worker nodes: `k3s-worker-61`, `k3s-worker-62`, `k3s-worker-63`, `k3s-worker-65`
- Portal: `<TRUENAS_IP>:3260` (from secrets.yml)

**Complete iSCSI Verification Workflow:**

```bash
# 1. Check Proxmox storage configuration
pvesm status | grep iscsi

# 2. Verify iSCSI sessions are active
iscsiadm -m session

# 3. Check block devices are visible
lsblk | grep sd

# 4. Verify iSCSI device paths
ls -la /dev/disk/by-path/ | grep iscsi

# 5. Check LVM configuration
vgs | grep iscsi-vg
lvs | grep iscsi-vg

# 6. If all above pass, storage is ready for VM creation
```

## Cloud-init Configuration

### SSH Key Setup
```bash
# Generate key if needed
ssh-keygen -t ed25519 -C "k3s-cluster"

# Add to VM
qm set <VMID> --sshkey ~/.ssh/id_ed25519.pub
```

### User Configuration
```bash
# Set default user
qm set <VMID> --ciuser ubuntu

# Set password (optional, SSH key preferred)
qm set <VMID> --cipassword <password>
```

## Troubleshooting Proxmox Issues

### VM Won't Start
```bash
# Check VM status
qm status <VMID>

# Check storage availability
pvesm status

# View detailed error
qm start <VMID> --verbose
```

### Cloud-init Not Working
- Verify cloud-init drive attached: `qm config <VMID> | grep ide2`
- Check VM can reach metadata server
- Inside VM: `journalctl -u cloud-init`

### Network Issues
```bash
# Check bridge configuration
ip link show vmbr0

# Check VM network config
qm config <VMID> | grep net0

# Verify DHCP server if using DHCP
```

## VM Console Access and Diagnostics

When a VM is unresponsive to SSH or network access, you can access its console and diagnostics through several methods:

### Method 1: QEMU Guest Agent (Preferred for Diagnostics)

The QEMU Guest Agent allows executing commands inside the VM even when SSH is down:

```bash
# Test basic guest agent connectivity
qm agent <VMID> ping

# Execute a command inside the VM
qm guest exec <VMID> -- <command>

# Examples:
qm guest exec <VMID> -- uname -a
qm guest exec <VMID> -- dmesg | tail -50
qm guest exec <VMID> -- journalctl -n 50 --no-pager
qm guest exec <VMID> -- cat /proc/meminfo

# Common failure: "Input/output error" indicates filesystem corruption or severe disk issues
```

**Important**: If all guest exec commands return "Input/output error", the VM's filesystem is corrupted and the VM needs to be restarted.

### Method 2: Serial Console Access

Access the VM's serial console to see boot messages and login prompt:

```bash
# Interactive terminal (use Ctrl+O to exit)
qm terminal <VMID>

# Non-interactive read (useful for scripts)
socat - UNIX-CONNECT:/var/run/qemu-server/<VMID>.serial0

# With timeout to prevent hanging
timeout 5 ssh root@<PROXMOX_HOST> "echo -e '\n' | socat - UNIX-CONNECT:/var/run/qemu-server/<VMID>.serial0"
```

**Note**: Serial console may hang if the VM is completely frozen. Use timeout commands.

### Method 3: Check VM Process and Resource Usage

Check if the VM process is running and consuming resources:

```bash
# Find the QEMU process for specific VM
ps aux | grep "qemu.*<VMID>" | grep -v grep

# Check CPU usage (high CPU on hung VM indicates kernel panic loop)
ps aux | grep "qemu.*<VMID>" | grep -v grep | awk '{print $3}'

# Get full VM status from Proxmox API
pvesh get /nodes/<NODE>/qemu/<VMID>/status/current
```

### Method 4: Check Storage Backend

If guest agent fails with I/O errors, verify the storage backend:

```bash
# Check iSCSI session status
iscsiadm -m session | grep <vm-storage-name>

# Check LVM volumes
lvs | grep <VMID>

# Check storage pool status
pvesm status | grep <storage-name>

# Look for I/O errors in host kernel logs
dmesg -T | grep -i "error\|fail" | grep -i "iscsi\|scsi"
```

### Diagnostic Decision Tree

1. **VM appears running but unresponsive**:
   - Check: `ping <VM_IP>` → If fails, network/VM is down
   - Check: `qm agent <VMID> ping` → If succeeds, SSH issue only
   - Check: `qm guest exec <VMID> -- uname -a` → If "I/O error", filesystem corrupted

2. **Filesystem I/O errors detected**:
   - Check storage backend (iSCSI, LVM) on Proxmox host
   - If storage is healthy → VM filesystem corruption, needs restart/fsck
   - If storage has errors → Fix storage first

3. **VM frozen (high CPU, no response)**:
   - Likely kernel panic or deadlock
   - Action: Force stop and restart

4. **Serial console shows journal write errors**:
   - Indicates severe filesystem corruption
   - Action: Restart VM, may need fsck in recovery mode

### Emergency VM Recovery

```bash
# Force stop unresponsive VM
qm stop <VMID>

# If stop hangs, kill the QEMU process
kill -9 $(ps aux | grep "qemu.*<VMID>" | grep -v grep | awk '{print $2}')

# Start the VM
qm start <VMID>

# Monitor startup via serial console
qm terminal <VMID>
```

### Post-Restart Filesystem Check

If the VM had I/O errors, check filesystem after restart:

```bash
# Inside the VM after successful boot
sudo systemctl status

# Check for filesystem errors
sudo dmesg | grep -i "ext4\|xfs\|error"

# Manual filesystem check (requires unmounting or single-user mode)
# Boot into rescue mode and run:
sudo fsck -f /dev/sda1  # Replace with actual device
```

## Node allocation strategy
- Master nodes: Place two masters (51, 52) on one Proxmox host <PROXMOX_HOST1_IP> and the third master (53) on the other Proxmox host <PROXMOX_HOST2_IP> for high availability. This way, if one Proxmox host goes down, we still have a master node available on the other host to maintain cluster control.
- Worker nodes: Place worker nodes across both Proxmox hosts for balanced resource utilization and redundancy. Worker nodes 61 and 62 can be placed on <PROXMOX_HOST1_IP> and worker nodes 63 and 65 can be placed on <PROXMOX_HOST2_IP>. This distribution ensures that if one Proxmox host experiences issues, we still have worker nodes available on the other host to run workloads.


## Current VM Specs

### K3s Master Nodes (1051-1053)
- **CPU**: 6 cores
- **RAM**: 10 GB
- **Disk**: 30 GB (local-raid)

### K3s Worker Nodes (1061-1065)
- **CPU**: 8 cores
- **RAM**: 22 GB
- **Disk**: 50 GB (local-raid)

## CPU Type Standard

> Supersedes #213 (the narrow masters-only question, already closed as subsumed). Tracked by #216.

**STANDARD: all PVE guest VMs use `cpu: host`.**

Both Proxmox hosts are identical Intel Xeon E5-2623 v4 (Broadwell-EP), so `host` exposes the full CPU feature set — including SSE4.2/POPCNT/AVX/AVX2 (i.e. `x86-64-v3`; no AVX-512, so `x86-64-v4` is unavailable) — and is live-migration-safe between the two identical hosts. The Proxmox default `kvm64` is only `x86-64-v1` (no AVX2) and crashlooped the Odysseus NumPy image (#207) until the workers were bumped; `host` prevents that entire class of bug.

**CAVEAT:** `cpu: host` ties live migration to identical-CPU hosts. If a NON-identical-CPU Proxmox host is ever added to the cluster, switch the standard to a portable named model (`Broadwell-noTSX`) or a synthetic baseline (`x86-64-v3`) instead of `host`.

**SCOPE:**
- **VMs** — applies (all PVE guest VMs).
- **LXC containers** (e.g. wg-hop 1045) share the host kernel/CPU, so CPU-type is N/A.
- The **qdevice** is a TrueNAS Docker container, not a PVE guest — N/A.

**APPLY PROCEDURE** — one node at a time. A CPU-model change requires a full power-cycle (`qm stop` + `qm start`), NOT a soft reboot, for the new model to take effect.

- **K3s nodes:**
  ```bash
  kubectl cordon <node>
  kubectl drain <node> --ignore-daemonsets --delete-emptydir-data   # respect CNPG/etcd PodDisruptionBudgets
  ssh root@<pvehost> 'qm stop <vmid>; qm set <vmid> --cpu host; qm start <vmid>'
  # wait for the node to report Ready
  # for MASTERS also verify: etcd 3/3 with has_leader, the #121 control-plane metrics
  #   (cm/scheduler/etcd up=1), and the /etc/k3s-resolv.conf pin
  kubectl uncordon <node>
  # only then proceed to the next node
  ```
  Master order: **1052 → 1053 → 1051** (1051 is the founding etcd member, do it last — per #148).

- **Docker Swarm nodes:**
  ```bash
  docker node update --availability drain <node>
  ssh root@<pvehost> 'qm stop <vmid>; qm set <vmid> --cpu host; qm start <vmid>'
  # confirm the node rejoins the swarm
  docker node update --availability active <node>
  ```

- **Templates (9000/9001):** set `cpu: host` while the template is stopped so future clones inherit it (no power-cycle needed):
  ```bash
  ssh root@<pvehost> 'qm set <template-vmid> --cpu host'
  ```

**Current state (2026-08-03):**
- Workers **1061 / 1062 / 1063 / 1065** — already `host`.
- Masters **1052 / 1053** — `host` since 2026-08-03 (#329 apply, phases 1 and 2).
- Swarm **1071 / 1072 / 1073** — `host` since 2026-08-03 (#329 phase 2).
- Master **1051** — still `kvm64`. **The only one left.** Deliberately deferred to an attended
  window: it is the founding etcd member, it carries the single `coredns` replica, and it sits on
  `takaros` next to the `qbittorrent` worker (see below). Tracked in #329.

Why `1051` is held back: power-cycling `1052` knocked the `qbittorrent` `airvpn` VPN sidecar off its
tunnel for ~2-3 min, even though that pod sits on an untouched worker (`k3s-worker-61`). It
self-recovered with no leak, but the coupling was not understood. Suspicion was host-level: `1052`
and `1061` are both on `takaros`. Phase 2 tested that - cycling `1053` (on `evanthoulaki`) produced
**zero** sidecar restarts over a 6 m 49 s watch, which is consistent with the host-local theory but
does not prove it, because the sidecar image was re-pinned between the two runs (#666/#667). Either
way `1051` is on `takaros`, so plan for the hit. Details in #329.

Measured: graceful `qm shutdown` never needed a forced `qm stop` fallback on any of the 5 VMs done
so far. `1052` 9 s to power off / 36 s VM downtime / ~1 m 46 s node-NotReady; `1053` 9 s / 51 s /
<= 1 m 33 s. Swarm VMs ~12-13 s to power off, ~46-49 s downtime, back in the swarm ~70-80 s.

**None of the three swarm VMs has a working qemu guest agent** - `qm agent <vmid> ping` fails on
`1071`, `1072` and `1073` alike, so `qm shutdown` is ACPI-only for all of them (it works, and exits
0, it just prints a `guest-ping failed` warning first). `1072` / `1073` are additionally not
SSH-reachable (host-key mismatch from the workstation, publickey denied from `ds-master`), so
guest-side `lscpu` cannot be checked there - verify those hypervisor-side instead, via the live qemu
`-cpu` flag and the swarm rejoin:
```bash
ssh root@<pvehost> 'tr "\0" "\n" < /proc/$(cat /var/run/qemu-server/<vmid>.pid)/cmdline | grep -A1 "^-cpu" | tail -1'
```

## Security Notes

- Use SSH keys, not passwords
- Disable root SSH login in cloud-init
- Keep Proxmox updated: `apt update && apt dist-upgrade`
- Use firewall rules to restrict access
- Separate management and production networks if possible
- Should be able to ssh into all nodes without password from local host and from each other using the internal network (net1) for cluster communication. For example, master nodes should be able to ssh into other master nodes and worker nodes using their internal IPs without password. This allows for secure and efficient cluster communication while still allowing access from the external network when needed.

## External References

- [Proxmox VE Documentation](https://pve.proxmox.com/wiki/Main_Page)
- [K3s Official Documentation](https://docs.k3s.io/)
- [k3sup Documentation](https://github.com/alexellis/k3sup)
- [Kubernetes Documentation](https://kubernetes.io/docs/)
- [Cloud-init Documentation](https://cloudinit.readthedocs.io/)
- [Helm Documentation](https://helm.sh/docs/)

- **OS**: Linux (Ubuntu 24.04) on VMs
- **Networking**: Bridge networking, Kube-VIP, kube-vip-cloud-provider for load balancing, coredns for DNS, traefik for ingress
- **Storage**: Nodes local storage for VM is iSCSI target on a TrueNAS server (IP in secrets.yml). k3s cluster will use local storage but in reality it is the TrueNAS iSCSI target. The storage pool in Proxmox is called iscsi-master-51 52 and 53 and iscsi-worker-61 62 63 65. The storage class in k3s will be called local-storage and it will use the local path provisioner to create PVs on the local storage of each node. The local storage of each node is actually the TrueNAS iSCSI target but it is presented as local storage to k3s. This allows us to have a single storage pool that is shared across all nodes but it is still presented as local storage to k3s. This is a common pattern for k3s clusters running on Proxmox VE.
- **Network Storage**: VMs should have an NFS share mounted for shared storage (example downloads, movies, series, animes are located in TrueNAS storage server and available on NFS shares). This allows us to have a single storage pool that is shared across all nodes but it is still presented as local storage to k3s. This is a common pattern for k3s clusters running on Proxmox VE.

The `secrets.yml` file has the following structure:
```yaml
proxmox-takaros_username: "<username>"
proxmox-takaros_password: "<password>"
proxmox-evanthoulaki_username: "<username>"
proxmox-evanthoulaki_password: "<password>"
```

## Backup and Recovery

When instructed to back up VM, stop the VM and create a snapshot or backup using Proxmox tools. The target of the snapshot is going to be the NFS storage location /mnt/pool1/dataset01/VMs/backup. For example:
```bash
# Stop the VM
qm stop <VMID>
# Create a backup (this will create a backup file in the NFS storage location)
vzdump <VMID> --storage local --mode snapshot --compress lzo --dumpdir /mnt/pool1/dataset01/VMs/backup
```

## SSH: password authentication is DISABLED (#745)

Both hosts run `PasswordAuthentication no` + `KbdInteractiveAuthentication no`
via the drop-in `/etc/ssh/sshd_config.d/99-no-password-auth.conf`, kept in this
repo at [`1-proxmox/ssh/99-no-password-auth.conf`](../../1-proxmox/ssh/99-no-password-auth.conf).

**Why:** one 12-character password was the value of **20** credentials in
`secrets.yml` — including root on both Proxmox hosts, TrueNAS admin, all 7 k3s
node passwords, the Postgres superuser and the router (#745). Rotating all
twenty was rejected as expensive and risky; making the password unusable
remotely is cheaper and stronger. Key auth was already the only method in
actual use — every session logged `Accepted publickey for root`.

**Apply on a rebuilt host:**

```bash
scp 1-proxmox/ssh/99-no-password-auth.conf root@<host>:/etc/ssh/sshd_config.d/
ssh root@<host> 'sshd -t && systemctl reload ssh'
ssh root@<host> "sshd -T | grep -iE '^(passwordauthentication|pubkeyauthentication)'"
```

Use `reload`, not `restart` — reload keeps existing sessions alive, so a mistake
does not disconnect you mid-change. Always `sshd -t` first.

**Verify both directions**, not just one:

```bash
ssh -o BatchMode=yes root@<host> 'echo ok'                       # key auth still works
ssh -o PubkeyAuthentication=no -o PreferredAuthentications=password \
    -o NumberOfPasswordPrompts=0 root@<host> true                # must fail: Permission denied (publickey)
```

**This does NOT cover the Proxmox web UI.** `root@pam` still authenticates with
that password at `https://<host>:8006`. Closing that vector needs TOTP on
`root@pam` or restricting the UI to the LAN — still open in #745.

**Recovery:** the web UI console (noVNC / xterm.js) and physical/IPMI access
both bypass `sshd`, so this cannot lock you out of a host — only out of SSH.
Revert with `rm /etc/ssh/sshd_config.d/99-no-password-auth.conf && systemctl reload ssh`.

**The k3s VMs already had this** (`PasswordAuthentication no`, `sudo` NOPASSWD),
so their 7 passwords are console-only and were deliberately left unrotated.

## Postfix aliases database (#720)

Every PVE node must have `/etc/aliases.db`. Debian ships `/etc/aliases` but
not the compiled database, and postfix cannot resolve `root` without it:

```
postfix/local: error: open database /etc/aliases.db: No such file or directory
postfix/local: warning: hash:/etc/aliases: lookup of 'root' failed
status=deferred (alias database unavailable)
```

The PVE builtin `default-matcher` routes **every** notification to the
`mail-to-root` sendmail target, so without this file every backup report,
every job failure and every health warning defers forever instead of being
delivered. Both nodes had **246 deferred messages** when this was found. It
is the mechanism behind #597 going unnoticed for three weeks.

Fix, and part of building any new node:

```bash
newaliases                 # builds /etc/aliases.db from /etc/aliases
postqueue -f               # flush what has been deferring
test -f /etc/aliases.db    # must succeed
```

There is deliberately no `root:` alias, so mail lands in `/var/mail/root`.
That is not the channel anyone watches - **ntfy is**, via the `ntfy-pve`
webhook target added in PR #716. The local mailbox exists as a greppable
on-disk record for incident forensics, which is the difference between a
target that delivers and one that swallows.

Still open in #720: whether PVE/PBS mail should go through the real relay so
it reaches a human inbox, the way Alertmanager does after #461.
