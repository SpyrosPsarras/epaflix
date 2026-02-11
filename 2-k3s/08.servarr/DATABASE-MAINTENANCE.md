# Database Maintenance Quick Reference

This document provides a quick reference for maintaining PostgreSQL databases for Sonarr, Sonarr2, and Radarr.

## Quick Health Check - All Services

```bash
cd /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/_shared/scripts
./check-all-databases-health.sh
```

This checks all three services (Sonarr, Sonarr2, Radarr) in one run.

## Individual Service Checks

```bash
# Sonarr (TV Shows)
cd /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/sonarr
./check-database-health.sh

# Sonarr2 (Anime)
cd /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/sonarr2
./check-database-health.sh

# Radarr (Movies)
cd /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/radarr
./check-database-health.sh
```

## Fix Jobs by Service

### Sonarr (TV Shows)

```bash
# Fix duplicate Series
kubectl apply -f /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/sonarr/fix-duplicate-series.yaml

# Fix duplicate Episodes
kubectl apply -f /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/sonarr/fix-duplicate-episodes.yaml

# Fix duplicate EpisodeFiles
kubectl apply -f /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/sonarr/fix-duplicate-episodefiles.yaml

# Restart after fixes
kubectl rollout restart deployment/sonarr -n servarr
```

### Sonarr2 (Anime)

```bash
# Fix duplicate Series
kubectl apply -f /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/sonarr2/fix-duplicate-series.yaml

# Fix duplicate Episodes
kubectl apply -f /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/sonarr2/fix-duplicate-episodes.yaml

# Fix duplicate EpisodeFiles
kubectl apply -f /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/sonarr2/fix-duplicate-episodefiles.yaml

# Restart after fixes
kubectl rollout restart deployment/sonarr2 -n servarr
```

### Radarr (Movies)

```bash
# Fix duplicate Movies
kubectl apply -f /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/radarr/fix-duplicate-movies.yaml

# Fix duplicate MovieFiles
kubectl apply -f /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/radarr/fix-duplicate-moviefiles.yaml

# Restart after fixes
kubectl rollout restart deployment/radarr -n servarr
```

## Common Scenarios

### After Database Restore

**Always** check and fix sequences after restoring from backup:

```bash
# Check all databases
./check-all-databases-health.sh

# Apply fixes for any service showing issues
# (The health check will tell you which fixes to apply)

# Restart affected services
kubectl rollout restart deployment/sonarr -n servarr
kubectl rollout restart deployment/sonarr2 -n servarr
kubectl rollout restart deployment/radarr -n servarr
```

### Regular Maintenance

Add to crontab for weekly checks:

```bash
# Run every Sunday at 2 AM
0 2 * * 0 /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/_shared/scripts/check-all-databases-health.sh
```

### Before Major Updates

```bash
# Check health before upgrading
./check-all-databases-health.sh

# If healthy, proceed with upgrade
# If issues found, fix first, then upgrade
```

## Common Errors and Fixes

### Error: "Expected one series, but found 2"

**Service**: Sonarr or Sonarr2  
**Cause**: Duplicate Series IDs  
**Fix**: Apply `fix-duplicate-series.yaml` for affected service

### Error: "Expected query to return X rows but returned Y"

**Service**: Sonarr or Sonarr2  
**Cause**: Duplicate Episode IDs  
**Fix**: Apply `fix-duplicate-episodes.yaml` for affected service

### Error: "Sequence contains more than one element"

**Service**: Any  
**Cause**: Duplicate File IDs  
**Fix**: Apply appropriate `fix-duplicate-*files.yaml` for affected service

### Downloads stuck in queue

**Service**: Any  
**Cause**: Likely duplicate IDs preventing import  
**Fix**: Run health check, apply fixes, restart service

## Database Connection Details

All databases hosted on: **192.168.10.105:5432**

| Service | Database      | User    | Secret Key           |
|---------|---------------|---------|----------------------|
| Sonarr  | sonarr-main   | sonarr  | sonarr-password      |
| Sonarr2 | sonarr2-main  | sonarr2 | sonarr2-password     |
| Radarr  | radarr-main   | radarr  | radarr-password      |

Credentials stored in Kubernetes secret: `servarr-postgres` (namespace: `servarr`)

## Manual Database Access

```bash
# Sonarr
SONARR_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.sonarr-password}' | base64 -d)
PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main

# Sonarr2
SONARR2_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.sonarr2-password}' | base64 -d)
PGPASSWORD="${SONARR2_PW}" psql -h 192.168.10.105 -U sonarr2 -d sonarr2-main

# Radarr
RADARR_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.radarr-password}' | base64 -d)
PGPASSWORD="${RADARR_PW}" psql -h 192.168.10.105 -U radarr -d radarr-main
```

## Useful SQL Queries

### Check for Duplicates

```sql
-- For Sonarr/Sonarr2
SELECT "Id", COUNT(*) FROM "Series" GROUP BY "Id" HAVING COUNT(*) > 1;
SELECT "Id", COUNT(*) FROM "Episodes" GROUP BY "Id" HAVING COUNT(*) > 1;
SELECT "Id", COUNT(*) FROM "EpisodeFiles" GROUP BY "Id" HAVING COUNT(*) > 1;

-- For Radarr
SELECT "Id", COUNT(*) FROM "Movies" GROUP BY "Id" HAVING COUNT(*) > 1;
SELECT "Id", COUNT(*) FROM "MovieFiles" GROUP BY "Id" HAVING COUNT(*) > 1;
```

### Check Sequence Alignment

```sql
-- For Sonarr/Sonarr2
SELECT MAX("Id") as max_id, (SELECT last_value FROM "Series_Id_seq") as sequence FROM "Series";
SELECT MAX("Id") as max_id, (SELECT last_value FROM "Episodes_Id_seq") as sequence FROM "Episodes";
SELECT MAX("Id") as max_id, (SELECT last_value FROM "EpisodeFiles_Id_seq") as sequence FROM "EpisodeFiles";

-- For Radarr
SELECT MAX("Id") as max_id, (SELECT last_value FROM "Movies_Id_seq") as sequence FROM "Movies";
SELECT MAX("Id") as max_id, (SELECT last_value FROM "MovieFiles_Id_seq") as sequence FROM "MovieFiles";
```

**Expected Result**: `sequence` should be >= `max_id`  
**If Not**: Run the appropriate fix job

## Monitoring Job Progress

```bash
# Watch job logs in real-time
kubectl logs -n servarr job/fix-sonarr-duplicate-series -f
kubectl logs -n servarr job/fix-sonarr2-duplicate-episodes -f
kubectl logs -n servarr job/fix-radarr-duplicate-movies -f

# Check job status
kubectl get jobs -n servarr | grep fix-

# Delete completed job (if needed)
kubectl delete job fix-sonarr-duplicate-series -n servarr
```

## Safety Features

All fix jobs include:
- ✅ Transaction protection (atomic commits)
- ✅ Idempotent (safe to run multiple times)
- ✅ Pre-checks (exit if no issues)
- ✅ Detailed logging
- ✅ Verification steps
- ✅ No data loss (duplicates are identical)
- ✅ Job logs retained for 1 hour

## Documentation

### Per-Service Documentation
- `sonarr/README.md` - Complete Sonarr tools documentation
- `sonarr2/README.md` - Complete Sonarr2 tools documentation
- `radarr/README.md` - Complete Radarr tools documentation

### Troubleshooting Guides
- `TROUBLESHOOTING-DUPLICATE-SERIES.md` - Series duplicate issues
- `TROUBLESHOOTING-DUPLICATE-EPISODES.md` - Episode duplicate issues
- `TROUBLESHOOTING-DUPLICATE-FILE-IDS.md` - File duplicate issues
- `TROUBLESHOOTING-DB-SCHEMA.md` - Schema issues

### History
- `.history/2026-02-26-sonarr-duplicate-series-fix.log` - Original Sonarr fix
- `.history/2026-02-26-sonarr2-radarr-database-tools.log` - Tool creation log

## Quick Workflow

1. **Check Health**
   ```bash
   ./check-all-databases-health.sh
   ```

2. **Apply Fixes** (if issues found)
   ```bash
   kubectl apply -f <service>/fix-duplicate-<table>.yaml
   ```

3. **Monitor Progress**
   ```bash
   kubectl logs -n servarr job/fix-<service>-duplicate-<table> -f
   ```

4. **Restart Service**
   ```bash
   kubectl rollout restart deployment/<service> -n servarr
   ```

5. **Verify Fix**
   ```bash
   ./check-all-databases-health.sh
   ```

## Support

For issues not covered here:
1. Check service-specific README in `sonarr/`, `sonarr2/`, or `radarr/`
2. Review troubleshooting guides in this directory
3. Check `.history/` for similar past issues
4. Check service logs: `kubectl logs -n servarr deployment/<service>`

## Version History

- **2026-02-26**: Initial creation
  - Sonarr tools created (Series, Episodes, EpisodeFiles)
  - Sonarr2 tools created (mirrored from Sonarr)
  - Radarr tools created (Movies, MovieFiles)
  - Master health check script created
  - All databases verified healthy