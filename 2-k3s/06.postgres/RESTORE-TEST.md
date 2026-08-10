# Restore test for `postgres-cluster`

How to prove the `postgres-cluster` backups can actually be restored, without touching the live
cluster or the live backup catalog.

Run it: after any change to the backup path (plugin bump, `ObjectStore` change, bucket move), and
otherwise every few months. The whole thing takes about 15 minutes.

First run: 2026-08-08, issue #570. It passed. All the numbers in this file are from that run.

## The footgun - read this before you apply anything

A recovering cluster that declares its own `spec.plugins` with `isWALArchiver: true` will archive
its WAL into `s3://postgres-backups/postgres-cluster/` - the live catalog. That corrupts the exact
thing we are testing.

So the scratch manifest below has **no** `plugins:` stanza at all. The Barman Cloud Plugin docs are
explicit about this: "The above configuration does not enable WAL archiving for the restored
cluster." Recovery reads the catalog through `externalClusters[].plugin`, and that is a read path
only.

The only signal that this went wrong is a **second `serverName` appearing in
`ObjectStore.status.serverRecoveryWindow`**. Nothing else warns you. That is why teardown asserts
the catalog still holds exactly one server.

Second guard: the scratch cluster runs in its own namespace `pg-restore-test`, not inside
`postgres-system`. Reason is teardown. With a dedicated namespace the riskiest step is a single
`kubectl --context epaflix delete ns pg-restore-test` - one object name, and a typo cannot land on a live object. If
the scratch cluster lived next to the real one, teardown would be a list of `delete cluster` /
`delete pvc` commands typed by hand right next to `postgres-cluster`, and one wrong name deletes
production.

This is a gated, live-cluster action. Do not run it unattended.

## Preflight

Check the live cluster is healthy and the catalog is where you think it is, before you start:

```bash
kubectl --context epaflix get cluster postgres-cluster -n postgres-system \
  -o jsonpath='{.status.readyInstances}/{.spec.instances} {.status.currentPrimary} {.status.timelineID} {.status.phase}{"\n"}'
kubectl --context epaflix get objectstore postgres-minio-store -n postgres-system \
  -o jsonpath='{.spec.configuration.destinationPath} {.spec.configuration.endpointURL}{"\n"}'
kubectl --context epaflix get objectstore postgres-minio-store -n postgres-system \
  -o jsonpath='{.status.serverRecoveryWindow}{"\n"}'
```

What it looked like on 2026-08-08:

```
  live: instances=3/3  primary=postgres-cluster-10  timeline=27  phase=Cluster in healthy state
  catalog: destination=s3://postgres-backups/  endpoint=https://minio.epaflix.com
  serverRecoveryWindow:
    postgres-cluster: 2026-07-29T02:02:37Z -> 2026-08-08T02:03:10Z
  live plugins: [{"enabled":true,"isWALArchiver":true,"name":"barman-cloud.cloudnative-pg.io","parameters":{"barmanObjectName":"postgres-minio-store"}}]
  14 user databases, largest: authentik 234 MB, prowlarr-main 77 MB, sonarr-main 52 MB, bazarr-main 50 MB
  worker free space: worker-61 9.1G (81% used), worker-62 12G (76%), worker-63 19G (61%), worker-65 15G (69%)
```

Things to confirm here:

- `serverRecoveryWindow` holds exactly **one** server, `postgres-cluster`. If it already holds two,
  stop - a previous test leaked WAL into the catalog.
- The recovery window end is recent (a day old at most).
- A worker has room for the scratch PVC. 10Gi is enough for a 14-database catalog whose biggest
  database is 234 MB, but `local-path` puts it on a worker disk and worker-61 was at 81% used.

## Setup

The scratch namespace needs its own copy of the MinIO credentials Secret and of the `ObjectStore`
CR, because both are namespaced and the recovery runs in `pg-restore-test`.

Keep the credential copy as a **pipe**. Never dump the Secret to a file or to the terminal - the
value ends up in the shell history and in the agent transcript.

```bash
kubectl --context epaflix create ns pg-restore-test
kubectl --context epaflix get secret minio-backup-credentials -n postgres-system -o json \
  | python3 -c "import sys,json;d=json.load(sys.stdin);d['metadata']={'name':d['metadata']['name'],'namespace':'pg-restore-test'};print(json.dumps(d))" \
  | kubectl --context epaflix apply -f -
kubectl --context epaflix get objectstore postgres-minio-store -n postgres-system -o json \
  | python3 -c "import sys,json;d=json.load(sys.stdin);d['metadata']={'name':d['metadata']['name'],'namespace':'pg-restore-test'};d.pop('status',None);print(json.dumps(d))" \
  | kubectl --context epaflix apply -f -
```

The `d.pop('status',None)` on the `ObjectStore` matters: the status carries the live recovery
window, and copying it over would give a confusing reading in the scratch namespace.

## The scratch cluster

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Cluster
metadata:
  name: pg-restore-test
  namespace: pg-restore-test
spec:
  instances: 1
  imageName: ghcr.io/cloudnative-pg/postgresql:16
  storage:
    size: 10Gi
    storageClass: local-path
  resources:
    requests:
      cpu: 250m
      memory: 512Mi
  # DELIBERATELY no 'plugins:' stanza. See the warning above.
  bootstrap:
    recovery:
      source: live-catalog
  externalClusters:
    - name: live-catalog
      plugin:
        name: barman-cloud.cloudnative-pg.io
        parameters:
          barmanObjectName: postgres-minio-store
          serverName: postgres-cluster
```

Notes on the fields:

- `instances: 1` - we are testing the data, not HA. One pod, one PVC, less to clean up.
- `imageName` must match live (`ghcr.io/cloudnative-pg/postgresql:16`). A physical base backup only
  restores into the same major version.
- `serverName: postgres-cluster` is the directory inside the bucket to read from. It is the live
  server name, and it is read-only here.

## Recovery timing

Apply the manifest and watch the phase. From the 2026-08-08 run:

```
  10:12:10 phase=Setting up primary ready=0
  ...
  10:15:12 phase=Waiting for the instances to become active ready=0
  10:15:28 phase=Cluster in healthy state ready=1
  final: phase=Cluster in healthy state  ready=1  timeline=28
```

About 3 minutes and 20 seconds from apply to healthy. The recovered cluster forked onto timeline
28. Live stayed on 27 - that is the expected split, the restore is a new branch of history and it
does not touch the live timeline.

## Validation

Two checks: every live database exists in the restore, and the rows are really there.

### Database list

```
=== every live database must exist in the recovery ===
  live: 14 databases   restored: 15 databases
  in live but MISSING from restore: (none)
  in restore but not live: app   <- dropped from live after the backup, harmless
```

`app` is the CNPG default bootstrap database. It existed when the base backup was taken and was
dropped from live later, so it shows up only in the restore. Nothing to fix.

**Trap 1 - `comm` on raw `psql` output silently passes.** `psql` does not return the database list
sorted the way `comm` needs it. On the first attempt `comm` printed `file 1 is not in sorted order`
and the comparison passed vacuously - it looked like a clean result and it had compared nothing.
Always pipe both sides through `sort` first:

```bash
... | sort > /tmp/live.txt
... | sort > /tmp/restored.txt
comm -23 /tmp/live.txt /tmp/restored.txt   # must be empty
```

### Row counts

```
=== real row counts, count(*) on concrete tables ===
  authentik.authentik_core_user      live=24       restored=24       MATCH
  authentik.authentik_events_event   live=4504     restored=4504     MATCH
  prowlarr-main."History"            live=155815   restored=155801   DIFF (14 rows, live kept writing)
  sonarr-main."History"              live=8076     restored=8076     MATCH
  radarr-main."MovieMetadata"        live=250      restored=250      MATCH
```

The 14-row difference on `prowlarr-main."History"` is not data loss. Live kept writing between the
base backup and the comparison. Append-only tables like `History` will always drift a bit - only a
**smaller** count on a table that does not get new rows would be a real problem.

**Trap 2 - `pg_stat_user_tables.n_live_tup` reports 0 rows on a fresh restore.** That is a
measurement artifact, not data loss. `n_live_tup` is planner statistics, and planner statistics are
not carried inside a physical base backup - they reset on restore and only come back after
`ANALYZE` or autovacuum. Reading them straight after recovery makes a perfectly good restore look
completely empty. Use `count(*)` on concrete tables instead. It is slower and it is the only number
that means anything here.

### How current is the restore

```
=== how current is the restore ===
  newest authentik event in restore: 2026-08-08 07:52:27.284114+00
  newest authentik event live:       2026-08-08 07:52:27.284114+00
  identical to the microsecond
```

Identical to the microsecond, which means WAL replay carried the restore right up to the end of the
archive - not just to the last base backup. That is the strongest single signal that continuous
archiving works.

## Teardown

```bash
kubectl --context epaflix delete ns pg-restore-test --wait=true --timeout=300s
# assert 1: the catalog must still hold exactly ONE serverName
kubectl --context epaflix get objectstore postgres-minio-store -n postgres-system -o jsonpath='{.status.serverRecoveryWindow}'
# assert 2: live must still be 3/3
kubectl --context epaflix get cluster postgres-cluster -n postgres-system -o jsonpath='{.status.readyInstances}'
```

Assert 1 is the footgun check. If a second `serverName` shows up next to `postgres-cluster`, the
scratch cluster archived into the live catalog and you have to clean that prefix out of the bucket.

From the 2026-08-08 run:

```
  namespace "pg-restore-test" deleted
  namespace gone
  leftover PVCs anywhere: 0
  catalog after teardown:
    postgres-cluster: 2026-07-29T02:02:37Z -> 2026-08-08T02:03:10Z
  still exactly one server in the catalog: the live cluster
  live: instances=3/3  primary=postgres-cluster-10  timeline=27  phase=Cluster in healthy state
  ContinuousArchiving: True - Continuous archiving is working
```

The recovery window after teardown is byte-identical to preflight, live is still 3/3 on timeline
27, and no PVC leaked anywhere in the cluster.

## Result

The `postgres-cluster` backups restore. Verified 2026-08-08, issue #570. Restorability is no longer
a hypothesis.

Correction while we are here: issue #570 and `README.md` say Barman Cloud Plugin **v0.12.0**. That
is stale. The live plugin reports **0.14.0** in `Cluster.status.pluginStatus`:

```bash
kubectl --context epaflix get cluster postgres-cluster -n postgres-system \
  -o jsonpath='{.status.pluginStatus[*].version}{"\n"}'
```

Read the live field, do not trust the version written in any doc, including this one.
