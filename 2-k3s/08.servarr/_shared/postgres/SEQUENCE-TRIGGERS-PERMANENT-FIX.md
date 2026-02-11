# PostgreSQL Sequence Auto-Sync Triggers - Permanent Fix

**Date Implemented:** 2026-01-27
**Problem:** PostgreSQL sequences becoming out of sync with actual max IDs, causing "duplicate key" errors
**Root Cause:** Applications inserting rows with explicit ID values (during bulk imports, restores, or API operations) don't automatically advance sequences

## Solution Implemented

Created database triggers that automatically synchronize sequences after every INSERT operation.

### Trigger Function

```sql
CREATE OR REPLACE FUNCTION sync_sequence_after_insert()
RETURNS TRIGGER AS $$
DECLARE
  seq_name TEXT;
  max_val BIGINT;
BEGIN
  seq_name := pg_get_serial_sequence(TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, 'Id');
  IF seq_name IS NOT NULL THEN
    EXECUTE format('SELECT COALESCE(MAX("Id"), 1) FROM %I.%I', TG_TABLE_SCHEMA, TG_TABLE_NAME) INTO max_val;
    PERFORM setval(seq_name, max_val);
  END IF;
  RETURN NULL;
END;
$$ LANGUAGE plpgsql;
```

### Triggers Applied

**sonarr-main (7 triggers):**
- Series, Episodes, EpisodeFiles, History, SubtitleFiles, DownloadHistory, ExtraFiles

**sonarr2-main (6 triggers):**
- Series, Episodes, EpisodeFiles, History, SubtitleFiles, DownloadHistory

**radarr-main (9 triggers):**
- Collections, Credits, MovieMetadata, Movies, AlternativeTitles, MovieFiles, MovieTranslations, History, DownloadHistory

**bazarr-main (2 triggers):**
- table_history_movie, table_history

**jellyseerr:**
- Trigger function created, apply to specific tables as needed (uses lowercase 'id' column)

## Backup Location

Schema backups taken before changes:
```
/workspaces/01-manual installation/manifests/08.servarr/_backups/postgres-schemas-20260127-155706/
```

## How It Works

1. **AFTER INSERT trigger** fires after any row(s) are inserted into a table
2. Trigger retrieves the sequence name associated with the table's Id column
3. Queries the current MAX(Id) from the table
4. Updates the sequence to match the max value using `setval()`
5. This ensures the next `nextval()` call returns `MAX(Id) + 1`

## Performance Impact

- **Minimal**: Triggers execute only ONCE per statement (not per row) using `FOR EACH STATEMENT`
- Single SELECT MAX() and setval() operation per INSERT statement
- No impact on SELECT queries or other operations

## Verification

Check triggers exist:
```bash
kubectl exec postgres-cluster-1 -n postgres-system -- \
  psql -U postgres -d sonarr-main \
  -c "SELECT tgname, tgrelid::regclass FROM pg_trigger WHERE tgname LIKE 'sync_%';"
```

## Rollback (if needed)

Drop all triggers:
```sql
-- For each database, drop triggers:
DROP TRIGGER IF EXISTS sync_series_seq ON "Series";
DROP TRIGGER IF EXISTS sync_episodes_seq ON "Episodes";
-- ... repeat for all triggers

-- Drop the function:
DROP FUNCTION IF EXISTS sync_sequence_after_insert();
```

Restore from backup:
```bash
kubectl exec -i postgres-cluster-1 -n postgres-system -- \
  psql -U postgres -d sonarr-main < \
  /workspaces/01-manual\ installation/manifests/08.servarr/_backups/postgres-schemas-20260127-155706/sonarr-main-schema.sql
```

## Alternative Solutions Considered

1. **Scheduled CronJob** - Periodic sequence fixes (rejected: doesn't prevent errors, only cleans up)
2. **Application Code Changes** - Modify apps to never use explicit IDs (rejected: requires forking/patching upstream)
3. **GENERATED ALWAYS AS IDENTITY** - Migrate to new identity columns (rejected: requires schema migration, app may not support)

## Notes

- This is a **permanent solution** that prevents sequence drift at the database level
- Works transparently - applications don't need any changes
- Survives database restarts and backups/restores
- Applies to all future data migrations and bulk imports
