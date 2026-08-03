# Servarr Ecosystem Deployment for k3s

This directory contains the Kubernetes manifests for deploying the complete Servarr media management ecosystem migrated from TrueNAS Electric Eel.

## Directory Structure

```
08.servarr/
├── _backups/              # PostgreSQL dumps and SQLite backup archives
│   ├── postgres-dumps/    # .sql and .sql.gz database backups
│   └── sqlite-zips/       # Original app backup zips
├── _shared/               # Shared resources across all apps
│   ├── ingress/          # Traefik IngressRoute configs
│   ├── middleware/       # Traefik middleware
│   ├── scripts/          # Deployment and migration scripts
│   ├── secrets/          # PostgreSQL and VPN credentials
│   └── storage/          # PV/PVC definitions
├── prowlarr/             # Indexer manager deployment
├── radarr/               # Movie management deployment
├── sonarr/               # TV show management deployment
├── sonarr2/              # Anime management deployment
├── bazarr/               # Subtitle management deployment
├── bazarr-autotranslate/ # Periodic scanner that requests Bazarr translations
├── lingarr/              # AI subtitle translator (Postgres-backed)
├── qbittorrent/          # Torrent client with VPN
├── jellyfin/             # Media server
├── seerr/                # Media request system (canonical; legacy jellyseerr/ retired)
├── homarr/               # Dashboard
├── wizarr/               # User invitation system
├── tdarr/                # Media transcoding
└── byparr/               # Cloudflare bypass for indexers (replaced FlareSolverr, #275)
```

## Architecture

- **Namespace**: `servarr`
- **Storage**: NFS for configs and media (568:568), local-path for transcoding cache
- **Networking**: Traefik IngressRoutes to 192.168.10.101
- **GPU**: NVIDIA RTX 2070 Super for Jellyfin and Tdarr
- **VPN**: WireGuard for qBittorrent downloads

## Applications

### Core *arr Apps
- **Sonarr**: TV shows management (port 8989) → `/tv` = `/mnt/pool1/dataset01/tvshows`
- **Sonarr2**: Anime management (port 28989) → `/animes` = `/mnt/pool1/dataset01/animes`
- **Radarr**: Movies management (port 7878) → `/movies` = `/mnt/pool1/dataset01/movies`
- **Prowlarr**: Indexer manager (port 9696)
- **Bazarr**: Subtitle management (port 6767, development branch) — configured with `translator_type: lingarr`
- **bazarr-autotranslate**: Hourly scanner that asks Bazarr to translate subs in BASE_LANGUAGES (en, ko) → TO_LANGUAGES (el)
- **Lingarr**: AI subtitle translator (port 9876) — backend for Bazarr's translation. Postgres-backed (`lingarr-main` DB). Uses custom fork image with zombie/concurrency fix pending upstream (PR #377).

### Media & Downloads
- **Jellyfin**: Media server (port 8096) with NVIDIA GPU transcoding
  - **Note**: `jellyfin.epaflix.com` is redirected to TrueNAS (192.168.10.200:30013) via `jellyfin/jellyfin-truenas-redirect.yaml`
  - The k3s Jellyfin pod runs but is not publicly accessible
- **Seerr**: Media request management (port 5055) — served at seerr.epaflix.com and the legacy jellyseerr.epaflix.com (both route to the `seerr` Service). Reuses the legacy `jellyseerr-config` PVC and `jellyseerr` Postgres DB.
- **qBittorrent**: Torrent client with WireGuard VPN (ports 8080, 8999)
- **Byparr**: Cloudflare bypass for indexers (port 8191; FlareSolverr-API drop-in, replaced FlareSolverr — #275)

### Utilities
- **Tdarr**: Media transcoding with DoVi node (ports 8265, 8266, NVIDIA GPU)
- **Homarr**: Dashboard
- **Wizarr**: User invitation system

## Storage Layout

### App Config Storage (local-path PVCs on K3s nodes)
Each app gets a `local-path` PVC for its config directory (auto-provisioned on deploy):
- `sonarr-config`, `sonarr2-config`, `radarr-config`, `prowlarr-config`
- `bazarr-config`, `jellyseerr-config` (legacy name, used by the `seerr` deployment), `qbittorrent-config`
- `jellyfin-config`, `jellyfin-cache`, `jellyfin-transcodes`
- `homarr-config`, `newtarr-config`, `cleanuparr-config`, `wizarr-config`

### NFS Media Storage (TrueNAS 192.168.10.200)
Mounted on K3s worker nodes via fstab, exposed as hostPath PVs:

| TrueNAS Export | Node Mount | Used By |
|----------------|------------|---------|
| `/mnt/pool1/dataset01/animes` | `/mnt/k3s-animes` | Sonarr2, Jellyfin |
| `/mnt/pool1/dataset01/tvshows` | `/mnt/k3s-tvshows` | Sonarr, Jellyfin |
| `/mnt/pool1/dataset01/movies` | `/mnt/k3s-movies` | Radarr, Jellyfin |
| `/mnt/pool1/dataset01/downloads` | `/mnt/k3s-downloads` | qBittorrent, all *arr apps |

## Prerequisites

### 1. PostgreSQL Databases
```bash
# Connect to PostgreSQL
PGPASSWORD="<POSTGRES_PASSWORD>" psql -h 192.168.10.105 -U postgres

# Create databases
CREATE DATABASE "sonarr-main";
CREATE DATABASE "sonarr2-main";
CREATE DATABASE "radarr-main";
CREATE DATABASE "prowlarr-main";
-- "jellyseerr" DB/user are legacy names reused by the canonical `seerr` deployment (no migration)
CREATE DATABASE "jellyseerr";

# Create users with secure passwords
CREATE USER sonarr WITH PASSWORD 'YOUR_SECURE_PASSWORD';
CREATE USER sonarr2 WITH PASSWORD 'YOUR_SECURE_PASSWORD';
CREATE USER radarr WITH PASSWORD 'YOUR_SECURE_PASSWORD';
CREATE USER prowlarr WITH PASSWORD 'YOUR_SECURE_PASSWORD';
CREATE USER jellyseerr WITH PASSWORD 'YOUR_SECURE_PASSWORD';

# Grant permissions
GRANT ALL PRIVILEGES ON DATABASE "sonarr-main" TO sonarr;
GRANT ALL PRIVILEGES ON DATABASE "sonarr2-main" TO sonarr2;
GRANT ALL PRIVILEGES ON DATABASE "radarr-main" TO radarr;
GRANT ALL PRIVILEGES ON DATABASE "prowlarr-main" TO prowlarr;
GRANT ALL PRIVILEGES ON DATABASE "jellyseerr" TO jellyseerr;
```

### 3. NVIDIA Device Plugin
```bash
kubectl apply -f https://raw.githubusercontent.com/NVIDIA/k8s-device-plugin/v0.14.0/nvidia-device-plugin.yml

# Verify GPU is available
kubectl get nodes -o yaml | grep nvidia.com/gpu
```

### 4. WireGuard Config
WireGuard config is stored in `secrets.yml`. Create the K8s secret from `_shared/secrets/wireguard-secret.yaml`.

## Deployment Order

1. **Namespace and Secrets**
   ```bash
   kubectl apply -f namespace.yaml
   kubectl apply -f secrets/
   ```

2. **Storage (PV/PVC)**
   ```bash
   kubectl apply -f storage/
   kubectl get pvc -n servarr  # Wait for all to be Bound
   ```

3. **Applications**
   ```bash
   # Deploy in order (some apps depend on others)
   kubectl apply -f prowlarr/
   kubectl apply -f byparr/
   kubectl apply -f qbittorrent/

   # Wait for download client to be ready
   kubectl wait --for=condition=ready pod -l app=qbittorrent -n servarr --timeout=300s

   # Deploy *arr apps
   kubectl apply -f sonarr/
   kubectl apply -f sonarr2/
   kubectl apply -f radarr/
   kubectl apply -f bazarr/

   # Deploy media apps
   kubectl apply -f jellyfin/
   kubectl apply -f seerr/

   # Deploy utilities
   kubectl apply -f homarr/
   ```

4. **Ingress Routes**
   ```bash
   kubectl apply -f ingress/
   ```

## Post-Deployment Configuration

### 1. Verify qBittorrent VPN
```bash
# Check VPN connection
kubectl exec -n servarr -it deployment/qbittorrent -- curl ifconfig.me
# Should show VPN IP, NOT 192.168.x.x
```

### 2. Configure Download Clients in *arr Apps
Update each app (Sonarr, Sonarr2, Radarr) to use:
- Host: `qbittorrent`
- Port: `8080`
- Category: `tv` / `anime` / `movies` respectively

> **In-cluster clients MUST use the internal Service URL (`qbittorrent:8080`),
> never the public `qbittorrent.epaflix.com`.** The public hostname sits behind
> Authentik forward-auth (#176) with only a priority-20 `/api` bypass. A client
> that probes any other path gets the Authentik login HTML instead of a JSON
> response. This silently broke Cleanuparr for 27 days (its qBittorrent client
> probes the legacy `/version/api` endpoint) — see the "Cleanuparr blind for 27
> days behind forward-auth" incident in `RECOVERY-newtarr-cleanuparr.md`. The
> same rule applies to every service-to-service call (`http://sonarr:8989`,
> etc.); the `.epaflix.com` URLs under *Access URLs* below are browser-only.
>
> Confirmed a second time on 2026-07-26: bazarr pointed at
> `sonarr.epaflix.com:443` / `radarr.epaflix.com:443` and its SignalR event feed
> died every few hours (20 restarts in 4d10h) because `/signalr/negotiate`
> returns the Authentik 302 as `text/html` and bazarr parses it as JSON. Its
> `/api/v3` calls worked the whole time, which is why it looked half-healthy.
> Fixed by repointing to `sonarr.servarr.svc.cluster.local:8989` and
> `radarr.servarr.svc.cluster.local:7878` with `ssl: false` (#465, #466).
> **When a client looks forward-auth-broken, check which path it calls first —
> `/api/*` works, everything else on a gated host does not.**

### DNS policy (decided, #466)

**In-cluster clients use internal Kubernetes Service DNS
(`<svc>.servarr.svc.cluster.local:<port>` or the short `<svc>:<port>` form) as
the actual connection target. The public `*.epaflix.com` hostname is kept only
in whichever field an app exposes for its own outbound UI links -
`external_url` in cleanuparr's schema, or the equivalent - never as the field
the app uses to make the call.**

This is already live in two places: bazarr's sonarr/radarr fields above
(#465) and cleanuparr's qBittorrent download-client entry. #468 applies the
same split to cleanuparr's three `arr_instances` rows (`url` = internal
Service DNS, `external_url` = the public hostname), which already has the
column - it was just unset.

The alternative - widening the Authentik forward-auth `/api` bypass to cover
`/signalr` or other non-`/api` paths - is deliberately **not** being done.
It would expose an unauthenticated endpoint publicly, and it's the same
one-bypass-per-symptom pattern that caused this three times already
(qBittorrent `/version/api`, bazarr `/signalr`, and any future integration
that isn't `/api`-only). Moving callers to internal DNS instead is lower
risk: it's plain app config, fails loudly on a typo (connection refused, not
a silent auth gap), and is reversible in seconds.

### 3. Configure Prowlarr Sync
Add applications in Prowlarr:
- Sonarr: `http://sonarr:8989`
- Sonarr2: `http://sonarr2:8989`
- Radarr: `http://radarr:7878`

Use existing API keys from TrueNAS configs.

### 4. Jellyfin GPU Transcoding
In Jellyfin settings:
- Dashboard → Playback → Transcoding
- Hardware acceleration: NVIDIA NVENC
- Enable hardware decoding for all codecs

### 5. Verify Hardlinks
```bash
# Download a file and let Sonarr/Radarr move it
# Then check:
ls -i /mnt/pool1/dataset01/downloads/completed/show.mkv
ls -i /mnt/pool1/dataset01/tvshows/Show/Season\ 01/show.mkv
# Inode numbers should match = hardlink successful
```

## Rotating the radarr / sonarr / sonarr2 API keys (#712)

Run twice as written (#712, then #740 hours later after the first rotation's
values leaked). The second pass needed no changes to the steps below, so treat
them as known-good.

Each *arr's API key is consumed by **seven** other things, and five of them keep
their copy **PVC-only or in the app's own DB** - so a rotation that only touches
git leaves those five authenticating with a dead key. Values live in the
git-ignored `secrets.yml` as `radarr_api_key`, `sonarr_api_key`,
`sonarr2_api_key`. Never print a key: pass it via stdin/env/file, compare with
`sha256`, print lengths not values (see `.github/instructions/general.instructions.md`).

**1. Change the key in the app itself.** `PUT /api/v3/config/host` **silently
ignores** an `apiKey` change (returns `202`, config.xml unchanged - verified on
Radarr 6.3 / Sonarr 4.0). The only working route is the file:

```bash
# NEW is read from stdin so the value never lands in argv
kubectl -n servarr exec -i deploy/radarr -c radarr -- sh -c '
  NEW=$(cat); export NEW
  cp -p /config/config.xml /config/config.xml.bak-rotate
  awk "{gsub(/<ApiKey>[^<]*<\/ApiKey>/, \"<ApiKey>\" ENVIRON[\"NEW\"] \"</ApiKey>\"); print}" \
     /config/config.xml > /tmp/c && cat /tmp/c > /config/config.xml && rm -f /tmp/c
  sed -n "s:.*<ApiKey>\([^<]*\)</ApiKey>.*:\1:p" /config/config.xml | tr -d "\n" | sha256sum' < /path/to/newkey
kubectl -n servarr rollout restart deploy/radarr    # the file is only read at start
```

Proof it took: `GET /api/v3/system/status` returns `200` with the new key and
`401` with the old one.

**2. Then every consumer, in one pass.** Git-side (this repo) is ArgoCD-managed;
the rest is live-only.

| Consumer | Where the copy lives | How to update |
|---|---|---|
| unpackerr (`UN_RADARR_0_API_KEY`, `UN_SONARR_0_API_KEY`, `UN_SONARR_1_API_KEY`) | `_shared/secrets/unpackerr-secret.enc.yaml` (git, SOPS) | edit + `sops -e -i`, merge; `reloader` rolls the Deployment |
| `orphan-census` CronJob | same Secret via `secretKeyRef` | nothing - fresh pod per run |
| newtarr | `_shared/secrets/newtarr-config-seed.enc.yaml` (git) **and** live `/config/sonarr.json`, `/config/radarr.json` **and** `huntarr.db` rows `app_configs` (`radarr`, `sonarr`) | patch seed + both live JSONs + both DB rows identically, then restart (the seed initContainer is non-clobber, and `newtarr-config-drift` compares seed vs live JSON - they must match byte-for-byte) |
| seerr | `_shared/secrets/seerr-config-seed.enc.yaml` (git) **and** live `/app/config/settings.json` | seed edit + `PUT /api/v1/settings/radarr/0`, `/sonarr/0`, `/sonarr/1` (drop `id` from the body - it is read-only); verify with `POST /api/v1/settings/{radarr,sonarr}/test` |
| prowlarr Applications | `prowlarr-main` Postgres, `"Applications"` rows (id 3 Radarr, 1 Sonarr, 2 Sonarr2) | `GET`/`PUT /api/v1/applications/{id}` with the new `apiKey` field. The API **masks** secret fields as `********` on read, so a read-back proves nothing - verify with `POST /api/v1/applications/testall` (`isValid: true`) |
| bazarr | live `/config/config/config.yaml` → `sonarr.apikey`, `radarr.apikey` (PVC-only, #465) | patch the file + `rollout restart`; verify `GET /api/system/health` is `[]` after triggering `update_series` / `update_movies`. Bazarr's **own** API key is `auth.apikey` in that same file |
| cleanuparr | live `/config/cleanuparr.db` → `arr_instances.api_key` (PVC-only, #196; config API is JWT-gated) | `UPDATE arr_instances SET api_key=?` + `rollout restart`; verify the log line `Arr instance <id> (Sonarr) health changed: Healthy` for all three |
| lingarr | `lingarr-main` Postgres, `settings.radarr_api_key` / `sonarr_api_key`, stored **encrypted** (~176 chars) | `POST /api/setting/encrypted` `{"Key":...,"Value":...}` (port-forward :9876 - lingarr has no API-key auth, only Authentik). **A plaintext grep will not find these** - that is why a value-scan alone is not a complete consumer hunt; read back with `POST /api/setting/multiple/encrypted/get` |

Ruled out on 2026-08-03 (evidence, not assumption): homarr (`integration` and
`integrationSecret` tables are **empty** - 0 integrations), bazarr-autotranslate,
byparr, qbittorrent, vpn-picker, jellyfin, wizarr (not deployed), plus a
cluster-wide value scan of **all 97 Secrets and 86 ConfigMaps** and all 26
repo `*.enc.yaml` files, and a `pg_dump` value scan of all 10 non-template
databases.

## Access URLs

### Internet (via Cloudflare + Traefik 192.168.10.101)
- Jellyfin: https://jellyfin.epaflix.com
- Seerr: https://seerr.epaflix.com (legacy https://jellyseerr.epaflix.com also resolves to seerr)

### Internal LAN (*.epaflix.com → 192.168.10.101 via Pi-hole)
- Sonarr: http://sonarr.epaflix.com
- Sonarr2: http://sonarr2.epaflix.com
- Radarr: http://radarr.epaflix.com
- Prowlarr: http://prowlarr.epaflix.com
- Bazarr: http://bazarr.epaflix.com
- qBittorrent: http://qbittorrent.epaflix.com
- Tdarr: http://tdarr.epaflix.com
- Homarr: http://homarr.epaflix.com
- Wizarr: http://wizarr.epaflix.com

## Migration from TrueNAS

> **Completed.** The TrueNAS → K3s migration was done in January 2026. Apps use `local-path` PVCs for config and PostgreSQL for databases. See `qbittorrent/MIGRATION-COMPLETE.md` for details.

## Troubleshooting

### Pods in CrashLoopBackOff
```bash
kubectl logs -n servarr <pod-name>
kubectl describe pod -n servarr <pod-name>
```

### PVC not binding
```bash
kubectl get pv,pvc -n servarr
kubectl describe pvc -n servarr <pvc-name>
# Check NFS mount on TrueNAS is accessible
```

### VPN not working
```bash
kubectl exec -n servarr -it deployment/qbittorrent -- bash
# Inside pod:
ip addr  # Check wg0 interface exists
curl ifconfig.me  # Should show VPN IP
ping 8.8.8.8  # Test connectivity
```

### GPU not detected
```bash
kubectl get nodes -o yaml | grep -A10 allocatable
# Look for nvidia.com/gpu
kubectl describe node <gpu-node>
```

### ArgoCD SyncFailed on a volumeMounts list-restructure (SSA list-merge)

**Symptom**: `app-servarr` (App-wide `ServerSideApply=true`) sits SyncFailed / Running, `selfHeal` can't converge, with an error like `spec.template.spec.containers[0].volumeMounts[1].name: Required value` or phantom empty-name entries. This happens when an *arr Deployment's `volumeMounts` **list** is restructured — e.g. the #195/#242 collapse of two media subPath mounts into one `/media` mount on `sonarr`, `sonarr2`, `radarr`. SSA strategic-merge can't reconcile the change of the list merge-key.

**Standard remediation** (out-of-band, one-time per restructure): replace the *whole* `volumeMounts` array to match git via a JSON patch, then let ArgoCD reconverge. Pull the exact `value` array from the Deployment manifest in git (currently `config` → `/config` and `media` → `/media`):
```bash
kubectl -n servarr patch deployment sonarr --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/volumeMounts","value":[
        {"name":"config","mountPath":"/config"},
        {"name":"media","mountPath":"/media"}
      ]}]'
# repeat for sonarr2 and radarr
```
After the patch the live object matches git, ArgoCD reconverges to Synced + Healthy, and the result is durable (`selfHeal` keeps it).

**Why not `Replace=true`**: `argocd.argoproj.io/sync-options: Replace=true` was deliberately **not** adopted. It forces a full-object `kubectl replace` on *every* sync (not just restructures), risking steady-state churn, dropped server-managed fields, and conflicts with the App-level ServerSideApply plus the `/spec/replicas` `ignoreDifferences` — all to cover a rare event. The patch-on-restructure remediation is preferred (same posture as #147).

Cross-links: #243, #147 (same SSA list-merge class, fixed git-durably in PR #198), #195/#242 (the cutover that triggered it), #240.

### Orphaned `spec.template.spec.volumes` entries (the silent sibling)

**Symptom**: none - and that is the whole problem. A `volumes` entry that no `volumeMounts` references does **not** fail SSA and does **not** show OutOfSync. The App stays Synced/Healthy while the live Deployment carries a volume git no longer declares. During #247, `sonarr`, `sonarr2` and `radarr` each still carried leftover `servarr-media-{animes,tvshows,movies,downloads}` volumes from before the #195 `/media` consolidation - unreferenced, invisible, sitting there 7+ weeks until #247 tripped over them. The cost is not just clutter: an orphaned volume keeps its PVC bound, so a PVC teardown either fails or quietly leaves dead storage attached.

**Detection** - one command over every servarr Deployment. Git-independent: any declared volume that no container mounts is an orphan.
```bash
kubectl -n servarr get deploy -o json | jq -r '
  .items[]
  | ([.spec.template.spec.containers[].volumeMounts[]?.name]
     + [.spec.template.spec.initContainers[]?.volumeMounts[]?.name]) as $mounted
  | [.spec.template.spec.volumes[]?.name] as $declared
  | select(($declared - $mounted) | length > 0)
  | "\(.metadata.name): orphaned volumes \($declared - $mounted)"'
```
No output = clean. (Run 2026-08-03: clean across all 17 servarr Deployments - #247's out-of-band patch held.)

**Remediation** - same shape as the `volumeMounts` recipe above: replace the whole list with git's. Copy the exact array out of the Deployment manifest in git (`sonarr/sonarr.yaml` and friends), do not hand-write it:
```bash
kubectl -n servarr patch deployment sonarr --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/volumes","value":[
        {"name":"config","persistentVolumeClaim":{"claimName":"sonarr-config"}},
        {"name":"media","persistentVolumeClaim":{"claimName":"servarr-media"}}
      ]}]'
```

**Why no drift CronJob for this**: `newtarr-config-drift` and `cleanuparr-blocklist-drift` exist because that state lives only on a PVC - git cannot see it, so only a job can compare. `volumes` is git-declared state; the gap in #247 was that nobody looked, not that looking was impossible. Cheaper answer: run the detection one-liner whenever servarr storage changes (adding/removing a mount, tearing down a PVC), and treat it as a review step on any PVC-removal PR.

Cross-links: #516, #247 (found the orphans), #243/#147 (the `volumeMounts` sibling above), #195/#242 (the consolidation that left them behind).

## Notes

- All apps run as PUID=568, PGID=568 (matching TrueNAS permissions)
- Timezone: Europe/Oslo
- Existing API keys from TrueNAS are preserved in config files
- Bazarr runs on development branch for latest features
- Hardlinks require full download volume mount (no subPath)
