# Nick's qBittorrent stack (192.168.10.41)

Codifies the stack that was migrated to the AirVPN Bluetit sidecar design in
#493, but never brought into git. This directory is documentation-only from
git's point of view - nothing here deploys automatically. Deploy/redeploy by
hand, on the box, as below.

It is someone else's machine. Coordinate with Nick before changing anything
live - these files describe the current state, they don't manage it.

## Access

- SSH: `ssh ubuntu@192.168.10.41` (or via the jumpbox per
  `../README.md` if off-LAN - Nick's VM is not one of the jumpbox-gated user
  VMs, it predates that pattern and is reached directly).
- First connection needs `-o StrictHostKeyChecking=accept-new` - the host key
  is not pre-seeded in `known_hosts`.
- `ubuntu` is **not** in the `docker` group. Every docker command needs
  `sudo -n docker ...` (the `-n` fails fast instead of hanging on a password
  prompt if that ever changes).
- The live stack files are owned by `nick`, not `ubuntu` - reading them also
  needs `sudo -n`.

## File inventory (on the box)

| Path | Purpose |
|---|---|
| `/home/nick/work/docker-compose.yaml` | the live stack - matches `docker-compose.yaml` in this directory |
| `/home/nick/work/bluetit.conf` | the live Bluetit directives - matches `bluetit.conf` in this directory |
| `/home/nick/work/.env` | real credentials, git-ignored, keys match `.env.example` here |
| `/home/nick/work/qbit/` | qBittorrent config volume (WebUI settings, `qBittorrent.conf`, downloads state) |
| `/home/nick/work/SETUP.md` | original host-setup doc (Tailscale, Samba share, data dirs) - still accurate for the host-level parts; its qBittorrent/compose section describes the **old** `tenseiken/qbittorrent-wireguard` stack this migration replaced, not the Bluetit one |
| `/home/nick/work/docker-compose.yaml.pre-bluetit-*` | pre-migration compose, kept as a rollback reference |
| `/opt/qbit/docker-compose.yaml` | dead weight from an even earlier `binhex/arch-qbittorrentvpn` setup - not running, not referenced by anything live |
| `/data/torrents/{downloads,incomplete}` | local data dirs, owner uid 1000 |

## Deploy / redeploy

```bash
ssh ubuntu@192.168.10.41
sudo -n su - nick   # or sudo -n docker compose, run from /home/nick/work as root via sudo
cd /home/nick/work
sudo -n docker compose up -d
```

Verify:

```bash
sudo -n docker exec airvpn goldcrest --bluetit-status | grep 'Connected to AirVPN server'
sudo -n docker exec airvpn ping -c20 -W2 10.128.0.1
```

## Credentials

`.env` at `/home/nick/work/.env` is git-ignored (repo-wide `.env` / `*.env`
rule in `.gitignore`, with `.env.example` allowlisted). Copy `.env.example`
from this directory and fill in real values sourced from
`.github/instructions/secrets.yml`:

- `AIRVPN_USERNAME` / `AIRVPN_PASSWORD` - `airvpn_user` / `airvpn_password`.
  **Same AirVPN account as the cluster** - only the device key (`airkey`,
  below) differs.
- `CLOUDFLARE_TUNNEL_TOKEN` - Nick's own pre-existing Cloudflare Zero Trust
  tunnel token, unrelated to the cluster's tunnels.

## What must stay in step with the cluster, and what is deliberately different

`bluetit.conf` here is the same content as
`2-k3s/08.servarr/qbittorrent/bluetit-config.yaml`'s ConfigMap body, with
**one intentional difference**:

- `airkey nick` here, vs `airkey Default` on the cluster. AirVPN allows one
  session per device key - sharing a key between the cluster and Nick's box
  would knock both offline in turn. Never change this to `Default`.

Everything else (server whitelist/blacklist, `networklockpersist`,
`allowprivatenetwork`, `allowping`, `ignorednspush`, `tunpersist`, `airipv6`)
is copied deliberately and should be changed on both sides together, or not
at all - see the WHY comments in each file for why each directive exists.
The compose file's `PROBE_TARGET` (`10.128.0.1`, the in-tunnel gateway) and
`QBT_TORRENTING_PORT` (`49135`, Nick's own AirVPN-forwarded port - **not**
the cluster's `39998`) are legitimately per-box and are not expected to match.

## Known gaps

- **DNS/Tailscale**: this box has Tailscale (`100.100.100.100`) alongside
  Pi-hole for DNS. `*.epaflix.com` resolves to Cloudflare here, not the LAN
  Traefik IP - different from every other host in this repo. Relevant to any
  future work that assumes LAN-only DNS resolution on this box (see #504 for
  related vpn-picker context).
- The RFC1918-blackhole issue found on the cluster pod (#517) - Bluetit's
  blunt `0.0.0.0/1` + `128.0.0.0/1` route pair re-swallowing private ranges
  the split-route set otherwise excludes - was **not** re-verified here. On a
  plain VM (not a pod) the host's own link-scope route to its `/24` normally
  wins over a `/1`, so it likely does not reproduce, but this was not
  measured on this box.
- `docker-compose.yaml.pre-bluetit-*` and `/opt/qbit/` are stale artifacts on
  the box, not vendored here - they are rollback/historical only.

## Related

#493 (the Bluetit migration this codifies), #498 (vpn-picker map - this box
would be one target once it needs live server switching, not wired up yet),
#517 (cluster-side DNS/routing findings referenced above).
