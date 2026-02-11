# PostgreSQL HA Cluster with CloudNativePG

This directory contains manifests and scripts for deploying a highly available PostgreSQL 16 cluster on k3s using the CloudNativePG operator.

## Architecture

- **PostgreSQL Version**: 16 (latest)
- **High Availability**: 3 instances (1 primary + 2 hot standby replicas)
- **Storage Strategy**:
  - PostgreSQL data: `local-path` storage class (20Gi per instance, RWO)
  - Backups & WAL archives: `local-path` storage class
- **Connection Pooling**: PgBouncer (3 replicas, transaction mode)
- **Backup Schedule**: Daily at 2:00 AM UTC (base backups to PVC)
- **Replication**: Streaming replication with automatic failover
- **Monitoring**: Manual PodMonitor creation (enablePodMonitor deprecated in v1.28)

## Network Exposure

All services exposed via kube-vip LoadBalancer:

| Service | IP | Purpose |
|---------|-----|---------|
| `postgres-rw` | 192.168.10.105 | Primary instance (read-write) |
| `postgres-ro` | 192.168.10.106 | Replica instances (read-only) |
| `postgres-r` | 192.168.10.107 | Any instance (read) |
| `postgres-pooler` | 192.168.10.108 | PgBouncer connection pooler (RW) |

## Database Configuration

- **Database Name**: `authentik`
- **Application User**: `authentik`
- **Application Password**: `<AUTHENTIK_DB_PASSWORD>`
- **Superuser**: `postgres`
- **Superuser Password**: `<POSTGRES_PASSWORD>`

## Directory Structure

```
06.postgres/
├── README.md                           # This file
├── namespace.yaml                      # postgres-system namespace
├── 01.install-operator.sh              # Install CloudNativePG operator
├── 02.deploy-cluster.sh                # Deploy PostgreSQL cluster
├── operator/
│   └── cnpg-operator.yaml             # CNPG operator v1.28.0 manifest
├── storage/
│   └── backup-nfs-storage.yaml        # NFS PV/PVC for backups
├── cluster/
│   ├── postgres-secret.yaml           # Superuser and app user credentials
│   ├── postgres-cluster.yaml          # PostgreSQL cluster definition
│   └── postgres-pooler.yaml           # PgBouncer connection pooler
├── services/
│   ├── postgres-lb-rw.yaml            # Primary LoadBalancer service
│   ├── postgres-lb-ro.yaml            # Replica LoadBalancer service
│   ├── postgres-lb-r.yaml             # Any instance LoadBalancer service
│   └── postgres-pooler-lb.yaml        # Pooler LoadBalancer service
└── backup/
    └── backup-schedule.yaml           # Daily backup schedule
```

## Installation

### Prerequisites

1. **k3s cluster** with 3 worker nodes (61, 62, 63)
2. **kube-vip** installed and configured (IP pool: 192.168.10.100-199)
3. **kubectl** configured to access the cluster

### Step 1: Install CloudNativePG Operator

```bash
cd /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/06.postgres
./01.install-operator.sh
```

This will:
- Create the `postgres-system` namespace
- Install CloudNativePG operator v1.28.0 in `cnpg-system` namespace
- Wait for the operator to be ready

Verify operator installation:
```bash
kubectl get pods -n cnpg-system
kubectl get crd | grep postgresql
```

### Step 2: Deploy PostgreSQL Cluster

```bash
./02.deploy-cluster.sh
```

This will:
- Create NFS PV/PVC for backup storage
- Create secrets for superuser and app user
- Deploy PostgreSQL cluster with 3 instances
- Deploy PgBouncer connection pooler (3 replicas)
- Create LoadBalancer services
- Configure daily backup schedule

The script will wait for the cluster and pooler to be ready (may take 3-5 minutes).

### Step 3: Verify Deployment

Check cluster status:
```bash
kubectl get cluster -n postgres-system
kubectl get pods -n postgres-system -o wide
```

Expected output:
```
NAME               AGE   INSTANCES   READY   STATUS                     PRIMARY
postgres-cluster   2m    3           3       Cluster in healthy state   postgres-cluster-1
```

Check services:
```bash
kubectl get svc -n postgres-system
```

All LoadBalancer services should have EXTERNAL-IP assigned.

## Connection Information

### Direct Connection Strings

**Primary (Read-Write):**
```bash
postgresql://authentik:<AUTHENTIK_DB_PASSWORD>@192.168.10.105:5432/authentik
```

**Replicas (Read-Only):**
```bash
postgresql://authentik:<AUTHENTIK_DB_PASSWORD>@192.168.10.106:5432/authentik
```

**Via PgBouncer Pooler (Recommended for applications):**
```bash
postgresql://authentik:<AUTHENTIK_DB_PASSWORD>@192.168.10.108:5432/authentik
```

### Connection from within Kubernetes

**Primary (RW):**
```bash
postgres-cluster-rw.postgres-system.svc.cluster.local:5432
```

**Replicas (RO):**
```bash
postgres-cluster-ro.postgres-system.svc.cluster.local:5432
```

**Via Pooler (Recommended):**
```bash
postgres-pooler-rw.postgres-system.svc.cluster.local:5432
```

### Superuser Connection (for administration)

```bash
postgresql://postgres:<POSTGRES_PASSWORD>@192.168.10.105:5432/authentik
```

## Testing Connection

### From within the cluster

```bash
kubectl run -it --rm psql-client --image=postgres:16 --restart=Never -n postgres-system -- \
  psql -h postgres-pooler-rw -U authentik -d authentik
```

### From your local machine (if you have psql installed)

```bash
PGPASSWORD='<AUTHENTIK_DB_PASSWORD>' psql -h 192.168.10.108 -U authentik -d authentik
```

### Test queries

```sql
-- Check connection
SELECT version();

-- Check current role
SELECT current_user, current_database();

-- List tables (if any)
\dt

-- Exit
\q
```

## Backup and Recovery

**Important:** The current configuration uses local volume snapshots for backups. For production use with Point-in-Time Recovery (PITR) and WAL archiving to NFS/S3, you should configure `barmanObjectStore` with proper object storage or use the barman-cloud plugin. See the [official backup documentation](https://cloudnative-pg.io/docs/1.28/backup/) for details.

### Verify Backup Configuration

Check scheduled backup:
```bash
kubectl get scheduledbackup -n postgres-system
```

Check backup status:
```bash
kubectl get backup -n postgres-system
```

View backup details:
```bash
kubectl describe backup <backup-name> -n postgres-system
```

### Manual Backup

Trigger an immediate backup:
```bash
kubectl cnpg backup postgres-cluster -n postgres-system
```

Or create a manual backup manifest:
```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Backup
metadata:
  name: postgres-manual-backup
  namespace: postgres-system
spec:
  cluster:
    name: postgres-cluster
  method: barmanObjectStore
```

### List Backups on NFS

Check backup PVC status:
```bash
kubectl get pvc -n postgres-system | grep backup
```

### Point-in-Time Recovery (PITR)

To restore to a specific point in time, create a new cluster with recovery configuration:

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: postgres-cluster-restored
  namespace: postgres-system
spec:
  instances: 3

  bootstrap:
    recovery:
      source: postgres-cluster
      recoveryTarget:
        targetTime: "2026-01-18 10:30:00.00000+00"

  externalClusters:
    - name: postgres-cluster
      barmanObjectStore:
        destinationPath: /var/lib/postgresql/backup
        serverName: postgres-cluster
        wal:
          compression: gzip
```

Apply and wait for recovery to complete.

## Monitoring

### CloudNativePG Status

```bash
# Cluster overview
kubectl cnpg status postgres-cluster -n postgres-system

# Detailed cluster info
kubectl describe cluster postgres-cluster -n postgres-system

# Replication status
kubectl cnpg status postgres-cluster -n postgres-system --verbose
```

### Pod Logs

```bash
# Primary pod logs
kubectl logs -n postgres-system postgres-cluster-1 -f

# Pooler logs
kubectl logs -n postgres-system -l cnpg.io/poolerName=postgres-pooler -f
```

### Resource Usage

```bash
kubectl top pods -n postgres-system
```

### Prometheus Metrics (if Prometheus is installed)

CloudNativePG exposes Prometheus metrics on port 9187.

**Note:** The `enablePodMonitor` field is deprecated in v1.28. To monitor the cluster, manually create a PodMonitor resource:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: postgres-cluster
  namespace: postgres-system
spec:
  selector:
    matchLabels:
      cnpg.io/cluster: postgres-cluster
  podMetricsEndpoints:
    - port: metrics
```

Key metrics:
- `cnpg_pg_replication_lag`: Replication lag in bytes
- `cnpg_pg_stat_database_xact_commit`: Transaction commit rate
- `cnpg_pg_database_size_bytes`: Database size

## High Availability and Failover

### Automatic Failover

CloudNativePG automatically handles failover when the primary instance fails. The operator will:
1. Detect primary failure
2. Promote the most up-to-date replica to primary
3. Update the `postgres-rw` service endpoint
4. Reconfigure remaining replicas to follow the new primary

### Testing Failover

Delete the primary pod to simulate a failure:

```bash
# Identify the primary
kubectl get pods -n postgres-system -l cnpg.io/cluster=postgres-cluster -L role

# Delete the primary pod
kubectl delete pod postgres-cluster-1 -n postgres-system

# Watch the failover process
kubectl get pods -n postgres-system -w
```

The operator should promote a replica within seconds. You can verify:

```bash
kubectl cnpg status postgres-cluster -n postgres-system
```

### Manual Switchover

To perform a planned switchover (zero downtime):

```bash
kubectl cnpg promote postgres-cluster-2 -n postgres-system
```

## Scaling

### Scale Replicas

To add or remove replicas:

```bash
kubectl cnpg scale postgres-cluster 5 -n postgres-system  # Scale to 5 instances
```

Or edit the cluster manifest:
```bash
kubectl edit cluster postgres-cluster -n postgres-system
# Change spec.instances: 5
```

### Scale PgBouncer Pooler

Edit the pooler manifest:
```bash
kubectl edit pooler postgres-pooler -n postgres-system
# Change spec.instances: 5
```

## Maintenance

### Upgrade PostgreSQL Version

CloudNativePG supports rolling upgrades. Edit the cluster manifest:

```bash
kubectl edit cluster postgres-cluster -n postgres-system
```

Change `spec.imageName` to the desired PostgreSQL version:
```yaml
spec:
  imageName: ghcr.io/cloudnative-pg/postgresql:17
```

The operator will perform a rolling upgrade with minimal downtime.

### Update Configuration

Edit PostgreSQL parameters:
```bash
kubectl edit cluster postgres-cluster -n postgres-system
```

Modify `spec.postgresql.parameters`:
```yaml
spec:
  postgresql:
    parameters:
      max_connections: "300"
      shared_buffers: "512MB"
```

CloudNativePG will apply the changes with a rolling restart.

### Restart Cluster

```bash
kubectl cnpg restart postgres-cluster -n postgres-system
```

This performs a rolling restart to minimize downtime.

## Troubleshooting

### Cluster Not Ready

Check cluster events:
```bash
kubectl describe cluster postgres-cluster -n postgres-system
```

Check pod events:
```bash
kubectl describe pods -n postgres-system
```

View operator logs:
```bash
kubectl logs -n cnpg-system deployment/cnpg-controller-manager
```

### Replication Issues

Check replication lag:
```bash
kubectl cnpg status postgres-cluster -n postgres-system --verbose
```

Connect to primary and check replication:
```bash
kubectl exec -it postgres-cluster-1 -n postgres-system -- psql -U postgres -c "SELECT * FROM pg_stat_replication;"
```

### Backup Failures

Check backup status:
```bash
kubectl get backup -n postgres-system
kubectl describe backup <backup-name> -n postgres-system
```

Verify NFS mount:
```bash
kubectl exec -it postgres-cluster-1 -n postgres-system -- ls -lah /var/lib/postgresql/backup/
```

### Connection Issues

Test from within cluster:
```bash
kubectl run -it --rm test-psql --image=postgres:16 --restart=Never -n postgres-system -- \
  psql -h postgres-pooler-rw -U authentik -d authentik -c "SELECT version();"
```

Check LoadBalancer IPs:
```bash
kubectl get svc -n postgres-system
```

Verify kube-vip cloud provider is running:
```bash
kubectl get pods -n kube-system -l app=kube-vip-cloud-provider
```

### Pod Anti-Affinity Issues

If pods are stuck in Pending due to anti-affinity rules (not enough nodes):

```bash
kubectl get pods -n postgres-system -o wide
kubectl describe pod postgres-cluster-3 -n postgres-system
```

You may need to reduce the number of instances or adjust anti-affinity rules.

## Uninstall

### Remove Cluster and Resources

```bash
# Delete scheduled backup
kubectl delete scheduledbackup postgres-daily-backup -n postgres-system

# Delete services
kubectl delete -f services/

# Delete pooler
kubectl delete pooler postgres-pooler -n postgres-system

# Delete cluster (WARNING: This will delete all data!)
kubectl delete cluster postgres-cluster -n postgres-system

# Delete secrets
kubectl delete secret postgres-superuser postgres-app-user -n postgres-system

# Delete storage
kubectl delete -f storage/backup-nfs-storage.yaml

# Delete namespace
kubectl delete namespace postgres-system
```

### Remove Operator

```bash
kubectl delete -f operator/cnpg-operator.yaml
kubectl delete namespace cnpg-system
```

### Clean Backup PVC Data

**WARNING**: This will permanently delete all backups!

```bash
kubectl delete pvc postgres-backup-pvc -n postgres-system
```

## Additional Resources

- [CloudNativePG Documentation](https://cloudnative-pg.io/documentation/)
- [PostgreSQL 16 Release Notes](https://www.postgresql.org/docs/16/release-16.html)
- [PgBouncer Documentation](https://www.pgbouncer.org/)

## Support

For issues specific to this deployment, check:
1. CloudNativePG operator logs
2. PostgreSQL pod logs
3. Cluster status via `kubectl cnpg status`

For k3s cluster issues, refer to [../README.md](../README.md).
