# Grafana Dashboard Troubleshooting Guide

## Issue: Empty Dashboard - "Kubernetes cluster monitoring (via Prometheus)"

### Problem Description

The dashboard "Kubernetes cluster monitoring (via Prometheus)" appears empty with no panels visible, even though Prometheus is collecting metrics correctly.

### Root Cause

The dashboard was imported incorrectly or is corrupted, resulting in a dashboard with 0 panels. This commonly happens when:
- Dashboard ID 315 from grafana.com fails to import properly
- The dashboard JSON is incompatible with the current Grafana version
- Datasource variables are not resolved during import

### Verification Steps

1. **Check if Prometheus is collecting data:**
```bash
# Port forward to Prometheus
kubectl port-forward -n observability svc/kube-prometheus-stack-prometheus 9090:9090

# Open http://localhost:9090/targets and verify targets are UP
# Or query from command line:
kubectl exec -n observability prometheus-kube-prometheus-stack-prometheus-0 -c prometheus -- \
  wget -qO- 'http://localhost:9090/api/v1/query?query=up' | python3 -m json.tool
```

2. **Check Grafana datasource configuration:**
```bash
kubectl exec -n observability deployment/kube-prometheus-stack-grafana -c grafana -- \
  curl -s -u admin:'<POSTGRES_PASSWORD>' http://localhost:3000/api/datasources | python3 -m json.tool
```

3. **List all dashboards and check panel counts:**
```bash
# List all dashboards
kubectl exec -n observability deployment/kube-prometheus-stack-grafana -c grafana -- \
  curl -s -u admin:'<POSTGRES_PASSWORD>' 'http://localhost:3000/api/search?type=dash-db'

# Check specific dashboard panel count
kubectl exec -n observability deployment/kube-prometheus-stack-grafana -c grafana -- \
  curl -s -u admin:'<POSTGRES_PASSWORD>' 'http://localhost:3000/api/dashboards/uid/<DASHBOARD_UID>' | \
  python3 -c "import sys, json; data=json.load(sys.stdin); print('Panels:', len(data['dashboard'].get('panels', [])))"
```

## Solution

### Option 1: Use Built-in kube-prometheus-stack Dashboards (Recommended)

The kube-prometheus-stack Helm chart includes excellent pre-configured dashboards that are already working. Use these instead of importing external dashboards:

#### Best Kubernetes Monitoring Dashboards:

1. **Kubernetes / Compute Resources / Cluster** ⭐ (Best overall cluster view)
   - URL: https://grafana.epaflix.com/d/efa86fd1d0c121a26444b636a3f509a8
   - Shows: CPU, Memory, Network usage across the entire cluster

2. **Kubernetes / Compute Resources / Namespace (Workloads)**
   - URL: https://grafana.epaflix.com/d/a87fb0d919ec0ea5f6543124e16c42a5
   - Shows: Resource usage per namespace

3. **Kubernetes / Compute Resources / Node (Pods)**
   - URL: https://grafana.epaflix.com/d/200ac8fdbfbb74b39aff88118e4d1c2c
   - Shows: Pod distribution and resource usage per node

4. **Node Exporter / Nodes** (Detailed system metrics)
   - URL: https://grafana.epaflix.com/d/7d57716318ee0dddbac5a7f451fb7753
   - Shows: CPU, memory, disk, network per node

5. **Kubernetes / Networking / Cluster**
   - URL: https://grafana.epaflix.com/d/ff635a025bcfea7bc3dd4f508990a3e9
   - Shows: Network bandwidth, packet rates

6. **Kubernetes / API server**
   - URL: https://grafana.epaflix.com/d/09ec8aa1e996d6ffcd6817bbaff4db1b
   - Shows: API server performance and health

7. **Kubernetes / Kubelet**
   - URL: https://grafana.epaflix.com/d/3138fa155d5915769fbded898ac09fd9
   - Shows: Kubelet metrics per node

8. **Kubernetes / Persistent Volumes**
   - URL: https://grafana.epaflix.com/d/919b92a8e8041bd567af9edab12c840c
   - Shows: PV/PVC usage and status

#### Complete Built-in Dashboard List:

```bash
# Get all built-in dashboards with UIDs
kubectl exec -n observability deployment/kube-prometheus-stack-grafana -c grafana -- \
  curl -s -u admin:'<POSTGRES_PASSWORD>' 'http://localhost:3000/api/search?type=dash-db' | \
  python3 -c "import sys, json; data=json.load(sys.stdin); [print(f\"{d['title']}\n  URL: https://grafana.epaflix.com{d['url']}\n\") for d in sorted(data, key=lambda x: x['title'])]"
```

### Option 2: Delete Empty Dashboard

If you have a corrupted/empty dashboard, delete it:

```bash
# Delete by UID (replace with actual UID from dashboard URL)
kubectl exec -n observability deployment/kube-prometheus-stack-grafana -c grafana -- \
  curl -s -X DELETE -u admin:'<POSTGRES_PASSWORD>' \
  'http://localhost:3000/api/dashboards/uid/<DASHBOARD_UID>'
```

### Option 3: Import External Dashboards via UI (Manual Import)

For external dashboards from grafana.com, use the Grafana UI:

1. Navigate to: https://grafana.epaflix.com/dashboard/import
2. Enter dashboard ID:
   - **1860** - Node Exporter Full (most popular, well-maintained)
   - **315** - Kubernetes Cluster Overview (older, may have issues)
   - **10347** - Proxmox VE Cluster (for Proxmox monitoring)
3. Click "Load"
4. Configure:
   - Select datasource: **Prometheus**
   - Choose folder: **Kubernetes Cluster** (or General)
5. Click "Import"

#### Recommended External Dashboards:

- **1860** - Node Exporter Full ⭐
  - Best for detailed node-level metrics
  - Very popular (900k+ downloads)
  - Actively maintained

- **315** - Kubernetes Cluster Monitoring
  - Basic cluster overview
  - May be outdated for modern Grafana versions
  - Built-in dashboards are better

- **10347** - Proxmox VE Cluster ⭐
  - Excellent for monitoring Proxmox hosts
  - Requires pve-exporter (already deployed)
  - Wait 2-3 minutes after import for initial scrape

## Prevention

### Best Practices for Dashboard Management:

1. **Prefer built-in dashboards**: kube-prometheus-stack includes excellent dashboards
2. **Test imports**: After importing, verify panels show data
3. **Use folders**: Organize dashboards into folders for easy management
4. **Document UIDs**: Keep track of working dashboard UIDs
5. **Version control**: Export working dashboards as JSON and commit to git

### Export Working Dashboard for Backup:

```bash
# Export dashboard to file
kubectl exec -n observability deployment/kube-prometheus-stack-grafana -c grafana -- \
  curl -s -u admin:'<POSTGRES_PASSWORD>' \
  'http://localhost:3000/api/dashboards/uid/<DASHBOARD_UID>' | \
  python3 -m json.tool > dashboard-backup.json
```

## Common Issues

### Issue: Dashboard shows "No data"

**Cause**: Datasource not selected or query syntax incorrect

**Fix**:
1. Edit panel
2. Select "Prometheus" as datasource
3. Verify PromQL query syntax

### Issue: Dashboard import fails with "bad request data"

**Cause**: Dashboard JSON incompatible with Grafana version

**Fix**:
1. Use manual UI import instead of API
2. Try a more recent dashboard revision
3. Use built-in dashboards instead

### Issue: Variables not working (e.g., $__all)

**Cause**: Variable not defined in dashboard

**Fix**:
1. Go to Dashboard Settings → Variables
2. Add missing variables:
   - Name: `Node`
   - Type: `Query`
   - Query: `label_values(node_uname_info, nodename)`
   - Multi-value: enabled
   - Include All option: enabled

### Issue: Provisioned dashboard cannot be deleted

**Cause**: Dashboard is managed by Helm/provisioning

**Fix**: This is expected behavior - provisioned dashboards are read-only. If you need to modify:
1. Disable provisioning in `prometheus-values.yaml`
2. Helm upgrade the stack
3. Dashboard will become editable/deletable

## Quick Reference

### Get Dashboard UID from URL
Dashboard URL format: `https://grafana.epaflix.com/d/<UID>/dashboard-name`

Example: `https://grafana.epaflix.com/d/efa86fd1d0c121a26444b636a3f509a8/kubernetes-compute-resources-cluster`
- UID: `efa86fd1d0c121a26444b636a3f509a8`

### Grafana API Authentication
```bash
# Username: admin
# Password: <POSTGRES_PASSWORD> (from prometheus-values.yaml)

# Test authentication
kubectl exec -n observability deployment/kube-prometheus-stack-grafana -c grafana -- \
  curl -s -u admin:'<POSTGRES_PASSWORD>' http://localhost:3000/api/org
```

### Useful Grafana API Endpoints
```bash
# List all dashboards
/api/search?type=dash-db

# Get dashboard by UID
/api/dashboards/uid/<UID>

# Delete dashboard by UID
DELETE /api/dashboards/uid/<UID>

# List datasources
/api/datasources

# Test datasource connection
/api/datasources/proxy/<ID>/api/v1/query?query=up
```

## Related Documentation

- [Grafana HTTP API Documentation](https://grafana.com/docs/grafana/latest/developers/http_api/)
- [kube-prometheus-stack Dashboards](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack/templates/grafana/dashboards-1.14)
- [Grafana Dashboard Best Practices](https://grafana.com/docs/grafana/latest/dashboards/build-dashboards/best-practices/)

## Support

If issues persist:
1. Check Grafana logs: `kubectl logs -n observability -l app.kubernetes.io/name=grafana -f`
2. Check Prometheus logs: `kubectl logs -n observability prometheus-kube-prometheus-stack-prometheus-0 -c prometheus`
3. Verify network connectivity between Grafana and Prometheus pods
4. Ensure ServiceMonitors are being discovered: `kubectl get servicemonitors -A`
