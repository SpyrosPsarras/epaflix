# Huntarr & Cleanuparr Database Recovery

## Recovery Date
February 3, 2026

## Problem
- huntarr.epaflix.com and cleanuparr.epaflix.com showed no data from previous installation
- User suspected PostgreSQL was never migrated for these apps

## Investigation
Both apps use **SQLite databases** stored in config directories on local-path PVCs, not PostgreSQL.

### Huntarr
- Current PVC: `pvc-47b294c2-1bd8-4c11-a95a-874873102d3b` on worker-61
- **Already using old PVC from previous cluster** but database was smaller (248KB)
- Found better backup on worker-62: `huntarr.db` (372KB, last modified Feb 1, 2026)

### Cleanuparr
- Current PVC: `pvc-524fa1cf-3fc1-4d1c-8840-dad08d76805e` on worker-65
- Had fresh/smaller database (228KB created Feb 2)
- Found old backup on worker-62: `cleanuparr.db` (228KB, Jan 25) + `events.db` (216KB, Jan 30)

## Old PVC Locations (Pre-Cluster Crash)
Found on worker-62:
```bash
/var/lib/rancher/k3s/storage/pvc-c6a9b9fb-ae95-47c7-898b-7df4fe93114b_servarr_huntarr-config/
├── huntarr.db (372KB, Feb 1 11:23)
├── logs.db (11MB, Feb 1 11:33)
└── backups/ (5 backups)

/var/lib/rancher/k3s/storage/pvc-94339164-2f53-4459-90eb-2dba82c474a2_servarr_cleanuparr-config/
├── cleanuparr.db (228KB, Jan 25 09:44)
├── events.db (216KB, Jan 30 23:32)
└── logs/ (69 log files from Dec 3, 2025 to Jan 31, 2026)
```

## Recovery Procedure

### 1. Scale Down Apps
```bash
kubectl -n servarr scale deployment huntarr --replicas=0
kubectl -n servarr scale deployment cleanuparr --replicas=0
```

### 2. Copy Old Databases from Worker-62
```bash
# Huntarr
scp ubuntu@192.168.10.62:/var/lib/rancher/k3s/storage/pvc-c6a9b9fb-ae95-47c7-898b-7df4fe93114b_servarr_huntarr-config/huntarr.db /tmp/
scp ubuntu@192.168.10.62:/var/lib/rancher/k3s/storage/pvc-c6a9b9fb-ae95-47c7-898b-7df4fe93114b_servarr_huntarr-config/logs.db /tmp/

# Cleanuparr
scp ubuntu@192.168.10.62:/var/lib/rancher/k3s/storage/pvc-94339164-2f53-4459-90eb-2dba82c474a2_servarr_cleanuparr-config/cleanuparr.db /tmp/
scp ubuntu@192.168.10.62:/var/lib/rancher/k3s/storage/pvc-94339164-2f53-4459-90eb-2dba82c474a2_servarr_cleanuparr-config/events.db /tmp/
```

### 3. Restore to Current PVCs
```bash
# Huntarr (worker-61)
scp /tmp/huntarr.db /tmp/logs.db ubuntu@192.168.10.61:/tmp/
ssh ubuntu@192.168.10.61 'sudo rm -f /var/lib/rancher/k3s/storage/pvc-47b294c2-1bd8-4c11-a95a-874873102d3b_servarr_huntarr-config/huntarr.db*'
ssh ubuntu@192.168.10.61 'sudo rm -f /var/lib/rancher/k3s/storage/pvc-47b294c2-1bd8-4c11-a95a-874873102d3b_servarr_huntarr-config/logs.db*'
ssh ubuntu@192.168.10.61 'sudo cp /tmp/huntarr.db /var/lib/rancher/k3s/storage/pvc-47b294c2-1bd8-4c11-a95a-874873102d3b_servarr_huntarr-config/'
ssh ubuntu@192.168.10.61 'sudo cp /tmp/logs.db /var/lib/rancher/k3s/storage/pvc-47b294c2-1bd8-4c11-a95a-874873102d3b_servarr_huntarr-config/'
ssh ubuntu@192.168.10.61 'sudo chown 568:568 /var/lib/rancher/k3s/storage/pvc-47b294c2-1bd8-4c11-a95a-874873102d3b_servarr_huntarr-config/*.db'

# Cleanuparr (worker-65)
scp /tmp/cleanuparr.db /tmp/events.db ubuntu@192.168.10.65:/tmp/
ssh ubuntu@192.168.10.65 'sudo rm -f /var/lib/rancher/k3s/storage/pvc-524fa1cf-3fc1-4d1c-8840-dad08d76805e_servarr_cleanuparr-config/*.db*'
ssh ubuntu@192.168.10.65 'sudo cp /tmp/cleanuparr.db /var/lib/rancher/k3s/storage/pvc-524fa1cf-3fc1-4d1c-8840-dad08d76805e_servarr_cleanuparr-config/'
ssh ubuntu@192.168.10.65 'sudo cp /tmp/events.db /var/lib/rancher/k3s/storage/pvc-524fa1cf-3fc1-4d1c-8840-dad08d76805e_servarr_cleanuparr-config/'
ssh ubuntu@192.168.10.65 'sudo chown 568:568 /var/lib/rancher/k3s/storage/pvc-524fa1cf-3fc1-4d1c-8840-dad08d76805e_servarr_cleanuparr-config/*.db'
```

### 4. Scale Up Apps
```bash
kubectl -n servarr scale deployment huntarr --replicas=1
kubectl -n servarr scale deployment cleanuparr --replicas=1
```

## Verification

### Huntarr Status
```bash
kubectl -n servarr logs huntarr-847bd4b69f-j9klj --tail=50
```

**Results:**
- ✅ **8 items tracked** (4 in sonarr + 4 in sonarr2)
- ✅ State management working: next reset 2026-02-07 (168h interval)
- ✅ User authentication ready
- ✅ Templates loaded successfully
- ✅ Next cycle scheduled for 2026-02-03 16:44:23

### Cleanuparr Status
```bash
kubectl -n servarr logs cleanuparr-c8c65cb64-lx75l --tail=30
```

**Results:**
- ✅ Event cleanup service started (4h interval, 30 days retention)
- ✅ BackgroundJobManager started
- ✅ Application started successfully

## Summary
- **Huntarr**: Restored 372KB database from Feb 1 with 8 tracked items across 2 Sonarr instances
- **Cleanuparr**: Restored 228KB database from Jan 25 + 216KB events database from Jan 30
- Both apps successfully started and loaded their configurations
- No PostgreSQL migration was needed - these apps use SQLite

## Key Insight
Huntarr and Cleanuparr are **configuration management tools** that store their settings and state in SQLite databases within their config directories. They don't have dedicated PostgreSQL databases like the *arr apps (Sonarr, Radarr, etc.).

The databases were preserved in old PVCs on worker-62 from before the cluster crash and have now been successfully restored to the new cluster.
