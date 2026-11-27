# slskd and Soularr

slskd connects to Soulseek and Soularr feeds it Lidarr's missing albums. Design: `docs/superpowers/specs/2026-11-27-slskd-soularr-design.md`.

## Network

- slskd leaves only through the gluetun sidecar, which runs AirVPN device `slskd` over WireGuard. AirVPN forwards TCP port 46327 to that device, and slskd listens on it.
- gluetun drops traffic outside the tunnel. slskd's VPN integration polls gluetun's control server on `127.0.0.1:8000` with the API key from Secret `slskd`, and stays off Soulseek while the tunnel is down.
- `enableServiceLinks` is off because slskd reads every `SLSKD_*` variable as a setting, including the ones the Service would inject.
- `https://slskd.epaflix.com` uses Traefik forward auth and permits only `authentik Admins`. slskd's own login is off. `slskd-ingress` admits TCP 5030 only from Traefik, Soularr and Prometheus. Port 9999 (gluetun health) is open for kubelet probes.

## Files

- slskd shares `/media/music` read-only and downloads to `/media/downloads/soulseek`. Incomplete files go to `/media/downloads/soulseek-incomplete`. The `download-dirs` init container creates both, because slskd refuses to start without them.
- slskd deletes completed and incomplete files nobody has accessed for 30 days (`retention` in ConfigMap `slskd-config`). Soularr moves albums Lidarr rejects into `/media/downloads/soulseek/failed_imports`, so they are covered by the same retention.

## Secret

`_shared/secrets/slskd.enc.yaml` holds the Soulseek account, the gluetun control-server key, and the WireGuard keys for AirVPN device `slskd`. The Soulseek account is also in the homelab vault.

## Scratch test before merge (2026-11-27)

A copy in namespace `slskd-scratch` with emptyDir volumes, deleted before the PR:

- gluetun connected to an AirVPN Amsterdam server. slskd logged in to Soulseek and listened on 46327.
- A TCP connection from the home network to the exit IP on 46327 succeeded. Port 46328 stayed closed.
- `/metrics` returned 139 `slskd_` lines.
- Stopping the tunnel through gluetun's control API made slskd log "Disconnected from the Soulseek server; VPN is required" and a direct connection to 1.1.1.1 timed out. Starting it again reconnected slskd within a second.
