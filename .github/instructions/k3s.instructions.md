---
applyTo: "2-k3s/**"
description: "Instructions for K3s Kubernetes cluster setup"
---

# K3s-Specific Instructions

When working with files in the `2-k3s/` directory, follow these K3s and Kubernetes-specific guidelines.

## K3s Architecture

- **Server (Master)**: Runs control plane components (API server, scheduler, controller)
- **Agent (Worker)**: Runs workloads, connects to server
- **Embedded Components**: CoreDNS, Traefik (ingress), kube-vip-cloud-provider (virtual IP management), local-path provisioner
- **Lightweight**: Single 50MB binary, minimal dependencies
- **Embeded Registry**: Local shared image storage

## Critical Pre-Installation Steps

### Always Disable Swap First
```bash
# Kubernetes cannot run with swap enabled
sudo swapoff -a
sudo sed -i '/ swap / s/^/#/' /etc/fstab
```

### Enable IP Forwarding
```bash
echo "net.ipv4.ip_forward=1" | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

###  Configure shared IP for HA
- Use kube-vip-cloud-provider for virtual IP management

## Installation Patterns

### Master Node Installation
```bash
# Basic installation
# Create Master Node 1
# IMPORTANT: Includes etcd auto-compaction to prevent database filling up
k3sup install \
  --cluster \
  --host 192.168.10.51 \
  --user ubuntu \
  --k3s-channel stable \
  --k3s-extra-args "--disable servicelb --disable traefik --node-ip 10.0.0.51 --advertise-address 10.0.0.51 --flannel-iface eth1 --node-taint node-role.kubernetes.io/control-plane:NoSchedule --write-kubeconfig-mode=644 --tls-san 192.168.10.100 --tls-san 192.168.10.51 --tls-san 192.168.10.52 --tls-san 192.168.10.53 --tls-san 10.0.0.51 --tls-san 10.0.0.52 --tls-san 10.0.0.53 --etcd-arg=--auto-compaction-mode=periodic --etcd-arg=--auto-compaction-retention=1h --etcd-arg=--quota-backend-bytes=8589934592 --kube-controller-manager-arg=bind-address=0.0.0.0 --kube-scheduler-arg=bind-address=0.0.0.0 --etcd-expose-metrics=true"

# Add Master Node 2
k3sup join \
  --server \
  --server-host 192.168.10.100 \
  --server-user ubuntu \
  --host 192.168.10.52 \
  --user ubuntu \
  --k3s-channel stable \
  --k3s-extra-args "--node-ip 10.0.0.52 --advertise-address 10.0.0.52 --flannel-iface eth1 --node-taint node-role.kubernetes.io/control-plane:NoSchedule --write-kubeconfig-mode=644 --disable servicelb --disable traefik --tls-san 192.168.10.100 --tls-san 192.168.10.51 --tls-san 192.168.10.52 --tls-san 192.168.10.53 --tls-san 10.0.0.51 --tls-san 10.0.0.52 --tls-san 10.0.0.53 --etcd-arg=--auto-compaction-mode=periodic --etcd-arg=--auto-compaction-retention=1h --etcd-arg=--quota-backend-bytes=8589934592 --kube-controller-manager-arg=bind-address=0.0.0.0 --kube-scheduler-arg=bind-address=0.0.0.0 --etcd-expose-metrics=true"

# Add Master Node 3
k3sup join \
  --server \
  --server-host 192.168.10.100 \
  --server-user ubuntu \
  --host 192.168.10.53 \
  --user ubuntu \
  --k3s-channel stable \
  --k3s-extra-args "--node-ip 10.0.0.53 --advertise-address 10.0.0.53 --flannel-iface eth1 --node-taint node-role.kubernetes.io/control-plane:NoSchedule --write-kubeconfig-mode=644 --disable servicelb --disable traefik --tls-san 192.168.10.100 --tls-san 192.168.10.51 --tls-san 192.168.10.52 --tls-san 192.168.10.53 --tls-san 10.0.0.51 --tls-san 10.0.0.52 --tls-san 10.0.0.53 --etcd-arg=--auto-compaction-mode=periodic --etcd-arg=--auto-compaction-retention=1h --etcd-arg=--quota-backend-bytes=8589934592 --kube-controller-manager-arg=bind-address=0.0.0.0 --kube-scheduler-arg=bind-address=0.0.0.0 --etcd-expose-metrics=true"
```

**Etcd Configuration Explained:**
- `--auto-compaction-mode=periodic`: Enables automatic compaction every hour
- `--auto-compaction-retention=1h`: Removes old etcd revisions older than 1 hour
- `--quota-backend-bytes=8589934592`: Sets 8GB quota (default is 2GB) to prevent "database full" errors

**Control-plane metrics exposure (issue #121):**
The three flags appended to each master's `--k3s-extra-args` above keep control-plane metrics
reachable on a fresh rebuild. The **running** cluster applies the same three args out-of-band via a
merged `/etc/rancher/k3s/config.yaml` drop-in on each master:

```yaml
kube-controller-manager-arg:
  - "bind-address=0.0.0.0"
kube-scheduler-arg:
  - "bind-address=0.0.0.0"
etcd-expose-metrics: true
```

This exposes kube-controller-manager (10257/https), kube-scheduler (10259/https) and etcd (2381/http)
metrics so Prometheus (kube-prometheus-stack) can scrape them. Without these binds, those components
listen only on 127.0.0.1 and the exporter Services show empty Endpoints.

The drop-in above is the *partial* (#121-era) form. It has since been consolidated into the full
single-source-of-truth file below (issue #148).

**Single source of truth: `/etc/rancher/k3s/config.yaml` (issue #148):**
All three masters (51/52/53) standardize on `/etc/rancher/k3s/config.yaml` as the **single source of
truth** for server + kubelet args, and the k3sup-generated systemd unit `ExecStart` is reduced to a
bare `/usr/local/bin/k3s server`.

**Why:** k3s CLI flags **override** `config.yaml`, so inline `ExecStart` args must NOT coexist with
the config file — that mixed state was the #148 drift. All three masters had carried their full
server/kubelet args inline in `ExecStart` while also sharing a partial `config.yaml`; the issue
originally mis-scoped this to only master-52, but inspection found all three affected.

Canonical consolidated `config.yaml` for a **joining** master (52/53) — the join token is NOT in this
file:

```yaml
server: https://<join-endpoint>:6443     # 52 -> 192.168.10.100 (VIP); 53 -> 10.0.0.51
node-ip: 10.0.0.5X
advertise-address: 10.0.0.5X
flannel-iface: eth1
write-kubeconfig-mode: "644"
embedded-registry: true
node-taint:
  - "node-role.kubernetes.io/control-plane:NoSchedule"
tls-san:
  - "192.168.10.100"
  - "192.168.10.51"
  - "192.168.10.52"
  - "192.168.10.53"
  - "10.0.0.51"
  - "10.0.0.52"
  - "10.0.0.53"
disable:
  - servicelb
  - traefik
etcd-arg:
  - "auto-compaction-mode=periodic"
  - "auto-compaction-retention=1h"
  - "quota-backend-bytes=8589934592"
etcd-expose-metrics: true
kubelet-arg:
  - "resolv-conf=/etc/k3s-resolv.conf"
kube-controller-manager-arg:
  - "bind-address=0.0.0.0"
kube-scheduler-arg:
  - "bind-address=0.0.0.0"
# join token NOT in this file — supplied via /etc/systemd/system/k3s.service.env (K3S_TOKEN, mode 0600)
```

Per-master distinctions:
- **51** = founding etcd member: the same file but with `cluster-init: true` instead of `server:`,
  and no join token at all.
- **52** = joins the VIP `https://192.168.10.100:6443`.
- **53** = joins 51's real IP `https://10.0.0.51:6443`.

**Join identity:** the join token stays in `/etc/systemd/system/k3s.service.env` as `K3S_TOKEN`
(mode `0600`, referenced by the unit's `EnvironmentFile=`), NEVER inline in `ExecStart` and NEVER in
`config.yaml`. (master-53's previously-inline token — a secret-hygiene issue in its 0644 unit — was
relocated here as part of the reconcile.)

**Gotchas:**
- `etcd-arg` list entries omit the leading `--` (k3s prepends it); contrast the `--k3s-extra-args`
  CLI form above, which keeps `--etcd-arg=--…`.
- `cluster-init: true` and `server:` are **mutually exclusive** — 51 uses the former, 52/53 the latter.

**Canonical master config invariants (#148):** treat the following as the host-local
contract for every master, applied out-of-band (NOT reconciled by ArgoCD):
- `/etc/rancher/k3s/config.yaml` is the **single source of truth** for server + kubelet args.
- The systemd unit `ExecStart` stays **bare** — `ExecStart=/usr/local/bin/k3s server`, with NO
  inline server/kubelet flags (CLI flags override `config.yaml`, so the two must not mix).
- The join token lives ONLY in `/etc/systemd/system/k3s.service.env` as `K3S_TOKEN` (mode `0600`,
  referenced via `EnvironmentFile=`) — NEVER inline in `ExecStart`, NEVER in `config.yaml`.
- The pod-side DNS pin `/etc/k3s-resolv.conf` = `nameserver 192.168.10.30`, wired via
  `kubelet-arg: resolv-conf=/etc/k3s-resolv.conf`.
- The #121 control-plane metrics binds (`kube-controller-manager-arg`/`kube-scheduler-arg`
  `bind-address=0.0.0.0`, `etcd-expose-metrics: true`) stay in `config.yaml`.

**Forward note (#44 / #201):** the system-upgrade-controller k3s upgrade is a **binary swap** — it
does NOT re-run `install.sh` and does NOT regenerate the systemd unit, so these invariants are
expected to survive controller-driven upgrades by construction. Because they are host-local and
un-reconciled, **verify them after the next real k3s bump** with the
"#148/#121 invariant checklist" + "Remediation" recipe in
`2-k3s/maintenance/system-upgrade/README.md` (Verification After Upgrade). On-host `.bak-148`
backups are the restore source if a check ever fails.

### Get Join Token
```bash
# On master node
sudo cat /var/lib/rancher/k3s/server/node-token
```

### Worker Node Installation
```bash
# Add Worker Node 1
k3sup join \
  --server-host 192.168.10.100 \
  --server-user ubuntu \
  --host 192.168.10.61 \
  --user ubuntu \
  --k3s-channel stable \
  --k3s-extra-args "--node-ip 10.0.0.61 --flannel-iface eth1"

# Add Worker Node 2
k3sup join \
  --server-host 192.168.10.100 \
  --server-user ubuntu \
  --host 192.168.10.62 \
  --user ubuntu \
  --k3s-channel stable \
  --k3s-extra-args "--node-ip 10.0.0.62 --flannel-iface eth1"

# Add Worker Node 3
k3sup join \
  --server-host 192.168.10.100 \
  --server-user ubuntu \
  --host 192.168.10.63 \
  --user ubuntu \
  --k3s-channel stable \
  --k3s-extra-args "--node-ip 10.0.0.63 --flannel-iface eth1"

# Add Worker Node 5
k3sup join \
  --server-host 192.168.10.100 \
  --server-user ubuntu \
  --host 192.168.10.65 \
  --user ubuntu \
  --k3s-channel stable \
  --k3s-extra-args "--node-ip 10.0.0.65 --flannel-iface eth1"
```

## IMPORTANT: Node Netplan Configuration

**Do NOT include `search: epaflix.com`** in node netplan configs. This causes DNS issues where external domains like `code.visualstudio.com` get resolved as `code.visualstudio.com.epaflix.com` and hit the Pi-hole wildcard catchall.

> **Pod-side DNS (separate from netplan):** Every k3s node also carries `/etc/k3s-resolv.conf`, pinned to `nameserver 192.168.10.30` and wired to kubelet via `kubelet-arg: resolv-conf=/etc/k3s-resolv.conf` in `/etc/rancher/k3s/config.yaml`. This is what pods with `dnsPolicy: Default` (including CoreDNS itself) see as their `/etc/resolv.conf` — it is independent of the host's netplan/systemd-resolved chain. To change pod-side upstream DNS, edit this file on *every* node (masters and workers) and `kubectl rollout restart -n kube-system deployment/coredns`. Setup procedure in `2-k3s/05.traefik-deployment/README.md` step 0.

Example correct netplan (`/etc/netplan/50-cloud-init.yaml`):
```yaml
network:
  version: 2
  ethernets:
    eth0:
      match:
        macaddress: "bc:24:11:xx:xx:xx"
      addresses:
      - "192.168.10.51/24"
      nameservers:
        addresses:
      - 192.168.10.30
        # NO search domain here!
      set-name: "eth0"
      routes:
      - to: "default"
        via: "192.168.10.1"
    eth1:
      match:
        macaddress: "bc:24:11:xx:xx:xx"
      addresses:
      - "10.0.0.51/24"
      nameservers:
        addresses:
        - 192.168.10.30
      set-name: "eth1"
```

## Configuration Management

### kubeconfig Setup
```bash
# Option 1: Copy to standard location
mkdir -p ~/.kube
sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config
sudo chown $USER:$USER ~/.kube/config

# Option 2: Environment variable
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

# For remote access: Edit server address in k3s.yaml
# Change 127.0.0.1 to actual master node IP
```

### Service Management
```bash
# Master node service
sudo systemctl status k3s
sudo systemctl restart k3s
sudo journalctl -u k3s -f

# Worker node service
sudo systemctl status k3s-agent
sudo systemctl restart k3s-agent
sudo journalctl -u k3s-agent -f
```

## Essential Add-on Installation Order

### 1. kube-vip

## Prerequisites

- k3s cluster with kube-vip installed for control-plane HA
- kubectl configured to access the cluster
- Available IP address range for LoadBalancer services

## Network Configuration

This cluster uses a **dual-network setup**:

- **External Network (`192.168.10.X`)**: Used for internet access, NFS connections, and **LoadBalancer services**
- **Internal Network (`10.0.0.X`)**: Used for internal cluster communication (flannel overlay network)
  - Node IPs: 10.0.0.51, 10.0.0.52, 10.0.0.53

### Important Notes:

1. **LoadBalancer IPs must use the external network (`192.168.10.X`)** - This is the network where clients connect from
2. kube-vip will advertise LoadBalancer IPs via ARP on the `192.168.10.X` network (eth0 interface)
3. The internal network (`10.0.0.X`) is already configured with flannel on eth1 and doesn't need changes
4. All IP pool configurations below should use IPs from the `192.168.10.X` range

### 2. Ingress Controller
```bash
# Install Helm first
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

```
# Traefik Reverse Proxy for *.epaflix.com

This deployment configures Traefik as a reverse proxy with automatic TLS certificates via Let's Encrypt and Cloudflare DNS challenge.

## Architecture

- **Static IP**: `192.168.10.101` (via kube-vip LoadBalancer)
- **TLS**: Let's Encrypt with Cloudflare DNS-01 challenge (supports wildcard `*.epaflix.com`)
- **Namespace**: `traefik-system`
- **Replicas**: 1
- **Service Type**: LoadBalancer (kube-vip will handle the virtual IP)
- **Router**: Forward ports 80/443 → `192.168.10.101`
- **DNS**: Pi-hole points `*.epaflix.com` → router public IP or `192.168.10.101` for LAN

### 3. Let's Encrypt and Cloudflare DNS challenge (SSL/TLS)

- Cloudflare API Token with DNS edit permissions for `epaflix.com`
- Router configured to forward 80 and 443 to `192.168.10.101`
- Pi-hole DNS records -> router public IP for external access and `192.168.10.101` for internal access.

### 4. Monitoring Stack (Optional)
```bash
# TO BE ADDED
```

## Kubernetes Resource Patterns

### Namespace Creation
```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: my-app
```

### Deployment Pattern
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
  namespace: my-app
spec:
  replicas: 3
  selector:
    matchLabels:
      app: my-app
  template:
    metadata:
      labels:
        app: my-app
    spec:
      containers:
      - name: app
        image: my-app:latest
        ports:
        - containerPort: 8080
```

### Service with LoadBalancer
```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-app
  namespace: my-app
spec:
  type: LoadBalancer
  selector:
    app: my-app
  ports:
  - port: 80
    targetPort: 8080
```

## Common Verification Commands

### Cluster Health
```bash
# Check nodes
kubectl get nodes -o wide

# Check system pods
kubectl get pods -A

# Check cluster info
kubectl cluster-info

# Check component status
kubectl get --raw='/readyz?verbose'
```

### Debugging Pods
```bash
# Get pod details
kubectl describe pod <pod-name> -n <namespace>

# Get logs
kubectl logs <pod-name> -n <namespace>
kubectl logs <pod-name> -n <namespace> -f  # Follow logs

# Get previous logs (if pod crashed)
kubectl logs <pod-name> -n <namespace> --previous

# Execute command in pod
kubectl exec -it <pod-name> -n <namespace> -- /bin/bash
```

### Resource Usage
```bash
# Node resources
kubectl top nodes

# Pod resources
kubectl top pods -A
```

## Troubleshooting K3s Issues

### Nodes Not Ready
```bash
# Check node status
kubectl describe node <node-name>

# Check K3s service
sudo systemctl status k3s
sudo journalctl -u k3s -n 50

# Check for common issues
# - Network plugin issues
# - Insufficient resources
# - Kubelet not running
```

### Pods in Pending State
```bash
# Check pod events
kubectl describe pod <pod-name> -n <namespace>

# Common causes:
# - Insufficient resources (CPU/memory)
# - No available nodes matching nodeSelector
# - PersistentVolume not available
# - Image pull errors
```

### Service Not Accessible
```bash
# Check service
kubectl get svc -n <namespace>

# Check endpoints
kubectl get endpoints -n <namespace>

# Check LoadBalancer status
kubectl get svc <service-name> -n <namespace> -o yaml

# If EXTERNAL-IP is pending, check MetalLB
kubectl get pods -n metallb-system
kubectl logs -n metallb-system deployment/controller
```

## Security Best Practices

- Use RBAC for access control
- Enable Pod Security Standards
- Use NetworkPolicies to restrict traffic
- Regularly update K3s version
- Use secrets for sensitive data
- Enable audit logging
- Use private container registry

## Uninstalling K3s

### On Master Node
```bash
/usr/local/bin/k3s-uninstall.sh
```

### On Worker Node
```bash
/usr/local/bin/k3s-agent-uninstall.sh
```

## K3s Configuration Files

- **Kubeconfig**: `/etc/rancher/k3s/k3s.yaml`
- **Data directory**: `/var/lib/rancher/k3s`
- **Manifests auto-deploy**: `/var/lib/rancher/k3s/server/manifests/`
- **Service file (master)**: `/etc/systemd/system/k3s.service`
- **Service file (worker)**: `/etc/systemd/system/k3s-agent.service`

## Performance Tuning

- **etcd**: K3s uses embedded etcd
- **Resource limits**: Set appropriate requests/limits on pods
- **Node taints/tolerations**: Control workload placement
- **HPA**: Horizontal Pod Autoscaling for dynamic scaling

- **Embedded Registry Mirror**: Enabled (Spegel P2P image sharing across all nodes via 10.0.0.0/24 network)

The `secrets.yml` file has the following structure:
```yaml
k3s-master-51_username: "<username>"
k3s-master-51_password: "<password>"
k3s-master-52_username: "<username>"
k3s-master-52_password: "<password>"
k3s-master-53_username: "<username>"
k3s-master-53_password: "<password>"
k3s-worker-61_username: "<username>"
k3s-worker-61_password: "<password>"
k3s-worker-62_username: "<username>"
k3s-worker-62_password: "<password>"
k3s-worker-63_username: "<username>"
k3s-worker-63_password: "<password>"
k3s-worker-65_username: "<username>"
k3s-worker-65_password: "<password>"
```
