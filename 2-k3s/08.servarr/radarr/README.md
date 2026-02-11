# Radarr Database Maintenance Tools

This directory contains tools and jobs for maintaining Radarr's PostgreSQL database health.

## Quick Start

### Check Database Health
```bash
./check-database-health.sh
```

This will check for:
- Duplicate IDs in Movies and MovieFiles tables
- Sequence misalignment (common after database restores)
- Overall database statistics

### Fix Issues

If the health check finds problems, apply the appropriate fix:

```bash
# Fix duplicate Movie IDs
kubectl apply -f fix-duplicate-movies.yaml

# Fix duplicate MovieFile IDs
kubectl apply -f fix-duplicate-moviefiles.yaml
```

**After any fix, restart Radarr:**
```bash
kubectl rollout restart deployment/radarr -n servarr
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
✅ Movies: No duplicates found
✅ MovieFiles: No duplicates found
✅ Movies: Sequence OK (max_id=118, sequence=148)
✅ MovieFiles: Sequence OK (max_id=469, sequence=469)

Database Statistics:
   Movies: 114
   MovieFiles: 104

✅ Database is HEALTHY
```

### 2. Fix Jobs

#### fix-duplicate-movies.yaml
Fixes duplicate Movie IDs in the database.

**What it does**:
1. Identifies all duplicate Movie IDs
2. Keeps first occurrence (by ctid) of each duplicate
3. Deletes all other duplicates
4. Fixes the `Movies_Id_seq` sequence
5. Verifies no duplicates remain

**Apply**:
```bash
kubectl apply -f fix-duplicate-movies.yaml
kubectl logs -n servarr job/fix-radarr-duplicate-movies -f
```

#### fix-duplicate-moviefiles.yaml
Fixes duplicate MovieFile IDs in the database.

**What it does**:
1. Identifies all duplicate MovieFile IDs
2. Keeps first occurrence (by ctid) of each duplicate
3. Deletes all other duplicates
4. Fixes the `MovieFiles_Id_seq` sequence
5. Verifies no duplicates remain

**Apply**:
```bash
kubectl apply -f fix-duplicate-moviefiles.yaml
kubectl logs -n servarr job/fix-radarr-duplicate-moviefiles -f
```

## Common Issues

### MultipleMoviesFoundException
**Symptom**: 
```
NzbDrone.Core.Movies.MultipleMoviesFoundException: Expected one movie, but found 2
```

**Cause**: Duplicate Movie IDs in database

**Fix**: 
```bash
kubectl apply -f fix-duplicate-movies.yaml
kubectl rollout restart deployment/radarr -n servarr
```

### Sequence contains more than one element
**Symptom**:
```
System.InvalidOperationException: Sequence contains more than one element
```

**Cause**: Duplicate MovieFile IDs in database

**Fix**:
```bash
kubectl apply -f fix-duplicate-moviefiles.yaml
kubectl rollout restart deployment/radarr -n servarr
```

### Import fails with "movie already exists"
**Symptom**:
```
Import failed: Movie already exists in database
```

**Cause**: Could be duplicate Movie IDs or sequence mismatch

**Fix**:
```bash
./check-database-health.sh
# Apply suggested fixes
kubectl rollout restart deployment/radarr -n servarr
```

## When to Use These Tools

### After Database Restores
Always check and fix sequences after restoring from backup:
```bash
./check-database-health.sh

# If issues found, apply fixes
kubectl apply -f fix-duplicate-movies.yaml
kubectl apply -f fix-duplicate-moviefiles.yaml

# Restart Radarr
kubectl rollout restart deployment/radarr -n servarr
```

### Regular Maintenance
Run health checks weekly:
```bash
# Add to cron
0 2 * * 0 /path/to/check-database-health.sh
```

### Before Major Updates
Check database health before Radarr version upgrades:
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
- ✅ Radarr auto-backups before database changes

## Database Access

The tools automatically retrieve credentials from Kubernetes secrets:
```bash
RADARR_PW=$(kubectl get secret servarr-postgres -n servarr \
  -o jsonpath='{.data.radarr-password}' | base64 -d)
```

**Database Details**:
- Host: 192.168.10.105
- Port: 5432
- Database: radarr-main
- User: radarr
- Password: (stored in `servarr-postgres` secret)

## Manual Database Access

If you need to access the database directly:
```bash
RADARR_PW=$(kubectl get secret servarr-postgres -n servarr \
  -o jsonpath='{.data.radarr-password}' | base64 -d)

PGPASSWORD="${RADARR_PW}" psql -h 192.168.10.105 -U radarr -d radarr-main
```

### Useful Queries

**Check for duplicates**:
```sql
-- Movies duplicates
SELECT "Id", COUNT(*) FROM "Movies" 
GROUP BY "Id" HAVING COUNT(*) > 1;

-- MovieFiles duplicates
SELECT "Id", COUNT(*) FROM "MovieFiles" 
GROUP BY "Id" HAVING COUNT(*) > 1;
```

**Check sequence alignment**:
```sql
-- Movies
SELECT MAX("Id") as max_id, 
       (SELECT last_value FROM "Movies_Id_seq") as sequence 
FROM "Movies";

-- MovieFiles
SELECT MAX("Id") as max_id, 
       (SELECT last_value FROM "MovieFiles_Id_seq") as sequence 
FROM "MovieFiles";
```

**Find specific movie**:
```sql
-- Search by title
SELECT "Id", "TmdbId", "Title", "Year" 
FROM "Movies" 
WHERE "Title" ILIKE '%search term%';

-- Find movie's file
SELECT m."Title", mf."RelativePath", mf."Size", mf."DateAdded"
FROM "Movies" m
LEFT JOIN "MovieFiles" mf ON m."MovieFileId" = mf."Id"
WHERE m."Id" = 1;
```

## Troubleshooting

### Job Won't Start
```bash
# Check if previous job exists
kubectl get jobs -n servarr | grep fix-radarr

# Delete old job
kubectl delete job fix-radarr-duplicate-movies -n servarr
```

### Can't Connect to Database
```bash
# Verify postgres is running
kubectl get pods -n postgres

# Test connection
RADARR_PW=$(kubectl get secret servarr-postgres -n servarr \
  -o jsonpath='{.data.radarr-password}' | base64 -d)
PGPASSWORD="${RADARR_PW}" psql -h 192.168.10.105 -U radarr -d radarr-main -c "SELECT 1;"
```

### Still Getting Errors After Fix
1. Verify job completed successfully
2. Check database has no remaining duplicates
3. Restart Radarr to clear caches:
   ```bash
   kubectl rollout restart deployment/radarr -n servarr
   ```
4. Wait 30 seconds for pod to be ready
5. Check logs for new errors:
   ```bash
   kubectl logs -n servarr deployment/radarr --tail=100
   ```

## Related Documentation

- `../sonarr/README.md` - Similar tools for TV shows (Sonarr)
- `../sonarr2/README.md` - Similar tools for anime (Sonarr2)
- `../TROUBLESHOOTING-DUPLICATE-SERIES.md` - Series duplicate fixes (applies to Movies too)
- `../TROUBLESHOOTING-DB-SCHEMA.md` - Schema issues

## Differences from Sonarr

Radarr is for movies, so the database structure differs:
- **Movies** table (instead of Series)
- **MovieFiles** table (instead of EpisodeFiles)
- No Episodes table (movies don't have episodes)
- Same fix patterns: All tools work identically
- Same corruption causes: Sequence misalignment after restores

## History

- **2026-02-26**: Created database fix tooling (mirrored from Sonarr)
  - All tools tested and verified working
  - Database currently healthy (no duplicates)
  - Movies: 114, MovieFiles: 104

## Support

If you encounter issues not covered here:
1. Run health check: `./check-database-health.sh`
2. Check Radarr logs: `kubectl logs -n servarr deployment/radarr --tail=100`
3. Review troubleshooting docs in parent directory
4. Check Sonarr documentation (same principles apply)
5. Check `.history/` directory for similar past issues

## Best Practices

### Regular Monitoring
```bash
# Create monitoring script
cat > /usr/local/bin/check-arr-health.sh << 'EOF'
#!/bin/bash
cd /path/to/k3s-swarm-proxmox/2-k3s/08.servarr
echo "=== Sonarr ==="
./sonarr/check-database-health.sh
echo ""
echo "=== Sonarr2 ==="
./sonarr2/check-database-health.sh
echo ""
echo "=== Radarr ==="
./radarr/check-database-health.sh
EOF

chmod +x /usr/local/bin/check-arr-health.sh

# Add to cron (weekly Sunday 2 AM)
echo "0 2 * * 0 /usr/local/bin/check-arr-health.sh | mail -s 'Arr Database Health' admin@example.com" | crontab -
```

### Before Backups
Always verify database health before backups:
```bash
./check-database-health.sh
# If healthy, proceed with backup
# If issues, fix first, then backup
```

### After Migrations
After migrating Radarr to new hardware or restoring:
```bash
# Always check and fix
./check-database-health.sh
kubectl apply -f fix-duplicate-movies.yaml
kubectl apply -f fix-duplicate-moviefiles.yaml
kubectl rollout restart deployment/radarr -n servarr
```
