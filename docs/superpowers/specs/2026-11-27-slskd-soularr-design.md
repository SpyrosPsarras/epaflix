# Soulseek as a second music source for Lidarr

Status: draft for Spyros's approval.

## Intent

Lidarr misses 187 of 290 monitored albums. A test search on 15 random missing albums on 2026-11-26 found no torrent release for 12 of them on any indexer, so better torrent settings will not close the gap. Soulseek is strong for older and less popular albums. slskd connects to Soulseek, and Soularr takes Lidarr's wanted list, searches slskd, and tells Lidarr to import what it downloads. Lidarr stays the only library manager.

## Decisions by Spyros (2026-11-27)

- D1. slskd runs in its own pod with its own AirVPN tunnel, not inside the qBittorrent pod.
- D2. slskd shares the whole music library (`/media/music`), read-only.
- D3. The agent generates the Soulseek account, stores it in a new vault item and in the SOPS secret, and never shows it in chat.
- D4. The slskd web UI is at `slskd.epaflix.com`, behind Authentik like Lidarr.
- D5. AirVPN has a free slot for a third always-on tunnel.
- D6. slskd deletes Soulseek files after 30 days, both completed and incomplete. Build note: slskd 0.26.2 measures age from last write, not last access. Downloads are not modified after they finish, so the effect is the same. Cleanuparr can't do this: it supports no Soulseek client, and its orphan scan would move albums before Soularr imports them.

## Approaches considered for the VPN (D1)

1. slskd as a container in the qBittorrent pod, sharing Bluetit. Least YAML. Spyros rejected it: it ties slskd to qBittorrent's pod.
2. **Chosen:** own pod with a gluetun sidecar, the same as SearXNG's `airvpn-proxy` (2-k3s/14.searxng). gluetun drops traffic outside the tunnel, and slskd's VPN integration supports gluetun natively: slskd stays off Soulseek until gluetun reports the tunnel up.
3. Own pod with a copy of the Bluetit container and the server-switching agent. About 250 lines copied, and the agent shares a ranking file with qBittorrent's agent. Rejected: more code than option 2 for the same result.

## Design

### slskd (`2-k3s/08.servarr/slskd/`)

- Deployment `slskd`, 1 replica, `Recreate`. Image `slskd/slskd` 0.26.2, pinned by digest like the other servarr images (`docs/servarr-image-pins.md`). Runs as UID/GID 568, like the other media apps.
- gluetun as a native sidecar (init container with `restartPolicy: Always` and a startup probe), so slskd starts only after the tunnel is up. Image `qmcgaw/gluetun` v3.41.3, the same pin as SearXNG. Settings:
  - AirVPN over WireGuard, with countries Netherlands, Germany and Sweden.
  - AirVPN device `slskd`.
  - `FIREWALL_VPN_INPUT_PORTS` set to the forwarded port.
  - `FIREWALL_INPUT_PORTS=5030,9999`, so the cluster can reach the web UI, metrics and gluetun's health port.
  - `FIREWALL_OUTBOUND_SUBNETS=10.42.0.0/16`, the `postStart` command that clears stale routing rules, and `dnsPolicy: None` with nameserver 127.0.0.1, all copied from SearXNG's `airvpn-proxy`.
- `enableServiceLinks: false` on the pod. Otherwise the Service `slskd` injects `SLSKD_PORT` and similar variables, and slskd reads every `SLSKD_*` variable as a setting.
- The control server is protected with an API key (`HTTP_CONTROL_SERVER_AUTH_DEFAULT_ROLE`). slskd is configured with `SLSKD_VPN=true`, `SLSKD_VPN_GLUETUN_URL=http://127.0.0.1:8000` and that API key.
- slskd is configured entirely through environment variables:
  - `SLSKD_SLSK_LISTEN_PORT` is the AirVPN forwarded port.
  - `SLSKD_SHARED_DIR=/media/music`.
  - `SLSKD_DOWNLOADS_DIR=/media/downloads/soulseek`.
  - `SLSKD_INCOMPLETE_DIR=/media/downloads/soulseek-incomplete`.
  - `SLSKD_NO_HTTPS=true` and `SLSKD_METRICS=true`.
- File retention (D6) exists only in slskd's YAML config, not as environment variables (`FileRetentionOptions` in `Options.cs` at 0.26.2). A ConfigMap holds a small `slskd.yml` with `retention.files.complete: 43200`, `retention.files.incomplete: 43200` and `retention.search: 1440`, mounted read-only. Remote configuration stays off, so slskd never writes to it.
- Mounts from PVC `servarr-media`: subPath `music` read-only at `/media/music`, and subPath `downloads` at `/media/downloads`. Lidarr sees the same paths, so no path mapping is needed.
- slskd keeps its state (SQLite database, share cache) on a new 5Gi PVC `slskd-config-pithari` (`truenas-iscsi`), defined in `_shared/storage/arr-configs.yaml`.
- Web auth matches Lidarr: `SLSKD_NO_AUTH=true` and `SLSKD_METRICS_NO_AUTH=true`. The protection is Authentik in front of the web UI, plus a NetworkPolicy that admits port 5030 only from Traefik, Soularr and Prometheus. That way slskd needs no API key and no metrics password.
- Service `slskd` on port 5030. ServiceMonitor `slskd` scraping `/metrics`, with `jobLabel: app`. A PDB like Lidarr's.
- IngressRoutes copied from `lidarr/ingressroute.yaml` (https with `authentik-forwardauth`, the outpost path, and an http→https redirect).

### Soularr (`2-k3s/08.servarr/soularr/`)

- Deployment `soularr`, 1 replica. Image `mrusse08/soularr` v1.2.3, pinned by digest. The v1.2.3 tag has the environment-variable expansion, `--config-dir`, `--var-dir` and `failed_import_denylist` this design relies on (checked 2026-11-27). `WEBUI_ENABLED=false`, because the config lives in Git. `SCRIPT_INTERVAL=300`.
- `config.ini` lives in a ConfigMap mounted at `/config`. Soularr's config parser expands environment variables, so the file uses `${LIDARR_API_KEY}`. That variable comes from the existing `music-stack` secret, key `lidarr-api-key`. The container runs with `--config-dir /config --var-dir /data`.
- `/data` holds the search-attempt and failed-import lists on a new 1Gi PVC `soularr-data-pithari`, so a restart does not search the same albums again.
- Mounts `downloads` at `/media/downloads`, because Soularr renames the folders it downloads before Lidarr imports them.
- Config values that differ from the example:
  - `[Lidarr] host_url = http://lidarr:8686` and `download_dir = /media/downloads/soulseek`.
  - `[Slskd] host_url = http://slskd:5030` and `download_dir = /media/downloads/soulseek`.
  - `[Slskd] api_key` gets a fixed placeholder, because slskd runs without auth.
  - `allowed_filetypes = mp3 320,flac,flac 24/44.1,flac 24/48,flac 24/96,flac 24/192`. MP3 320 comes first, to match the Lidarr profile.
  - `search_source = missing`, `failed_import_denylist = True`, `log_to_file = False`, with logs going to stdout, where Loki collects them.

### Secrets and the vault

- New SOPS file `_shared/secrets/slskd.enc.yaml`, listed in `ksops-generator.yaml`. It holds Secret `slskd` with these keys:
  - `SLSKD_SLSK_USERNAME` and `SLSKD_SLSK_PASSWORD`, generated.
  - The gluetun control-server role JSON and `SLSKD_VPN_GLUETUN_API_KEY`, generated.
  - `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_PRESHARED_KEY` and `WIREGUARD_ADDRESSES`, fetched from the AirVPN generator API for device `slskd` with the `airvpn.org` vault item's `API` field, as in the SearXNG plan.
- A new vault login item holds the Soulseek username and password. Its notes say the SOPS file is the source for the other values. The WireGuard keys are fetched with the AirVPN API key from the vault. Vault item names stay out of the repo (rule S2).
- Values are generated or fetched inside the command that writes them and are never printed.

### Network and DNS

- NetworkPolicy `slskd-ingress`: port 5030 only from Traefik, `app: soularr` and Prometheus. Kubelet probes come from the node and need no rule (CI's `test-servarr-network-policies.py` requires a `from` on every rule).
- `lidarr-ingress` also admits `app: soularr` on 8686.
- Soularr gets no ingress rule, because it has no listening port.
- After the merge, the agent adds `address=/slskd.epaflix.com/<Traefik IP>` to `1-proxmox/pihole/dnsmasq.d/10-epaflix.conf` and the `local-zone` line to `1-proxmox/pihole/unbound-no-aaaa-leak.conf`, then applies both on CT 1030, following the Lidarr README.

### Authentik

- The encrypted blueprint `2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml` gets a single-app forward-auth proxy provider for `slskd.epaflix.com`, an application, a binding to `authentik Admins`, and membership in the embedded outpost, the same entries the rackpeek design lists.

### Observability

- Prometheus scrapes slskd's `/metrics`.
- New alert `SlskdDown` in `custom-alerts.yaml`, modelled on `LidarrDown`, with a test in `custom-alerts-tests.yaml`.
- Soularr has no metrics endpoint. Its logs go to Loki, and Kubernetes restarts it on a crash.

## Owner step (before secrets can be built)

In the AirVPN client area:

1. Create device `slskd`.
2. Forward one TCP port and bind it to that device.
3. Tell the agent the port number. It is not secret: qBittorrent's port is in the repo.

## Non-goals

- Changing the qBittorrent pod or the VPN picker.
- Tubifarry or other Lidarr plugins.
- Upload speed limits. The slskd defaults apply, and you can set them later in the UI.
- Backup scripts for the new PVCs.
- The Soularr web UI.

## Constraints and risks

- Only one tunnel per AirVPN device may run. Any scratch test must be gone before Argo syncs.
- R2: sharing `/media/music` uploads the library to other Soulseek users. Spyros accepted this.
- Soularr imports go through Lidarr's matching, so the "Album match is not close enough" failures seen with torrents can happen here too. `failed_import_denylist` stops Soularr retrying the same download.
- D6 deletes files under `/media/downloads/soulseek` and `soulseek-incomplete` only. It never touches `/media/music`, which slskd mounts read-only.

## Verification after the merge

1. The gluetun public IP is an AirVPN address (`/v1/publicip/ip`), and slskd logs show it logged in to Soulseek.
2. slskd's port check reports the listen port as reachable.
3. `https://slskd.epaflix.com` redirects to Authentik, and a scratch pod in `default` gets connection refused on 5030.
4. Soularr's first run logs searches. Within a day, at least one album is imported from `/media/downloads/soulseek`.
5. Prometheus has `up{job="slskd"} == 1`.
