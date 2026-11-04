# Lidarr

Lidarr manages music downloads and imports into `/media/music` on `servarr-media`. It runs as UID/GID 568 with a 5Gi `truenas-iscsi` config PVC. CNPG owns `lidarr-main` and `lidarr-log`.

The app reads PostgreSQL settings and a read-only passfile from `servarr-postgres`. Its password environment variable stays empty so startup connection-string logs do not expose it. The API key comes from `music-stack/lidarr-api-key`.

`https://lidarr.epaflix.com` uses Traefik forward auth and permits only `authentik Admins`. Lidarr uses External authentication, so its internal `/initialize.json` endpoint can reveal the API key. `lidarr-ingress` permits TCP 8686 only from Traefik and same-namespace Prowlarr, Cleanuparr, Configarr and digarr pods. Prometheus can reach only exportarr on TCP 9708. The ServiceMonitor sets `jobLabel: app`, yielding `job="lidarr"` for `LidarrDown`.

Kube-router still permits traffic from a pod's own node, including host-network pods on that node. This accepted cluster limit also applies here. Other Servarr apps are unchanged.

## Live steps still to do

All live changes need owner confirmation. This PR declares resources only. Do not use these steps as permission to apply manifests or call write APIs.

- A1. After review and merge, sync the reviewed resources through Argo CD. PR 1 spans `postgres`, `servarr`, `authentik`, `maintenance`, `observability` and `app-of-apps`. Wait for the CNPG databases and Secrets before starting Lidarr. Verify `/ping`, exportarr metrics, the ServiceMonitor target and Authentik access. Confirm a non-admin cannot enter.
- A2. The owner applies Pi-hole and Unbound changes on CT 1030 using `1-proxmox/pihole/README.md`. Verify that `lidarr.epaflix.com` resolves to `192.168.10.101` and does not leak AAAA lookups upstream.
- A3. Test the NetworkPolicy after deployment. From a scratch pod in another namespace, `curl -m 5 http://lidarr.servarr:8686/initialize.json` must time out. Do not print response bodies or keys if the request unexpectedly succeeds. Prowlarr's Lidarr application test must pass after A6.
- A4. Create quality profile `MP3-AAC` using names from `GET /api/v1/qualitydefinition`. Enable upgrades. Put MP3-320 and AAC-320 in a top group named `320`, also the cutoff. Below it, allow MP3-VBR-V0, MP3-256 and AAC-256 as separate items. Disable AAC-VBR, everything below 256 kbps and all lossless formats. Do not reuse the default High Quality Lossy group, which groups V0 with 320 and prevents that upgrade. Create metadata profile `Studio` with primary type Album only.
- A5. Add root folder `/media/music` with monitoring `None` and new albums `None`. Enable Write Audio Tags for new files with MusicBrainz IDs. Read the existing Sonarr and Radarr qBittorrent category paths, then add category `music` next to them under `/media/downloads` and configure Lidarr's download client.
- A6. Add an untagged Prowlarr application `Lidarr` using its default Lidarr categories. Immediately run `prowlarr-appsync-drift` once. Its SQL ignores categories, so indexers without music categories can produce false findings. If needed, tag Lidarr to route only music indexers or review a category filter for that SQL before the next scheduled run.
- A7. Confirm Cleanuparr's malware blocker has a `lidarr` key, then add the Lidarr instance and a public `music` seeding rule. Restart Cleanuparr after confirmation. Expect `1 private rule(s) created` and `music (private) | private | ratio<=5.0 | seed<=720.0h`. The policy CronJob now covers Lidarr's executable-only blocklist while preserving unrelated settings.
- A8. Add one artist with `monitorNewItems: none` and one monitored album. After `RefreshArtist`, verify exactly one album is monitored. After import, verify `MUSICBRAINZ_TRACKID` with `ffprobe` on a file.

Read API keys from Kubernetes Secrets inside each consuming command. Record API calls below using only method, path, non-secret fields and HTTP status. Never include keys, passwords, credential-bearing request bodies or response bodies that may expose them. Configarr-managed Lidarr profiles are a later PR, not part of this setup.

## Live changes
