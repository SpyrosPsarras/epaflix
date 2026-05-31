# Newtarr & Cleanuparr Database Recovery

> **Note (#131, 2026-05-31):** Huntarr was renamed to **Newtarr** (ElfHosted
> fork, `ghcr.io/elfhosted/newtarr:rolling`); the config PVC is now
> `newtarr-config` and the Deployment/Service/PDB are `newtarr`. The historical
> commands below are preserved verbatim and still reference the old `huntarr`
> Deployment, `huntarr-config` PVC, and `huntarr.db*` filenames — substitute the
> `newtarr` equivalents when running this runbook today. The on-disk SQLite file
> may remain `huntarr.db` if the fork keeps the original filename, so do NOT
> blindly rename it (see TODO in `newtarr/newtarr.yaml`).

## Recovery Date
February 3, 2026 (original; Huntarr-era)

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
Newtarr (formerly Huntarr) and Cleanuparr are **configuration management tools** that store their settings and state in SQLite databases within their config directories. They don't have dedicated PostgreSQL databases like the *arr apps (Sonarr, Radarr, etc.).

The databases were preserved in old PVCs on worker-62 from before the cluster crash and have now been successfully restored to the new cluster.

---

## Incident 2026-05-31 — Cleanuparr "K-foodie S04E13" strike runaway (#138)

**Symptom:** Cleanuparr reported "download keeps coming back after deletion" for
`K-foodie.meets.J-foodie.S04E13.1080p.WEB.h264-EDITH`
(hash `66a4dc6201cb149ff70eed12b9902317cb82ed87`), strikeCount up to 285, 76
Action Required events.

**Reality:** the active loop had already ended months earlier — all 76 events
dated 2026-03-26..03-29, the torrent was gone from qbittorrent, and Cleanuparr's
queue-cleaner striking for Sonarr was already disabled (per-arr
`failed_import_max_strikes=-1`). What remained was stale residue plus a
still-monitored+missing S04E13 that could **re-arm** via newtarr
(`hunt_missing_items=1`, 15-min cadence) combined with Sonarr
`autoRedownloadFailed=true`. The Sonarr blocklist is keyed by
**title+indexer+GUID, not infohash**, so cross-posted variants slipped past it.

**Fix applied (4 steps, all verified):**
1. Archived + resolved the 76 stale Cleanuparr `manual_events` (76 → 0).
2. Unmonitored Sonarr `episodeId 3143` (seriesId 40).
3. Cleanuparr content-blocker: created `/config/custom-blocklist-sonarr.txt`
   (850 upstream `flmorg/cleanuperr` entries + regex
   `/K.?foodie.?meets.?J.?foodie.*S04E13.*EDITH/i`) and repointed
   `sonarr_blocklist_path` at it.
4. Confirmed no live S04E13 torrent. Cleanuparr healthy after restart.
   DBs backed up in-pod (`.bak-20260531-135309`).

> **Durability caveat:** `custom-blocklist-sonarr.txt` and the repointed
> `sonarr_blocklist_path` live **only on the Cleanuparr config local-path PVC** —
> they are NOT in any git manifest, so this fix is lost on a PVC rebuild. The
> custom list is also a static snapshot of upstream (won't auto-track updates,
> unlike the Radarr list which still uses the live URL). Soak-confirm + codify
> tracked in **#138**. A separate stalled torrent K-foodie S04E07
> (`828ea9eb36f00f821772d4d431dddf12ea6bd0c2`, `stalledDL`) is triaged
> independently in **#139**.
