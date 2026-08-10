# Lingarr (k3s)

AI subtitle translator. Calls Ollama (OpenAI-compatible endpoint) on TrueNAS. Pairs with Bazarr (`translator_type: lingarr`) and `bazarr-autotranslate` scheduler.

## Image

Upstream `ghcr.io/lingarr-translate/lingarr:main`, digest-pinned by the
`images:` block in `../kustomization.yaml` (`newTag: main@sha256:...`). The
`:main` dev branch is deliberate: it is the only ref that carries upstream
#432 `6f9d879d` "fix: on npgsql convert DateTime to utc" (2026-05-19), which
fixes the Bazarr->Lingarr 500 `Cannot write DateTime with Kind=Local to
timestamptz`. Latest upstream release `1.2.4` (2026-05-15) predates that
commit, and `:latest` is byte-identical to `1.2.4`, so neither is usable.

The old fork `ghcr.io/spyrospsarras/lingarr:fix-zombie-concurrency-exec-update`
is **retired**, not running anywhere: its upstream PR #377 (refs #339) merged
2026-04-23 and shipped in `1.2.4`.

**Unpin condition (#270).** When upstream publishes a release newer than
`1.2.4`, confirm it contains `6f9d879d`, then swap the `images:` entry to that
plain release tag and drop the lingarr `automerge: false` rule in
`.github/renovate.json` (it exists only because `:main` is a dev branch).

Nobody has to remember this. `.github/workflows/upstream-release-watch.yml`
checks weekly and reopens #270 the moment the release appears:

```shell
gh api repos/lingarr-translate/lingarr/releases --jq '.[0].tag_name'   # != 1.2.4 -> act
```

## Database

CloudNativePG (namespace `postgres-system`, cluster `postgres-cluster`). Lingarr's own EF Core migrations manage the schema on first connect.

- DB: `lingarr-main`
- User: `lingarr`
- Creds: `servarr-postgres` Secret keys `lingarr-host/port/database/user/password`
- Connect LB IP: `192.168.10.105:5432` (postgres-rw)

### Initial DB + user bootstrap (manual)

The `postgres-setup-job.yaml` in the parent dir documents the pattern. For Lingarr (additive) the one-shot was:

```sql
CREATE DATABASE "lingarr-main";
CREATE USER lingarr WITH PASSWORD '<random 24-char>';
GRANT ALL PRIVILEGES ON DATABASE "lingarr-main" TO lingarr;
\c lingarr-main
GRANT ALL ON SCHEMA public TO lingarr;
ALTER SCHEMA public OWNER TO lingarr;
```

Then:
```bash
kubectl -n servarr patch secret servarr-postgres --type=merge -p \
  '{"stringData":{"lingarr-host":"192.168.10.105","lingarr-port":"5432","lingarr-database":"lingarr-main","lingarr-user":"lingarr","lingarr-password":"<PW>"}}'
```

## SQLite → Postgres data migration

Required once, when moving the existing TrueNAS Lingarr install to Postgres. Uses pgloader data-only so EF's schema stays canonical.

1. Stop source: `midclt call app.stop lingarr` on TrueNAS.
2. Copy SQLite: `scp truenas_admin@192.168.10.200:/mnt/apps/lingarr/local.db /tmp/`.
3. Upload into a temp pod: `kubectl -n servarr run pgloader --image=dimitri/pgloader:latest --restart=Never --command -- sleep 300 && kubectl cp /tmp/local.db servarr/pgloader:/tmp/lingarr.db`.
4. Exec pgloader with a command file:
   ```
   LOAD DATABASE
     FROM sqlite:///tmp/lingarr.db
     INTO postgresql://lingarr:<PW>@192.168.10.105:5432/lingarr-main
     WITH include no drop, truncate, data only, reset sequences
     EXCLUDING TABLE NAMES LIKE 'sqlite_%', '__EFMigrationsLock', 'version_info';
   ```
5. Bump IDENTITY sequences past `max(id)` per table (pgloader's `reset sequences` doesn't touch EF IDENTITY columns). Use the anonymous `DO $$ ... $$` block in `docs/fix-identities.sql` (committed in this repo).
6. Delete pod: `kubectl -n servarr delete pod pgloader`.
7. Start Lingarr on Postgres.

## ASP.NET Data Protection keys

Encrypted settings (Sonarr/Radarr/Anthropic/OpenAI API keys stored as `CfDJ8...`) are decrypted using DP keys at `/app/config/keys/*.xml`. These MUST come across from the TrueNAS install, else every encrypted setting must be re-saved.

```bash
ssh truenas_admin@192.168.10.200 "tar -czf /tmp/lingarr-keys.tar.gz -C /mnt/apps/lingarr keys"
scp truenas_admin@192.168.10.200:/tmp/lingarr-keys.tar.gz /tmp/
kubectl -n servarr cp /tmp/lingarr-keys.tar.gz <lingarr-pod>:/tmp/keys.tgz
kubectl -n servarr exec <lingarr-pod> -- sh -c 'cd /app/config && tar -xzf /tmp/keys.tgz && chown -R 568:568 /app/config/keys'
kubectl -n servarr rollout restart deploy/lingarr
```

## Boot-time job-queue reconcile (#870)

A restart used to duplicate queued translation work and leave jobs that cancel
could not reach. The `enforce-db-invariants` initContainer now clears that state
before the app starts, using `files/reconcile-job-queue.sql`.

### What upstream does

- `ScheduleInitializationService` runs on application start and calls
  `ScheduleService.Initialize()` (`ScheduleService.cs:107`), which calls
  `TranslationRequestService.ResumeTranslationRequests()`
  (`TranslationRequestService.cs:453-476`).
- That method re-enqueues EVERY `Pending`/`InProgress` request unconditionally
  (line 471) and repoints `translation_requests.job_id` at the new job. It never
  deletes the job the request pointed at before, so the old `hangfire.job` row
  stays `Enqueued` and its `hangfire.jobqueue` row stays fetchable - now
  referenced by nothing.
- Cancel deletes only the job named by `job_id` (`TranslationRequestService.cs:341-344`),
  which is why the orphan survives a cancel and the title gets translated anyway.
- `TranslationJob` carries no `[DisableConcurrentExecution]`, so the duplicate is
  free to actually run.
- Upstream's own startup cleanup covers `Processing` orphans only. The 2026-08-08
  boot log shows `Cleaned up orphaned processing job 894` with no `Enqueued`
  equivalent.

### Safety contract

**Pre-boot only. Never a CronJob. Never write to `translation_requests`.**

- The guard is safe because the Deployment is `replicas: 1` with
  `strategy: Recreate`, so no Lingarr process is alive while an initContainer
  runs. That single-writer window is the whole guarantee.
- Nulling `job_id` would send `ResumeTranslationRequests` down its
  `JobId == null` branch (`TranslationRequestService.cs:462-469`), which silently
  marks every pending request `Interrupted` - permanent, user-visible work loss.
- Honest limit on the "no work is lost" claim. `OnApplicationStarted` is
  `async void` and the resume loop inside `Initialize()` is not transactional.
  If it throws part way through, the exception is swallowed and boot continues:
  the requests it already reached are re-enqueued, and the ones after the throw
  are left with their old queue row already deleted by the guard and no new job.
  They sit idle until the NEXT successful boot. That is delay, not permanent
  loss - `translation_requests.job_id` and `status` are durable, so the next
  resume picks the same rows up again. Permanent loss would need `job_id`
  nulled, which the guard never does.

### Failure policy: fail-closed (#925)

A failed reconcile is fatal. The `psql -f` runs under `if ! ...; then echo FATAL;
exit 1; fi`, so a non-zero psql exit fails the `enforce-db-invariants`
initContainer and lingarr does not start. Same policy as the `request_timeout`
statement above it (#844) - the two invariants in one container no longer have
two different failure policies.

It used to run with `|| echo "WARN: ..."`. `set -e` does not fire on the left of
`||`, so a failed reconcile exited `0`, the initContainer reported `Completed`,
and lingarr booted on the duplicated queue with one unread `WARN` line as the
only trace. #870 could come back and the pod would still look healthy.

What is and is not a failure:

| Outcome | psql exit | Boot |
|---|---|---|
| Rows cleared (`cleared N ... reaped M`) | `0` | proceeds |
| Nothing to reconcile (`cleared 0 ... reaped 0`) | `0` | proceeds |
| First-ever install, no hangfire schema (`NOTICE ... skipped`) | `0` | proceeds |
| Reconcile attempted and failed (SQL error, revoked grant, lock timeout) | non-zero | **blocked**, `FATAL:` in the initContainer log |

The availability cost is smaller than it looks. The `request_timeout` psql hits
the same database first and is already fatal, so every connection-level failure
(postgres down, wrong host, bad credentials) already blocked boot. The only
newly-fatal class is a reconcile-specific failure, which is exactly the class
that used to be invisible.

If a reconcile failure ever blocks a boot, fix the database cause - do not re-add
a `||` fallback. Read the psql error above the `FATAL:` line in
`kubectl --context epaflix -n servarr logs deploy/lingarr -c enforce-db-invariants`,
then reproduce it interactively with the by-hand run below. There is no live
escape hatch worth documenting: `servarr` is an automated self-healing ArgoCD App,
so a `kubectl patch` that drops the guard is reverted on the next sync. A genuine
emergency goes through a merged PR like any other change.

### This is a workaround

It stays until upstream fixes the defect. The re-check is automated, not left to
memory: `.github/workflows/upstream-release-watch.yml` carries a row keyed on
issue #870 and reopens that issue when `lingarr-translate/lingarr` cuts a release
past `1.2.4`. Upstream #315 fixed the `Processing` case only; the `Enqueued`
sibling is still unfixed.

Retire condition: a release whose notes or git log show `ResumeTranslationRequests`
no longer re-enqueues unconditionally, or `TranslationJob` gaining
`[DisableConcurrentExecution]`. Then delete `files/reconcile-job-queue.sql`, its
`lingarr-jobqueue-guard` entry in the `configMapGenerator:` block of
`../kustomization.yaml`, and the `psql -f` line plus the `jobqueue-guard`
volume/volumeMount in `lingarr.yaml`. Keep the initContainer itself - it still
enforces `request_timeout`.

### Running it by hand

Only if you need it out of band. Scale to 0 FIRST - the SQL is not safe against a
live pod:

```bash
kubectl --context epaflix -n servarr scale deploy/lingarr --replicas=0
kubectl --context epaflix -n servarr rollout status deploy/lingarr --timeout=120s
PG=$(kubectl --context epaflix -n postgres-system get pod \
  -l cnpg.io/instanceRole=primary -o jsonpath='{.items[0].metadata.name}')
kubectl --context epaflix -n postgres-system exec -i "$PG" -c postgres -- \
  psql -d lingarr-main -f - < 2-k3s/08.servarr/lingarr/files/reconcile-job-queue.sql
kubectl --context epaflix -n servarr scale deploy/lingarr --replicas=1
```

On its first boot after this change lands the guard also performs a one-off
cleanup of the unreachable `Enqueued` `TranslationJob` rows left over from the
2026-08-08 burst. Count the rows before the roll and expect `0` after it - the
absolute number drifts with normal use, so do not treat any fixed figure as the
pass condition. It was 240 when this was measured on 2026-08-09.

## Files

| File | Purpose |
|---|---|
| `lingarr.yaml` | Deployment + ClusterIP Service |
| `pdb.yaml` | PodDisruptionBudget |
| `ingress.yaml` | Traefik IngressRoute for `lingarr.epaflix.com` (internal per project convention — see below) |
| `files/reconcile-job-queue.sql` | Boot-time Hangfire job-queue guard (#870), mounted from the `lingarr-jobqueue-guard` ConfigMap |
| `docs/fix-identities.sql` | Post-pgloader IDENTITY reset |
| `docs/pgloader.load` | pgloader command template |

`lingarr-config` PVC lives alongside the other *arr config PVCs in `_shared/storage/arr-configs.yaml`.

## Apply

```bash
kubectl apply -f ../_shared/storage/arr-configs.yaml   # if PVC not already created
kubectl apply -f lingarr.yaml
kubectl apply -f pdb.yaml
kubectl apply -f ingress.yaml
```

## Internal-only access

Convention in this stack (same as sonarr/radarr/cleanuparr/etc.): a hostname is "internal-only" when it has **no public Cloudflare DNS record**. Pi-hole resolves it to `192.168.10.101` (Traefik LB) for LAN clients; external DNS returns NXDOMAIN. TLS stays on the real Let's Encrypt wildcard cert — no self-signed.

Steps:

1. **Pi-hole record** (tracked here; apply on `192.168.10.30`):
   ```
   # /etc/dnsmasq.d/10-epaflix.conf
   address=/lingarr.epaflix.com/192.168.10.101
   ```
   Reload: `pihole restartdns` (or `systemctl restart pihole-FTL`).

2. **Cloudflare**: ensure there's no `lingarr.epaflix.com` A/CNAME record in the `epaflix.com` zone. The wildcard TLS cert still issues via DNS-01 — it doesn't need an A record.

## Rollback to TrueNAS

SQLite `/mnt/apps/lingarr/local.db.backup-pre-pg-<stamp>` is intact on TrueNAS. To revert:

1. `kubectl -n servarr scale deploy lingarr --replicas=0`
2. On TrueNAS: swap Lingarr compose back to `DB_CONNECTION` unset (default sqlite), `midclt call app.start lingarr`.

Postgres DB `lingarr-main` can stay — dropping requires `DROP DATABASE "lingarr-main"` as `postgres` superuser.
