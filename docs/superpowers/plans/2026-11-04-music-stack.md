# Music stack implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Lidarr, Navidrome and digarr in `servarr`. The owner's household plays music they own, their plays reach ListenBrainz, and digarr turns similar listeners' taste into downloads the owner approves.

**Architecture:** Three Deployments in `2-k3s/08.servarr/` (Argo app `servarr`). Lidarr and digarr keep their data in the shared CNPG cluster, through `DatabaseRole` and `Database` objects in `2-k3s/06.postgres/` (Argo app `postgres`). Navidrome uses SQLite on an iSCSI PVC, plus its own nightly backup to the media share. Authentik proxy providers live in the encrypted blueprint `2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml` (Argo app `authentik`). NetworkPolicies let only Traefik, plus named in-cluster callers, reach the two apps that trust a login header. The rollout is five PRs. Each merges only after the owner confirms, and its live check must pass before the next PR starts.

**Tech stack:** Kustomize, KSOPS/SOPS, Argo CD, Traefik IngressRoute and Middleware, Authentik blueprints, CNPG 1.30, Prometheus Operator, promtool. The CNI is Flannel with kube-router, so NetworkPolicy is enforced (`2-k3s/13.t3code/env/network-isolation.md`).

**Spec:** `docs/superpowers/specs/2026-11-04-music-stack-design.md`. Research: `2-k3s/08.servarr/research/music_*.md`.

## Global constraints

- Images: `newTag: <version>@sha256:<digest>` in the `images:` block of `2-k3s/08.servarr/kustomization.yaml`, with a row in `docs/servarr-image-pins.md`. Every pin, including init containers, uses `tag@sha256`. LinuxServer images come from `ghcr.io/linuxserver`.
- Versions:
  - Lidarr `3.1.6` (`ghcr.io/linuxserver/lidarr`, clean tag)
  - Navidrome `0.64.2` (`navidrome/navidrome`)
  - digarr `2.2.0` (`ghcr.io/iuliandita/digarr`)
  - exportarr `v2.3.0` (`ghcr.io/onedr0p/exportarr`)
  - plugin `listenbrainz-daily-playlist.ndp` `v6.0.0`, sha256 read from the release asset at pin time
- Hostnames: `lidarr.epaflix.com`, `navidrome.epaflix.com`, `digarr.epaflix.com`.
- Pod CIDR: `10.42.0.0/16`, from `kubectl get nodes -o jsonpath='{..podCIDR}'`. Header trust is only safe together with the NetworkPolicies in Tasks 3 and 4. Host-network pods on the same node still pass (Known limits).
- Secrets:
  - Generate each value inside the command that writes it: `vault run --name sops-age-k3s-cluster ... sops set`, or `sops -e -i`.
  - To edit the blueprint:
    1. From the repo root, decrypt it with `sops -d --output <tmp>` to a `mktemp -d` file with mode 700, and edit that copy.
    2. Re-encrypt it with `sops -e --filename-override 2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml --output 2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml <tmp>`. `vault run` discards stdout, so always use `--output`.
    3. For review, decrypt the base version and the new version to temp files and diff them. Show the diff with secret-like values redacted. Delete all plaintext afterwards.
  - Never print a value. List every new `.enc.yaml` in its `ksops-generator.yaml`.
- D1, approving a pick:
  - An album pick downloads only that album.
  - An artist pick adds the artist with nothing monitored. The owner picks albums by hand.
  - Nothing is auto-approved.
  - Release Radar results wait in the review queue.
- D2: MP3 320 and AAC 320 are the target. MP3 V0, MP3 256 and AAC 256 are allowed as a start, and Lidarr upgrades them to 320. Lossless is disabled.
- D3:
  - Navidrome and digarr are open to the group `Music users` plus `authentik Admins`. Lidarr is open to `authentik Admins` only.
  - The owner is admin in Navidrome and digarr, and only the owner's account can send to Lidarr.
- Out of scope: Unpackerr changes, Soulseek, streaming rippers, and an AI provider in digarr.
- Live changes happen only after the owner confirms: API calls, Authentik test users, Pi-hole, and merges. Record each API call in `2-k3s/08.servarr/lidarr/README.md` as method, path, non-secret fields and HTTP status only. Never record request bodies that hold keys or passwords. Read keys from Secrets inside the command.
- Argo sync: use the dry-run then real `operation` patch from `2-k3s/13.t3code-homelab/files/homelab-ssh.md`. List every Application the PR touches with its resources. PR 1 touches `postgres`, `servarr`, `authentik`, `maintenance`, `observability` and `app-of-apps` (for `app-postgres.yaml`).

## Review focus

1. A pod in another namespace calls `navidrome.servarr:4533` or `digarr.servarr:3000` with `X-authentik-username` or `X-Forwarded-User` set to the owner. Expected: the connection is refused by NetworkPolicy. Pinned in Task 3 Step 7 and Task 4 Step 7.
2. An internet client sends `X-authentik-username: <existing Navidrome user>` to `/rest/`. Expected: Subsonic error 10, not 40 or `ok`. Pinned in Task 3 Step 7.
3. The owner approves one artist pick and one album pick. Expected: the artist has 0 monitored albums, the album pick has exactly 1, no search is queued for the artist, and the artist's quality profile is `MP3-AAC`. Pinned in Task 4 Step 8.
4. A household member approves a pick, or a scheduled digarr run finishes. Expected: Lidarr's artist list and queue are unchanged. Pinned in Task 4 Step 8.
5. A private-tracker torrent in category `music` reaches ratio 1.0. Expected: Cleanuparr keeps it seeding until ratio 5.0 or 720 hours. Pinned in Task 2 Step 4.

## File map

| Files | Task | Action |
| --- | --- | --- |
| `2-k3s/06.postgres/databases/music.yaml`, `music-db-roles.enc.yaml`, `kustomization.yaml`, `ksops-generator.yaml`; `2-k3s/11.argocd/apps/app-postgres.yaml` | 1 | Create the roles, the databases and the role-password Secrets. Add `/status` ignoreDifferences for `Database` and `DatabaseRole`. |
| `2-k3s/08.servarr/_shared/secrets/servarr-postgres.enc.yaml` | 1 | Add the keys `lidarr-host`, `lidarr-port`, `lidarr-database`, `lidarr-user`, `lidarr-pgpass`. |
| `2-k3s/08.servarr/_shared/secrets/music-stack.enc.yaml`, `ksops-generator.yaml` | 1 | Create the Secret `music-stack` with the keys `lidarr-api-key`, `digarr-encryption-key`, `digarr-db-password`. |
| `2-k3s/08.servarr/lidarr/{lidarr.yaml,ingressroute.yaml,pdb.yaml,servicemonitor.yaml,network-policy.yaml,README.md}`, `_shared/storage/arr-configs.yaml` | 2 | Create the Deployment with an exportarr sidecar, plus the Service, routes, PDB, ServiceMonitor, NetworkPolicy and PVC `lidarr-config-iscsi` (5Gi). |
| `2-k3s/maintenance/cleanuparr-policy-cronjob.yaml` | 2 | Add Lidarr to `malware_blocker` and fix the selftest. |
| `2-k3s/08.servarr/configarr/configarr.yaml` | 2b | Add Lidarr profiles after a scratch dry-run. |
| `2-k3s/08.servarr/navidrome/{navidrome.yaml,ingressroute.yaml,servicemonitor.yaml,network-policy.yaml}`, `_shared/storage/media-app-configs.yaml` | 3 | Create the Deployment, Service, routes, strip Middleware, ServiceMonitor, NetworkPolicy and PVC `navidrome-data-iscsi` (5Gi). |
| `2-k3s/08.servarr/digarr/{digarr.yaml,ingressroute.yaml,network-policy.yaml}` | 4 | Create the Deployment, Service, routes, digarr-only forwardauth Middleware and NetworkPolicy. |
| `authentik-iac-blueprint.enc.yaml`, `2-k3s/07.authentik-deployment/files/check_authentik_blueprint.py` if needed | 2, 3, 4 | Add the providers, applications, bindings, outpost entries, the group `Music users` and the scope mapping `digarr-forwarded-user`. Grant add, change and delete rights for every new model type to the `ak-blueprint-check` role (`check_authentik_blueprint.py:77-79`). |
| `2-k3s/10.observability/alertmanager-config/custom-alerts.yaml`, `custom-alerts-tests.yaml` | 2, 3, 4 | Add the alerts and their promtool tests. |
| `1-proxmox/pihole/dnsmasq.d/10-epaflix.conf`, `unbound-no-aaaa-leak.conf` | 2, 3, 4 | Add an `address=/<host>/192.168.10.101` line and a local-zone line per host. |
| `2-k3s/08.servarr/kustomization.yaml`, `docs/servarr-image-pins.md` | 2, 3, 4, 5 | Add resources and pins. |

---

### Task 1: Databases and secrets (PR 1)

**Interfaces:**
- Produces Postgres roles `lidarr` and `digarr`, plus the databases `lidarr-main` and `lidarr-log` (owner `lidarr`) and `digarr` (owner `digarr`).
- `servarr-postgres` gains the `lidarr-*` keys. Its `lidarr-pgpass` uses the same format as `sonarr-pgpass`, with `*` in the database field.
- `music-stack` holds:
  - `lidarr-api-key`: 32 hex characters
  - `digarr-encryption-key`: `openssl rand -hex 32`
  - `digarr-db-password`: the bare password on one line, the format digarr's `DB_PASS_FILE` reads (`deploy/docker/README.md` v2.2.0)

- [ ] **Step 1:** Copy the shape of `2-k3s/25.fytorium/database.yaml` into `2-k3s/06.postgres/databases/music.yaml`: two `DatabaseRole` (wave -1, `passwordSecret` `lidarr-db-role` and `digarr-db-role`, retain) and three `Database` (wave 0, retain, no extensions).
- [ ] **Step 2:** Generate each password once inside one `vault run` command. Write it to the role Secret (`kubernetes.io/basic-auth`, `postgres-system`) and to its consumer key: `lidarr-pgpass`, or `digarr-db-password`.
- [ ] **Step 3:** Run `kustomize build --enable-alpha-plugins --enable-exec` on `2-k3s/06.postgres` and on `2-k3s/08.servarr`, under `vault run`. Expected: exit 0, 5 CNPG objects, and none of the rendered Secrets contains `ENC[`. Print only kinds and names.
- [ ] **Step 4:** After sync, run `kubectl get databases.postgresql.cnpg.io,databaseroles.postgresql.cnpg.io -n postgres-system`. Expected: the 5 new objects show `APPLIED true`.

### Task 2: Lidarr (PR 1)

**Interfaces:**
- Consumes the Task 1 Secrets.
- Produces Service `lidarr:8686` (API key `music-stack/lidarr-api-key`) and quality profile `MP3-AAC`. Metadata profile `Studio` holds primary type Album only.
- Root folder `/media/music`, with monitoring `None` and new albums `None`.
- qBittorrent category `music`, saving next to the Sonarr and Radarr category paths under `/media/downloads`. The builder reads those paths from qBittorrent.

- [ ] **Step 1: Deployment.** Copy `sonarr/sonarr.yaml`, without its legacy `ServerSideApply=false` annotation and without `valueFrom: null`. Keep:
  - Recreate strategy, UID 568, the `servarr-media` mount, and the passfile volume with key `lidarr-pgpass`.
  - Probes on `/ping`; Lidarr serves it.

  Env:
  - `LIDARR__POSTGRES__{HOST,PORT,MAINDB,USER}` from `servarr-postgres`
  - `LIDARR__POSTGRES__LOGDB=lidarr-log`, `LIDARR__POSTGRES__PASSWORD=""`, `PGPASSFILE`
  - `LIDARR__AUTH__METHOD=External`, `LIDARR__AUTH__APIKEY` from `music-stack`

  Lidarr's `Bootstrap.cs` binds `Lidarr:Postgres` and `Lidarr:Auth`. Sidecar `exportarr lidarr`: `PORT=9708`, `URL=http://localhost:8686`, `API_KEY` from `music-stack`, port name `metrics`.
- [ ] **Step 1b: NetworkPolicy** `lidarr-ingress`. With External auth, `/initialize.json` hands the API key to any caller (`InitializeJsonController.cs`), so any pod that reaches Lidarr could add downloads. Allow TCP 8686 only from:
  - Traefik pods
  - pods labelled `app` in `prowlarr`, `cleanuparr`, `configarr` and `digarr`, in `servarr`
  - the exportarr sidecar, which uses localhost and needs no rule
  - Prometheus, on port 9708 only

  Read the real pod labels with `kubectl -n servarr get pods --show-labels` before writing the selectors. Check: from a scratch pod in another namespace, `curl -m 5 http://lidarr.servarr:8686/initialize.json` times out, and Prowlarr's "Test" on the Lidarr app passes.
- [ ] **Step 2: Routes, Authentik and DNS.**
  - Copy `sonarr/ingressroute.yaml` with port 8686.
  - In the blueprint, copy the `sonarr` provider, application and `authentik Admins` binding with `lidarr` names. Append the provider to the outpost list and leave the existing entries.
  - Add the `lidarr.epaflix.com` Pi-hole lines in Git. The owner applies them on CT 1030 using `1-proxmox/pihole/README.md`.
- [ ] **Step 3: Profiles, live, after the owner confirms.** Through Lidarr's API:
  - Create quality profile `MP3-AAC`, with `upgradeAllowed: true` and lossless disabled. Take quality names from `GET /api/v1/qualitydefinition`.
    - Do not reuse Lidarr's default "High Quality Lossy" group: it puts V0 and 320 together, so V0 would never be upgraded (`Quality.cs`).
    - Cutoff and top item: a group `320` holding MP3-320 and AAC-320.
    - Below it, as separate allowed items: MP3-VBR-V0, MP3-256, AAC-256.
    - AAC-VBR and everything below 256 kbps are disabled.
  - Create metadata profile `Studio`: primary type Album only.
  - Set the root folder defaults, download client and category, and turn on "Write Audio Tags" for new files with MusicBrainz IDs.
  - Add the qBittorrent category `music`.
  - Add the Prowlarr application `Lidarr`, untagged, with Prowlarr's default Lidarr categories. Right after adding it, run the `prowlarr-appsync-drift` check once. Prowlarr skips indexers without music categories, and the drift SQL ignores categories, so it may flag them. If it does, give Lidarr a tag that routes only music indexers, or add a category filter to the SQL, before the next scheduled run.
  - Record each call in the README in the safe form.
- [ ] **Step 4: Cleanuparr.**
  - Add `"lidarr": ARR_LIST` to `DESIRED["malware_blocker"]`.
  - In `selftest()`, keep the disabled `lidarr` entry. Add a separate `readarr` entry and move the "unlisted arrs must survive" assertion to it. Assert `cfg["lidarr"]["blocklistPath"] == BLOCKLIST` and `len(changes) == 6`.
  - Run `python3 policy.py --selftest`. Expected: exit 0.
  - Check that the live `GET /api/configuration/malware_blocker` has a `lidarr` key.
  - After the owner confirms, add the Lidarr instance and a public `music` seeding rule in Cleanuparr through its API, then restart Cleanuparr. Expected init log: `1 private rule(s) created`, and `music (private) | private | ratio<=5.0 | seed<=720.0h`.
- [ ] **Step 5: Tag check.** After the owner confirms, add one artist through the API with `monitorNewItems: none` and one album monitored. Expected: after `RefreshArtist` there is exactly 1 monitored album. After the import, `ffprobe` shows `MUSICBRAINZ_TRACKID` on a file.
- [ ] **Step 6: Alerts.**
  - `LidarrDown`: `max(kube_deployment_status_replicas_available{namespace="servarr",deployment="lidarr"}) == 0 or absent(up{job="<ServiceMonitor job>"} == 1)`, for 15m, warning.
  - `LidarrHealthIssue`: `max(lidarr_system_health_issues) > 0`, for 1h, warning.
  - promtool tests: each alert fires on the bad series and stays silent on the good one. Run the commands in the header of `custom-alerts-tests.yaml`. Expected: `SUCCESS`.
- [ ] **Step 7: PR 1.** Add the pins and resources. Run review-gate, the owner merges, then sync.

### Task 2b: Configarr manages Lidarr profiles (PR 1b)

- [ ] **Step 1:** Run a scratch Job copied from the CronJob with `DRY_RUN=true`, `LIDARR_API_KEY`, and a `lidarr.main` block. Set `base_url: http://lidarr.servarr.svc.cluster.local:8686`. In `quality_profiles` and `metadata_profiles`, copy the live `MP3-AAC` and `Studio` from Task 2 Step 3. Run it only after the owner confirms. Expected: the Job exits 0 and reports no changes.
- [ ] **Step 2:** Add the block to `configarr.yaml` in PR 1b. Run review-gate, the owner merges, and the next hourly run completes without `ConfigarrFailed`.

### Task 3: Navidrome (PR 2)

**Interfaces:**
- Produces `navidrome:4533`, Subsonic at `/rest/`, Jellyfin API at `/jellyfin/`, and backups in `/media/backups/navidrome`.

- [ ] **Step 1: Deployment.**
  - UID 568, Recreate.
  - PVC `navidrome-data-iscsi` at `/data`. `/media/music` read-only at `/music`. `/media/backups/navidrome` at `/backups`.
  - Probes on `/ping`.

  Env:
  - `ND_MUSICFOLDER=/music`
  - `ND_EXTAUTH_USERHEADER=X-authentik-username`
  - `ND_EXTAUTH_TRUSTEDSOURCES=10.42.0.0/16`
  - `ND_PROMETHEUS_ENABLED=true`
  - `ND_JELLYFIN_ENABLED=true`
  - `ND_BACKUP_PATH=/backups`, `ND_BACKUP_SCHEDULE="0 4 * * *"`, `ND_BACKUP_COUNT=7`
  - `ND_ENABLEINSIGHTSCOLLECTOR=false`
  - `ND_LASTFM_ENABLED=false`
  - `ND_SCANNER_SCHEDULE=1h`
- [ ] **Step 2: NetworkPolicy** `navidrome-ingress`, in the shape of `2-k3s/26.vaultwarden/network-policy.yaml`. Allow TCP 4533 only from:
  - Traefik pods (`traefik-system`, `app.kubernetes.io/name=traefik`)
  - Prometheus (`observability`, `app.kubernetes.io/name=prometheus`)
  - digarr pods (`app: digarr`)
- [ ] **Step 3: Routes.**
  - `navidrome-https`, priority 10: `security-headers` plus `authentik-forwardauth`.
  - `navidrome-apps-https`, priority 20, `Host && (PathPrefix(/rest/) || PathPrefix(/jellyfin/))`: `security-headers` plus Middleware `navidrome-strip-identity`. Its `customRequestHeaders` set `X-authentik-username`, `True-Client-IP` and `X-Real-IP` to `""`.
  - The outpost route and the redirect, as for Sonarr.
  - The Pi-hole lines for the host.
- [ ] **Step 4: Authentik.**
  - Add the group `Music users`, with no members yet.
  - Add the provider, the application, and bindings for `authentik Admins` and `Music users`, with policy engine mode `any`. Add the provider to the outpost list.
  - Grant `ak-blueprint-check` add, change and delete rights for `authentik_core.group` if the role lacks them. Run `check_authentik_blueprint.py` against a decrypted temp copy if it runs offline. Otherwise wait for the next CronJob run. Expected: no 403.
- [ ] **Step 5: Observability.** Add a ServiceMonitor on port `http`, path `/metrics`. Add a `NavidromeDown` alert and test in the same shape as `LidarrDown`.
- [ ] **Step 6: PR 2.** Run review-gate, the owner merges, then sync. The owner opens Navidrome first, through Authentik, before anyone joins `Music users`. Expected: the owner's user has `isAdmin: true`.
- [ ] **Step 7: Live checks.**
  - Run `curl -s -H 'X-authentik-username: <owner Navidrome username>' 'https://navidrome.epaflix.com/rest/ping.view?f=json&v=1.16.1&c=probe'`. Expected body: `"status":"failed"` with `"code":10`.
  - The same header on `/jellyfin/Users/Me` returns 401. If that path returns 404, use another authenticated Jellyfin path. Anything other than 200 with user data passes.
  - From a scratch pod in another namespace (owner confirms), run `curl -m 5 -H 'X-authentik-username: <owner>' http://navidrome.servarr:4533/api/user`. Expected: timeout or connection refused.
  - `/` without a session redirects to Authentik.
  - A user outside `Music users` gets access denied. The owner creates and later deletes this test user, or tests with an existing non-member account.
  - A phone app play appears on ListenBrainz within 5 minutes.
  - After 04:00, `/media/backups/navidrome` holds a backup `.db` file.

### Task 4: digarr (PR 3)

**Interfaces:**
- Consumes the Task 1 database and `music-stack`, Lidarr, and Navidrome as Subsonic.
- Produces `digarr:3000`.

- [ ] **Step 1: Deployment.**
  - One replica, Recreate, `runAsUser: 10001`, `fsGroup: 10001` (upstream Helm values).
  - `emptyDir` at `/app/backups`, which pending migrations need (`docs/OPERATIONS.md` v2.2.0).
  - Startup and readiness probes on `/health`. Liveness is a TCP check on 3000, because `/health` returns 503 during a database blip.

  Env:
  - `PORT=3000`
  - `ALLOWED_ORIGIN=https://digarr.epaflix.com`
  - `DB_HOST=postgres-cluster-rw.postgres-system.svc.cluster.local`, `DB_PORT=5432`, `DB_USER=digarr`, `DB_NAME=digarr`
  - `DB_PASS_FILE`, pointing to `digarr-db-password`, mounted with mode 0440
  - `DIGARR_ENCRYPTION_KEY` from `music-stack`
  - `PROXY_AUTH_ENABLED=true`, `PROXY_AUTH_TRUSTED_PROXIES=10.42.0.0/16`
- [ ] **Step 2: NetworkPolicy** `digarr-ingress`. Allow TCP 3000 only from Traefik pods.
- [ ] **Step 3: Authentik.**
  - Add scope mapping `digarr-forwarded-user`, with expression `return {"ak_proxy": {"user_attributes": {"additionalHeaders": {"X-Forwarded-User": request.user.username}}}}`.
  - Add the provider with Sonarr's five mappings plus this one, the application, and bindings for `Music users` and `authentik Admins`. Add the provider to the outpost list.
  - Grant `ak-blueprint-check` add, change and delete rights for the scope mapping model if needed.
- [ ] **Step 4: Middleware and routes.**
  - Create Middleware `digarr-forwardauth` in `servarr`, a copy of `authentik-forwardauth` with `X-Forwarded-User` added to `authResponseHeaders`. Traefik deletes listed headers before copying, so a client value never passes. Use it on `digarr-https` only.
  - Add the outpost route, the redirect and the Pi-hole lines.
- [ ] **Step 5: Alert** `DigarrDown`, on Deployment availability. Readiness on `/health` covers app health. Add its test.
- [ ] **Step 6: PR 3.** Run review-gate, the owner merges, then sync. The owner logs in to digarr first, before anyone joins `Music users`. Expected: the owner is admin.
- [ ] **Step 7: Live checks.**
  - Run `curl -s -o /dev/null -w '%{http_code}' -H 'X-Forwarded-User: <owner>' https://digarr.epaflix.com/api/v1/auth/me`. Expected: 302.
  - Logged in as a test member, send `X-Forwarded-User: <owner>` to `/api/v1/auth/me`. Expected: the response names the member, not the owner.
  - From a scratch pod in another namespace, the same header sent straight to `digarr.servarr:3000` times out.
  - `/health` reports the `postgres` backend.
  - If the header mapping fails, use the spec's fallback: no custom mapping, `PROXY_AUTH_ENABLED=false`, the standard middleware. Record it in the PR.
- [ ] **Step 8: Owner wiring, then D1 checks.**
  - Connections: ListenBrainz (owner token), and Navidrome as Subsonic.
  - Lidarr target settings:
    - `monitorNewItems` `None`
    - quality profile `MP3-AAC`, metadata profile `Studio`
    - assigned to the owner only
  - Settings > Recommendations: auto-approve off, auto-approve monitoring `None`. Every other member's policy is "Request only". Members have no targets, so approving a member's request can never download anything. Only picks the owner approves through his own target reach Lidarr.
  - Schedule "Similar Users (Deep)" and Release Radar weekly on Tuesday.
  - Optional: the TIDAL connection, while Tidal is active.
  - Checks (Review focus 3 and 4):
    - Approve one artist pick. Lidarr shows the artist with 0 monitored albums, no queued search, and `qualityProfileId` of `MP3-AAC`.
    - Approve one album pick. Exactly 1 monitored album.
    - A test member's approval leaves Lidarr unchanged.
    - After one scheduled run, Lidarr's artist count and queue are unchanged.

### Task 5: ListenBrainz playlists in Navidrome (PR 4)

- [ ] **Step 1:** Add an initContainer, `busybox` as `tag@sha256`, to the Navidrome Deployment:
  - If `/data/plugins/listenbrainz-daily-playlist.ndp` matches the pinned sha256, exit 0.
  - Otherwise, download v6.0.0 and verify it. Fail closed on a mismatch.
  - Set `ND_PLUGINS_FOLDER=/data/plugins`.
- [ ] **Step 2: PR 4.** Run review-gate, the owner merges, then sync.
- [ ] **Step 3: Owner, as Navidrome admin.** Turn on "Library Permission" for the plugin, as the v6.0.0 release notes require. In the plugin config, add each user's ListenBrainz username and token. Select `weekly-exploration`, `weekly-jams`, `daily-jams` and the Soundiiz playlist IDs. Expected: after the next weekly run, the playlists appear and hold the tracks already in the library.

## Owner steps outside the PRs

- Now, before cancelling Tidal:
  - Create the pseudonymous ListenBrainz account and follow `troi-bot`.
  - Run the Soundiiz transfer.
- Apply the Pi-hole lines on CT 1030 after each PR.
- After Tasks 3 and 4, add household members to `Music users`. Create their Navidrome passwords and ListenBrainz accounts.
- Install the apps: Symfonium or Tempus, Amperfy or Arpeggi, Feishin.
- Keep a vault copy of `digarr-encryption-key`, using a hidden-input script the builder prepares.

## Known limits

- kube-router accepts traffic from a pod's own node. Host-network pods (in democratic-csi, kube-system, observability and tailscale) on the same node as Navidrome, digarr or Lidarr can still reach them. They arrive from the node's cni0 address, which is inside `10.42.0.0/16`, so the identity headers would be trusted. The risk is low and accepted.
- Sonarr and Radarr have the same open `/initialize.json` today. This plan fixes it for Lidarr only.
