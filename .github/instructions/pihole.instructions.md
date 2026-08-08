---
applyTo: "**"
description: "Pi-hole and Unbound DNS instructions for epaflix infrastructure"
---

# Pi-hole & Unbound DNS Instructions

## Infrastructure Overview

- **Host**: `pihole` (Debian GNU/Linux 13 trixie)
- **IP**: `192.168.10.30`
- **SSH**: `ssh root@192.168.10.30` (passwordless)
- **Pi-hole version**: v6.4 (FTL v6.5)
- **Unbound version**: 1.22.0
- **DNS port**: 53 (Pi-hole FTL / dnsmasq)
- **Web UI**: `https://192.168.10.30/admin/` (ports 80 and 443, self-signed cert at `/etc/pihole/tls.pem`)
  - ⚠️ The bare root `/` returns **403 Forbidden** — navigate directly to `/admin/`
- **Web UI password**: stored in `/etc/pihole/cli_pw`
- **Role**: Primary DNS resolver for the entire `192.168.10.0/24` LAN + ad blocking

---

## Architecture

```
LAN clients
    │
    ▼  port 53
Pi-hole FTL / dnsmasq (192.168.10.30)
    │
    ├── 1. dnsmasq address= directives  ← /etc/dnsmasq.d/10-epaflix.conf          ← WINS (public A records)
    ├── 2. dnsmasq address= directives  ← /etc/dnsmasq.d/10-vm-epaflix.conf       ← WINS (user-VM A records)
    ├── 3. dnsmasq address= directives  ← /etc/dnsmasq.d/15-proxmox-hosts.conf    ← WINS (Proxmox host A records)
    ├── 4. filter-rr=HTTPS              ← /etc/dnsmasq.d/20-filter-https-records.conf ← NODATA for HTTPS type queries
    ├── 5. dnsmasq address= directives  ← /etc/dnsmasq.d/30-epaflix-lan.conf      ← WINS (*.epaflix.lan, LAN-only)
    ├── 6. edns-packet-max=1232         ← /etc/dnsmasq.d/99-edns.conf             ← UDP payload cap, no records
    └── 7. Upstream: Unbound            ← 127.0.0.1:5335                          ← all other queries
                │
                └── DNS-over-TLS → Google 8.8.8.8:853 / 8.8.4.4:853
```

`/etc/dnsmasq.d/` is the **single source of truth** for all `epaflix.com` DNS records.
Pi-hole's `custom.list` is intentionally empty. Unbound holds no `local-data` for `epaflix.com`
domains — its only role is upstream resolution for everything not in dnsmasq.d, plus two
`local-zone: static` guards that stop names leaking to public DNS: one for the whole
`vm.epaflix.com` zone, one per name for four internal-only `*.epaflix.com` names
(see "AAAA and the Cloudflare wildcard" below).

> **HTTPS record type filtering (verified 2026-03-22):** Modern browsers (Firefox, Chrome)
> send a `query[HTTPS]` (DNS type 65) alongside every A record query. Pi-hole's `address=`
> directives only intercept A/AAAA queries — HTTPS type queries were previously forwarded
> to Unbound → public DNS → Cloudflare, which returned `ipv4hint` pointing to Cloudflare
> public IPs and an ECH key. This caused Firefox to bypass the local A record override and
> connect to Cloudflare instead of local K3s Traefik, resulting in TLS cert mismatches
> (including `MOZILLA_PKIX_ERROR_SELF_SIGNED_CERT`). Fixed by `filter-rr=HTTPS` in
> `/etc/dnsmasq.d/20-filter-https-records.conf` — Pi-hole now returns `NODATA (Filtered)`
> for all HTTPS record queries, forcing browsers to use the A record.

---

## DNS Files — What Each One Does

| File | Role |
|---|---|
| `/etc/dnsmasq.d/10-epaflix.conf` | **Active** — all `*.epaflix.com` public subdomain A records |
| `/etc/dnsmasq.d/10-vm-epaflix.conf` | **Active** — `*.vm.epaflix.com` user-VM A records (LAN-only) |
| `/etc/dnsmasq.d/15-proxmox-hosts.conf` | **Active** - Proxmox host A records: `takaros` and `evanthoulaki`, each as a `*.epaflix.com` name and as a bare hostname |
| `/etc/dnsmasq.d/20-filter-https-records.conf` | **Active** — `filter-rr=HTTPS` blocks HTTPS type queries from reaching public DNS |
| `/etc/dnsmasq.d/30-epaflix-lan.conf` | **Active** - all `*.epaflix.lan` records: direct LAN IPs for SSH and admin access, never public |
| `/etc/dnsmasq.d/99-edns.conf` | **Active** - `edns-packet-max=1232` only, no records |
| `/etc/pihole/custom.list` | **Empty** — intentionally cleared, do not repopulate |
| `/etc/unbound/unbound.conf.d/pi-hole.conf` | Unbound core: port 5335, cache, DoT upstream |
| `/etc/unbound/unbound.conf.d/vm-epaflix.conf` | `local-zone: static` security directive only — no data entries |
| `/etc/unbound/unbound.conf.d/no-aaaa-leak.conf` | **Active** - four per-name `local-zone: static` entries that stop AAAA leaking to the Cloudflare wildcard (#868). Tracked in git at `1-proxmox/pihole/unbound-no-aaaa-leak.conf` |
| `/etc/unbound/unbound.conf.d/remote-control.conf` | Enables `unbound-control` via `/run/unbound.ctl` |
| `/etc/unbound/unbound.conf.d/disable-ipv6.conf` | Placeholder (`server:` stanza only, no directives) |
| `/etc/pihole/pihole.toml` | Pi-hole v6 config — managed by FTL, do not edit directly |

---

## Current DNS Records

### `/etc/dnsmasq.d/10-epaflix.conf` — public subdomains

Most point to `192.168.10.101` (K3s Traefik LoadBalancer). Several do not - check the
IP column, do not assume. Rows are in live-file order, so a `diff` against the box lines up.

| Domain | IP | Serves |
|---|---|---|
| `sonarr.epaflix.com` | 192.168.10.101 | Sonarr |
| `sonarr2.epaflix.com` | 192.168.10.101 | Sonarr, second instance |
| `radarr.epaflix.com` | 192.168.10.101 | Radarr |
| `prowlarr.epaflix.com` | 192.168.10.101 | Prowlarr |
| `bazarr.epaflix.com` | 192.168.10.101 | Bazarr |
| `seerr.epaflix.com` | 192.168.10.101 | Seerr |
| `jellyseerr.epaflix.com` | 192.168.10.101 | Jellyseerr |
| `jellyfin.epaflix.com` | 192.168.10.101 | Jellyfin |
| `qbittorrent.epaflix.com` | 192.168.10.101 | qBittorrent WebUI |
| `homarr.epaflix.com` | 192.168.10.101 | Homarr |
| `cleanuparr.epaflix.com` | 192.168.10.101 | Cleanuparr |
| `auth.epaflix.com` | 192.168.10.101 | Authentik |
| `grafana.epaflix.com` | 192.168.10.101 | Grafana |
| `traefik.epaflix.com` | 192.168.10.101 | Traefik dashboard, behind Authentik SSO |
| `truenas.epaflix.com` | 192.168.10.101 | TrueNAS SCALE web UI - Traefik proxies to `192.168.10.200:443`, real `*.epaflix.com` cert |
| `lingarr.epaflix.com` | 192.168.10.101 | Lingarr |
| `wg-hop.epaflix.com` | **192.168.10.45** | wg-easy on LXC 1045 - straight to the LXC, no Traefik |
| `argocd.epaflix.com` | 192.168.10.101 | ArgoCD |
| `pegaprox.epaflix.com` | 192.168.10.101 | PegaProx UI - Traefik proxies to `192.168.10.21:5000` |
| `minio.epaflix.com` | 192.168.10.101 | MinIO S3 API on TrueNAS, fronted by Traefik (added 2026-05-22) |
| `minio-console.epaflix.com` | 192.168.10.101 | MinIO console, same backend |
| `newtarr.epaflix.com` | 192.168.10.101 | Newtarr |
| `bastion.epaflix.com` | **192.168.10.43** | Odysseus bastion VM - straight to the VM, no Traefik |
| `searxng.epaflix.com` | 192.168.10.102 | SearXNG - **internal entry point on purpose** (#547). The public route on `.101` requires Authentik; LAN and API clients use this unauthenticated internal route. Do not move it back to `.101`. |
| `syncthing.epaflix.com` | **192.168.10.110** | Syncthing GUI - its own kube-vip LoadBalancer (`syncthing-gui`), no Traefik |
| `api.epaflix.com` | **192.168.1.50** | **KNOWN BAD** - off-subnet (our LAN is `192.168.10.0/24`), unreachable, and the identical `address=` line appears twice in the conf. Removal is a live change, tracked in #877. Do not copy this row as a pattern |
| `remote-pi.epaflix.com` | **192.168.10.102** | Remote Pi relay - Traefik `internal` entry point, not the public LB |
| `cliproxy.epaflix.com` | **192.168.10.102** | CLIProxyAPI - Traefik `internal` entry point, not the public LB (#858) |

> **Not every record is 192.168.10.101.** Services on Traefik's `internal` entry
> point resolve to the dedicated `traefik-internal` LoadBalancer at
> `192.168.10.102`, which the router forwards nothing to. `cliproxy.epaflix.com`
> and `remote-pi.epaflix.com` are the two of those.

> **No wildcard**: any unlisted `*.epaflix.com` subdomain falls through to public DNS
> and resolves to the real Cloudflare IPs (`172.67.179.219` / `104.21.59.155`).

### `/etc/dnsmasq.d/10-vm-epaflix.conf` — user-VM subdomains

LAN-only names for individual user VMs. No public DNS records exist for this zone —
Unbound's static zone directive ensures unlisted names NXDOMAIN instead of leaking
to public DNS. The zone was renamed from `internal.epaflix.com` on 2026-04-18.

| Domain | IP | Notes |
|---|---|---|
| `nick.vm.epaflix.com` | 192.168.10.41 | Individual user VM |
| `vidar.vm.epaflix.com` | 192.168.10.42 | Individual user VM |

> **NXDOMAIN protection**: Unbound's `local-zone: "vm.epaflix.com." static` ensures
> any unlisted `*.vm.epaflix.com` name returns NXDOMAIN and never leaks to public DNS.
> Verified: `random999.vm.epaflix.com` → NXDOMAIN.

> ⚠️ **FTL restart required for new entries**: `pihole reloaddns` (SIGHUP) does **not** always
> pick up new `address=` entries for the `*.vm.epaflix.com` zone. Always use
> `systemctl restart pihole-FTL` after adding a new entry here.

### `/etc/dnsmasq.d/15-proxmox-hosts.conf` - Proxmox hosts

Direct LAN IPs for the two Proxmox VE hosts. Each host gets two names: the
`*.epaflix.com` form, which overrides the public Cloudflare answer, and the bare
hostname, which is what short SSH targets and Proxmox's own cluster tooling use.
These are **not** Traefik-fronted - they go straight to the Proxmox web UI on `:8006`.

| Domain | IP | Serves |
|---|---|---|
| `takaros.epaflix.com` | 192.168.10.10 | Proxmox host `takaros` |
| `evanthoulaki.epaflix.com` | 192.168.10.11 | Proxmox host `evanthoulaki` |
| `takaros` | 192.168.10.10 | Same host, bare hostname |
| `evanthoulaki` | 192.168.10.11 | Same host, bare hostname |

### `/etc/dnsmasq.d/30-epaflix-lan.conf` - the `epaflix.lan` zone

Direct LAN IPs for SSH and admin straight to the box. These bypass Traefik
completely - no ingress, no TLS termination, no Authentik. `epaflix.lan` is not a
real public TLD, so it never resolves outside the LAN and can never leak to
Cloudflare. That is the point: `truenas.epaflix.com` gives you the Traefik-proxied
web UI, `truenas.epaflix.lan` gives you the box itself.

| Domain | IP | Serves |
|---|---|---|
| `jumpbox.epaflix.lan` | 192.168.10.40 | Jump-box LXC 1040 (Alpine) |
| `takaros.epaflix.lan` | 192.168.10.10 | Proxmox host `takaros` |
| `evanthoulaki.epaflix.lan` | 192.168.10.11 | Proxmox host `evanthoulaki` |
| `k3s-master-51.epaflix.lan` | 192.168.10.51 | K3s master, VMID 1051 |
| `k3s-master-52.epaflix.lan` | 192.168.10.52 | K3s master, VMID 1052 |
| `k3s-master-53.epaflix.lan` | 192.168.10.53 | K3s master, VMID 1053 |
| `k3s-worker-61.epaflix.lan` | 192.168.10.61 | K3s worker, VMID 1061 |
| `k3s-worker-62.epaflix.lan` | 192.168.10.62 | K3s worker, VMID 1062 |
| `k3s-worker-63.epaflix.lan` | 192.168.10.63 | K3s worker, VMID 1063 |
| `k3s-worker-65.epaflix.lan` | 192.168.10.65 | K3s worker, VMID 1065 |
| `truenas.epaflix.lan` | 192.168.10.200 | TrueNAS box directly, not the Traefik proxy |
| `pegaprox.epaflix.lan` | 192.168.10.21 | PegaProx LXC 1021 directly, not the Traefik proxy |

### `/etc/dnsmasq.d/99-edns.conf` - EDNS payload cap

One line, `edns-packet-max=1232`, and no records. It caps the advertised UDP
payload size so large answers fall back to TCP instead of fragmenting.

---

## AAAA and the Cloudflare wildcard

### The mechanism

`address=/name/<IPv4>` makes Pi-hole authoritative for the **A** record only.
An **AAAA** query for the same name is not answered locally - it is forwarded
to Unbound and out to public DNS, where the Cloudflare-proxied
`*.epaflix.com` wildcard synthesizes an AAAA. So an IPv6-capable client asking
for an internal-only name can be sent to Cloudflare instead of to the LAN.

An **exact DNS-only (grey cloud) record in Cloudflare** stops it. The wildcard
only synthesizes for names it still covers, and an exact record takes the name
out of the wildcard. Proven twice, from a public resolver:

```bash
dig +short A cliproxy.epaflix.com @1.1.1.1   # 192.168.10.102 - exact, DNS-only
dig +short AAAA cliproxy.epaflix.com @1.1.1.1 # empty
dig +short A wg-hop.epaflix.com @1.1.1.1     # 81.167.233.67 - exact, DNS-only
dig +short AAAA wg-hop.epaflix.com @1.1.1.1  # empty
dig +short AAAA bastion.epaflix.com @1.1.1.1 # Cloudflare IPv6 - no exact record
```

`cliproxy` and `wg-hop` are the only two `*.epaflix.com` names with an exact
DNS-only Cloudflare record, and they are the only two that do not answer AAAA.

> **The LAN is IPv4-only today**, so this whole class is **latent, not
> exploitable**. No global IPv6 address and no IPv6 default route on the
> workstation, `takaros`, `k3s-master-51` or the Pi-hole, and Unbound runs
> `do-ip6: no`. The day IPv6 is enabled anywhere, Happy Eyeballs prefers v6 and
> every unguarded name goes to Cloudflare **first**, not as a fallback. Tracked
> as an IPv6 tripwire in #882.

### The rule

**A `*.epaflix.com` name not pointed at `192.168.10.101` needs protection, or
it answers AAAA from Cloudflare.**

A name pointed at `192.168.10.101` has a public Traefik route by design, so a
Cloudflare answer for it is a hairpin, not a boundary break. Those are left
alone. Everything else points straight at a box on the LAN and needs one of:

- an **exact DNS-only Cloudflare record**, when the target IP is already public
  or is harmless to publish; or
- a per-name **Unbound `local-zone: static`** in
  `/etc/unbound/unbound.conf.d/no-aaaa-leak.conf`, when publishing the target IP
  is not acceptable. This keeps the fix on the LAN and discloses nothing.

### The non-`192.168.10.101` names

| Domain | IP | AAAA protection |
|---|---|---|
| `bastion.epaflix.com` | 192.168.10.43 | Unbound `local-zone` (#868) |
| `takaros.epaflix.com` | 192.168.10.10 | Unbound `local-zone` (#868) |
| `evanthoulaki.epaflix.com` | 192.168.10.11 | Unbound `local-zone` (#868) |
| `syncthing.epaflix.com` | 192.168.10.110 | Unbound `local-zone` (#868) |
| `remote-pi.epaflix.com` | 192.168.10.102 | Exact DNS-only Cloudflare A record (#868). `.102` is already public via the `cliproxy` record, so this adds no new disclosure. Required since the relay landed - see `2-k3s/17.remote-pi/README.md` |
| `cliproxy.epaflix.com` | 192.168.10.102 | Exact DNS-only Cloudflare A record, already in place (#858). Clean |
| `wg-hop.epaflix.com` | 192.168.10.45 | Exact DNS-only Cloudflare A record, already in place. Clean |
| `api.epaflix.com` | 192.168.1.50 | **None, and none needed** - the record is off-subnet and unreachable, removal is tracked in #877. Removing it fixes A and AAAA together |

The four `local-zone` names deliberately get **no** Cloudflare record. One would
publish `192.168.10.43`, `.10`, `.11` and `.110` in public DNS, which is worse
than the leak it fixes.

The bare hostnames `takaros` and `evanthoulaki` in
`/etc/dnsmasq.d/15-proxmox-hosts.conf` need nothing. They have no TLD, so they
never resolve publicly - verified, AAAA is empty for both.

> **Keep this table current.** It is derived from the record tables above. Any
> new row there with an IP other than `192.168.10.101`, or any existing row that
> is moved off `192.168.10.101`, needs a row here in the **same change**.

> **Open drift, 2026-08-08:** the live box answers
> `searxng.epaflix.com` → `192.168.10.102`, while the record table above still
> says `192.168.10.101` and nothing in git moved it. That makes it a ninth
> non-`.101` name with no AAAA protection. It is not fixed here because the
> change that moved it has not landed - whoever lands it owns both the table row
> and the protection row.

### Rejected options - do not re-litigate

| Option | Why not |
|---|---|
| `address=/name/::` as a companion line | Unsafe. Connecting to the unspecified address `::` is treated as loopback, so the client connects to **its own machine** on port 443 instead of falling back to IPv4. That is a worse failure than the leak - it looks like a local service, not a DNS problem |
| `filter-rr=AAAA` in `/etc/dnsmasq.d/` | Supported by this build (`pi-hole-v2.92.2` dnsmasq, same option as the working `filter-rr=HTTPS`), but `filter-rr` has **no domain scope**. It would kill AAAA LAN-wide for every domain, not just `*.epaflix.com`. Too blunt |

---

## How to Manage DNS Records

### The Golden Rule

Pick the file by what the name is for:

- `*.epaflix.com` service → `/etc/dnsmasq.d/10-epaflix.conf`
- `*.vm.epaflix.com` user VM → `/etc/dnsmasq.d/10-vm-epaflix.conf`
- Proxmox host name → `/etc/dnsmasq.d/15-proxmox-hosts.conf`
- `*.epaflix.lan` SSH/admin name → `/etc/dnsmasq.d/30-epaflix-lan.conf`

Do not touch `custom.list`, Unbound data entries, or `pihole.toml` hosts for this purpose.

### Check the doc against live

The tables above are the only durable record of what should be in `/etc/dnsmasq.d/`.
A rebuild of the Pi-hole restores from them, so a missing row is a silently lost record.
Diff them against the box:

```bash
ssh root@192.168.10.30 "grep -h '^address=' /etc/dnsmasq.d/*.conf | sort"
ssh root@192.168.10.30 "ls -1 /etc/dnsmasq.d/"
```

Check both ways - every live record needs a row, and every row needs a live record.
Add or remove the table row in the **same change** as the record itself, not later.

### Adding a new record

```bash
ssh root@192.168.10.30

# Public service (*.epaflix.com)
echo "address=/newapp.epaflix.com/192.168.10.71" >> /etc/dnsmasq.d/10-epaflix.conf

# User VM (*.vm.epaflix.com)
echo "address=/newvm.vm.epaflix.com/192.168.10.50" >> /etc/dnsmasq.d/10-vm-epaflix.conf

# Apply
pihole reloaddns

# Verify
dig newapp.epaflix.com @192.168.10.30 +short
# Expected: 192.168.10.71
```

### Changing an existing record (e.g. migrating from K3s to Docker Swarm)

```bash
ssh root@192.168.10.30

SERVICE="sonarr.epaflix.com"
OLD_IP="192.168.10.101"
NEW_IP="192.168.10.71"

sed -i "s|address=/${SERVICE}/${OLD_IP}|address=/${SERVICE}/${NEW_IP}|" \
    /etc/dnsmasq.d/10-epaflix.conf

# Apply
pihole reloaddns

# Verify
dig ${SERVICE} @192.168.10.30 +short
# Expected: 192.168.10.71
```

### Removing a record

```bash
ssh root@192.168.10.30

SERVICE="oldapp.epaflix.com"

sed -i "/address=\/${SERVICE}\//d" /etc/dnsmasq.d/10-epaflix.conf

# Apply — must be a full restart, not just reloaddns (see stale cache note below)
systemctl restart pihole-FTL

# Verify — should fall through to public DNS
dig ${SERVICE} @192.168.10.30 +short
```

> ⚠️ **Stale cache on removal**: Pi-hole's generated `dnsmasq.conf` sets `use-stale-cache=3600`.
> When **removing** an `address=` entry, `pihole reloaddns` (SIGHUP) does **not** purge the
> in-memory cache — the old answer keeps being served for up to 3600 seconds.
> **Always use `systemctl restart pihole-FTL` after a removal** to take effect immediately.
> For additions and changes (not removals), `pihole reloaddns` is sufficient.

### Migrating ALL traffic to Docker Swarm (bulk update)

> Tracked in #878. The Swarm has been dormant since ~2026-06 (#583), so this
> runbook has no live use right now - whether it gets deleted or gated is decided there.

```bash
ssh root@192.168.10.30

sed -i 's|/192.168.10.101$|/192.168.10.71|g' /etc/dnsmasq.d/10-epaflix.conf

pihole reloaddns

# Spot check
for domain in sonarr radarr prowlarr auth traefik; do
    echo -n "${domain}.epaflix.com → "
    dig ${domain}.epaflix.com @192.168.10.30 +short
done
```

> **Also update** the router port forward: `80/443 → 192.168.10.101` → `80/443 → 192.168.10.71`
> before or at the same time, otherwise external traffic will break.

---

## Unbound Configuration Reference

### Config files

| File | Purpose |
|---|---|
| `/etc/unbound/unbound.conf` | Entry point — includes all `unbound.conf.d/*.conf` |
| `/etc/unbound/unbound.conf.d/pi-hole.conf` | Core: listen on `127.0.0.1:5335`, cache, DoT upstream |
| `/etc/unbound/unbound.conf.d/vm-epaflix.conf` | `local-zone: static` - security only, no data entries |
| `/etc/unbound/unbound.conf.d/no-aaaa-leak.conf` | Four per-name `local-zone: static` entries - AAAA leak guard, no data entries. Source of truth is `1-proxmox/pihole/unbound-no-aaaa-leak.conf` in git |
| `/etc/unbound/unbound.conf.d/remote-control.conf` | Enables `unbound-control` via `/run/unbound.ctl` |
| `/etc/unbound/unbound.conf.d/disable-ipv6.conf` | Placeholder (`server:` stanza only, no directives) |

### Key settings in `pi-hole.conf`

- Listens on `127.0.0.1:5335` — not reachable from LAN directly, only from Pi-hole
- IPv4 only (`do-ip6: no`)
- Cache: `rrset-cache-size: 256m`, `msg-cache-size: 128m`
- `cache-min-ttl: 300`, `cache-max-ttl: 14400`
- `serve-expired: yes` with `serve-expired-ttl: 3600` — serves stale while refreshing
- `prefetch: yes` — pre-warms cache before TTL expires
- `qname-minimisation: yes` — privacy: sends minimal info upstream
- `harden-glue: yes`, `harden-dnssec-stripped: yes`, `harden-referral-path: yes`
- `private-address` blocks for all RFC1918 ranges — prevents DNS rebinding attacks
- Upstream: **DNS-over-TLS to Google** (`8.8.8.8@853`, `8.8.4.4@853`)
- Cloudflare and Quad9 DoT configured but commented out

### Switching upstream DNS provider

```bash
ssh root@192.168.10.30
vi /etc/unbound/unbound.conf.d/pi-hole.conf
# Comment out Google lines, uncomment Cloudflare or Quad9, then:
unbound-control reload

# Verify
dig google.com @127.0.0.1 -p 5335 +short
```

---

## Common Operations

### Check what IP a domain resolves to

```bash
# Through Pi-hole — what LAN clients actually get (bypasses local resolver cache)
dig sonarr.epaflix.com @192.168.10.30 +short

# Through Unbound directly — bypasses Pi-hole/dnsmasq layer entirely
ssh root@192.168.10.30 "dig sonarr.epaflix.com @127.0.0.1 -p 5335 +short"
```

### Check Pi-hole and Unbound status

```bash
ssh root@192.168.10.30 "pihole status"
ssh root@192.168.10.30 "unbound-control status"
```

### Reload Pi-hole DNS (additions and changes only — see stale cache warning for removals)

```bash
ssh root@192.168.10.30 "pihole reloaddns"
```

### Full Pi-hole FTL restart (required after removals to purge stale cache)

```bash
ssh root@192.168.10.30 "systemctl restart pihole-FTL"
```

### Reload Unbound config

```bash
ssh root@192.168.10.30 "unbound-control reload"
```

### Flush a single record from Unbound cache

```bash
ssh root@192.168.10.30 "unbound-control flush sonarr.epaflix.com"
```

### Check Unbound cache statistics

```bash
ssh root@192.168.10.30 "unbound-control stats_noreset | grep -E 'total|cache|query'"
```

### Update Pi-hole blocklists

```bash
ssh root@192.168.10.30 "pihole -g"
```

### Watch live DNS queries

```bash
ssh root@192.168.10.30 "pihole -t"
```

### Check logs

```bash
# Pi-hole FTL
ssh root@192.168.10.30 "journalctl -u pihole-FTL -f --no-pager"

# Unbound
ssh root@192.168.10.30 "journalctl -u unbound -f --no-pager"
```

### Validate Unbound config before applying

```bash
ssh root@192.168.10.30 "unbound-checkconf /etc/unbound/unbound.conf && echo OK"
```

### Verify HTTPS record filtering is active

```bash
# Should return ANSWER: 0 and EDE: 17 (Filtered) — not the Cloudflare HTTPS record
dig seerr.epaflix.com HTTPS @192.168.10.30

# A record must still resolve correctly
dig seerr.epaflix.com A @192.168.10.30 +short
# Expected: 192.168.10.101
```

> If `filter-rr=HTTPS` ever needs to be disabled (e.g. for a host that genuinely needs
> HTTP/3 hints from public DNS), remove `/etc/dnsmasq.d/20-filter-https-records.conf`
> and run `systemctl restart pihole-FTL`. Be aware this re-exposes the Cloudflare ECH
> bypass for all `*.epaflix.com` domains.

### Verify AAAA is not leaking

> ⚠️ **Flush both caches first, or this gives a false green.** Unbound runs
> `cache-max-ttl: 14400` with `serve-expired: yes`, and FTL's generated
> `dnsmasq.conf` sets `use-stale-cache=3600`. A name that answered AAAA before
> the fix keeps answering AAAA from cache for hours. Same trap in reverse after
> a rollback.

```bash
# 1. Flush both caches
ssh root@192.168.10.30 "unbound-control flush_zone epaflix.com && systemctl restart pihole-FTL"

# 2. On the client, drop the local stub cache too
sudo resolvectl flush-caches

# 3. Every name below must show its LAN IPv4 and an EMPTY AAAA
for n in bastion takaros evanthoulaki syncthing remote-pi cliproxy wg-hop; do
    printf '%-14s A=[%s] AAAA=[%s]\n' "$n" \
      "$(dig +short A    $n.epaflix.com @192.168.10.30 | tr '\n' ' ')" \
      "$(dig +short AAAA $n.epaflix.com @192.168.10.30 | tr '\n' ' ')"
done
```

Expected: `AAAA=[]` on every row, and the A column matching the record table.
A Cloudflare address (`2606:4700:...`) in the AAAA column means that name is
unprotected - find it in the non-`192.168.10.101` table above and give it a
`local-zone` or an exact DNS-only Cloudflare record.

```bash
# The Unbound layer on its own, if you want to isolate it
ssh root@192.168.10.30 "dig AAAA bastion.epaflix.com @127.0.0.1 -p 5335"
# Expected: no ANSWER section
```

---

## Pi-hole v6 Notes

Pi-hole v6 replaced `setupVars.conf` and `pihole-FTL.conf` with a single `pihole.toml`.

| v5 | v6 |
|---|---|
| `setupVars.conf` | `pihole.toml` |
| `pihole-FTL.conf` | merged into `pihole.toml` |
| `pihole restartdns` | `pihole reloaddns` |
| `pihole -w` | `pihole allow` / `pihole allowlist` |
| Web on port 80 only | Web on ports 80 and 443 |

**Do not edit `pihole.toml` directly** — it is managed by pihole-FTL and changes may be
overwritten. Use the `pihole` CLI or the web UI at `https://192.168.10.30/admin/`.

Pi-hole v6 generates `/etc/pihole/dnsmasq.conf` and `/etc/pihole/hosts/custom.list`
automatically from `pihole.toml`. These are read-only — do not edit them.
Custom DNS entries belong in `/etc/dnsmasq.d/`.

---

## DNS Zone Architecture

### `epaflix.com` — public subdomains

Answered by `address=` directives in `/etc/dnsmasq.d/10-epaflix.conf`, plus the two
Proxmox host names in `/etc/dnsmasq.d/15-proxmox-hosts.conf`.
- No wildcard — each subdomain listed explicitly
- These names **do** exist publicly. `epaflix.com` is a real Cloudflare-hosted zone,
  so an unlisted subdomain falls through and resolves to the Cloudflare IPs. A dropped
  row is not a NXDOMAIN, it is a silent redirect off-LAN
- Most → `192.168.10.101` (K3s Traefik). The rest go straight to a box or to the
  `internal` Traefik LB at `192.168.10.102` - see the record table

### `vm.epaflix.com` — user-VM subdomains

Two active entries in `/etc/dnsmasq.d/10-vm-epaflix.conf`:
- `nick.vm.epaflix.com` → `192.168.10.41`
- `vidar.vm.epaflix.com` → `192.168.10.42`

No public DNS records exist for this zone — it is LAN-only and only traversed
after a jumpbox hop (see `1-proxmox/user-vms/README.md`). K3s has zero
IngressRoutes for `vm.epaflix.com`.

Unbound's `local-zone: "vm.epaflix.com." static` (in `vm-epaflix.conf`)
ensures any unlisted name in this zone returns NXDOMAIN instead of leaking to public DNS.
This is a security directive — keep it even if no `local-data` entries exist.

> Renamed from `internal.epaflix.com` on 2026-04-18.

### `epaflix.lan` - LAN-only admin and SSH names

Answered by `address=` directives in `/etc/dnsmasq.d/30-epaflix-lan.conf`. Twelve
entries: the jump-box, both Proxmox hosts, all seven K3s nodes, TrueNAS and PegaProx.
- Direct LAN IPs. No Traefik, no ingress, no TLS termination, no Authentik
- `epaflix.lan` is not a real public TLD, so it never resolves publicly. Unlike
  `*.epaflix.com`, a missing row here cannot leak to Cloudflare - it just fails
- Use it for SSH and for admin UIs on the box's own port. Use `*.epaflix.com` for
  anything that should go through Traefik

> **Not the same thing as the `lan` DHCP domain below.** `epaflix.lan` is a set of
> explicit `address=` records in a dnsmasq.d file. The `lan` entry below is a DHCP
> hostname suffix set in `pihole.toml`. Different mechanism, different file, and
> neither one creates the other.

### `lan` — DHCP domain

`dns.domain.name = "lan"` in `pihole.toml`. DHCP hostnames get a `.lan` suffix.
Queries for `.lan` never forward upstream. This is dnsmasq appending a suffix to
DHCP-learned names - it is **not** the `epaflix.lan` zone above, which is explicit
`address=` records.

### `nick.vm.epaflix.com` and `vidar.vm.epaflix.com`

These are individual user VM entries. Each lives in **exactly one place**:
`/etc/dnsmasq.d/10-vm-epaflix.conf`.
- `custom.list`: not present
- `pihole.toml` hosts: `[]` (empty)
- Unbound: no `local-data` entries (only `local-zone: static` remains)

| Entry | IP |
|---|---|
| `nick.vm.epaflix.com` | `192.168.10.41` |
| `vidar.vm.epaflix.com` | `192.168.10.42` |

---

## Troubleshooting

### A domain is not resolving or resolving to wrong IP

```bash
ssh root@192.168.10.30

# 1. Check the active source files
grep "myapp.epaflix.com" /etc/dnsmasq.d/10-epaflix.conf
grep "myapp.vm.epaflix.com" /etc/dnsmasq.d/10-vm-epaflix.conf

# 2. Test resolution at each layer
dig myapp.epaflix.com @127.0.0.1 +short          # Pi-hole (dnsmasq layer)
dig myapp.epaflix.com @127.0.0.1 -p 5335 +short  # Unbound directly

# 3. If you just edited a file, reload
pihole reloaddns
# Or for removals:
systemctl restart pihole-FTL
```

### A DNS change isn't taking effect on a client

```bash
# Full FTL restart flushes dnsmasq's in-memory stale cache
ssh root@192.168.10.30 "systemctl restart pihole-FTL"

# Flush Unbound cache for a specific record
ssh root@192.168.10.30 "unbound-control flush myapp.epaflix.com"

# On the client (Linux), flush systemd-resolved cache
sudo resolvectl flush-caches

# Verify the answer is now coming fresh from Pi-hole (bypass local cache)
dig myapp.epaflix.com @192.168.10.30 +short
```

### Unbound not starting after a config edit

```bash
ssh root@192.168.10.30

# Always validate before restarting
unbound-checkconf /etc/unbound/unbound.conf

# Check logs
journalctl -u unbound --no-pager | tail -30
```

### Pi-hole blocking a domain it shouldn't

```bash
# Check if it's in a blocklist
ssh root@192.168.10.30 "pihole -q somedomain.com"

# Allow it
ssh root@192.168.10.30 "pihole allow somedomain.com"
```

#### Persistent allowlist entries

| Domain | Reason | Added |
|---|---|---|
| `link.storjshare.io` | TrueNAS update domains (`update.ixsystems.com`, `update.sys.truenas.net`, `download.sys.truenas.net`) all CNAME here. Firebog Prigent-Malware list classifies it as malware (false positive — Storj is the legitimate CDN iXsystems uses for SCALE updates). Without this, TrueNAS update checks fail with `ECONNRESET` / `TLSV1_UNRECOGNIZED_NAME`. | 2026-05-16 |

> If Firebog re-adds the entry on a future gravity refresh, Pi-hole's exact-match allow still wins over the blocklist (verified). If you ever see `pihole -q link.storjshare.io` report no allow row, re-run `pihole allow link.storjshare.io`.

### External DNS queries failing (upstream DoT down)

```bash
ssh root@192.168.10.30

# Test DoT to Google directly
dig google.com @8.8.8.8 -p 853 +tcp +short

# Check port 853 is reachable
nc -zv 8.8.8.8 853

# Check Unbound upstream stats
unbound-control stats_noreset | grep "total.num.queries"

# Test Unbound's upstream resolution directly
dig google.com @127.0.0.1 -p 5335 +short
```

---

## Security Notes

- `listeningMode = "LOCAL"` in `pihole.toml` — Pi-hole only accepts DNS from `192.168.10.0/24`
- Unbound listens **only on `127.0.0.1:5335`** — not reachable from LAN (verified: port 5335 connection refused from LAN)
- All upstream queries use **DNS-over-TLS** — ISP cannot snoop on DNS traffic
- `qname-minimisation` reduces data sent to upstream resolvers
- `private-address` rules block RFC1918 responses from upstream (DNS rebinding protection)
- `harden-glue` and `harden-dnssec-stripped` protect against DNS spoofing attacks
- `local-zone: "vm.epaflix.com." static` — unknown user-VM names never reach public DNS
- Four per-name `local-zone: static` entries in `no-aaaa-leak.conf` - internal-only `*.epaflix.com` names return an empty AAAA instead of the Cloudflare wildcard's synthesized IPv6 (#868). Latent today because the LAN is IPv4-only; live the moment IPv6 is enabled anywhere (#882)
- `filter-rr=HTTPS` — blocks DNS type 65 (HTTPS record) queries from reaching public DNS, preventing browsers from receiving Cloudflare ECH/ipv4hint overrides that would bypass local A record resolution
- DNSSEC: **disabled** (`dnssec = false` in `pihole.toml`) — can be enabled if needed

---

## External References

- [Pi-hole v6 Documentation](https://docs.pi-hole.net/)
- [Pi-hole v6 Config Reference](https://docs.pi-hole.net/reference/config/)
- [dnsmasq address= directive](https://thekelleys.org.uk/dnsmasq/docs/dnsmasq-man.html)
- [Unbound Documentation](https://unbound.docs.nlnetlabs.nl/)
- [Pi-hole + Unbound Setup Guide](https://docs.pi-hole.net/guides/dns/unbound/)