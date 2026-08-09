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

## Files

| File | Purpose |
|---|---|
| `lingarr.yaml` | Deployment + ClusterIP Service |
| `pdb.yaml` | PodDisruptionBudget |
| `ingress.yaml` | Traefik IngressRoute for `lingarr.epaflix.com` (internal per project convention — see below) |
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
