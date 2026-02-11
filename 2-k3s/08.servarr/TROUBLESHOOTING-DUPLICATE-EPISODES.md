# Duplicate Episode IDs - Database Corruption Fix

## Problem: "Expected query to return X rows but returned Y" Error

### Symptoms
- Sonarr API errors in logs:
  ```
  System.ApplicationException: Expected query to return 11 rows but returned 13
  at NzbDrone.Core.Datastore.BasicRepository`1.Get(IEnumerable`1 ids)
  at NzbDrone.Core.Tv.EpisodeService.GetEpisodes(IEnumerable`1 ids)
  ```
- UI may show errors when browsing series
- Episode API calls fail
- Series pages may not load correctly

### Root Cause
**Database corruption**: Multiple rows in the `Episodes` table with the same primary key `Id`.

This typically happens when:
- The `Episodes_Id_seq` sequence gets reset or corrupted (e.g., to 115 when max ID is 4738)
- Database restored from backup without fixing sequences
- Manual database manipulation
- Incomplete database migration

When the sequence is incorrect, new episodes get assigned IDs that already exist, creating duplicates.

## Diagnosis Steps

### 1. Check for duplicate episode IDs
```bash
SONARR_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.sonarr-password}' | base64 -d)

PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
-- Find all duplicate episode IDs
SELECT "Id", COUNT(*)
FROM "Episodes"
GROUP BY "Id"
HAVING COUNT(*) > 1
ORDER BY "Id";
EOF
```

### 2. Check sequence mismatch
```bash
PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
SELECT 
  COUNT(*) as total_episodes,
  MAX("Id") as max_episode_id,
  (SELECT last_value FROM "Episodes_Id_seq") as sequence_value
FROM "Episodes";
EOF
```

**Red flag**: If `sequence_value` is much lower than `max_episode_id`, you have a problem!

Example of corrupted state:
- total_episodes: 4219
- max_episode_id: 4738
- sequence_value: 115 ❌ (should be > 4738)

### 3. View duplicate details
```bash
PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
-- Show details of duplicates
SELECT e."Id", e."SeriesId", s."Title" as "SeriesTitle",
       e."SeasonNumber", e."EpisodeNumber", e."Title" as "EpisodeTitle"
FROM "Episodes" e
JOIN "Series" s ON e."SeriesId" = s."Id"
WHERE e."Id" IN (
  SELECT "Id" FROM "Episodes" GROUP BY "Id" HAVING COUNT(*) > 1
)
ORDER BY e."Id", e."SeriesId";
EOF
```

## Automated Fix

### Quick Fix (Recommended)
```bash
kubectl apply -f /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/sonarr/fix-duplicate-episodes.yaml
```

Wait for job completion and check logs:
```bash
kubectl logs -n servarr job/fix-sonarr-duplicate-episodes -f
```

The job will:
1. ✅ Identify all duplicate episode IDs
2. ✅ Keep the first occurrence (by ctid) of each duplicate
3. ✅ Delete all other duplicates
4. ✅ Fix the sequence to MAX(Id) + 1
5. ✅ Verify no duplicates remain

### Manual Fix (If needed)

```bash
SONARR_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.sonarr-password}' | base64 -d)

PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
BEGIN;

-- Create temp table with one episode per unique ID (keep earliest by ctid)
CREATE TEMP TABLE episodes_to_keep AS
SELECT DISTINCT ON ("Id") 
  "Id",
  ctid as keep_ctid
FROM "Episodes"
ORDER BY "Id", ctid;

-- Show counts
SELECT COUNT(*) as total FROM "Episodes";
SELECT COUNT(*) as to_keep FROM episodes_to_keep;

-- Delete duplicates
DELETE FROM "Episodes" e
WHERE NOT EXISTS (
  SELECT 1 FROM episodes_to_keep k
  WHERE k."Id" = e."Id" AND k.keep_ctid = e.ctid
);

-- Fix sequence
SELECT setval('"Episodes_Id_seq"', (SELECT MAX("Id") FROM "Episodes") + 1, false);

-- Verify
SELECT COUNT(*) as remaining_duplicates
FROM (SELECT "Id" FROM "Episodes" GROUP BY "Id" HAVING COUNT(*) > 1) sub;

COMMIT;
EOF
```

## Verification

### 1. Check no duplicates remain
```bash
PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main \
  -c "SELECT COUNT(*) as duplicates FROM (SELECT \"Id\" FROM \"Episodes\" GROUP BY \"Id\" HAVING COUNT(*) > 1) sub;"
```
**Expected**: `0`

### 2. Verify sequence is correct
```bash
PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
SELECT 
  MAX("Id") as max_id,
  (SELECT last_value FROM "Episodes_Id_seq") as sequence
FROM "Episodes";
EOF
```
**Expected**: `sequence` should be > `max_id`

### 3. Check Sonarr logs
```bash
kubectl logs -n servarr deployment/sonarr --tail=50
```
**Expected**: No more "Expected query to return X rows but returned Y" errors

### 4. Test UI
- Browse to a series page that was previously failing
- Check that episode lists load correctly
- Verify no API errors in browser console

## Prevention

### After Database Restores
Always run after restoring from backup:
```bash
# Check and fix ALL sequences
kubectl apply -f /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/sonarr/fix-duplicate-episodes.yaml
```

### Regular Health Checks
Add to maintenance scripts:
```bash
#!/bin/bash
SONARR_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.sonarr-password}' | base64 -d)

echo "Checking Sonarr database health..."

PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
-- Check for duplicates in all critical tables
SELECT 'Episodes' as table_name, COUNT(*) as duplicate_ids
FROM (SELECT "Id" FROM "Episodes" GROUP BY "Id" HAVING COUNT(*) > 1) sub
UNION ALL
SELECT 'EpisodeFiles', COUNT(*)
FROM (SELECT "Id" FROM "EpisodeFiles" GROUP BY "Id" HAVING COUNT(*) > 1) sub
UNION ALL
SELECT 'Series', COUNT(*)
FROM (SELECT "Id" FROM "Series" GROUP BY "Id" HAVING COUNT(*) > 1) sub;

-- Check sequence alignment
SELECT 
  'Episodes' as table_name,
  MAX("Id") as max_id,
  (SELECT last_value FROM "Episodes_Id_seq") as sequence,
  CASE
    WHEN (SELECT last_value FROM "Episodes_Id_seq") > MAX("Id") THEN '✅ OK'
    ELSE '❌ MISMATCH'
  END as status
FROM "Episodes";
EOF
```

## History

**2026-02-24**: Fixed major duplicate episode issue
- **Problem**: 37 duplicate episode IDs found
- **Root Cause**: Sequence at 115 while max ID was 4738
- **Resolution**: 
  - Deleted 37 duplicate records
  - Fixed sequence to 4739
  - Verified 0 remaining duplicates
- **Impact**: Sonarr API errors resolved, UI working normally

## Related Issues

- [TROUBLESHOOTING-DUPLICATE-FILE-IDS.md](TROUBLESHOOTING-DUPLICATE-FILE-IDS.md) - Similar issue with EpisodeFiles table
- [TROUBLESHOOTING-DB-SCHEMA.md](TROUBLESHOOTING-DB-SCHEMA.md) - Missing columns issue

## Recovery Steps Summary

1. **Diagnose**: Check for duplicates and sequence mismatch
2. **Backup**: Always backup before fixes (Sonarr auto-backups on updates)
3. **Fix**: Run the automated job or manual SQL
4. **Verify**: Check duplicates = 0 and sequence is correct
5. **Monitor**: Watch logs for 5-10 minutes
6. **Test**: Browse UI and check functionality

## Additional Notes

- **No pod restart needed**: Database changes take effect immediately
- **Safe to run multiple times**: The fix is idempotent
- **Transaction protected**: All changes in BEGIN/COMMIT block
- **Logs preserved**: Job logs available for 1 hour via `ttlSecondsAfterFinished`
