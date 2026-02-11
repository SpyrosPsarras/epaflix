# K3s Maintenance Scripts & Tools

## 📋 Overview

This directory contains **reusable maintenance and troubleshooting tools** for ongoing K3s cluster operations.

**Note:** One-time recovery scripts are kept in `.history/` folders, not here.

| Tool | File | Purpose |
|------|------|---------|
| **K3s Auto-Upgrade** | `system-upgrade/system-upgrade-plans.yaml` | Rolling K3s upgrades via system-upgrade-controller |
| **Node OS Updater** | `node-os-updater-cronjob.yaml` | Weekly `apt upgrade` on all nodes with safe cordon/drain/reboot/uncordon |
| **Servarr Image Updater** | `servarr-image-updater-cronjob.yaml` | Weekly restart of all servarr pods to pull latest images |
| **Containerd Cleanup** | `image-cleanup-cronjob.yaml` | Weekly removal of unused images/containers from workers |
| **Disk Pressure Fix** | `fix-worker-61-diskpressure.sh` | Interactive tool to recover a worker with disk pressure |
| **Database Backup** | `backup-all-databases.sh` | Dump all servarr PostgreSQL databases |

---

## 🛠️ Troubleshooting Tools

### Disk Pressure Resolution
**Script:** `fix-worker-61-diskpressure.sh`

Interactive tool to resolve disk pressure on worker nodes.

**Usage:**
```bash
./fix-worker-61-diskpressure.sh
```

**Options:**
1. Cordon node and move workloads (recommended)
2. Expand disk from 20GB to 40GB (requires TrueNAS + Proxmox)
3. Clean up data (prometheus/postgres/images)

**When to use:** When a worker node shows `DiskPressure` condition or >75% disk usage

---

## 🧹 Container Image Maintenance

Automated cleanup for worker nodes to prevent disk space issues.

## 📋 Overview

**Automated Cleanup**: Weekly CronJob removes unused containers and images

**Image Sharing**: K3s Embedded Registry Mirror (Spegel) provides peer-to-peer image distribution across all nodes via 10.0.0.0/24 network. Images pulled on any node are automatically available to all other nodes without duplicate downloads.

## 🗓️ Schedule

| CronJob | Schedule | Time | Purpose |
|---------|----------|------|---------|
| **node-os-updater** | `0 2 * * 6` | 2 AM Saturday | `apt upgrade` all nodes; cordon/drain/reboot/uncordon when kernel updates |
| **containerd-cleanup** | `0 2 * * 0` | 2 AM Sunday | Clean unused containers & images from worker nodes |
| **servarr-image-updater** | `0 3 * * 0` | 3 AM Sunday | Restart all servarr pods to pull latest images |

### Stopped Containers
- Containers in "Exited" state
- Old init containers from pod restarts
- Bootstrap containers from PostgreSQL

### Unused Images
- Image layers not referenced by any container
- Old versions of images after updates
- Unreferenced snapshots in overlayfs

### What's SAFE (Never Deleted)
- ✅ Running containers
- ✅ Images currently used by pods
- ✅ NFS-mounted data (/config, /tv, /downloads)
- ✅ Local-path PVCs

## � Embedded Registry Mirror (Spegel)

**Enabled**: February 3, 2026

- **P2P Network**: 10.0.0.0/24 (eth1) - 2.5G dedicated network on HPEs
- **P2P Port**: 5001
- **Registry API**: Port 6443 (same as K8s API)
- **Configuration**: `/etc/rancher/k3s/registries.yaml` on all nodes with wildcard `"*"` mirror
- **How it works**: When any node pulls an image, it becomes available to all other nodes via peer-to-peer sharing without re-downloading from the internet
- **Benefits**: Zero image duplication, faster deployments, reduced internet bandwidth, automatic cache distribution
- `lscr.io/linuxserver/bazarr:development`
- `docker.io/jellyfin/jellyfin:latest`
- `docker.io/fallenbagel/jellyseerr:preview-OIDC`
- `ghcr.io/cleanuparr/cleanuparr:2.5.1`
- `docker.io/prompve/prometheus-pve-exporter:latest`

## 🚀 Manual Execution

### Trigger Cleanup Manually
```bash
# Create one-time job from CronJob
kubectl create job --from=cronjob/containerd-cleanup cleanup-manual -n kube-system

# Watch progress
kubectl -n kube-system logs -f job/cleanup-manual
```

### Trigger Image Pre-pull Manually
```bash
# Create one-time job
kubectl create job --from=cronjob/image-prepull-weekly prepull-manual -n kube-system

# Watch progress
kubectl -n kube-system logs -f job/prepull-manual
```

## 📊 Monitoring

### Check CronJob Status
```bash
kubectl -n kube-system get cronjobs
```

### View Last Execution
```bash
# List recent jobs
kubectl -n kube-system get jobs --sort-by=.status.startTime

# View logs from last cleanup
kubectl -n kube-system logs job/containerd-cleanup-<timestamp>

# View logs from last pre-pull
kubectl -n kube-system logs job/image-prepull-weekly-<timestamp>
```

### Check Worker Disk Usage
```bash
for node in 61 62 63 65; do
  echo "=== Worker-$node ==="
  ssh ubuntu@192.168.10.$node 'df -h / | tail -1'
done
```

### Check Image Count Per Worker
```bash
for node in 61 62 63 65; do
  echo "Worker-$node:"
  ssh ubuntu@192.168.10.$node 'sudo crictl images --quiet | wc -l'
  echo "images"
done
```

## 🔧 Configuration

### Modify Cleanup Schedule
```bash
kubectl -n kube-system edit cronjob containerd-cleanup

# Change schedule field (cron format):
# 0 2 * * 0  = 2 AM Sunday
# 0 3 * * *  = 3 AM daily
# 0 */6 * * * = Every 6 hours
```

### Add/Remove Images to Pre-pull
Edit [image-prepull-cronjob.yaml](image-prepull-cronjob.yaml) and add to IMAGES list:
```yaml
IMAGES="
  your-registry.com/your-image:tag
  ...
"
```

Then apply:
```bash
kubectl apply -f image-prepull-cronjob.yaml
```

## 🎯 Expected Results

### Before First Cleanup (Feb 3, 2026)
- Worker-61: 52% used (9.5GB)
- Worker-62: 93% used (17GB) ⚠️ CRITICAL
- Worker-63: 69% used (13GB)
- Worker-65: 68% used (13GB)

### After First Cleanup
- Worker-61: 52% used (~100MB freed)
- Worker-62: 84% used (1.5GB freed) ✅
- Worker-63: 65% used (800MB freed)
- Worker-65: 65% used (1GB freed)

### Weekly Maintenance Impact
- **Space freed per cleanup**: 500MB - 2GB per worker
- **Network bandwidth saved**: ~2-5GB per worker (shared images)
- **Pod start time**: Faster (images already cached)

---

## 🔄 K3s Auto-Upgrade

Automated rolling upgrades for all cluster nodes using the [system-upgrade-controller](https://github.com/rancher/system-upgrade-controller).

**Files:** `system-upgrade/system-upgrade-plans.yaml`, `system-upgrade/README.md`

### How it works

1. The controller watches the K3s **stable release channel** for new versions
2. **Masters are upgraded one at a time** to preserve etcd quorum (3-node HA)
3. **Workers wait** until all masters finish, then upgrade **2 at a time**

### Setup (one-time)

```bash
# 1. Install the controller
kubectl apply -f https://github.com/rancher/system-upgrade-controller/releases/latest/download/system-upgrade-controller.yaml

# 2. Apply the upgrade plans
kubectl apply -f system-upgrade/system-upgrade-plans.yaml

# 3. Verify plans are active
kubectl -n system-upgrade get plans
```

### Monitor an upgrade

```bash
# Watch upgrade jobs run in real time
kubectl -n system-upgrade get jobs -w

# Check current K3s version per node
kubectl get nodes -o wide
```

See [system-upgrade/README.md](system-upgrade/README.md) for full documentation.

---

## 🖥️ Node OS Auto-Update

Weekly CronJob that runs `apt upgrade` on every K3s node (3 masters + 4 workers) and safely reboots nodes when a kernel or system package update requires it.

**File:** `node-os-updater-cronjob.yaml`

### How it works

Nodes are processed in three phases to maintain cluster stability:

```
Phase 1 — Other master nodes (one at a time)
  └─ preserves etcd quorum: only 1 master is ever down simultaneously

Phase 2 — Worker nodes (one at a time)
  └─ keeps workloads running: at least 3 of 4 workers always available

Phase 3 — Current master node (the one running this job pod)
  └─ reboot only, no drain — avoids self-evicting the job pod
```

When a reboot is required, the node goes through: **cordon → drain → reboot → wait for Ready → uncordon**.
The current master node skips cordon/drain and simply reboots as the final step (Kubernetes will not schedule pods onto a `NotReady` node, so it is safe).

### Setup (one-time)

```bash
kubectl apply -f node-os-updater-cronjob.yaml

# Verify ServiceAccount, ClusterRole, ClusterRoleBinding, and CronJob
kubectl -n kube-system get cronjob node-os-updater
kubectl get clusterrolebinding node-os-updater
```

### Trigger manually

```bash
kubectl create job --from=cronjob/node-os-updater node-os-update-manual -n kube-system
kubectl -n kube-system logs -f job/node-os-update-manual
```

### Monitor a running job

```bash
# Watch the job pod's logs live
kubectl -n kube-system get pods -l app=node-os-updater -w

# Check node cordon status during the run
kubectl get nodes

# View logs from last completed run
kubectl -n kube-system get jobs --sort-by=.status.startTime | grep node-os-updater
kubectl -n kube-system logs job/<node-os-updater-timestamp>
```

### Verify nodes are up to date after the run

```bash
for ip in 51 52 53; do
  echo "=== master-$ip ==="
  ssh ubuntu@192.168.10.$ip 'sudo apt list --upgradable 2>/dev/null | grep -c upgradable || echo "0 packages pending"'
done

for ip in 61 62 63 65; do
  echo "=== worker-$ip ==="
  ssh ubuntu@192.168.10.$ip 'sudo apt list --upgradable 2>/dev/null | grep -c upgradable || echo "0 packages pending"'
done
```

### Prerequisites

The master node's `/root/.ssh` private key must be authorized on all cluster nodes (ubuntu user). This is the same requirement as `containerd-cleanup`. Verify with:

```bash
# Run from a master node
for ip in 51 52 53 61 62 63 65; do
  ssh -o StrictHostKeyChecking=no ubuntu@192.168.10.$ip 'echo "192.168.10.'$ip' ok"'
done
```

---

## 🖼️ Servarr Image Auto-Update

Weekly CronJob that performs a **rolling restart** of all Deployments in the `servarr` namespace, forcing Kubernetes to pull the latest version of each image.

**File:** `servarr-image-updater-cronjob.yaml`

### How it works

- Runs every **Sunday at 3 AM** (one hour after containerd-cleanup frees disk space)
- Iterates through every Deployment in `servarr`, restarts it, and waits for the rollout to succeed before moving to the next
- If any rollout fails, the job exits with a non-zero code so it shows as `Failed` in CronJob history

### imagePullPolicy requirements

Images tagged `:latest` already default to `imagePullPolicy: Always`. The following deployments use **mutable non-latest tags** and have been explicitly patched:

| Deployment | Image Tag | Fix Applied |
|------------|-----------|-------------|
| `bazarr` | `:development` | `imagePullPolicy: Always` added |
| `jellyseerr` | `:preview-OIDC` | `imagePullPolicy: Always` added |
| `seerr` | `:preview-OIDC` | `imagePullPolicy: Always` added |

### Setup (one-time)

```bash
kubectl apply -f servarr-image-updater-cronjob.yaml

# Verify ServiceAccount, Role, RoleBinding, and CronJob were created
kubectl -n kube-system get cronjob servarr-image-updater
kubectl -n servarr get rolebinding servarr-image-updater
```

### Trigger manually

```bash
kubectl create job --from=cronjob/servarr-image-updater servarr-update-manual -n kube-system
kubectl -n kube-system logs -f job/servarr-update-manual
```

### Check last run

```bash
# List recent update jobs
kubectl -n kube-system get jobs --sort-by=.status.startTime | grep servarr

# View logs from last run
kubectl -n kube-system logs job/<servarr-image-updater-timestamp>
```

---

## 🔍 Troubleshooting

### CronJob Not Running
```bash
# Check if CronJob is suspended
kubectl -n kube-system get cronjob containerd-cleanup -o yaml | grep suspend

# Check recent jobs
kubectl -n kube-system get jobs

# Check for errors in CronJob
kubectl -n kube-system describe cronjob containerd-cleanup
```

### Cleanup Job Failed
```bash
# View job logs
kubectl -n kube-system logs job/<job-name>

# Check if SSH access from master to workers is working
kubectl -n kube-system get pods -l job-name=<job-name> -o wide
```

### Worker Still Full After Cleanup
```bash
# SSH to worker and check what's using space
ssh ubuntu@192.168.10.62 'sudo du -sh /var/lib/rancher/k3s/agent/* | sort -h'

# Check for large log files
ssh ubuntu@192.168.10.62 'sudo du -sh /var/log/*'

# Check containerd content
ssh ubuntu@192.168.10.62 'sudo du -sh /var/lib/rancher/k3s/agent/containerd/io.containerd.* | sort -h'
```

## 📝 Notes

- **Worker disk size**: 19GB per VM (not expandable per user request)
- **Image duplication**: ~5GB across 4 workers is normal and acceptable
- **Cleanup is safe**: Only removes stopped containers and unreferenced images
- **Pre-pull benefits**: Speeds up pod starts, reduces internet bandwidth usage
- **SSH access required**: CronJobs run from master nodes and SSH to workers

## Related Documentation

- [Observability Stack](../10.observability/README.md)
- [K3s Cluster Overview](../README.md)
- [Recovery Guide](../RECOVERY.md)
