# slskd and Soularr

slskd connects to Soulseek and Soularr feeds it Lidarr's missing albums. Design: `docs/superpowers/specs/2026-11-27-slskd-soularr-design.md`.

## Network

- slskd leaves only through the gluetun sidecar, which runs AirVPN device `slskd` over WireGuard. AirVPN forwards TCP port 46327 to that device, and slskd listens on it.
- gluetun drops traffic outside the tunnel. slskd's VPN integration polls gluetun's control server on `127.0.0.1:8000` with the API key from Secret `slskd`, and stays off Soulseek while the tunnel is down. One failed poll counts as down, and the 1 s default timeout failed whenever a Soularr search flooded slskd's thread pool, so the timeout is 10 s. gluetun's firewall, not this poll, is what keeps traffic in the tunnel.
- `enableServiceLinks` is off because slskd reads every `SLSKD_*` variable as a setting, including the ones the Service would inject.
- `https://slskd.epaflix.com` uses Traefik forward auth and permits only `authentik Admins`. slskd's own login is off. `slskd-ingress` admits TCP 5030 only from Traefik, Soularr and Prometheus. Kubelet probes come from the node and need no rule.

## Files

- slskd shares `/media/music` read-only and downloads to `/media/downloads/soulseek`. Incomplete files go to `/media/downloads/soulseek-incomplete`. The `download-dirs` init container creates both, because slskd refuses to start without them.
- slskd deletes completed and incomplete files that have not been modified for 30 days (`retention` in ConfigMap `slskd-config`). It checks last-write time, not last access (`PruneFiles` in slskd 0.26.2). The scan is recursive, so it also covers `/media/downloads/soulseek/failed_imports`, where Soularr moves albums Lidarr rejects.
- slskd rescans `/media/music` once a day (`SLSKD_SHARE_CACHE_RETENTION=1440`), so newly imported albums get shared.

## Secret

`_shared/secrets/slskd.enc.yaml` holds the Soulseek account, the gluetun control-server key, and the WireGuard keys for AirVPN device `slskd`. The Soulseek account is also in the homelab vault.

## Scratch test before merge (2026-11-27)

A copy in namespace `slskd-scratch` with emptyDir volumes, deleted before the PR:

- gluetun connected to an AirVPN Amsterdam server. slskd logged in to Soulseek and listened on 46327.
- A TCP connection from the home network to the exit IP on 46327 succeeded. Port 46328 stayed closed.
- `/metrics` returned 139 `slskd_` lines.
- Stopping the tunnel through gluetun's control API made slskd log "Disconnected from the Soulseek server; VPN is required" and a direct connection to 1.1.1.1 timed out. Starting it again reconnected slskd within a second.

## Rollout (2026-11-27)

PR #2475 merged as `de01ca6e`. Argo `servarr`, `observability` and `authentik` synced to it.

- slskd: gluetun connected to AirVPN Amsterdam, and slskd logged in to Soulseek. The first share scan found 1,111 files in 153 folders and took 85 s.
- Soularr: the first run started during that scan and got connection refused, then retried after 5 minutes. Within 15 minutes Lidarr imported 7 albums (79 tracks) from `/media/downloads/soulseek`, and missing albums went from 187 to 180.
- Pi-hole on CT 1030: backups `/root/10-epaflix.conf.bak-20261127-slskd` and `/root/no-aaaa-leak.conf.bak-20261127-slskd`. Copied `dnsmasq.d/10-epaflix.conf` (live equalled the repo before the change). Inserted the `slskd.epaflix.com.` local-zone after `lidarr`, because the live unbound file differs from the repo in comments and order but has the same zones. `unbound-checkconf` clean, `unbound-control reload`, `systemctl restart pihole-FTL`. `slskd.epaflix.com` A is `192.168.10.101`, AAAA empty.
- `https://slskd.epaflix.com/` redirects to Authentik. A busybox pod in `default` cannot reach `slskd:5030`. Prometheus has `up{job="slskd"} == 1` and `SlskdDown` is inactive.
