# PostgreSQL Sequence Auto-Sync — Generic Edition

**Date Implemented:** 2026-05-01 (replaces 2026-01-27 version)
**Problem:** PostgreSQL sequences becoming out of sync with actual MAX(id), causing `duplicate key value violates unique constraint` errors on insert.
**Root Cause:** Late-January 2026 SQLite → Postgres migration of the servarr stack imported rows with explicit IDs but did not `setval()` the sequences afterwards. App-level inserts that subsequently called `nextval()` then collided with already-used IDs.

## Why the previous fix wasn't enough

The original `sync_sequence_after_insert()` function (Jan 2026) hardcoded the column name `'Id'` (case-sensitive). Consequences:
- Bazarr uses lowercase `id` → trigger silently no-op'd. Function existed in bazarr but produced zero effect.
- Bazarr's claimed 2 triggers (`table_history`, `table_history_movie`) did **not** actually exist in `pg_trigger`. Either never applied, or wiped by a later restore.
- Non-PK serial columns (`sonarrEpisodeId`, `radarrId`, `sonarrSeriesId`) were never covered.
- Tables that hadn't received an insert since 2026-01-27 (config tables seeded once at install — `Indexers`, `IndexerStatus`, `ScheduledTasks`, `QualityDefinitions`, etc.) still had sequences sitting at `1`.

The 2026-05-01 incident (`bazarr table_history_movie_pkey` UniqueViolation, seq=310 vs MAX=1447) made this visible. Audit showed 9 + 9 + 4 + 0 drifted sequences across sonarr / sonarr2 / radarr / bazarr.

## Current Solution

`sync-sequences.sql` in this directory installs:

1. A generic trigger function `sync_table_sequences()` that discovers every serial-backed column on the firing table at runtime via `pg_get_serial_sequence()` — no hardcoded column names, works for `Id`, `id`, `sonarrEpisodeId`, anything.
2. An auto-attach DO block that creates an `AFTER INSERT FOR EACH STATEMENT` trigger named `zz_sync_sequences` on every table in `public` that has at least one serial-backed column.
3. A one-shot catch-up that `setval`s every sequence to its column's current MAX, clearing all pre-existing drift.

### Trigger function

```sql
CREATE OR REPLACE FUNCTION sync_table_sequences()
RETURNS TRIGGER AS $$
DECLARE
    col_rec RECORD;
    seq_qualified TEXT;
    max_val BIGINT;
BEGIN
    FOR col_rec IN
        SELECT a.attname AS name
        FROM   pg_attribute a
        WHERE  a.attrelid = TG_RELID
          AND  a.attnum > 0
          AND  NOT a.attisdropped
          AND  pg_get_serial_sequence(
                   format('%I.%I', TG_TABLE_SCHEMA, TG_TABLE_NAME),
                   a.attname
               ) IS NOT NULL
    LOOP
        seq_qualified := pg_get_serial_sequence(
            format('%I.%I', TG_TABLE_SCHEMA, TG_TABLE_NAME),
            col_rec.name
        );
        EXECUTE format('SELECT COALESCE(MAX(%I), 0)::bigint FROM %I.%I',
                       col_rec.name, TG_TABLE_SCHEMA, TG_TABLE_NAME) INTO max_val;
        IF max_val > 0 THEN
            PERFORM setval(seq_qualified, max_val);
        END IF;
    END LOOP;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
```

### Coverage (post-2026-05-01 apply)

| DB           | Tables w/ trigger |
|--------------|-------------------|
| sonarr-main  | 37                |
| sonarr2-main | 37                |
| radarr-main  | 40                |
| bazarr-main  | 12                |

Compared to legacy: claimed 24 triggers, actual ~22 (bazarr's 2 didn't exist). New coverage: 126 across 4 DBs, all with dynamic column discovery.

## Apply

```bash
2-k3s/08.servarr/_shared/postgres/apply-sync-sequences.sh
```

The script reads connection details from the `servarr-postgres` secret in the `servarr` namespace, then runs `sync-sequences.sql` against `sonarr-main`, `sonarr2-main`, `radarr-main`, `bazarr-main`. Idempotent — safe to re-run.

## Verification

The standing weekly audit (`2-k3s/maintenance/postgres-sequence-audit-cronjob.yaml`) reports any sequence whose `last_value < MAX(column)`. Expected output: `drift_count=0` for every DB.

Trigger one immediately:
```bash
kubectl create job -n servarr sequence-audit-now --from=cronjob/postgres-sequence-audit
kubectl logs  -n servarr -f       job/sequence-audit-now
```

## Performance

- `AFTER INSERT FOR EACH STATEMENT` — fires once per INSERT statement, not per row.
- For each serial column on the table: one `MAX()` (index scan, O(log n)) and one `setval()`.
- Negligible for servarr workloads (low write volume relative to typical postgres).

## Rollback

```sql
-- Drop all installed triggers (one DB at a time)
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN
    SELECT n.nspname AS s, c.relname AS t
    FROM pg_trigger tg
    JOIN pg_class c ON c.oid = tg.tgrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE tg.tgname = 'zz_sync_sequences' AND NOT tg.tgisinternal
  LOOP
    EXECUTE format('DROP TRIGGER zz_sync_sequences ON %I.%I', r.s, r.t);
  END LOOP;
END $$;
DROP FUNCTION IF EXISTS sync_table_sequences();
```

## Related

- `2-k3s/maintenance/postgres-sequence-audit-cronjob.yaml` — weekly drift detector
- `2-k3s/08.servarr/TROUBLESHOOTING-DUPLICATE-FILE-IDS.md` — symptom history
- `.history/2026-05-01-bazarr-sequence-drift-fix.md` — incident that triggered this rewrite
