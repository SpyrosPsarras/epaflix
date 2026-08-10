# Comprehensive Observability Stack

Complete monitoring, logging, and service mesh observability for the K3s cluster with Proxmox host monitoring.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Data Flow                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  Proxmox Hosts ──► pve-exporter ──────┐                         │
│  K8s Nodes ─────► node-exporter ──────┤                         │
│  Pods ──────────► kube-state-metrics ─┤                         │
│  Cilium ────────► cilium-agent ───────┼──► Prometheus ──┐       │
│  Hubble ────────► hubble-relay ───────┘                 │       │
│  Istio ─────────► envoy sidecars ─────────────────────  │       │
│                                                          │       │
│  Pod Logs ───► Promtail ───► Loki ─────────────────────┼──┐    │
│                                                          │  │    │
│                                            ┌─────────────┘  │    │
│                                            ▼                ▼    │
│                                         Grafana ◄───── AlertMgr │
│                                            │                     │
│                                            │                     │
│  Service Mesh Visualization:               │                     │
│  Istio Services ──► Kiali ─────────────────┘                    │
│  Cilium Flows ──► Hubble UI                                     │
└─────────────────────────────────────────────────────────────────┘
```

## Stack Components

### Currently Deployed ✅
- **Prometheus** (1 replica): Time-series metrics database with **8d** retention (see `### Prometheus Storage` - the 20GiB size cap is the real bound, #913)
- **Grafana** (2 replicas): Unified dashboards with folder organization, OAuth via Authentik
- **AlertManager** (3 replicas): Email alerting to admin@epaflix.com
- **node-exporter** (DaemonSet): Node-level CPU, memory, disk, network metrics
- **kube-state-metrics** (2 replicas): Kubernetes cluster state metrics
- **pve-exporter** (1 replica): Proxmox VE host and VM metrics from 192.168.10.10 and .11

### Optional Components (Not Installed)
- **Cilium CNI**: eBPF-based networking (currently using Flannel)
- **Hubble**: L3-L7 network observability (requires Cilium)
- **Istio**: Service mesh with mTLS, traffic management, telemetry (not currently deployed)
- **Kiali**: Service mesh topology visualization (not currently deployed)
- **Loki**: Log aggregation with 31d retention
- **Promtail**: Log collection from pods

## Access URLs

| Component | URL | Credentials | Status |
|-----------|-----|-------------|--------|
| **Grafana** | https://grafana.epaflix.com | Authentik SSO (or admin / <POSTGRES_PASSWORD>) | ✅ Running |
| **Prometheus** | Port-forward only | N/A | ✅ Running |
| **AlertManager** | Port-forward only | N/A | ✅ Running |

## Installation
   # - k3s-master-51: 10GB (192.168.10.51, Proxmox takaros)
   # - k3s-master-52: 10GB (192.168.10.52, Proxmox takaros)
   # - k3s-master-53: 10GB (192.168.10.53, Proxmox evanthoulaki)
   # Workers: 22GB RAM each (workload pods)
   # - k3s-worker-61: 22GB (192.168.10.61, Proxmox takaros)
   # - k3s-worker-62: 22GB (192.168.10.62, Proxmox takaros)
   # - k3s-worker-63: 22GB (192.168.10.63, Proxmox evanthoulaki)
   # - k3s-worker-65: 22GB (192.168.10.65, Proxmox evanthoulaki)
   # Total Cluster RAM: 118GB (30GB control-plane + 88GB workload capacity)
   ```

2. **Proxmox API Tokens** (CONFIGURED ✅):
   - Token created: `root@pam!grafana`
   - Configured for both hosts: 192.168.10.10 and 192.168.10.11
   - pve-exporter deployed and running
   ```bash
   # Backup etcd
   kubectl exec -n kube-system $(kubectl get pod -n kube-system -l component=etcd -o jsonpath='{.items[0].metadata.name}') -- \
     etcdctl --cacert=/var/lib/rancher/k3s/server/tls/etcd/server-ca.crt \
     --cert=/var/lib/rancher/k3s/server/tls/etcd/server-client.crt \
     --key=/var/lib/rancher/k3s/server/tls/etcd/server-client.key \
     snapshot save /tmp/etcd-backup.db

   # Install Cilium CLI
   CILIUM_CLI_VERSION=$(curl -s https://raw.githubusercontent.com/cilium/cilium-cli/main/stable.txt)
   curl -L --remote-name-all https://github.com/cilium/cilium-cli/releases/download/${CILIUM_CLI_VERSION}/cilium-linux-amd64.tar.gz
   tar xzvfC cilium-linux-amd64.tar.gz .
   sudo mv cilium /usr/local/bin/

   # Install Cilium with Hubble
   cilium install \
     --set ipam.operator.clusterPoolIPv4PodCIDR=10.42.0.0/16 \
     --set k8sServiceHost=192.168.10.100 \
     --set k8sServicePort=6443 \
     --set kubeProxyReplacement=false \
     --set hubble.relay.enabled=true \
     --set hubble.ui.enabled=true \
     --set hubble.metrics.enabled="{dns,drop,tcp,flow,icmp,http}" \
     --set prometheus.enabled=true \
     --set operator.prometheus.enabled=true

   # Restart all pods
   kubectl get pods -A -o custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name --no-headers | \
     while read ns pod; do kubectl delete pod -n $ns $pod --wait=false; done

   # Verify
   cilium status --wait
   cilium connectivity test

   # Remove Flannel
   kubectl -n kube-system delete ds kube-flannel-ds || true

   # Test for 48 hours, then enable kube-proxy replacement:
   cilium upgrade --set kubeProxyReplacement=true --set bpf.masquerade=true
   # Monitor for 24 hours post-upgrade
   ```

   </details>

4. **Authentik OAuth Provider** (CONFIGURED ✅):
   - Login to https://auth.epaflix.com
   - Create OAuth2/OIDC Provider
   - Name: Grafana Monitor
   - Redirect URI: https://graf (CONFIGURED ✅):
   - Grafana OAuth configured with Authentik
   - Application: "Grafana Monitor"
   - URL: https://grafana.epaflix.com
   - Group-based role assignment: "Grafana Admins" → Admin, "Grafana Editors" → Editor, default → Viewer
cd /workspaces/01-manual\ installation/manifests/10.observability/

# Make deploy script executable
chmod +x deploy.sh

# Run deployment
./deploy.sh
```

The script will:
1. Create namespace and PostgreSQL database for Grafana
2. Create PersistentVolumeClaims (100Gi for Prometheus, 200Gi for Loki)
3. Install kube-prometheus-stack (Prometheus + Grafana)
4. Install Loki and Promtail
5. Deploy Proxmox VE Exporter
6. Apply custom alert rules
7. Create ingress routes

### Istio / Cilium / Kiali

Not currently deployed. The cluster uses Flannel CNI. Istio/Cilium can be added later for service mesh capabilities.

## Grafana Configuration

### Dashboard Folders

Dashboards are organized into folders:
- **Kubernetes Cluster**: Cluster overview, node metrics, kube-state-metrics
- **Network & Service Mesh**: Cilium/Hubble metrics, Istio dashboards
- **Infrastructure**: Proxmox VE monitoring
- **Logs**: Loki logs browser and stack monitoring

### Default Home Dashboard

The default home dashboard is "Kubernetes Cluster Overview" (Grafana ID 315).

### Pinned Favorites

Pre-configured favorite dashboards:
- Kubernetes Cluster Overview
- Node Exporter Full
- Loki Logs Browser
- Cilium Hubble Overview

### Importing Additional Dashboards

```bash
# From Grafana UI
# 1. Click "+" → "Import"
# 2. Enter dashboard ID:
#    - 315: Kubernetes Cluster Overview
#    - 1860: Node Exporter Full
#    - 16611: Cilium Hubble Overview
#    - 7645: Istio Control Plane
#    - 7636: Istio Service Dashboard
# 3. Select folder
# 4. Select Prometheus datasource
# 5. Click "Import"
**Current Dashboard Status:**
- ✅ **315** (Kubernetes Cluster Overview) - Working, some panels may be empty
- ✅ **1860** (Node Exporter Full) - Fully working
- ⏳ **10347** (Proxmox VE Cluster) - Working (wait 2-3 min for initial scrape)
- ❌ **16611** (Cilium Hubble) - Requires Cilium CNI installation
- ❌ **7645** (Istio Control Plane) - Requires Istio installation
- ❌ **7636** (Istio Service) - Requires Istio installation

To import dashboards in Grafana:
1. Navigate to https://grafana.epaflix.com/dashboard/import
2. Enter dashboard ID and click "Load"
3. Select folder and Prometheus datasource
4. Click "Import"
# Port forward for CLI access
cilium hubble port-forward &
```

### Common Commands

```bash
# Observe all flows
hubble observe --follow

# Observe specific namespace
hubble observe --namespace app-authentik --follow

# Filter by protocol
hubble observe --protocol http --follow
hubble observe --protocol dns --follow

# See dropped packets
hubble observe --verdict DROPPED --follow

# Filter by pod
hubble observe --pod authentik-server --follow

# View L7 HTTP requests
hubble observe --namespace app-authentik --protocol http -o json | jq '.l7.http'

# See DNS queries
hubble observe --protocol dns -o compact

# Network policy denials
hubble observe --verdict DENIED --follow
```

## Performance Metrics

After Cilium kube-proxy replacement, expected improvements:
- **Latency**: 40-50% reduction in p50/p95/p99
- **Throughput**: 10-20% increase
- **CPU overhead**: 20-30% reduction vs iptables

See [PERFORMANCE-METRICS.md](PERFORMANCE-METRICS.md) for detailed before/after comparison.

## Alerting

### Email Configuration

The single `email` receiver carries two legs - `email_configs` (SMTP) and
`webhook_configs` (ntfy topic `k8s-alertmanager`, added by #568/PR #574).

SMTP relays through `vh4c.ezhellas.com:587` (STARTTLS + AUTH), **not**
`mail.epaflix.com`. `mail.epaflix.com` is caught by the proxied
`*.epaflix.com` Cloudflare wildcard, so it answers on 443 but carries no
SMTP and every send timed out for months (#461). `vh4c.ezhellas.com` is the
real hosting node behind the domain's MX record
(`_dc-mx.a4bc4ec9e03b.epaflix.com` → `188.40.204.212`) and its TLS cert CN
matches, so `smtp_require_tls: true` verifies.

Recipient is the provisioned `alert@epaflix.com` mailbox. The previous
`admin@epaflix.com` does not exist on the relay - it answers
`550 5.1.1 ... User unknown in virtual alias table`.

Credentials live in the SOPS-encrypted
`secrets/alertmanager-config-secret.enc.yaml`; the plaintext source is the
git-ignored `secrets.yml` under the `alert_email_*` keys. The Prometheus
Operator regenerates
`alertmanager-kube-prometheus-stack-alertmanager-generated` when that Secret
changes and the `config-reloader` sidecar POSTs `/-/reload`, so rotating the
password needs no pod restart (the #299 `secretKeyRef` gap does not apply -
this is a mounted volume, not an env var).

### Default Alerts

- **Node**: Memory >85%, Disk >90%, CPU sustained high load
- **Pods**: CrashLoopBackOff, Failed, OOMKilled
- **Storage**: PVC usage >85%
- **Prometheus**: Scrape failures, rule evaluation failures
- **Loki**: Storage >85%, ingestion failures
- **Proxmox**: Host unreachable
- **Cilium**: Agent down, high packet drop rate
- **Istio**: Sidecar crash loops

#### What is deliberately NOT emailed (#908)

Two alerts are routed to a `null` receiver and reach nobody:

| Alert | Why |
|---|---|
| `Watchdog` | kube-prometheus-stack's deadman switch. Its expr is `vector(1)`, so it fires forever by design. An email about it carries no information. Nothing consumes the deadman signal yet - that is #970. |
| `InfoInhibitor` | Exists only to suppress other `severity: info` alerts. Carries `severity: none`. |

The `null` route is the **first** child route and has no `continue: true`, so those two
stop there and everything else falls through to `email`. Check it without deploying:

```bash
amtool config routes test --config.file=<decrypted alertmanager.yaml> alertname=Watchdog severity=none
#   -> null
amtool config routes test --config.file=<decrypted alertmanager.yaml> alertname=KubeJobFailed severity=warning
#   -> email
```

The email leg also sets `send_resolved: true`. Alertmanager defaults that to **false**
for `email_configs`, and it was simply absent, so the mailbox only ever received
`[FIRING` and a fault that fixed itself was never retracted. Adding a new receiver
means setting it explicitly - the default is the wrong one.

### Testing Email Alerts

```bash
# Trigger test alert
kubectl exec -n observability alertmanager-kube-prometheus-stack-alertmanager-0 -- \
  amtool alert add test_alert alertname=TestEmailAlert

# Check AlertManager status
kubectl port-forward -n observability svc/kube-prometheus-stack-alertmanager 9093:9093
# Open http://localhost:9093

# Silence alert
kubectl exec -n observability alertmanager-kube-prometheus-stack-alertmanager-0 -- \
  amtool silence add alertname=TestEmailAlert
```

## Storage Management

### Storage Architecture

Observability components use K3s `local-path` StorageClass, which provisions storage on the node's local filesystem. This filesystem is actually backed by TrueNAS iSCSI targets attached to each VM at the Proxmox layer.

**Storage Flow:**
```
Prometheus PVC → local-path provisioner → /var/lib/rancher/k3s/storage/ → VM disk → iSCSI target → TrueNAS Apps pool (SSD)
```

**Important Considerations:**
- **ReadWriteOnce limitation**: PVCs are bound to specific nodes, pods cannot move to other nodes
- **No automatic HA**: If a worker node fails, Prometheus/Loki data is inaccessible until the node recovers
- **Scaling constraint**: Prometheus is limited to 1 replica (ReadWriteOnce PVC). Grafana runs 2 replicas (uses database-backed sessions, not local storage).

For production environments requiring HA, consider:
- Deploying Prometheus with remote write to long-term storage (Thanos, VictoriaMetrics)
- Using Loki with S3-compatible object storage backend
- Migrating to shared storage (Longhorn, Ceph RBD with RWX support)

### Prometheus Storage

- **Size**: 25Gi PVC using local-path StorageClass - **advisory only**, see the note below
- **Retention**: **8 days** (`prometheus.prometheusSpec.retention: 8d`)
- **Size cap**: 20GiB (`retentionSize: "20GB"` - Prometheus parses `GB` as base-2)
- **StorageClass**: local-path (provisioned from node's `/var/lib/rancher/k3s/storage/`, which resides on VM disk backed by TrueNAS iSCSI)
- **Access Mode**: ReadWriteOnce
- **Measured usage** (2026-08-10): 16.6GiB of blocks plus 0.5GiB WAL = 86% of the cap, at 618k head series and ~1.95GiB/day

> **Retention is 8d, not 15d, and that is deliberate (#913).** It used to say `15d`
> while the 20GiB size cap evicted blocks at about 8.5 days, so documented retention
> was 43% longer than real retention and nothing reported the disagreement. Two
> settings that disagree silently are worse than the smaller number, because an
> investigation that assumes 15 days of history quietly gets wrong answers - that
> is exactly what happened while triaging #908/#909.
>
> **The volume was not grown instead, because it does not fit.** 15 days needs about
> 29GiB of blocks, roughly 12GiB more than today. `local-path` is a bind mount with
> **no quota**, so the 25Gi claim is advisory and the real bound is the node root
> disk: `k3s-worker-62` measured 11.7GiB free of 47.4GiB (75% used). #463 already
> showed the failure mode - at `45GB` Prometheus grew to 30G, pushed a worker to 85%
> and broke kubelet image GC.
>
> To get more history, cut cardinality (618k head series is the lever) or move
> Prometheus to a bigger disk. Do **not** raise `retentionSize` above the claim, and
> do not raise `retention` without re-measuring both.

To re-check that the two settings still agree:

```bash
kubectl --context epaflix -n observability port-forward svc/kube-prometheus-stack-prometheus 19090:9090 &
# real retention in days - must stay at or below the configured retention
curl -sG http://127.0.0.1:19090/api/v1/query \
  --data-urlencode 'query=(time() - prometheus_tsdb_lowest_timestamp_seconds)/86400'
# blocks+WAL against the 20GiB cap
curl -sG http://127.0.0.1:19090/api/v1/query \
  --data-urlencode 'query=prometheus_tsdb_storage_blocks_bytes + prometheus_tsdb_wal_storage_size_bytes'
```

### Loki Storage

- **Size**: 15Gi PVC using local-path StorageClass
- **Retention**: 31 days
- **StorageClass**: local-path (provisioned from node's `/var/lib/rancher/k3s/storage/`, which resides on VM disk backed by TrueNAS iSCSI)
- **Access Mode**: ReadWriteOnce
- **Expected usage**: ~11GB (7 nodes × 500MB/day × 31d)
- **Note**: Worker nodes have 40GB total disk space from iSCSI targets

### Monitoring Storage Usage

```bash
# Check PVC usage
kubectl get pvc -n observability

# Detailed storage metrics in Grafana
# Dashboard: "Kubernetes / Persistent Volumes"
```

### Cleanup Old Data

**Do not run `helm upgrade --set` against this stack.** This whole directory is an
ArgoCD Application, so a direct `helm upgrade` leaves live state diverged from git
and the next sync silently reverts it. Retention is changed by editing
`prometheus-values.yaml` (Prometheus) or `loki-values.yaml` (Loki) and merging, then
letting ArgoCD sync. Read the retention note under `### Prometheus Storage` first -
lowering `retention` without checking `retentionSize` is how the 15d-versus-8d
disagreement in #913 happened.

```bash
# Confirm which retention Prometheus is actually enforcing, not which one is written
kubectl --context epaflix -n observability get prometheus \
  kube-prometheus-stack-prometheus -o jsonpath='{.spec.retention}{"  size="}{.spec.retentionSize}{"\n"}'

# Force a sync after merging a values change
kubectl --context epaflix -n argocd patch application observability \
  --type merge -p '{"operation":{"sync":{"revision":"HEAD"}}}'
```

## Troubleshooting

### Grafana Not Loading

```bash
# Check pods
kubectl get pods -n observability -l app.kubernetes.io/name=grafana

# Check logs
kubectl logs -n observability -l app.kubernetes.io/name=grafana -f

# Check database connection
kubectl exec -n observability deployment/kube-prometheus-stack-grafana -- \
  psql -h postgres-pooler.postgres-system.svc.cluster.local -U observability -d observability -c "SELECT 1;"
```

### Prometheus Not Scraping Targets

```bash
# Port forward to Prometheus UI
kubectl port-forward -n observability svc/kube-prometheus-stack-prometheus 9090:9090

# Open http://localhost:9090/targets
# Check for targets in "DOWN" state

# Check ServiceMonitor
kubectl get servicemonitor -n observability
kubectl describe servicemonitor -n observability <name>
```

### Control-plane exporter Endpoints (issue #147)

The three control-plane exporters — `kube-controller-manager` (:10257),
`kube-scheduler` (:10259) and `kube-etcd` (:2381) — are scraped via
**selector-less headless Services** that the kube-prometheus-stack chart
templates into `kube-system`. Their `endpoints:` lists (the three master
InternalIPs `10.0.0.51/52/53`) live in `prometheus-values.yaml` under the
`kubeControllerManager` / `kubeScheduler` / `kubeEtcd` blocks.

**Mechanism (Option B).** Because those Services have no pod selector,
Kubernetes does not auto-populate their backing endpoints; the chart hand-rolls
a legacy v1 `Endpoints` object (#121). ArgoCD's global `resource.exclusions`
(`2-k3s/11.argocd/helm-values.yaml`) drops core `Endpoints` cluster-wide, so
those chart Endpoints are **out-of-band**: not in any Application's managed set,
not selfHeal-recreatable, and they silently vanish if anything deletes them —
the fragility tracked in **#147**.

The fix declares the backing endpoints as **git-managed static
`discovery.k8s.io/v1` EndpointSlices** in
`control-plane-endpointslices.yaml`, scoped to the observability Application.
`EndpointSlice` is **not** in the ArgoCD exclusion list (proven by the
`jellyfin-truenas` static slice in `08.servarr`), so ArgoCD reconciles them and
selfHeal recreates them. EndpointSlice is also the v1 replacement that is
future-proof against the v1 `Endpoints` deprecation in **k8s 1.33+**.

**Selector-strip truth.** Keeping the `endpoints:` lists in
`prometheus-values.yaml` is what makes the chart render the Services
*selector-less* in the first place — so there is **no recurring manual
selector-strip step**. Leave those lists in place. The chart still renders a
now-redundant excluded v1 `Endpoints` alongside our EndpointSlices; Prometheus
deduplicates the targets, so the overlap is harmless. The
`kubernetes.io/service-name` label on each slice binds it to its headless
Service so the matching ServiceMonitor target resolves to the master IPs.

**DR / verify runbook.**

```bash
# Recreate the slices after any loss (ArgoCD owns them now):
argocd app sync observability

# Confirm the 3 static slices exist with the three master IPs:
kubectl -n kube-system get endpointslice | grep -Ei "controller-manager|scheduler|etcd"
kubectl -n kube-system get endpointslice \
  kube-prometheus-stack-kube-controller-manager-static \
  kube-prometheus-stack-kube-scheduler-static \
  kube-prometheus-stack-kube-etcd-static \
  -o custom-columns=NAME:.metadata.name,IPS:.endpoints[*].addresses,PORT:.ports[*].port

# Confirm Prometheus targets are UP for all three control-plane jobs:
kubectl port-forward -n observability svc/kube-prometheus-stack-prometheus 9090:9090
# Open http://localhost:9090/targets and check that
#   serviceMonitor/observability/kube-prometheus-stack-kube-controller-manager
#   serviceMonitor/observability/kube-prometheus-stack-kube-scheduler
#   serviceMonitor/observability/kube-prometheus-stack-kube-etcd
# each show 3 endpoints (10.0.0.51/52/53) in state UP.
```

### Loki Not Receiving Logs

```bash
# Check Promtail pods
kubectl get pods -n observability -l app.kubernetes.io/name=promtail

# Check Promtail logs
kubectl logs -n observability -l app.kubernetes.io/name=promtail --tail=100

# Test Loki query
kubectl port-forward -n observability svc/loki-gateway 3100:80
curl http://localhost:3100/ready
```

### Cilium Issues

```bash
# Check status
cilium status

# Check agent logs
kubectl logs -n kube-system ds/cilium -c cilium-agent --tail=100

# Restart Cilium agents
kubectl rollout restart ds/cilium -n kube-system

# Check connectivity
cilium connectivity test
```

### Istio Sidecar Not Injecting

```bash
# Verify namespace label
kubectl get namespace app-authentik --show-labels

# Check webhook
kubectl get mutatingwebhookconfiguration istio-sidecar-injector -o yaml

# Manual injection (if automatic fails)
kubectl get deployment -n app-authentik authentik-server -o yaml | istioctl kube-inject -f - | kubectl apply -f -
```

## Maintenance

### Updating Components

```bash
# Update kube-prometheus-stack
helm repo update
helm upgrade kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n observability -f prometheus-values.yaml

# Update Loki
helm upgrade loki grafana/loki -n observability -f loki-values.yaml

# Update Cilium
cilium upgrade --version 1.15.0

# Update Istio
istioctl upgrade
```

### Backup

```bash
# Backup Prometheus data
kubectl exec -n observability prometheus-kube-prometheus-stack-prometheus-0 -c prometheus -- \
  tar czf /prometheus/backup-$(date +%Y%m%d).tar.gz /prometheus/data

# Backup Grafana dashboards (stored in PostgreSQL)
PGPASSWORD='<POSTGRES_PASSWORD>' pg_dump -h 192.168.10.105 -U observability observability > grafana-backup.sql

# Backup Loki data
kubectl exec -n observability loki-backend-0 -- tar czf /tmp/loki-backup.tar.gz /var/loki
```

## Resource Usage

### Expected Cluster Overhead

| Component | CPU | RAM | Count | Total RAM |
|-----------|-----|-----|-------|-----------|
| Prometheus | 500m-2000m | 2-4Gi | 2 | 4-8Gi |
| Grafana | 250m-1000m | 512Mi-1Gi | 2 | 1-2Gi |
| Loki (write/read/backend) | 500m-1000m | 1-2Gi | 6 | 6-12Gi |
| AlertManager | 100m-200m | 128-256Mi | 3 | 384-768Mi |
| node-exporter | 100m-200m | 100-200Mi | 7 | 700Mi-1.4Gi |
| Promtail | 100m-200m | 128-256Mi | 7 | 896Mi-1.8Gi |
| Cilium | 200m-500m | 300-500Mi | 7 | 2.1-3.5Gi |
| Istio sidecars | 100m-500m | 128Mi-512Mi | ~30 | 3.8-15Gi |
| **Total** | - | - | - | **19-45Gi** |

**Cluster Capacity After Upgrades**: 42GB total (6×6GB + 1×8GB)
**Headroom**: Comfortable for all observability components + workloads

## Future Enhancements

### GPU Worker Setup (When GPU Available)

Worker-65 is available for GPU workloads (currently 22GB RAM, same as other workers):

```bash
# When GPU physically installed:
# 1. Shutdown worker-65 VM in Proxmox
# 2. Optionally increase RAM: qm set 1065 --memory 32768  # 32GB if transcoding needs it
# 3. Add PCIe device: qm set 1065 --hostpci0 <GPU_PCI_ID>,pcie=1
# 4. Boot VM
# 5. Install NVIDIA drivers
# 6. Label node: kubectl label node k3s-worker-65 nvidia.com/gpu=present
# 7. Update Jellyfin/Tdarr with GPU resource requests
```

### Additional Monitoring

- **Service Mesh Tracing**: Jaeger or Zipkin integration with Istio
- **Cost Monitoring**: Kubecost for resource cost allocation
- **Security Scanning**: Falco for runtime security
- **Backup Monitoring**: Velero integration with alerts

## Support

For issues or questions:
- Check logs: `kubectl logs -n observability <pod-name>`
- View events: `kubectl get events -n observability --sort-by='.lastTimestamp'`
- Grafana forums: https://community.grafana.com/
- Cilium Slack: https://cilium.io/slack
- Istio discuss: https://discuss.istio.io/
