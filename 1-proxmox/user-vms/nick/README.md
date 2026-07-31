# Nick's VM - qBittorrent behind an AirVPN Bluetit sidecar

VM `1041` / `192.168.10.41` / `nick.vm.epaflix.com`, on evanthoulaki. Reached
through the jumpbox (see `../README.md`).

This directory is the **target** state. It is not what the VM runs yet - see
[#493](https://github.com/SpyrosPsarras/epaflix/issues/493).

## Why this exists

On 2026-07-30 a speedtest on this VM found the AirVPN tunnel, not the line, was
the bottleneck. It was pinned to a single endpoint, `Kajam`, one of three 2 Gbit
Alblasserdam boxes measured dropping packets:

| | Result |
|---|---|
| Kajam entry IP, 100 pings | 6.7% loss |
| Upload through the tunnel | 0.03 MB/s |
| Upload on the bare host | 16.6 MB/s |

The cluster was migrated to the AirVPN Suite (Bluetit) in
[#489](https://github.com/SpyrosPsarras/epaflix/pull/489) so the config holds a
server list and AirVPN chooses, rather than us hardcoding one IP that then rots.
This applies the same design here.

## Accessing the VM

```bash
# host key is not in known_hosts on a fresh workstation
ssh -o StrictHostKeyChecking=accept-new ubuntu@192.168.10.41
```

`ubuntu` is **not** in the `docker` group, so every docker command needs
`sudo -n docker ...`.

## What runs today

The live containers come from `/home/nick/work/docker-compose.yaml`, which is
Nick's own file. Note there is a **stale** copy at
`/opt/qbit/docker-compose.yaml` that does not match reality (it still says
`binhex/arch-qbittorrentvpn` with OpenVPN, different volumes and a different
network). Running `docker compose up` from `/opt/qbit` would replace a working
stack with a different one - do not.

## Migration

Prerequisites, both already true on this VM (verified 2026-07-31):

- `wireguard` kernel module loaded
- `/lib/modules/$(uname -r)/modules.builtin` present

Steps:

1. Copy `docker-compose.yaml` and `bluetit.conf` from this directory to the VM,
   alongside Nick's existing `qbit/` config directory.

2. Create a git-ignored `.env` next to them:

   ```
   AIRVPN_USERNAME=<secrets.yml airvpn_user>
   AIRVPN_PASSWORD=<secrets.yml airvpn_password>
   CLOUDFLARE_TUNNEL_TOKEN=<Nick's existing token, currently inline in his compose>
   ```

   The token is currently pasted literally into his compose file. Moving it to
   `.env` is part of this migration - do not copy it into git.

3. Back up his current compose and config before switching:

   ```bash
   sudo -n cp /home/nick/work/docker-compose.yaml /home/nick/work/docker-compose.yaml.pre-bluetit
   sudo -n cp -a /home/nick/work/qbit /home/nick/work/qbit.pre-bluetit
   ```

4. `docker compose up -d`, then verify (below).

## Verifying

The single check that matters - a green WebUI proves nothing here, because
qBittorrent bound to a device that no longer exists still serves the WebUI while
moving zero data:

```bash
sudo -n docker exec airvpn sh -c 'ss -tulnp | grep 49135'
# want: 10.154.38.229%tun0:49135  on both tcp and udp
```

Then:

```bash
sudo -n docker exec airvpn goldcrest --bluetit-status | grep -E "Connected to AirVPN server|Key:"
#   -> a server NOT in airblackserverlist, and "Key: nick"
sudo -n docker exec airvpn ping -c100 -i0.2 10.128.0.1 | tail -2      # want ~0% loss
sudo -n docker logs qbt-bind-interface                                 # want the tun0 rebind
```

For throughput, do **not** trust a single `curl` while torrents are running -
it measures leftover headroom, not capacity. Sample the counters instead:

```bash
sudo -n docker exec airvpn sh -c \
  'a=$(grep tun0 /proc/net/dev|awk "{print \$10}"); sleep 10; \
   b=$(grep tun0 /proc/net/dev|awk "{print \$10}"); \
   echo "$a $b" | awk "{printf \"tx %.1f Mbit/s\n\", (\$2-\$1)*8/10/1000000}"'
```

Single-stream numbers will look poor regardless: adding ~25 ms of RTT by routing
through Amsterdam caps a single TCP stream, while aggregate stays fine.
BitTorrent is massively parallel, so it sees the aggregate.

## Things that will bite you

- **`airkey nick`** must stay. AirVPN permits one session per device key, so
  sharing the cluster's `Default` would knock the two offline in turn.
- **Port 49135**, not the cluster's 39998 - it is the port AirVPN forwards for
  Nick's device. Now set declaratively via `QBT_TORRENTING_PORT`.
- **First boot fails once** and self-heals after the VPN container restarts, see
  [#491](https://github.com/SpyrosPsarras/epaflix/issues/491). Expect a couple
  of minutes before qBittorrent starts.
- **Do not re-add `net.ipv6.conf.all.disable_ipv6=1`.** It stops the tunnel
  connecting at all.
- The `SYS_MODULE` capability and the `/lib/modules` mount are both required.

## Rollback

```bash
sudo -n docker compose down
sudo -n cp /home/nick/work/docker-compose.yaml.pre-bluetit /home/nick/work/docker-compose.yaml
sudo -n rm -rf /home/nick/work/qbit && sudo -n mv /home/nick/work/qbit.pre-bluetit /home/nick/work/qbit
sudo -n docker compose up -d
```

The config restore matters: the migration rewrites the interface binding to
`tun0`, which the old image cannot use.
