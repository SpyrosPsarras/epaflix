# Working Grafana Dashboards - Quick Reference

This document lists all verified working dashboards in your Grafana installation.

## Access Information

- **Grafana URL**: https://grafana.epaflix.com
- **Username**: `admin`
- **Password**: `<POSTGRES_PASSWORD>`
- **OAuth**: Authentik SSO (auth.epaflix.com)

## Built-in Dashboards (kube-prometheus-stack)

These dashboards are pre-installed and fully working with your Prometheus datasource.

### 🌟 Recommended Primary Dashboards

#### 1. Kubernetes / Compute Resources / Cluster
**Best overall cluster resource view**
- **URL**: https://grafana.epaflix.com/d/efa86fd1d0c121a26444b636a3f509a8
- **Shows**: CPU, Memory, Network usage across entire cluster
- **Use for**: Daily cluster monitoring, capacity planning

#### 2. Node Exporter / Nodes
**Detailed system metrics per node**
- **URL**: https://grafana.epaflix.com/d/7d57716318ee0dddbac5a7f451fb7753
- **Shows**: CPU, Memory, Disk, Network per node
- **Use for**: Node health monitoring, disk space tracking

#### 3. Kubernetes / Compute Resources / Namespace (Workloads)
**Resource usage per namespace**
- **URL**: https://grafana.epaflix.com/d/a87fb0d919ec0ea5f6543124e16c42a5
- **Shows**: CPU/Memory by namespace and workload
- **Use for**: Identifying resource-hungry applications

### 📊 Cluster Health & Performance

#### Kubernetes / API server
- **URL**: https://grafana.epaflix.com/d/09ec8aa1e996d6ffcd6817bbaff4db1b
- **Shows**: API server latency, request rates, errors
- **Use for**: Control plane health monitoring

#### Kubernetes / Kubelet
- **URL**: https://grafana.epaflix.com/d/3138fa155d5915769fbded898ac09fd9
- **Shows**: Kubelet metrics, runtime operations
- **Use for**: Node-level Kubernetes component health

#### Kubernetes / Scheduler
- **URL**: https://grafana.epaflix.com/d/2e6b6a3b4bddf1427b3a55aa1311c656
- **Shows**: Scheduling latency, pod queue depth
- **Use for**: Troubleshooting pod scheduling issues

#### Kubernetes / Controller Manager
- **URL**: https://grafana.epaflix.com/d/72e0e05bef5099e5f049b05fdc429ed4
- **Shows**: Controller work queue, reconciliation rates
- **Use for**: Control plane component monitoring

#### etcd
- **URL**: https://grafana.epaflix.com/d/c2f4e12cdf69feb95caa41a5a1b423d9
- **Shows**: etcd performance, disk sync duration
- **Use for**: Database health (critical for cluster stability)

#### CoreDNS
- **URL**: https://grafana.epaflix.com/d/vkQ0UHxik
- **Shows**: DNS request rates, cache hits, errors
- **Use for**: DNS troubleshooting

### 🌐 Network Monitoring

#### Kubernetes / Networking / Cluster
- **URL**: https://grafana.epaflix.com/d/ff635a025bcfea7bc3dd4f508990a3e9
- **Shows**: Network bandwidth, packet rates across cluster
- **Use for**: Network capacity planning

#### Kubernetes / Networking / Namespace (Pods)
- **URL**: https://grafana.epaflix.com/d/8b7a8b326d7a6f1f04244066368c67af
- **Shows**: Network traffic per namespace/pod
- **Use for**: Identifying bandwidth-heavy applications

#### Kubernetes / Networking / Pod
- **URL**: https://grafana.epaflix.com/d/7a18067ce943a40ae25454675c19ff5c
- **Shows**: Detailed network metrics for specific pod
- **Use for**: Deep-dive pod network troubleshooting

#### Cilium v1.12 Agent Metrics
- **URL**: https://grafana.epaflix.com/d/vtuWtdumz
- **Shows**: Cilium CNI metrics, policy enforcement
- **Use for**: CNI health and network policy debugging

### 📦 Resource Management

#### Kubernetes / Compute Resources / Node (Pods)
- **URL**: https://grafana.epaflix.com/d/200ac8fdbfbb74b39aff88118e4d1c2c
- **Shows**: Pod distribution and resource usage per node
- **Use for**: Node balancing, resource allocation

#### Kubernetes / Compute Resources / Pod
- **URL**: https://grafana.epaflix.com/d/6581e46e4e5c7ba40a07646395ef7b23
- **Shows**: Single pod resource usage over time
- **Use for**: Pod performance analysis

#### Kubernetes / Compute Resources / Workload
- **URL**: https://grafana.epaflix.com/d/a164a7f0339f99e89cea5cb47e9be617
- **Shows**: Deployment/StatefulSet/DaemonSet resources
- **Use for**: Workload optimization

#### Kubernetes / Persistent Volumes
- **URL**: https://grafana.epaflix.com/d/919b92a8e8041bd567af9edab12c840c
- **Shows**: PV/PVC usage, capacity, status
- **Use for**: Storage monitoring

### 🔧 Service Mesh (Istio)

#### Istio Control Plane Dashboard
- **URL**: https://grafana.epaflix.com/d/1813f692a8e4ac77155348d4c7d2fba8
- **Shows**: Istio control plane health, configuration
- **Use for**: Istio operator monitoring

#### Istio Service Dashboard
- **URL**: https://grafana.epaflix.com/d/502f696a-c627-483f-806e-444e7b9a4657
- **Shows**: Service mesh traffic, latency, errors
- **Use for**: Service-to-service communication monitoring

### 📈 Observability Stack

#### Prometheus
- **URL**: https://grafana.epaflix.com/d/
- **Shows**: Prometheus metrics, scrape duration, storage
- **Use for**: Monitoring the monitoring system

#### Alertmanager / Overview
- **URL**: https://grafana.epaflix.com/d/alertmanager-overview
- **Shows**: Alert status, notification delivery
- **Use for**: Alert management

#### Grafana Overview
- **URL**: https://grafana.epaflix.com/d/6be0s85Mk
- **Shows**: Grafana instance metrics, user sessions
- **Use for**: Grafana performance monitoring

### 💾 Node-Level Metrics

#### Node Exporter / USE Method / Cluster
- **URL**: https://grafana.epaflix.com/d/3e97d1d02672cdd0861f4c97c64f89b2
- **Shows**: Utilization, Saturation, Errors methodology
- **Use for**: Systematic performance analysis

#### Node Exporter / MacOS (if applicable)
- **URL**: https://grafana.epaflix.com/d/629701ea43bf69291922ea45f4a87d37

#### Node Exporter / AIX (if applicable)
- **URL**: https://grafana.epaflix.com/d/7e0a61e486f727d763fb1d86fdd629c2

### 🖥️ Infrastructure (Proxmox)

#### Proxmox VE Cluster
- **Dashboard ID**: 10347 (import manually)
- **Status**: Requires manual import from grafana.com
- **Note**: Wait 2-3 minutes after import for initial scrape from pve-exporter

## Importing Additional Dashboards

### From Grafana.com (Recommended External Dashboards)

1. Navigate to: https://grafana.epaflix.com/dashboard/import
2. Enter dashboard ID
3. Select datasource: **Prometheus**
4. Click "Import"

#### Recommended External Dashboard IDs:

- **1860** - Node Exporter Full ⭐
  - Most popular node exporter dashboard
  - 900k+ downloads, actively maintained
  - Excellent for detailed system metrics

- **10347** - Proxmox VE Cluster ⭐
  - Monitor Proxmox hypervisor hosts
  - Works with pve-exporter (already deployed)

- **315** - Kubernetes Cluster Monitoring
  - Basic cluster overview
  - Note: Built-in dashboards are better/newer

## Dashboard Organization

### Folder Structure

```
General/
├── Most built-in dashboards
├── Alertmanager, CoreDNS, etcd
└── All Kubernetes/* dashboards

Kubernetes Cluster/
└── (Custom imported dashboards)

Network & Service Mesh/
├── Cilium v1.12 Agent Metrics
├── Istio Control Plane Dashboard
└── Istio Service Dashboard

Infrastructure/
└── (For Proxmox, hardware dashboards)

Logs/
└── (For Loki log dashboards)
```

## Quick Access Favorites

Set these as pinned/starred dashboards for quick access:

1. Kubernetes / Compute Resources / Cluster (overall health)
2. Node Exporter / Nodes (system metrics)
3. Kubernetes / Networking / Cluster (network health)
4. Proxmox VE Cluster (hypervisor monitoring)
5. Alertmanager / Overview (alert status)

## Datasources

### Prometheus
- **Name**: Prometheus
- **Type**: prometheus
- **URL**: http://kube-prometheus-stack-prometheus.observability.svc.cluster.local:9090
- **Status**: ✅ Working
- **UID**: PBFA97CFB590B2093

### Loki
- **Name**: Loki
- **Type**: loki
- **URL**: http://loki-gateway.observability.svc.cluster.local
- **Status**: ✅ Working
- **UID**: P8E80F9AEF21F6940

## Troubleshooting

### Dashboard shows "No data"

1. Check time range (top-right corner)
2. Verify datasource is selected (edit panel)
3. Check if Prometheus has metrics:
   ```bash
   kubectl port-forward -n observability svc/kube-prometheus-stack-prometheus 9090:9090
   # Visit http://localhost:9090/targets
   ```

### Dashboard panels are empty

- Wait a few minutes for initial metric collection
- Some panels require specific labels (check panel query)
- Verify ServiceMonitors exist: `kubectl get servicemonitors -A`

### Cannot find a dashboard

```bash
# List all dashboards via CLI
kubectl exec -n observability deployment/kube-prometheus-stack-grafana -c grafana -- \
  curl -s -u admin:'<POSTGRES_PASSWORD>' 'http://localhost:3000/api/search?type=dash-db' | \
  python3 -m json.tool
```

## Related Documentation

- [Main Observability README](./README.md)
- [Dashboard Troubleshooting](./DASHBOARD-TROUBLESHOOTING.md)
- [Grafana OAuth Configuration](./GRAFANA-OAUTH-TROUBLESHOOTING.md)

## Performance Metrics

Expected resource usage for monitoring stack:

- **Prometheus**: 500m CPU, 2-4GB RAM
- **Grafana**: 250m CPU, 512MB RAM (×2 replicas)
- **Node Exporter**: 100m CPU, 100MB RAM per node
- **kube-state-metrics**: 100m CPU, 256MB RAM (×2 replicas)
- **Loki**: 1 CPU, 2GB RAM
- **Promtail**: 100m CPU, 128MB RAM per node

Total: ~5GB RAM, ~3 CPU cores for full observability stack

## Notes

- All dashboards use the same Prometheus datasource
- Built-in dashboards are read-only (provisioned by Helm)
- Custom/imported dashboards can be edited and saved
- Dashboard UIDs are stable across restarts
- Bookmarking dashboard URLs is recommended for quick access

## Last Updated

This document reflects the state of Grafana dashboards as of the kube-prometheus-stack deployment.