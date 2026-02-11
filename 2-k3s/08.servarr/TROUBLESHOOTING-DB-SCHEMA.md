0000# Servarr Database Schema Issues

## History.DownloadId Missing Column Error

### Symptoms
- Sonarr/Radarr API calls fail with error:
  ```
  42703: column History.DownloadId does not exist
  ```
- Queue operations (remove, blocklist) fail
- Manual import fails with similar error
- May also see `column "Languages" does not exist` error

### Root Cause
The PostgreSQL `History` table is missing required columns (`DownloadId` and/or `Languages`). This typically happens after:
- Database restored from an old backup (before migration/schema fixes)
- Incomplete database migration from SQLite to PostgreSQL
- Database corruption

### Expected Schema

**Sonarr & Sonarr2** (10 columns):
```
Id, EpisodeId, SeriesId, SourceTitle, Date, Quality, Data, EventType, DownloadId, Languages
```

**Radarr** (9 columns):
```
Id, SourceTitle, Date, Quality, Data, EventType, DownloadId, MovieId, Languages
```

### Quick Fix

Run the automated fix script:
```bash
/home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/_shared/scripts/fix-all-history-schemas.sh
```

This script:
- ✅ Checks all Servarr databases (Sonarr, Sonarr2, Radarr)
- ✅ Adds missing columns if needed
- ✅ Is idempotent (safe to run multiple times)
- ✅ Shows current status of each database

### Manual Fix (Single App)

For Sonarr only:
```bash
kubectl apply -f /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/sonarr/fix-history-schema.yaml
```

For manual PostgreSQL fix:
```bash
# Get credentials
SONARR_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.sonarr-password}' | base64 -d)

# Connect and fix
PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main << EOF
ALTER TABLE "History" ADD COLUMN IF NOT EXISTS "DownloadId" text;
ALTER TABLE "History" ADD COLUMN IF NOT EXISTS "Languages" text DEFAULT '[]'::text NOT NULL;
EOF
```

### Verification

Check the current schema:
```bash
# For Sonarr
SONARR_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.sonarr-password}' | base64 -d)
PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main \
  -c "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'History' ORDER BY ordinal_position;"
```

Expected output should show:
- **Sonarr/Sonarr2**: 10 columns including `DownloadId` and `Languages`
- **Radarr**: 9 columns including `DownloadId` and `Languages`

### After Fix

1. **No need to restart pods** - Changes take effect immediately
2. **Test the failing operation** - Retry queue removal, manual import, etc.
3. **Check logs** - Verify no more schema errors:
   ```bash
   kubectl logs -n servarr deployment/sonarr -f | grep -i "does not exist"
   ```

### Prevention

When restoring from backups:
1. Check PostgreSQL database dates before restoring
2. Run the fix-all-history-schemas.sh script after any database restore
3. Consider this fix as part of post-restore procedures

### History

This issue has occurred:
- **2026-02-21**: Initial discovery and fix
- **2026-02-22**: Reoccurred after database restore, created automated fix script

See [`.history/2026-02-21-sonarr-history-schema-fix.log`](../../../.history/2026-02-21-sonarr-history-schema-fix.log) for detailed history.

### Related Files

- [`fix-all-history-schemas.sh`](_shared/scripts/fix-all-history-schemas.sh) - Automated fix for all apps
- [`sonarr/fix-history-schema.yaml`](sonarr/fix-history-schema.yaml) - Kubernetes Job for Sonarr only
- [`.history/2026-02-21-sonarr-history-schema-fix.log`](../../.history/2026-02-21-sonarr-history-schema-fix.log) - Detailed fix history

---

## SceneMappings.Type Missing Column Error

### Symptoms
- Sonarr fails during import or scene-mapping refresh with error:
  ```
  42703: column SceneMappings.Type does not exist
  ```
- Error originates in `SceneMappingService.UpdateMappings()`
- Imports may silently fail or error out

### Root Cause
The PostgreSQL `SceneMappings` table is missing one or more columns (`Type`, `SceneOrigin`, `SearchMode`, `Comment`, etc.) that Sonarr requires. Happens after:
- Database restored from an older backup that predates the full column set
- Incomplete SQLite → PostgreSQL migration

### Expected Schema

**Sonarr & Sonarr2** (12 columns):
```
Id, TvdbId, SeasonNumber, SearchTerm, ParseTerm, Title, Type,
SceneSeasonNumber, FilterRegex, SceneOrigin, SearchMode, Comment
```

### Quick Fix

Apply the Kubernetes Job (idempotent — safe to re-run):

```bash
# Sonarr
kubectl apply -f 2-k3s/08.servarr/sonarr/fix-scene-mappings-schema.yaml

# Sonarr2 (apply proactively to keep both in sync)
kubectl apply -f 2-k3s/08.servarr/sonarr2/fix-scene-mappings-schema.yaml

# Wait for completion
kubectl wait --for=condition=complete --timeout=60s \
  job/sonarr-fix-scene-mappings-schema \
  job/sonarr2-fix-scene-mappings-schema \
  -n servarr
```

### Manual Fix

```bash
# Get credentials
SONARR_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.sonarr-password}' | base64 -d)

# Connect and fix
PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main << 'EOF'
ALTER TABLE "SceneMappings" ADD COLUMN IF NOT EXISTS "Type" text;
ALTER TABLE "SceneMappings" ADD COLUMN IF NOT EXISTS "SeasonNumber" integer;
ALTER TABLE "SceneMappings" ADD COLUMN IF NOT EXISTS "SceneSeasonNumber" integer;
ALTER TABLE "SceneMappings" ADD COLUMN IF NOT EXISTS "FilterRegex" text;
ALTER TABLE "SceneMappings" ADD COLUMN IF NOT EXISTS "SceneOrigin" text;
ALTER TABLE "SceneMappings" ADD COLUMN IF NOT EXISTS "SearchMode" integer;
ALTER TABLE "SceneMappings" ADD COLUMN IF NOT EXISTS "Comment" text;
ALTER TABLE "SceneMappings" ADD COLUMN IF NOT EXISTS "Title" text;
EOF
```

### Verification

```bash
SONARR_PW=$(kubectl get secret servarr-postgres -n servarr -o jsonpath='{.data.sonarr-password}' | base64 -d)
PGPASSWORD="${SONARR_PW}" psql -h 192.168.10.105 -U sonarr -d sonarr-main \
  -c "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'SceneMappings' ORDER BY ordinal_position;"
```

Expected: **12 columns** including `Type`, `SceneOrigin`, `SearchMode`, `Comment`.

### After Fix

1. **No restart needed** — column additions take effect immediately
2. **Retry the failing import** — the `SceneMappingService` will re-run on next trigger
3. **Verify logs are clean**:
   ```bash
   kubectl logs -n servarr deployment/sonarr --since=2m | grep -i "does not exist"
   ```

### History

This issue has occurred:
- **2026-02-26**: Discovered during import; `Type` column missing from live DB despite being present in the Jan 2026 schema backup; fixed with Kubernetes Job

### Related Files

- [`sonarr/fix-scene-mappings-schema.yaml`](sonarr/fix-scene-mappings-schema.yaml) - Kubernetes Job for Sonarr
- [`sonarr2/fix-scene-mappings-schema.yaml`](sonarr2/fix-scene-mappings-schema.yaml) - Kubernetes Job for Sonarr2

---

### Other Database Issues

See also:
- [RECOVERY.md](../RECOVERY.md) - General cluster recovery procedures
- [README.md](README.md) - Servarr setup and PostgreSQL configuration
