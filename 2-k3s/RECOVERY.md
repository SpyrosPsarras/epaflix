# K3s Cluster Recovery Guide

**Status:** ARCHIVED — Recovery completed February 2026
**Date:** February 15, 2026
**Previous Cluster Backup:** February 5, 2026 16:40 UTC

> **Note:** This document is kept for reference. The cluster has been fully recovered and is operational.

---

## Table of Contents

1. [Current Status](#current-status)
2. [Backup Inventory](#backup-inventory)
3. [Recovery Prerequisites](#recovery-prerequisites)
4. [Recovery Steps](#recovery-steps)
5. [Verification](#verification)
6. [Known Issues](#known-issues)

---

## Current Status

- ✅ **K3s Cluster:** Up and running (fresh installation)
- ✅ **Manifests:** Available in numbered folders (01-10)
- ✅ **Backups Located:** Database dumps and application configs identified on TrueNAS
- ⏳ **Recovery:** In progress

---

## Backup Inventory

### 1. PostgreSQL Database Backups
**Location:** `truenas_admin@192.168.10.200:/mnt/pool1/dataset01/VMs/backup/postgres-20260205-154025/`

```
authentik-20260205-154025.sql          (106MB) + MD5
sonarr-main-20260205-154025.sql        (57MB)  + MD5
sonarr2-main-20260205-154025.sql       (45MB)  + MD5
radarr-main-20260205-154025.sql        (9.3MB) + MD5
prowlarr-main-20260205-154025.sql      (7.1MB) + MD5
jellyseerr-20260205-154025.sql         (144KB) + MD5
observability-20260205-154025.sql      (2.4MB) + MD5
postgres-full-cluster-20260205-154025.sql (242MB) + MD5
backup-manifest.txt
```

### 2. Application Configurations & Data
**Location:** `truenas_admin@192.168.10.200:/home/truenas_admin/backup/k3s-containers/containers/`

| Application | Backed Up Data | Priority |
|------------|----------------|----------|
| **traefik** | `acme.json` (SSL certificates) | HIGH |
| **authentik** | Application icons, media files | HIGH |
| **observability-loki** | WAL, chunks, TSDB (full Loki data) | MEDIUM |
| **observability-prometheus** | Time-series data | MEDIUM |
| **prowlarr-config** | 700+ indexer definitions (.yml) | HIGH |
| **radarr-config** | Application config | MEDIUM |
| **sonarr-config** | Application config | MEDIUM |
| **sonarr2-config** | Application config | MEDIUM |
| **bazarr-config** | `config.yaml` and settings | MEDIUM |
| **jellyfin-config** | Media library metadata | HIGH |
| **jellyseerr-config** | Request history | LOW |
| **qbittorrent-config** | Torrent state | LOW |
| **homarr-config** | Dashboard settings | LOW |
| **flaresolverr-config** | CloudFlare bypass settings | LOW |

### 3. Proxmox VM Backups
**Location:** `truenas_admin@192.168.10.200:/mnt/pool1/dataset01/VMs/backup/20260204-090744/`
- 6 VM snapshots in `.vma.zst` format (~30GB total)

---

## Recovery Prerequisites

### Required Access
- [ ] SSH access to k3s master node(s)
- [ ] SSH access to TrueNAS at `192.168.10.200` as `truenas_admin`
- [ ] kubectl configured and connected to the new cluster
- [ ] Helm 3 installed

### Verify Cluster Is Ready
```bash
kubectl get nodes
kubectl get ns
kubectl version --short
```

### Install Required Tools
```bash
# Helm (if not installed)
curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

# Verify Helm
helm version
```

---

## Recovery Steps

Follow these steps **in order**. Each numbered folder corresponds to a deployment stage.

### Stage 0: Namespace Creation
```bash
cd /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s

# Create all required namespaces
kubectl apply -f 03.kube-vip-cloud-provider/namespace.yaml  # if exists
kubectl apply -f 05.traefik-deployment/namespace.yaml
kubectl apply -f 06.postgres/namespace.yaml
kubectl apply -f 07.authentik-deployment/namespace.yaml
kubectl apply -f 08.servarr/namespace.yaml
kubectl apply -f 09.metrics/namespace.yaml
kubectl apply -f 10.observability/namespace.yaml
```

---

### Stage 1: Kube-VIP (Load Balancer)
**Directory:** `01.kube-vip/`

```bash
cd 01.kube-vip
kubectl apply -f kube-vip-daemonset.yaml

# Verify
kubectl get pods -n kube-system | grep kube-vip
```

**Documentation:** See `01.kube-vip/README.md`

---

### Stage 2: Kube-VIP Cloud Provider
**Directory:** `03.kube-vip-cloud-provider/`

```bash
cd ../03.kube-vip-cloud-provider

# Install the cloud provider
./install.sh

# Apply IP pool configuration
kubectl apply -f ip-pool-configmap.yaml

# Verify
kubectl get cm -n kube-system | grep kubevip
kubectl logs -n kube-system -l app=kube-vip-cloud-provider
```

**Documentation:** See `03.kube-vip-cloud-provider/README.md`

---

### Stage 3: CoreDNS (Optional Customization)
**Directory:** `04.coredns/`

```bash
cd ../04.coredns

# Apply custom CoreDNS config (if needed)
kubectl apply -f coredns-custom.yaml

# Verify
kubectl get cm -n kube-system coredns -o yaml
```

---

### Stage 4: Traefik (Ingress Controller)
**Directory:** `05.traefik-deployment/`

```bash
cd ../05.traefik-deployment

# Install Helm (if needed)
./00.get_helm.sh

# Deploy Traefik
./01.deploy.sh

# Verify deployment
kubectl get pods -n traefik-system
kubectl get svc -n traefik-system

# Check Traefik dashboard
kubectl get ingress -n traefik-system
```

#### Restore Traefik SSL Certificates
```bash
# Copy acme.json from backup
scp truenas_admin@192.168.10.200:/home/truenas_admin/backup/k3s-containers/containers/traefik/acme.json ./acme.json

# Create secret from acme.json
kubectl create secret generic traefik-acme \
  --from-file=acme.json=./acme.json \
  -n traefik-system \
  --dry-run=client -o yaml | kubectl apply -f -

# Restart Traefik to pick up certificates
kubectl rollout restart deployment traefik -n traefik-system
```

**Documentation:** See `05.traefik-deployment/README.md`

---

### Stage 5: PostgreSQL (Database)
**Directory:** `06.postgres/`

```bash
cd ../06.postgres

# Install CloudNative-PG operator
./01.install-operator.sh

# Verify operator is running
kubectl get pods -n cnpg-system

# Deploy PostgreSQL cluster
./02.deploy-cluster.sh

# Wait for cluster to be ready (this may take 5-10 minutes)
kubectl get cluster -n postgres -w

# Verify all pods are running
kubectl get pods -n postgres
```

#### Restore PostgreSQL Databases

**Option A: Using PG Restore Job (Recommended)**
```bash
cd backup

# Edit restore-all-databases.sh with correct backup path on TrueNAS
# Update: BACKUP_PATH=/mnt/pool1/dataset01/VMs/backup/postgres-20260205-154025

# Run restore
./restore-all-databases.sh
```

**Option B: Manual Restore**
```bash
# Copy database dumps from TrueNAS
scp truenas_admin@192.168.10.200:/mnt/pool1/dataset01/VMs/backup/postgres-20260205-154025/*.sql ./

# Get postgres pod name
POSTGRES_POD=$(kubectl get pod -n postgres -l role=primary -o jsonpath='{.items[0].metadata.name}')

# Restore each database
for db in authentik sonarr-main sonarr2-main radarr-main prowlarr-main jellyseerr observability; do
  echo "Restoring $db..."
  kubectl exec -n postgres -i $POSTGRES_POD -- psql -U postgres -d $db < ${db}-20260205-154025.sql
done
```

**Verification:**
```bash
# Connect to postgres and verify databases
kubectl exec -n postgres -it $POSTGRES_POD -- psql -U postgres

# In psql:
\l
\c authentik
\dt
\q
```

**Documentation:** See `06.postgres/README.md`

---

### Stage 6: Authentik (SSO/Authentication)
**Directory:** `07.authentik-deployment/`

```bash
cd ../07.authentik-deployment

# Deploy Authentik using Helm
./deploy.sh

# Wait for pods to be ready
kubectl get pods -n authentik -w

# Verify ingress
kubectl get ingress -n authentik
```

#### Restore Authentik Media/Icons
```bash
# Copy authentik media files from backup
scp -r truenas_admin@192.168.10.200:/home/truenas_admin/backup/k3s-containers/containers/authentik/public ./authentik-media

# Copy to authentik pod (adjust pod name)
AUTHENTIK_POD=$(kubectl get pod -n authentik -l app.kubernetes.io/name=authentik -o jsonpath='{.items[0].metadata.name}')
kubectl cp ./authentik-media/public $AUTHENTIK_POD:/media -n authentik
```

**Verification:**
```bash
# Check Authentik is accessible
curl -k https://authentik.epaflix.com

# Check database connection
kubectl logs -n authentik -l app.kubernetes.io/name=authentik | grep -i "database"
```

**Documentation:** See `07.authentik-deployment/README.md`

---

### Stage 7: Servarr Stack (Media Management)
**Directory:** `08.servarr/`

```bash
cd ../08.servarr

# Apply namespace
kubectl apply -f namespace.yaml

# Setup PostgreSQL databases for Servarr apps
kubectl apply -f postgres-setup-job.yaml
kubectl wait --for=condition=complete job/postgres-setup -n servarr --timeout=300s

# Deploy each application in order:

# 1. Prowlarr (Indexer Manager)
cd prowlarr
kubectl apply -f .
kubectl wait --for=condition=ready pod -l app=prowlarr -n servarr --timeout=300s
cd ..

# 2. Radarr (Movies)
cd radarr
kubectl apply -f .
kubectl wait --for=condition=ready pod -l app=radarr -n servarr --timeout=300s
cd ..

# 3. Sonarr (TV Shows)
cd sonarr
kubectl apply -f .
kubectl wait --for=condition=ready pod -l app=sonarr -n servarr --timeout=300s
cd ..

# 4. Sonarr2 (Second instance)
cd sonarr2
kubectl apply -f .
cd ..

# 5. Bazarr (Subtitles)
cd bazarr
kubectl apply -f .
cd ..

# 6. QBittorrent (Downloads)
cd qbittorrent
kubectl apply -f .
cd ..

# 7. Jellyfin (Media Server)
cd jellyfin
kubectl apply -f .
cd ..

# 8. Jellyseerr (Requests)
cd jellyseerr
kubectl apply -f .
cd ..

# 9. FlareSolverr (Cloudflare Bypass)
cd flaresolverr
kubectl apply -f .
cd ..

# 10. Homarr (Dashboard)
cd homarr
kubectl apply -f .
cd ..

# Verify all pods
kubectl get pods -n servarr
kubectl get ingress -n servarr
```

#### Restore Servarr Configurations
```bash
# Prowlarr indexer definitions (700+ files)
# These are stored in ConfigMaps or as part of the database backup
# Already restored via PostgreSQL restore

# For application-specific configs:
# Copy from TrueNAS backup as needed
# Example for bazarr:
scp truenas_admin@192.168.10.200:/home/truenas_admin/backup/k3s-containers/containers/bazarr-config/config.yaml ./bazarr-config.yaml

# Apply as ConfigMap or copy to pod
```

**Documentation:** See `08.servarr/README.md` and `08.servarr/QUICKSTART.md`

---

### Stage 8: FileBrowser
**Directory:** `09.filebrowser/`

```bash
cd ../09.filebrowser
kubectl apply -f .
```

**Documentation:** See `09.filebrowser/README.md`

---

### Stage 9: Observability (Prometheus, Loki, Grafana)
**Directory:** `10.observability/`

```bash
cd ../10.observability

# Apply namespace
kubectl apply -f namespace.yaml

# Setup storage
kubectl apply -f storage/

# Setup PostgreSQL for Grafana
kubectl apply -f postgres-setup-job.yaml
kubectl wait --for=condition=complete job/grafana-postgres-setup -n observability --timeout=300s

# Apply secrets
kubectl apply -f grafana-db-secret.yaml

# Deploy using Helm
./deploy.sh

# Verify all components
kubectl get pods -n observability
kubectl get ingress -n observability
```

#### Restore Loki Data (Optional - Historical Logs)
```bash
# Warning: This is a large dataset and may not be necessary
# Only restore if you need historical logs

# Copy Loki data from backup
# This will be a very large rsync operation
rsync -avz --progress \
  truenas_admin@192.168.10.200:/home/truenas_admin/backup/k3s-containers/containers/observability-loki/ \
  ./loki-data-backup/

# Copy to Loki pod PVC
# (This is complex and may require PVC mounting - consult Loki docs)
```

**Documentation:** See `10.observability/README.md`

---

## Verification

### Cluster Health
```bash
# Check all nodes
kubectl get nodes -o wide

# Check all namespaces
kubectl get ns

# Check all pods
kubectl get pods -A

# Check ingresses
kubectl get ingress -A

# Check services with LoadBalancer IPs
kubectl get svc -A | grep LoadBalancer
```

### Application Health Checks

#### Traefik
```bash
kubectl get pods -n traefik-system
curl -I https://traefik.epaflix.com
```

#### Authentik
```bash
kubectl get pods -n authentik
curl -I https://auth.epaflix.com
```

#### PostgreSQL
```bash
kubectl get cluster -n postgres
kubectl exec -n postgres -it postgres-cluster-1 -- psql -U postgres -c "\l"
```

#### Servarr Stack
```bash
# Check each application
for app in prowlarr radarr sonarr sonarr2 bazarr qbittorrent jellyfin jellyseerr; do
  echo "=== $app ==="
  kubectl get pods -n servarr -l app=$app
done

# Check ingresses
kubectl get ingress -n servarr
```

#### Observability
```bash
kubectl get pods -n observability
# Check Grafana
curl -I https://grafana.epaflix.com
# Check Prometheus
curl -I https://prometheus.epaflix.com
```

---

## Known Issues

### Issue Tracking
- [ ] TODO: List any issues encountered during recovery
- [ ] TODO: Document workarounds

### Common Problems

#### 1. Pods Stuck in Pending
```bash
# Check node resources
kubectl describe nodes

# Check PVC status
kubectl get pvc -A

# Check events
kubectl get events -A --sort-by='.lastTimestamp'
```

#### 2. Database Connection Failures
```bash
# Check PostgreSQL cluster status
kubectl get cluster -n postgres

# Check database logs
kubectl logs -n postgres postgres-cluster-1

# Verify network connectivity
kubectl exec -n postgres -it postgres-cluster-1 -- pg_isready
```

#### 3. Ingress Not Working
```bash
# Check Traefik status
kubectl get pods -n traefik-system
kubectl logs -n traefik-system -l app.kubernetes.io/name=traefik

# Check IngressRoute
kubectl get ingressroute -A

# Verify DNS resolution
nslookup epaflix.com
```

#### 4. SSL Certificate Issues
```bash
# Check cert-manager (if used)
kubectl get certificates -A

# Check Traefik ACME storage
kubectl get secret traefik-acme -n traefik-system

# Force cert renewal
kubectl delete certificate <cert-name> -n <namespace>
```

---

## Next Steps After Recovery

1. **Verify All Applications:** Test each service endpoint
2. **Update DNS Records:** Ensure all domains point to correct LoadBalancer IPs
3. **Test Authentication:** Verify Authentik SSO flows
4. **Monitor Logs:** Check for any errors in observability stack
5. **Schedule Backups:** Set up automated backup jobs
6. **Document Changes:** Note any deviations from original setup
7. **Archive This File:** Keep for reference

---

## Backup Rotation Scripts

After recovery is complete, set up automated backups:

### PostgreSQL Backup Cron
```bash
# Edit 06.postgres/backup/backup-cronjob.yaml
kubectl apply -f 06.postgres/backup/backup-cronjob.yaml
```

### Configuration Backup
```bash
# Set up automated backup to TrueNAS
# (See maintenance/ directory for scripts)
```

---

## Emergency Contacts & Resources

- **K3s Documentation:** https://docs.k3s.io/
- **CloudNative-PG Docs:** https://cloudnative-pg.io/
- **Traefik Docs:** https://doc.traefik.io/traefik/
- **Authentik Docs:** https://docs.goauthentik.io/

---

## Recovery Log

| Date | Step | Status | Notes |
|------|------|--------|-------|
| 2026-02-15 | Backup inventory | ✅ Complete | All backups located on TrueNAS |
| 2026-02-15 | Recovery plan created | ✅ Complete | This document |
| | Kube-VIP | ⏳ Pending | |
| | Traefik | ⏳ Pending | |
| | PostgreSQL | ⏳ Pending | |
| | Database restore | ⏳ Pending | |
| | Authentik | ⏳ Pending | |
| | Servarr stack | ⏳ Pending | |
| | Observability | ⏳ Pending | |
| | Verification | ⏳ Pending | |

---

**Last Updated:** 2026-02-15
**Recovery Status:** Completed
