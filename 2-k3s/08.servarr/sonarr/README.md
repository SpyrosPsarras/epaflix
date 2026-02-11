# Sonarr Database Maintenance Tools

This directory contains tools and jobs for maintaining Sonarr's PostgreSQL database health.

## Quick Start

### Check Database Health
```bash
./check-database-health.sh
```

This will check for:
- Duplicate IDs in Series, Episodes, and EpisodeFiles tables
- Sequence misalignment (common after database restores)
- Overall database statistics

### Fix Issues

If the health check finds problems, apply the appropriate fix:

```bash
# Fix duplicate Series IDs
kubectl apply -f fix-duplicate-series.yaml

# Fix duplicate Episode IDs
kubectl apply -f fix-duplicate-episodes.yaml

# Fix duplicate EpisodeFile IDs
kubectl apply -f fix-duplicate-episodefiles.yaml
```

**After any fix, restart Sonarr:**
```bash
kubectl rollout restart deployment/sonarr -n servarr
```

## Available Tools

### 1. Health Check Script
**File**: `check-database-health.sh`

- ✅ Checks all critical tables for duplicates
- ✅ Validates sequence alignment
- ✅ Provides actionable fix commands
- ✅ Color-coded output for easy reading

**Usage**:
```bash
./check-database-health.sh
```

**Example Output**:
```
✅ Series: No duplicates found
✅ Episodes: No duplicates found
✅ EpisodeFiles: No duplicates found
✅ Series: Sequence OK (max_id=90, sequence=91)
✅ Episodes: Sequence OK (max_id=4796, sequence=4796)
✅ EpisodeFiles: Sequence OK (max_id=2514, sequence=2515)

Database Statistics:
   Series: 80
   Episodes: 4212
   EpisodeFiles: 1431

✅ Database is HEALTHY
```

### 2. Fix Jobs

#### fix-duplicate-series.yaml
Fixes duplicate Series IDs in the database.

**What it does**:
1. Identifies all duplicate Series IDs
2. Keeps first occurrence (by ctid) of each duplicate
3. Deletes all other duplicates
4. Fixes the `Series_Id_seq` sequence
5. Verifies no duplicates remain

**Apply**:
```bash
kubectl apply -f fix-duplicate-series.yaml
kubectl logs -n servarr job/fix-sonarr-duplicate-series -f
```

#### fix-duplicate-episodes.yaml
Fixes duplicate Episode IDs in the database.

**What it does**:
1. Identifies all duplicate Episode IDs
2. Keeps first occurrence (by ctid) of each duplicate
3. Deletes all other duplicates
4. Fixes the `Episodes_Id_seq` sequence
5. Verifies no duplicates remain

**Apply**:
```bash
kubectl apply -f fix-duplicate-episodes.yaml
kubectl logs -n servarr job/fix-sonarr-duplicate-episodes -f
```

#### fix-duplicate-episodefiles.yaml
Fixes duplicate EpisodeFile IDs in the database.

**What it does**:
1. Identifies all duplicate EpisodeFile IDs
2. Keeps first occurrence (by ctid) of each duplicate
3. Deletes all other duplicates
4. Fixes the `EpisodeFiles_Id_seq` sequence
5. Verifies no duplicates remain

**Apply**:
```bash
kubectl apply -f fix-duplicate-episodefiles.yaml
kubectl logs -n servarr job/fix-sonarr-duplicate-episodefiles -f
```

## Common Issues

### MultipleSeriesFoundException
**Symptom**: 
```
NzbDrone.Core.Tv.MultipleSeriesFoundException: Expected one series, but found 2
```

**Cause**: Duplicate Series IDs in database

**Fix**: 
```bash
kubectl apply -f fix-duplicate-series.yaml
kubectl rollout restart deployment/sonarr -n servarr
```

**Documentation**: See `../TROUBLESHOOTING-DUPLICATE-SERIES.md`

### Expected query to return X rows but returned Y
**Symptom**:
```
System.ApplicationException: Expected query to return 11 rows but returned 13
```

**Cause**: Duplicate Episode IDs in database

**Fix**:
```bash
kubectl apply -f fix-duplicate-episodes.yaml
kubectl rollout restart deployment/sonarr -n servarr
```

**Documentation**: See `../TROUBLESHOOTING-DUPLICATE-EPISODES.md`

### Sequence contains more than one element
**Symptom**:
```
System.InvalidOperationException: Sequence contains more than one element
```

**Cause**: Duplicate EpisodeFile IDs in database

**Fix**:
```bash
kubectl apply -f fix-duplicate-episodefiles.yaml
kubectl rollout restart deployment/sonarr -n servarr
```

**Documentation**: See `../TROUBLESHOOTING-DUPLICATE-FILE-IDS.md`

## When to Use These Tools

### After Database Restores
Always check and fix sequences after restoring from backup:
```bash
./check-database-health.sh

# If issues found, apply fixes
kubectl apply -f fix-duplicate-series.yaml
kubectl apply -f fix-duplicate-episodes.yaml
kubectl apply -f fix-duplicate-episodefiles.yaml

# Restart Sonarr
kubectl rollout restart deployment/sonarr -n servarr
```

### Regular Maintenance
Run health checks weekly:
```bash
# Add to cron
0 2 * * 0 /path/to/check-database-health.sh
```

### Before Major Updates
Check database health before Sonarr version upgrades:
```bash
./check-database-health.sh
```

## How It Works

### Root Cause
Database corruption typically happens when:
1. Database is restored from backup
2. Table data is restored with existing IDs
3. PostgreSQL sequences reset to incorrect values
4. New inserts use sequence values → collide with existing IDs
5. Result: Duplicate primary keys

### The Fix
All fix jobs follow the same pattern:
1. **Identify**: Find all duplicate IDs using GROUP BY
2. **Choose**: Keep first occurrence by `ctid` (stable, deterministic)
3. **Delete**: Remove all other duplicates in transaction
4. **Fix Sequence**: Set sequence to `MAX(Id) + 1`
5. **Verify**: Ensure zero duplicates remain
6. **Commit**: All or nothing (transaction-protected)

### Safety
- ✅ All operations in `BEGIN`/`COMMIT` transaction
- ✅ Idempotent (safe to run multiple times)
- ✅ Only removes exact duplicates (same ID)
- ✅ No unique data is lost
- ✅ Job logs available for 1 hour after completion
- ✅ Sonarr auto-backups before database changes

## Database Access

The tools automatically retrieve credentials from Kubernetes secrets:
```bash
SONARR_PW=$(kubectl get secret servarr-postgres -n servarr \
  -o jsonpath='{.data.sonarr-password}' | base64 -d)
```

**Database Details**:
- Host: 192.168.10.105
- Port: 5432
- Database: sonarr-main
- User: sonarr
- Password: (stored in `servarr-postgres` secret)

## Manual Database Access

If you need to access the database directly:
```bash
SONARR_PW=$(kubectl get secret servarr-postgres -n servarr \
  -o jsonpath='{.data.sonarr-password}' | base64 -d)

PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main
```

### Useful Queries

**Check for duplicates**:
```sql
-- Series duplicates
SELECT "Id", COUNT(*) FROM "Series" 
GROUP BY "Id" HAVING COUNT(*) > 1;

-- Episodes duplicates
SELECT "Id", COUNT(*) FROM "Episodes" 
GROUP BY "Id" HAVING COUNT(*) > 1;

-- EpisodeFiles duplicates
SELECT "Id", COUNT(*) FROM "EpisodeFiles" 
GROUP BY "Id" HAVING COUNT(*) > 1;
```

**Check sequence alignment**:
```sql
-- Series
SELECT MAX("Id") as max_id, 
       (SELECT last_value FROM "Series_Id_seq") as sequence 
FROM "Series";

-- Episodes
SELECT MAX("Id") as max_id, 
       (SELECT last_value FROM "Episodes_Id_seq") as sequence 
FROM "Episodes";

-- EpisodeFiles
SELECT MAX("Id") as max_id, 
       (SELECT last_value FROM "EpisodeFiles_Id_seq") as sequence 
FROM "EpisodeFiles";
```

## Troubleshooting

### Job Won't Start
```bash
# Check if previous job exists
kubectl get jobs -n servarr | grep fix-sonarr

# Delete old job
kubectl delete job fix-sonarr-duplicate-series -n servarr
```

### Can't Connect to Database
```bash
# Verify postgres is running
kubectl get pods -n postgres

# Test connection
SONARR_PW=$(kubectl get secret servarr-postgres -n servarr \
  -o jsonpath='{.data.sonarr-password}' | base64 -d)
PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main -c "SELECT 1;"
```

### Still Getting Errors After Fix
1. Verify job completed successfully
2. Check database has no remaining duplicates
3. Restart Sonarr to clear caches:
   ```bash
   kubectl rollout restart deployment/sonarr -n servarr
   ```
4. Wait 30 seconds for pod to be ready
5. Check logs for new errors

## Related Documentation

- `../TROUBLESHOOTING-DUPLICATE-SERIES.md` - Series duplicate fixes
- `../TROUBLESHOOTING-DUPLICATE-EPISODES.md` - Episode duplicate fixes
- `../TROUBLESHOOTING-DUPLICATE-FILE-IDS.md` - EpisodeFile duplicate fixes
- `../TROUBLESHOOTING-DB-SCHEMA.md` - Schema issues
- `../../.history/2026-02-26-sonarr-duplicate-series-fix.log` - Detailed fix history

## History

- **2026-02-26**: Created comprehensive fix tooling
  - Fixed Band of Brothers duplicate series issue
  - Fixed 10 duplicate EpisodeFiles
  - Created health check script
  - All fixes successful, zero data loss

## Support

If you encounter issues not covered here:
1. Run health check: `./check-database-health.sh`
2. Check Sonarr logs: `kubectl logs -n servarr deployment/sonarr --tail=100`
3. Review troubleshooting docs in parent directory
4. Check `.history/` directory for similar past issues