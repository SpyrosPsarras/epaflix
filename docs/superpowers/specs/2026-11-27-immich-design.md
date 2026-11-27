# Immich for photos

## Why

The owner wants Immich for his own photos and his friends' photos. Each person has a private library: nobody sees anyone else's photos unless they share on purpose. Everyone in the Authentik group "Servarr users" can log in. The photos live on `pithari` and get an offsite copy in Google Drive.

The groundwork is done. `postgres-cluster` runs PostgreSQL 18.7 with VectorChord 1.1.1 and pgvector 0.8.7 (`docs/superpowers/specs/2026-11-26-postgres-18-design.md`, PRs #2474 and #2477). The `pithari` storage design reserved `pithari/immich` for this.

## Goals

- Immich at `https://immich.epaflix.com`, reachable from the internet, so phones back up from anywhere, long videos included.
- Login only through Authentik OpenID. Every member of "Servarr users" can log in, and Immich creates the account on first login. Password login is off. Members of "authentik Admins" are Immich admins.
- One private library per user. Shared albums, share links and partner sharing stay available as Immich features, each opt-in by the user.
- No storage quotas (owner's choice).
- Photos in the encrypted dataset `pithari/immich`, with daily snapshots from the existing recursive `pithari` task.
- A nightly encrypted copy of the originals and Immich's database dumps in Google Drive.
- Face recognition and smart search on the GPU node.
- Renovate updates the chart and images.

## Non-goals

- No import. Everyone starts empty (owner's choice).
- No external libraries (photos read in place from other folders).
- No per-user quotas, now or as a default.
- No other photo apps, no Immich Kiosk or Frame.

## Anti-goals

- A friend can see another person's photos without being shared them.
- Losing photos: a delete in Immich must not reach the offsite copy before the local 30-day snapshots and Immich's own trash would have caught it.
- An Authentik login page in front of Immich (forward auth). It breaks phone backup and public share links.
- Secrets in Git outside SOPS files.

## Facts this rests on

Checked on 2026-11-27.

- Immich v3.4.2 is the latest release. The official chart `immich` 0.13.4 in `immich-app/immich-charts` builds on bjw-s app-template. It can deploy Valkey (`valkey.enabled`), the server and machine learning. It takes an existing claim for the library (`immich.persistence.library.existingClaim`) and renders `immich.configuration` into a ConfigMap or Secret (`configurationKind`).
- Immich OpenID settings (Immich docs, "OAuth Authentication"): the redirect URIs are `app.immich:///oauth-callback` (phone app), `https://<host>/auth/login` and `https://<host>/user-settings`. The role claim is `immich_role` ("user" or "admin"), Auto Register defaults to true, and Default Storage Quota 0 means unlimited.
- Immich against an existing Postgres (Immich docs, "Pre-existing Postgres"): it works with Postgres 14 to below 20, pgvector 0.7 to below 0.9 and VectorChord 0.3 to below 2.0. Without superuser rights the extensions must exist before Immich starts: `vchord` (with `vector`) and `earthdistance` (with `cube`). Its automatic database dumps need superuser (`pg_dumpall`).
- Repo patterns: Paperless mounts NFS from `pithari` through a static PV (`2-k3s/33.paperless/storage.yaml`), sits on Traefik `websecure` at `192.168.10.101`, and logs in through an Authentik OpenID provider in the encrypted blueprint. Windmill installs a Helm chart through kustomize `helmCharts` and defines its database with CNPG `Database` and `DatabaseRole` objects in `postgres-system`. Ollama runs on the GPU node with `runtimeClassName: nvidia`, nodeSelector `gpu: nvidia` and the `nvidia.com/gpu` toleration and resource.
- Offsite today: TrueNAS cloud sync task 1 pushes `/mnt/mpara-tank-lan/minio` to Google Drive folder `/epaflix-offsite/minio`, encrypted, SYNC mode, daily at 22:00. The `postgres-cluster` backups in MinIO reach Google Drive that way.

## Approaches

- **O7 (picked by the owner).** The official chart through kustomize `helmCharts`, with the library on NFS from `pithari/immich` and the database in `postgres-cluster`.
- **O8.** Own manifests instead of the chart. Rejected: about 400 more lines to maintain, and no chart updates from upstream.
- **O9.** Library on a democratic-csi iSCSI disk. Rejected. The pithari design says media belongs in a dataset, and cloud sync cannot read a zvol.

Login behind forward auth was rejected (see anti-goals). Login is OpenID inside Immich.

## Design

Folder `2-k3s/39.immich`, Argo CD Application `immich` (automated, `selfHeal`, `prune`, the same ignoreDifferences as Paperless). Namespace `immich`.

### Storage

- The dataset `pithari/immich` is encrypted the same way as `pithari/k8s`: AES-256-GCM, generated key stored on TrueNAS, no passphrase. **Owner step:** export its key into the vault, as for the other two.
- A TrueNAS user and group `immich` with a new uid and gid, no login. A TrueNAS NFS share on `/mnt/pithari/immich` for hosts `192.168.10.61` to `65`, mapping all clients to that user. The pods then need no root on the share, as with Paperless.
- A static PV and PVC `immich-library` (ReadWriteMany, `Retain`, `nfsvers=4.2,hard`), as in `2-k3s/33.paperless/storage.yaml`. The chart's `existingClaim` points at it.
- The machine learning model cache gets its own small `truenas-iscsi` PVC instead of `emptyDir`, so a restart does not download the models again.

### Database

In `postgres-system`, following Windmill's pattern:

- `DatabaseRole` `immich`, login, password from a SOPS Secret. Not superuser.
- `Database` `immich`, owner `immich`, with CNPG's `extensions` list: `vchord`, `vector`, `cube`, `earthdistance`.
- Immich gets `DB_VECTOR_EXTENSION=vectorchord`. It connects to `postgres-cluster-rw.postgres-system.svc` directly, not through the pooler. The pooler's transaction mode does not suit Immich's migrations.
- Immich's own automatic database dumps are turned off: they need superuser. The cluster's barman backups (WAL plus daily base backup, reaching Google Drive through MinIO) cover the database.

### Immich

Chart values:

- Chart `valkey.enabled: true`, emptyDir, as the chart ships it.
- Server image `ghcr.io/immich-app/immich-server`, and machine learning `ghcr.io/immich-app/immich-machine-learning` with the `-cuda` tag. Both pinned by tag and digest to the same Immich version.
- Machine learning goes on `k3s-worker-64` with `runtimeClassName: nvidia`, nodeSelector `gpu: nvidia`, the toleration and `nvidia.com/gpu: 1`, as Ollama does. **R10.** Ollama already takes the one GPU. The builder checks how the device plugin shares it (time-slicing or not) before choosing between a request and a shared setup.
- The config file comes from a SOPS Secret, not from chart values, because it holds the OpenID client secret. It sets:
  - `oauth`: enabled, issuer `https://auth.epaflix.com/application/o/immich/`, client id and secret, scope `openid email profile`, autoRegister true, autoLaunch true, roleClaim `immich_role`, storageLabelClaim `preferred_username`, defaultStorageQuota 0.
  - `passwordLogin.enabled: false`.
  - `server.externalDomain: https://immich.epaflix.com`.
  - `backup.database.enabled: false`.
- Immich's own metrics on, with a ServiceMonitor for the existing Prometheus.

### Authentik

In the encrypted blueprint, following Paperless and Fytorium:

- OAuth2 provider `immich`, confidential client, strict redirect URIs `https://immich.epaflix.com/auth/login`, `https://immich.epaflix.com/user-settings` and `app.immich:///oauth-callback`.
- A scope mapping `immich_role` that returns "admin" for members of "authentik Admins" and "user" for everyone else.
- Application `immich`, bound to "Servarr users" and "authentik Admins".
- The client secret goes straight from the blueprint into the Immich config Secret, not through chat.

### Network

- IngressRoutes on `websecure` and `web` (redirect) for `immich.epaflix.com`, as Paperless has, with the `security-headers` middleware. No forward auth. Traefik sets no body size limit by default. The builder confirms that no middleware or entrypoint setting caps uploads.
- Pi-hole: `address=/immich.epaflix.com/192.168.10.101` and the matching `unbound-no-aaaa-leak.conf` entry, as for Paperless. The builder checks how public DNS reaches the other public hosts and does the same.
- NetworkPolicies: server ingress only from Traefik. Machine learning ingress only from the server. Valkey ingress only from the server. Egress from the server to Postgres, Valkey, machine learning and Authentik.

### Offsite copy

TrueNAS cloud sync task 2: `/mnt/pithari/immich` to Google Drive folder `/epaflix-offsite/immich`. It uses the existing Google Drive credential, encryption on with new filename and data keys, and runs daily at 03:00, after the 00:45 snapshot.

- It copies only what cannot be rebuilt: `upload/`, `library/` and `profile/`. It skips `thumbs/` and `encoded-video/`, which Immich rebuilds.
- Mode COPY, not SYNC. A photo deleted in Immich stays in Drive. **My call**, because of the anti-goal: SYNC would empty Drive one day after a mistaken delete. Drive grows by everything ever uploaded. A later pruning job is a follow-up, not part of this.
- **Owner step:** store the new cloud sync encryption password and salt in the vault. Without them the Drive copy cannot be read.
- **R9.** The Google Drive plan's free space is unknown to the agent. The owner checks it before friends start uploading.

### Monitoring

- Alerts: Immich server down, machine learning down, the cloud sync task failed or did not run in 36 hours. `pithari` free space already has an alert.
- Blackbox probe on `https://immich.epaflix.com/api/server/ping`, as for other public hosts.

### Owner steps

1. Export the `pithari/immich` dataset key and the cloud sync encryption secrets into the vault.
2. First login as an admin through Authentik, then invite friends by adding them to "Servarr users".
3. Check the Google Drive plan's free space (R9).

## Risks

- **R9.** Google Drive fills up. Owner check above. Cloud sync failure alerts catch it later.
- **R10.** GPU contention with Ollama. Builder checks first.
- **R11.** Immich upgrades run database migrations on the shared cluster. Renovate never auto-merges Immich. The cluster's daily backup is the rollback.
- **R12.** VectorChord updates need `ALTER EXTENSION vchord UPDATE` and `REINDEX` of `face_index` and `clip_index` in `immich`. The Renovate rule from #2477 already says so.
- **R13.** The site is public. Login goes only through Authentik, and share links are public by design, so anyone with a link can open that album.

## Left to the builder

- Exact chart, image versions and digests at build time.
- The uid and gid for user `immich` on TrueNAS.
- The NetworkPolicy details and the Authentik egress path.
- The ML cache size.
- The alert expressions.
