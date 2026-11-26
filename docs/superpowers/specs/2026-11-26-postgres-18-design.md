# Upgrade `postgres-cluster` to PostgreSQL 18

## Why

Immich needs the VectorChord extension. The owner wants it in the shared cluster, not in a separate database. On PostgreSQL 18, CNPG can mount VectorChord from its own image (image volume extensions), so the cluster keeps the stock CNPG image. The cluster runs 16.15 on Debian 11 with the floating tag `:16`, and Renovate does not watch it. The owner wants it on 18 and kept current by Renovate.

## Goals

- `postgres-cluster` runs `ghcr.io/cloudnative-pg/postgresql:18.<minor>-standard-trixie`, with all 27 databases, roles and data intact.
- VectorChord loads in the cluster through an image volume extension, ready for Immich.
- Backups after the upgrade go to a new path in MinIO, and the 16 backups stay readable.
- Renovate opens PRs for the Postgres image and the VectorChord image.

## Non-goals

- Immich itself, its database and its storage. Separate design after this one.
- Postgres 19. VectorChord has no PG 19 image yet (`tensorchord/vchord-scratch` stops at `pg18-v1.1.1`).
- The CNPG operator and barman plugin versions. Renovate does not track those raw manifests either; follow-up if wanted.
- Moving the cluster off `local-path`.

## Anti-goals

- Losing data, or losing the ability to go back to 16 until the owner confirms 18 works.
- Any live step without the owner's yes. Step 3 takes every app that uses Postgres offline.

## Facts this rests on

Checked on 2026-11-26.

- The cluster has 3 instances, CNPG 1.30.0, `local-path` storage, 2.0 GB of data, and 27 databases. Every database uses collation `C`, ctype `C` and the libc provider, with no ICU collations. So the Debian change from 11 to 13 does not change index order.
- Extensions in use are `pg_trgm`, `unaccent` and `btree_gist` in `fytorium`. All three ship with 18.
- CNPG's in-place major upgrade (`pg_upgrade --link`) only works between images on the same Debian release, and PG 18 images exist only for bookworm and trixie. So the move needs a step through 16 on trixie first. Source: CNPG 1.30 docs, "PostgreSQL upgrades".
- After `pg_upgrade`, the cluster gets a new system ID and timeline 1. The barman plugin does not separate archives by itself, so CNPG's docs say to change `serverName` in the same change as `imageName`. 16 backups cannot be used for recovery to a point after the upgrade.
- The `standard` image includes pgvector, pgaudit, all locales and JIT. VectorChord needs pgvector, so only VectorChord comes from an extension image.
- Image volume extensions need PG 18 and Kubernetes `ImageVolume`. The cluster runs k3s 1.36 with containerd 2.3.4, and the API server reports `ImageVolume` as enabled.
- Argo CD app `postgres` syncs automatically with `selfHeal: true` and `prune: false`.

## Approaches

- **O3 (picked by the owner). In place, in steps.** Each step changes the image in Git. The cluster name, services, pooler, Database CRs and secrets stay as they are.
- **O4. New PG 18 cluster that imports from the old one, then switch the services over.** Rejected. The old cluster stays as a fallback, but the cluster name, Database CRs, pooler, roles, secrets and backup path all have to move, which is too much for 2 GB.
- **TensorChord's `cloudnative-vectorchord` image on 16.** Rejected by the owner. Every database would run on a third-party image.

## Design

One PR per step. Each step is merged and checked before the next.

### Step 1. Renovate first

Before any image change, add to `.github/renovate.json`:

- A regex manager for `imageName:` in `2-k3s/06.postgres/cluster/postgres-cluster.yaml`, with datasource `docker`. It uses versioning `regex:^(?<major>\d+)\.(?<minor>\d+)-standard-trixie$`, so it ignores the timestamped, minimal and bookworm tags.
- A regex manager for the VectorChord extension image in the same file. It uses versioning `regex:^pg(?<compatibility>\d+)-v(?<major>\d+)\.(?<minor>\d+)\.(?<patch>\d+)$`, so the `pg18` part never changes.
- Rules:
  - Postgres minor updates auto-merge at night. Each one restarts the instances one at a time with a switchover, and apps reconnect within seconds.
  - Postgres major updates already wait for review under the existing major rule. The rule's description says to merge a major only once VectorChord has an image for it and the extension's image tag moves in the same PR. Merging the major runs `pg_upgrade` and needs a new `serverName`, so it follows the step 3 procedure.
  - VectorChord updates never auto-merge. Each one needs `ALTER EXTENSION vchord UPDATE` and reindexing of Immich's `face_index` and `clip_index` (Immich docs, "Pre-existing Postgres").

Renovate only sees the image once the tag is a full version, so this PR also replaces `:16` with `16.15-standard-trixie`. That is step 2.

### Step 2. 16 on Debian 13

Change `imageName` to `ghcr.io/cloudnative-pg/postgresql:16.<latest>-standard-trixie` (16.16 today). This merges in the same PR as step 1, or right before it. CNPG treats it as a minor update and restarts the instances one at a time, starting with the replicas.

Check afterwards: all 3 pods run the new image, `/etc/os-release` says trixie, `amcheck`'s `bt_index_check` passes on every index in every database, and a backup succeeds.

### Step 3. 18, offline

Owner picks the window and says yes. The window sits outside Renovate's 02:00 to 06:00 schedule. The agent runs every live step below after that yes.

The step 3 PR is written by hand. If Renovate has opened its own 18 PR, close it. Renovate never auto-merges a major, so it cannot start the upgrade on its own.

Before the PR merges:

1. Stop app writes. Record which roles have `LOGIN` and are not superusers or `streaming_replica`. Run `ALTER ROLE ... NOLOGIN` on each, then `pg_terminate_backend` on their sessions. CNPG does not manage these roles (`spec.managed.roles` is empty), so nothing puts the logins back. Apps fail to connect from here until step 3 ends. Scaling Deployments and pausing Argo are not needed. The owner picked this over accepting a few minutes of writes missing from the dump.
2. Take a logical dump of everything: a throwaway Job (not in Git) runs `pg_dumpall` as the superuser and writes the file to TrueNAS, outside the cluster. Check that the file is complete (its last line is the dump's completion comment) and record its size.
3. Trigger an on-demand base backup and wait for it to finish.

The PR changes, in `postgres-cluster.yaml`:

- `imageName` to `ghcr.io/cloudnative-pg/postgresql:18.<latest>-standard-trixie`.
- The barman plugin's `parameters` get `serverName: postgres-cluster-pg18`. Without a `serverName` today, the archive path is `postgres-cluster`. The 16 backups stay there.
- In the same PR, the Vaultwarden backup's `postgres` image moves from `16.10-bookworm` to 18 in `2-k3s/26.vaultwarden/kustomization.yaml`. A 16 `pg_dump` refuses to dump an 18 server. No other manifest runs `pg_dump`. The `psql` clients on 15 and 16 in servarr and maintenance keep working against 18 and stay as they are.

CNPG stops all three instances, runs the upgrade Job, starts the primary on 18, then deletes and rebuilds the two replica PVCs.

After the upgrade:

1. Run `ALTER ROLE ... LOGIN` for exactly the roles recorded before the dump. `pg_upgrade` keeps `NOLOGIN`. Run `update_extensions.sql` if `pg_upgrade` wrote one, then `vacuumdb --all --analyze-in-stages`. `pg_upgrade` does not carry planner statistics over.
2. Trigger an on-demand base backup into `postgres-cluster-pg18` and wait for it.
3. Check: the server reports version 18, all 27 databases are there, `count(*)` on a few real tables matches the numbers from before the upgrade, every Argo app that uses Postgres is Healthy, and the backup-age alert does not fire.

Rollback:

- If the upgrade Job fails: revert the PR. CNPG deletes the failed Job and starts 16 again on the untouched data (CNPG docs, "Offline In-Place Major Upgrades"). Then allow logins again for the recorded roles.
- If 18 started but something is wrong: there is no going back in place. Restore from the `pg_dumpall` file into a 16 cluster, or recover from the 16 base backup under `postgres-cluster`. Writes made after the upgrade are lost either way. The way back is decided at that point and needs the owner's yes, because replacing `postgres-cluster` deletes its PVCs.
- Before the window, the agent rehearses the dump restore once. It takes a dump, restores it into a throwaway 16 cluster in its own namespace without a `plugins:` block (same pattern as README "Postgres restore"), compares the database list and row counts, then deletes the namespace.

Keep the 16 copies, the TrueNAS dump and the MinIO path `postgres-cluster/`, until the owner confirms 18. After that, the agent deletes them with the owner's yes. The retention policy may only prune the active `serverName`, so this cleanup is done by hand on purpose.

### Step 4. VectorChord

Add to the Cluster:

- `postgresql.extensions` with one entry `vchord` and image `ghcr.io/tensorchord/vchord-scratch:pg18-v<latest>`, pinned by digest the way the repo pins other images. The Postgres `imageName` keeps a tag without a digest. CNPG rebuilds its images often, and a digest pin would make Renovate restart every database at night after each rebuild.
- `shared_preload_libraries: "vchord"`.

This restarts the instances one at a time. Check: `pg_available_extensions` lists `vchord` and `vector`, and in a scratch database `CREATE EXTENSION vchord CASCADE` works and a small `vchordrq` index builds. Then drop the scratch database. No real database gets the extension until Immich.

### Docs

- README "Postgres restore": the test cluster uses the 18 image, and recovery reads `serverName: postgres-cluster-pg18`. The note about a fresh path after a real loss suggests the next free name.
- The backup alerts in `custom-alerts.yaml` need no change. They read the plugin's metric, not the path.

## Risks

- **R4.** Every app that uses Postgres is down during step 3. With `--link` and 2 GB the upgrade should take minutes, but this is not measured. The replicas rebuild after the primary is up and do not block apps.
- **R5.** Some app might misbehave on 18, which is two majors newer. The window allows for checking every app before the owner confirms. Rollback after start is a restore (see step 3).
- **R6.** The `vchord-scratch` layout might not match what CNPG expects (the `lib` and `share/extension` folders). VectorChord's docs say it works as a CNPG image volume extension, but the agent has not checked the image contents. If it doesn't match, set the paths in the extension entry. If that fails too, the fallback is TensorChord's `cloudnative-vectorchord:18.x` image, which needs the owner's yes because it was rejected on 16 for being third-party.
- **R7.** Renovate's minor auto-merge restarts every database at night. That's the owner's choice of being current over zero restarts.

## Left to the builder

- Exact versions and digests at build time.
- The dump Job's image, node placement and target path on TrueNAS.
- The PR split between steps 1 and 2.
- Which tables to count in the step 3 check.
