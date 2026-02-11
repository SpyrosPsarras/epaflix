# Duplicate EpisodeFile IDs - Database Corruption Fix

## Problem: "Sequence contains more than one element" Error

### Symptoms
- Sonarr shows endless import errors:
  ```
  System.InvalidOperationException: Sequence contains more than one element
  at NzbDrone.Core.MediaFiles.EpisodeImport.Specifications.UpgradeSpecification.IsSatisfiedBy
  ```
- Queue items keep coming back after deletion (without `removeFromClient=true`)
- Downloads complete but won't import
- Specific to certain episodes/series

### Root Cause
**Database corruption**: Multiple rows in the `EpisodeFiles` table with the same primary key `Id`.

This typically happens after:
- Database restore from backup with sequence mismatch
- Incomplete database migration
- Manual database manipulation gone wrong

### Example Found (Feb 22, 2026)
Two files had ID = 1:
1. MasterChef S12E01 (SeriesId 89) - older, Jan 25
2. Shrinking S03E04 (SeriesId 18) - newer, Feb 22

When Sonarr tried to load the episode file, it got 2 results instead of 1 → "Sequence contains more than one element"

## Diagnosis Steps

### 1. Identify the problematic series/episode from logs
```bash
kubectl logs -n servarr deployment/sonarr --tail=100 | grep "InvalidOperationException"
# Look for the file path or series name
```

### 2. Find the series and episode IDs
```bash
PGPASSWORD="<password>" psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
SELECT "Id", "Title" FROM "Series" WHERE "Title" ILIKE '%SeriesName%';
SELECT "Id", "SeasonNumber", "EpisodeNumber", "EpisodeFileId"
FROM "Episodes"
WHERE "SeriesId" = <SeriesId> AND "SeasonNumber" = X AND "EpisodeNumber" = Y;
EOF
```

### 3. Check for duplicate file IDs
```bash
PGPASSWORD="<password>" psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
-- Find ALL duplicate IDs in EpisodeFiles table
SELECT "Id", COUNT(*)
FROM "EpisodeFiles"
GROUP BY "Id"
HAVING COUNT(*) > 1
ORDER BY "Id";

-- Show details of duplicates
SELECT "Id", "SeriesId", "SeasonNumber", "RelativePath", "DateAdded"
FROM "EpisodeFiles"
WHERE "Id" IN (
    SELECT "Id" FROM "EpisodeFiles"
    GROUP BY "Id" HAVING COUNT(*) > 1
)
ORDER BY "Id", "DateAdded";
EOF
```

### 4. Find the maximum ID to know where to start reassignment
```bash
PGPASSWORD="<password>" psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
SELECT MAX("Id") FROM "EpisodeFiles";
EOF
```

## Fix Script

### Automated Fix for All Duplicates
```bash
#!/bin/bash
# Fix all duplicate EpisodeFile IDs in Sonarr database

PGPASSWORD=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.sonarr-password}' | base64 -d)
DB_HOST="192.168.10.105"

echo "🔍 Checking for duplicate EpisodeFile IDs..."

PGPASSWORD="${PGPASSWORD}" psql -h "${DB_HOST}" -U sonarr -d sonarr-main << 'EOF'
BEGIN;

-- Get the current max ID
DO $$
DECLARE
    max_id INT;
    new_id INT;
    dup_record RECORD;
    keep_record RECORD;
BEGIN
    SELECT MAX("Id") INTO max_id FROM "EpisodeFiles";
    new_id := max_id;

    -- For each duplicate ID
    FOR dup_record IN
        SELECT "Id"
        FROM "EpisodeFiles"
        GROUP BY "Id"
        HAVING COUNT(*) > 1
        ORDER BY "Id"
    LOOP
        RAISE NOTICE 'Found duplicate ID: %', dup_record."Id";

        -- Keep the oldest file with this ID, reassign newer ones
        SELECT * INTO keep_record
        FROM "EpisodeFiles"
        WHERE "Id" = dup_record."Id"
        ORDER BY "DateAdded" ASC
        LIMIT 1;

        RAISE NOTICE '  Keeping: SeriesId=%, Path=%', keep_record."SeriesId", keep_record."RelativePath";

        -- Reassign all other files with this ID
        FOR dup_file IN
            SELECT * FROM "EpisodeFiles"
            WHERE "Id" = dup_record."Id"
            AND ("SeriesId" != keep_record."SeriesId" OR "RelativePath" != keep_record."RelativePath")
        LOOP
            new_id := new_id + 1;
            RAISE NOTICE '  Reassigning: SeriesId=% to new Id=%', dup_file."SeriesId", new_id;

            -- Update the file ID
            UPDATE "EpisodeFiles"
            SET "Id" = new_id
            WHERE "Id" = dup_record."Id"
              AND "SeriesId" = dup_file."SeriesId"
              AND "RelativePath" = dup_file."RelativePath";

            -- Update episodes that reference this file
            UPDATE "Episodes"
            SET "EpisodeFileId" = new_id
            WHERE "EpisodeFileId" = dup_record."Id"
              AND "SeriesId" = dup_file."SeriesId";
        END LOOP;
    END LOOP;

    -- Update the sequence
    PERFORM setval('"EpisodeFiles_Id_seq"', new_id, true);
    RAISE NOTICE 'Updated sequence to: %', new_id;
END $$;

-- Verify no duplicates remain
SELECT 'Remaining duplicates (should be empty):';
SELECT "Id", COUNT(*)
FROM "EpisodeFiles"
GROUP BY "Id"
HAVING COUNT(*) > 1;

COMMIT;
EOF

echo "✅ Done! Check output above for details."
```

### Manual Fix (Single Duplicate)
If you have only one duplicate ID (e.g., ID 1):

```bash
PGPASSWORD="<password>" psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
BEGIN;

-- Get max ID
SELECT MAX("Id") FROM "EpisodeFiles";  -- e.g., returns 2514

-- Reassign the NEWER file to next available ID
UPDATE "EpisodeFiles"
SET "Id" = 2515  -- max + 1
WHERE "Id" = 1 AND "SeriesId" = 18;  -- Specify which duplicate to move

-- Update episodes that were linked to this file
UPDATE "Episodes"
SET "EpisodeFileId" = 2515
WHERE "EpisodeFileId" = 1 AND "SeriesId" = 18;

-- Update sequence
SELECT setval('"EpisodeFiles_Id_seq"', 2515, true);

-- Verify
SELECT "Id", COUNT(*) FROM "EpisodeFiles" GROUP BY "Id" HAVING COUNT(*) > 1;

COMMIT;
EOF
```

## Verification

### 1. Check no duplicate IDs remain
```bash
PGPASSWORD="<password>" psql -h 192.168.10.105 -U sonarr -d sonarr-main \
  -c "SELECT \"Id\", COUNT(*) FROM \"EpisodeFiles\" GROUP BY \"Id\" HAVING COUNT(*) > 1;"
# Should return 0 rows
```

### 2. Check episode linkage
```bash
PGPASSWORD="<password>" psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
SELECT e."Id", e."SeriesId", e."SeasonNumber", e."EpisodeNumber",
       e."EpisodeFileId", ef."RelativePath"
FROM "Episodes" e
LEFT JOIN "EpisodeFiles" ef ON e."EpisodeFileId" = ef."Id"
WHERE e."Id" = <EpisodeId>;
# Should show only ONE file path
EOF
```

### 3. Monitor Sonarr logs
```bash
kubectl logs -n servarr deployment/sonarr -f | grep -i "InvalidOperationException\|Sequence contains"
# Should see no more errors
```

### 4. Check queue
```bash
curl -s 'https://sonarr.epaflix.com/api/v3/queue' \
  -H 'X-Api-Key: <API_KEY>' | jq '.records | length'
# Failed imports should clear and not come back
```

## Prevention

### After Database Restores
1. Always check for duplicate IDs after restoring from backup
2. Verify sequences are up to date:
   ```sql
   SELECT MAX("Id") FROM "EpisodeFiles";
   SELECT last_value FROM "EpisodeFiles_Id_seq";
   -- These should match or sequence should be higher
   ```

### Regular Health Checks
Add to maintenance scripts:
```bash
# Check for duplicate primary keys
PGPASSWORD="..." psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
SELECT 'EpisodeFiles duplicates:' as check, "Id", COUNT(*)
FROM "EpisodeFiles" GROUP BY "Id" HAVING COUNT(*) > 1;

SELECT 'Episodes duplicates:' as check, "Id", COUNT(*)
FROM "Episodes" GROUP BY "Id" HAVING COUNT(*) > 1;

SELECT 'Series duplicates:' as check, "Id", COUNT(*)
FROM "Series" GROUP BY "Id" HAVING COUNT(*) > 1;
EOF
```

## History

**2026-02-22**: Fixed duplicate ID 1 in Sonarr `EpisodeFiles` table
- MasterChef S12E01 (kept at ID 1)
- Shrinking S03E04 (moved to ID 2515)
- Resolution: Queue cleared, import errors stopped

## Related Issues

- [TROUBLESHOOTING-DB-SCHEMA.md](TROUBLESHOOTING-DB-SCHEMA.md) - Missing columns issue
- [`.history/2026-02-21-sonarr-history-schema-fix.log`](../../.history/2026-02-21-sonarr-history-schema-fix.log) - Previous database fixes
