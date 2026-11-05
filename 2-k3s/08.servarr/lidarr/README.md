# Lidarr

Lidarr manages music downloads and imports into `/media/music` on `servarr-media`. It runs as UID/GID 568 with a 5Gi `truenas-iscsi` config PVC. CNPG owns `lidarr-main` and `lidarr-log`.

The app reads PostgreSQL settings and a read-only passfile from `servarr-postgres`. Its password environment variable stays empty so startup connection-string logs do not expose it. The API key comes from `music-stack/lidarr-api-key`.

`https://lidarr.epaflix.com` uses Traefik forward auth and permits only `authentik Admins`. Lidarr uses External authentication, so its internal `/initialize.json` endpoint can reveal the API key. `lidarr-ingress` permits TCP 8686 only from Traefik and same-namespace Prowlarr, Cleanuparr, Configarr and digarr pods. Prometheus can reach only exportarr on TCP 9708. The ServiceMonitor sets `jobLabel: app`, yielding `job="lidarr"` for `LidarrDown`.

Kube-router still permits traffic from a pod's own node, including host-network pods on that node. This accepted cluster limit also applies here. Other Servarr apps are unchanged.

## Setup rules

Read API keys from Kubernetes Secrets inside each consuming command. Record API calls below using only method, path, non-secret fields and HTTP status. Never include keys, passwords, credential-bearing request bodies or response bodies that may expose them. Configarr-managed Lidarr profiles are a later PR, not part of this setup.

## Live changes

All done 2026-11-04 and 2026-11-05 after owner confirmation, at merge `f3e03858` (#2199).

Argo CD: `app-of-apps`, `postgres`, `servarr`, `authentik` and `observability` synced as dry run, then for real, listing only this PR's resources. `maintenance` auto-synced to the same revision. The CNPG roles failed once with `secrets ... forbidden` until the operator added the new role Secrets to its Role, then all five objects reported `APPLIED true`. The `servarr-postgres` change made Reloader restart Sonarr, Radarr, Prowlarr, Bazarr, Lingarr and Seerr. The first `cleanuparr-policy` run exited 1 while it enabled Lidarr in the malware blocker; later runs exit 0.

Checks: Lidarr 3.1.6.5079 on `postgreSQL` with `authentication: external`. `/api/v1/system/status` returns 200 with the key and 401 without. exportarr publishes `lidarr_system_health_issues`. From Prowlarr, `/ping` returns 200; a scratch `curlimages/curl` pod in `default` gets connection refused. `https://lidarr.epaflix.com/` redirects to Authentik.

Pi-hole on CT 1030: backup `10-epaflix.conf.bak-20261104-lidarr` and `/root/no-aaaa-leak.conf.bak-20261104-lidarr`. Copied `dnsmasq.d/10-epaflix.conf` (live equals repo), inserted the `lidarr.epaflix.com.` local-zone after `prowlarr`, `unbound-checkconf` clean, `unbound-control reload`, `systemctl restart pihole-FTL`. `lidarr.epaflix.com` A is `192.168.10.101`, AAAA empty.

| App | Method and path | Non-secret fields | Status |
| --- | --- | --- | --- |
| Lidarr | `POST /api/v1/qualityprofile` | `MP3-AAC` id 4, upgrades on, cutoff group `320` (MP3-320, AAC-320); also allowed MP3-256, AAC-256, MP3-VBR-V0 | 201 |
| Lidarr | `POST /api/v1/metadataprofile` | `Studio` id 3: Album; secondary Studio; Official | 201 |
| Lidarr | `POST /api/v1/downloadclient` | `qBittorrent` id 1, host `qbittorrent:8080`, Web API key auth, category `music`, remove failed | 201 |
| Lidarr | `POST /api/v1/rootfolder` | `/media/music` id 1, quality 4, metadata 3, monitor `none`, new items `none` | 201 |
| Lidarr | `PUT /api/v1/config/metadataprovider/1` | `writeAudioTags: newFiles` | 202 |
| Lidarr | `PUT /api/v1/config/naming/1` | `renameTracks: true` (standard format puts each album in its own folder) | 202 |
| qBittorrent | `POST /api/v2/torrents/createCategory` | `music`, empty save path like `tv-sonarr` and `radarr` | 200 |
| Prowlarr | `POST /api/v1/applications` | `Lidarr` id 2, `fullSync`, `http://lidarr:8686`, categories 3000-3060. First try 500 (`PK_Applications`) | 201 |
| Postgres `prowlarr-main` | `setval` to `max("Id")` | `ApplicationIndexerMapping` 37, `IndexerStatus` 17, `ApplicationStatus` 3, `Applications` 3. Sequences lagged before this work; audit `drift_count=0` after | ok |
| Prowlarr | `POST /api/v1/tag`, `PUT /api/v1/indexer/bulk`, `PUT /api/v1/applications/bulk` | tag `music` id 36 on IPTorrents, Mpellas, Nyaa.si, The Pirate Bay, TorrentDay, TorrentDownload; Lidarr app tags `[36]`. AvistaZ has no music categories and stays out | 201, 202, 202 |
| Prowlarr | `POST /api/v1/command` | `ApplicationIndexerSync`; Lidarr has 6 indexers; `prowlarr-appsync-drift` reports converged | 201 |
| Cleanuparr | `POST /api/configuration/lidarr/instances` | `Lidarr`, `http://lidarr.servarr.svc.cluster.local:8686/`, version 3 (test 200) | 201 |
| Cleanuparr | `POST /api/seeding-rules/<qbittorrent client>` | `music`, Public, priority 4, ratio 1, max 400 h; restart created `music (private)` priority 14, ratio 5, max 720 h | 201 |
| Lidarr | `POST /api/v1/artist`, `PUT /api/v1/artist/editor`, `PUT /api/v1/album/monitor` | test: Daft Punk, monitor `none`, new items `none`; only *Discovery* monitored; after `RefreshArtist` 1 of 4 monitored, queue empty | 201, 202, 202 |
| Lidarr | `POST /api/v1/command` | `AlbumSearch` grabbed an MP3 320 release; 14 tracks imported with MusicBrainz album, artist, release group, track and recording (UFID) IDs; `RenameFiles` moved them to `Daft Punk/Discovery (2001)/` | 201 |

New music indexers in Prowlarr need the `music` tag to reach Lidarr.
