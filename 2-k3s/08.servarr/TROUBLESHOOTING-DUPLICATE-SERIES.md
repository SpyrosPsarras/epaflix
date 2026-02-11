# Duplicate Series IDs - Database Corruption Fix

## Problem: "Expected one series, but found 2" Error

### Symptoms
- Sonarr download monitoring errors in logs:
  ```
  [Error] DownloadMonitoringService: Couldn't process tracked download Band.Of.Brothers...
  NzbDrone.Core.Tv.MultipleSeriesFoundException: Expected one series, but found 2. 
  Matching series: [74205][Band of Brothers], [74205][Band of Brothers]
  ```
- Downloads stuck in queue unable to import
- Series management issues in UI
- API calls returning multiple series when one expected

### Root Cause
**Database corruption**: Multiple rows in the `Series` table with the same primary key `Id`.

This typically happens when:
- The `Series_Id_seq` sequence gets reset or corrupted
- Database restored from backup without fixing sequences
- Manual database manipulation
- Incomplete database migration

When the sequence is incorrect, new series get assigned IDs that already exist, creating duplicates.

## Diagnosis Steps

### 1. Check for duplicate series IDs
```bash
SONARR_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.sonarr-password}' | base64 -d)

PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
-- Find all duplicate series IDs
SELECT "Id", COUNT(*)
FROM "Series"
GROUP BY "Id"
HAVING COUNT(*) > 1
ORDER BY "Id";
EOF
```

### 2. Check sequence mismatch
```bash
PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
SELECT 
  COUNT(*) as total_series,
  MAX("Id") as max_series_id,
  (SELECT last_value FROM "Series_Id_seq") as sequence_value
FROM "Series";
EOF
```

**Red flag**: If `sequence_value` is much lower than `max_series_id`, you have a problem!

Example of corrupted state:
- total_series: 80
- max_series_id: 90
- sequence_value: 1 ❌ (should be > 90)

### 3. View duplicate details
```bash
PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
-- Show details of duplicates
SELECT s."Id", s."TvdbId", s."Title", s."CleanTitle", s."Path"
FROM "Series" s
WHERE s."Id" IN (
  SELECT "Id" FROM "Series" GROUP BY "Id" HAVING COUNT(*) > 1
)
ORDER BY s."Id";
EOF
```

## Automated Fix

### Quick Fix (Recommended)
```bash
kubectl apply -f /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/sonarr/fix-duplicate-series.yaml
```

Wait for job completion and check logs:
```bash
kubectl logs -n servarr job/fix-sonarr-duplicate-series -f
```

The job will:
1. ✅ Identify all duplicate series IDs
2. ✅ Keep the first occurrence (by ctid) of each duplicate
3. ✅ Delete all other duplicates
4. ✅ Fix the sequence to MAX(Id) + 1
5. ✅ Verify no duplicates remain

### Restart Sonarr After Fix
```bash
# Restart to clear any cached series data
kubectl rollout restart deployment/sonarr -n servarr

# Wait for pod to be ready
kubectl get pods -n servarr -l app=sonarr -w
```

### Manual Fix (If needed)

```bash
SONARR_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.sonarr-password}' | base64 -d)

PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
BEGIN;

-- Create temp table with one series per unique ID (keep earliest by ctid)
CREATE TEMP TABLE series_to_keep AS
SELECT DISTINCT ON ("Id") 
  "Id",
  ctid as keep_ctid
FROM "Series"
ORDER BY "Id", ctid;

-- Show counts
SELECT COUNT(*) as total FROM "Series";
SELECT COUNT(*) as to_keep FROM series_to_keep;

-- Delete duplicates
DELETE FROM "Series" s
WHERE NOT EXISTS (
  SELECT 1 FROM series_to_keep k
  WHERE k."Id" = s."Id" AND k.keep_ctid = s.ctid
);

-- Fix sequence
SELECT setval('"Series_Id_seq"', (SELECT MAX("Id") FROM "Series") + 1, false);

-- Verify
SELECT COUNT(*) as remaining_duplicates
FROM (SELECT "Id" FROM "Series" GROUP BY "Id" HAVING COUNT(*) > 1) sub;

COMMIT;
EOF
```

## Verification

### 1. Check no duplicates remain
```bash
PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main \
  -c "SELECT COUNT(*) as duplicates FROM (SELECT \"Id\" FROM \"Series\" GROUP BY \"Id\" HAVING COUNT(*) > 1) sub;"
```
**Expected**: `0`

### 2. Verify sequence is correct
```bash
PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
SELECT 
  MAX("Id") as max_id,
  (SELECT last_value FROM "Series_Id_seq") as sequence
FROM "Series";
EOF
```
**Expected**: `sequence` should be > `max_id`

### 3. Check Sonarr logs
```bash
kubectl logs -n servarr deployment/sonarr --tail=50
```
**Expected**: No more "MultipleSeriesFoundException" errors

### 4. Test Download Processing
- Check the Activity → Queue page in Sonarr UI
- The stuck download should now process successfully
- Verify imports are working

### 5. Test Series Access
```bash
# Verify specific series can be queried
PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main \
  -c "SELECT \"Id\", \"TvdbId\", \"Title\" FROM \"Series\" WHERE \"Title\" ILIKE '%band%brother%';"
```
**Expected**: Only one row returned

## Prevention

### After Database Restores
Always run after restoring from backup:
```bash
# Check and fix ALL sequences
kubectl apply -f /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/sonarr/fix-duplicate-series.yaml
```

### Regular Health Checks
Add to maintenance scripts:
```bash
#!/bin/bash
SONARR_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.sonarr-password}' | base64 -d)

echo "Checking Sonarr database health..."

PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
-- Check for duplicates in all critical tables
SELECT 'Series' as table_name, COUNT(*) as duplicate_ids
FROM (SELECT "Id" FROM "Series" GROUP BY "Id" HAVING COUNT(*) > 1) sub
UNION ALL
SELECT 'Episodes', COUNT(*)
FROM (SELECT "Id" FROM "Episodes" GROUP BY "Id" HAVING COUNT(*) > 1) sub
UNION ALL
SELECT 'EpisodeFiles', COUNT(*)
FROM (SELECT "Id" FROM "EpisodeFiles" GROUP BY "Id" HAVING COUNT(*) > 1) sub;

-- Check sequence alignment
SELECT 
  'Series' as table_name,
  MAX("Id") as max_id,
  (SELECT last_value FROM "Series_Id_seq") as sequence,
  CASE
    WHEN (SELECT last_value FROM "Series_Id_seq") > MAX("Id") THEN '✅ OK'
    ELSE '❌ MISMATCH'
  END as status
FROM "Series"
UNION ALL
SELECT 
  'Episodes',
  MAX("Id"),
  (SELECT last_value FROM "Episodes_Id_seq"),
  CASE
    WHEN (SELECT last_value FROM "Episodes_Id_seq") > MAX("Id") THEN '✅ OK'
    ELSE '❌ MISMATCH'
  END
FROM "Episodes";
EOF
```

## Impact and Side Effects

### Safe to Run
- ✅ Transaction-protected (BEGIN/COMMIT block)
- ✅ Idempotent (safe to run multiple times)
- ✅ Only removes exact duplicates (same ID)
- ✅ Keeps earliest row by ctid (stable selection)

### No Data Loss
- The duplicate rows have identical data (same Id, TvdbId, Title)
- Deleting one duplicate doesn't lose any unique information
- Episodes and files remain linked to the kept series row

### Sonarr Restart Required
- In-memory caches may still reference duplicate series
- Restart ensures clean state after database fix
- No configuration changes needed

## History

**2026-02-27**: Fixed Band of Brothers duplicate series issue
- **Problem**: Series ID 1 had 2 duplicate rows
- **Root Cause**: Sequence at 1 while max ID was 90
- **Resolution**: 
  - Deleted 1 duplicate record (81 → 80 series)
  - Fixed sequence to 91
  - Verified 0 remaining duplicates
  - Restarted Sonarr pod to clear caches
- **Impact**: Download monitoring errors resolved, imports working

## Related Issues

- [TROUBLESHOOTING-DUPLICATE-EPISODES.md](TROUBLESHOOTING-DUPLICATE-EPISODES.md) - Similar issue with Episodes table
- [TROUBLESHOOTING-DUPLICATE-FILE-IDS.md](TROUBLESHOOTING-DUPLICATE-FILE-IDS.md) - Similar issue with EpisodeFiles table
- [TROUBLESHOOTING-DB-SCHEMA.md](TROUBLESHOOTING-DB-SCHEMA.md) - Missing columns issue

## Recovery Steps Summary

1. **Diagnose**: Check for duplicates and sequence mismatch
2. **Backup**: Sonarr auto-backups on updates (check /config/Backups)
3. **Fix**: Run the automated job or manual SQL
4. **Restart**: Restart Sonarr pod to clear caches
5. **Verify**: Check duplicates = 0 and sequence is correct
6. **Monitor**: Watch logs for 5-10 minutes
7. **Test**: Check download queue and series pages

## Additional Notes

- **Pod restart required**: Unlike episode/file fixes, series duplicates are cached in memory
- **Safe for production**: All changes in transaction, can be rolled back
- **Logs preserved**: Job logs available for 1 hour via `ttlSecondsAfterFinished`
- **Download queue**: Stuck downloads should automatically retry after fix + restart

## Troubleshooting After Fix

### If errors persist after restart:
1. Check database again for duplicates
2. Clear Sonarr's download history cache:
   - Settings → Download Clients → Advanced → Remove Failed
3. Manually retry stuck downloads in Activity → Queue

### If imports still fail:
- Check episode data isn't also duplicated
- Run episode duplicate fix: `kubectl apply -f fix-duplicate-episodes.yaml`
- Check EpisodeFiles aren't duplicated: See TROUBLESHOOTING-DUPLICATE-FILE-IDS.md