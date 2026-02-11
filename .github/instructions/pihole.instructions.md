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
    ├── 2. dnsmasq address= directives  ← /etc/dnsmasq.d/10-internal-epaflix.conf ← WINS (internal A records)
    ├── 3. filter-rr=HTTPS              ← /etc/dnsmasq.d/20-filter-https-records.conf ← NODATA for HTTPS type queries
    └── 4. Upstream: Unbound            ← 127.0.0.1:5335                          ← all other queries
                │
                └── DNS-over-TLS → Google 8.8.8.8:853 / 8.8.4.4:853
```

`/etc/dnsmasq.d/` is the **single source of truth** for all `epaflix.com` DNS records.
Pi-hole's `custom.list` is intentionally empty. Unbound holds no `local-data` for `epaflix.com`
domains — its only role is upstream resolution for everything not in dnsmasq.d, and a
`local-zone: static` directive to prevent unknown `*.internal.epaflix.com` names from
leaking to public DNS.

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
| `/etc/dnsmasq.d/10-internal-epaflix.conf` | **Active** — `nick.internal.epaflix.com` only |
| `/etc/dnsmasq.d/20-filter-https-records.conf` | **Active** — `filter-rr=HTTPS` blocks HTTPS type queries from reaching public DNS |
| `/etc/pihole/custom.list` | **Empty** — intentionally cleared, do not repopulate |
| `/etc/unbound/unbound.conf.d/pi-hole.conf` | Unbound core: port 5335, cache, DoT upstream |
| `/etc/unbound/unbound.conf.d/internal-epaflix.conf` | `local-zone: static` security directive only — no data entries |
| `/etc/unbound/unbound.conf.d/remote-control.conf` | Enables `unbound-control` via `/run/unbound.ctl` |
| `/etc/unbound/unbound.conf.d/disable-ipv6.conf` | Placeholder (`server:` stanza only, no directives) |
| `/etc/pihole/pihole.toml` | Pi-hole v6 config — managed by FTL, do not edit directly |

---

## Current DNS Records

### `/etc/dnsmasq.d/10-epaflix.conf` — public subdomains

All point to `192.168.10.101` (K3s Traefik LoadBalancer).
As services migrate to Docker Swarm, individual records will be updated to `192.168.10.71`.

| Domain | IP | HTTP (verified 2026-03-22) | App |
|---|---|---|---|
| `sonarr.epaflix.com` | 192.168.10.101 | ✅ 200 | Sonarr |
| `sonarr2.epaflix.com` | 192.168.10.101 | ✅ 200 | Sonarr (second instance) |
| `radarr.epaflix.com` | 192.168.10.101 | ✅ 200 | Radarr |
| `prowlarr.epaflix.com` | 192.168.10.101 | ✅ 200 | Prowlarr |
| `bazarr.epaflix.com` | 192.168.10.101 | ✅ 200 | Bazarr |
| `seerr.epaflix.com` | 192.168.10.101 | ✅ 200 | Seerr |
| `jellyseerr.epaflix.com` | 192.168.10.101 | ✅ 200 | Jellyseerr |
| `jellyfin.epaflix.com` | 192.168.10.101 | ✅ 200 | Jellyfin |
| `qbittorrent.epaflix.com` | 192.168.10.101 | ✅ 200 | qBittorrent WebUI |
| `homarr.epaflix.com` | 192.168.10.101 | ✅ 200 | Homarr |
| `huntarr.epaflix.com` | 192.168.10.101 | ✅ 200 | Huntarr |
| `cleanuparr.epaflix.com` | 192.168.10.101 | ✅ 200 | Cleanuparr |
| `auth.epaflix.com` | 192.168.10.101 | ✅ 200 | Authentik |
| `filebrowser.epaflix.com` | 192.168.10.101 | ✅ 200 | FileBrowser Quantum |
| `grafana.epaflix.com` | 192.168.10.101 | ✅ 200 | Grafana |
| `traefik.epaflix.com` | 192.168.10.101 | ✅ 200 | Traefik dashboard (behind Authentik SSO) |

> **No wildcard**: any unlisted `*.epaflix.com` subdomain falls through to public DNS
> and resolves to the real Cloudflare IPs (`172.67.179.219` / `104.21.59.155`).

### `/etc/dnsmasq.d/10-internal-epaflix.conf` — internal subdomains

Two entries exist. All other `*.internal.epaflix.com` entries were removed on
2026-03-22 — K3s has zero IngressRoutes for `internal.epaflix.com` (verified).

| Domain | IP | Notes |
|---|---|---|
| `nick.internal.epaflix.com` | 192.168.10.41 | Individual user VM |
| `vidar.internal.epaflix.com` | 192.168.10.42 | Individual user VM — added 2026-04-11 |

> **NXDOMAIN protection**: Unbound's `local-zone: "internal.epaflix.com." static` ensures
> any unlisted `*.internal.epaflix.com` name returns NXDOMAIN and never leaks to public DNS.
> Verified: `random999.internal.epaflix.com` → NXDOMAIN.

> ⚠️ **FTL restart required for new entries**: `pihole reloaddns` (SIGHUP) does **not** always
> pick up new `address=` entries for the `*.internal.epaflix.com` zone. Always use
> `systemctl restart pihole-FTL` after adding a new entry here.

---

## How to Manage DNS Records

### The Golden Rule

**Edit only `/etc/dnsmasq.d/10-epaflix.conf`** for public services, or
**`/etc/dnsmasq.d/10-internal-epaflix.conf`** for internal VM entries.
Do not touch `custom.list`, Unbound data entries, or `pihole.toml` hosts for this purpose.

### Adding a new record

```bash
ssh root@192.168.10.30

# Public service (*.epaflix.com)
echo "address=/newapp.epaflix.com/192.168.10.71" >> /etc/dnsmasq.d/10-epaflix.conf

# Internal VM (*.internal.epaflix.com)
echo "address=/newvm.internal.epaflix.com/192.168.10.50" >> /etc/dnsmasq.d/10-internal-epaflix.conf

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
| `/etc/unbound/unbound.conf.d/internal-epaflix.conf` | `local-zone: static` — security only, no data entries |
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

Answered by `address=` directives in `/etc/dnsmasq.d/10-epaflix.conf`.
- No wildcard — each subdomain listed explicitly
- Currently all → `192.168.10.101` (K3s Traefik)
- Will be selectively updated to `192.168.10.71` (Docker Swarm Traefik) as services migrate

### `internal.epaflix.com` — internal subdomains

Two active entries in `/etc/dnsmasq.d/10-internal-epaflix.conf`:
- `nick.internal.epaflix.com` → `192.168.10.41`
- `vidar.internal.epaflix.com` → `192.168.10.42`

All other `*.internal.epaflix.com` entries were removed on 2026-03-22. K3s has zero
IngressRoutes for `internal.epaflix.com` (verified by `kubectl get ingressroute -A`).

Unbound's `local-zone: "internal.epaflix.com." static` (in `internal-epaflix.conf`)
ensures any unlisted name in this zone returns NXDOMAIN instead of leaking to public DNS.
This is a security directive — keep it even if no `local-data` entries exist.

### `lan` — DHCP domain

`dns.domain.name = "lan"` in `pihole.toml`. DHCP hostnames get a `.lan` suffix.
Queries for `.lan` never forward upstream.

### `nick.internal.epaflix.com` and `vidar.internal.epaflix.com`

These are individual user VM entries. Each lives in **exactly one place**:
`/etc/dnsmasq.d/10-internal-epaflix.conf`.
- `custom.list`: not present
- `pihole.toml` hosts: `[]` (empty)
- Unbound: no `local-data` entries (only `local-zone: static` remains)

| Entry | IP |
|---|---|
| `nick.internal.epaflix.com` | `192.168.10.41` |
| `vidar.internal.epaflix.com` | `192.168.10.42` |

---

## Troubleshooting

### A domain is not resolving or resolving to wrong IP

```bash
ssh root@192.168.10.30

# 1. Check the active source files
grep "myapp.epaflix.com" /etc/dnsmasq.d/10-epaflix.conf
grep "myapp.internal.epaflix.com" /etc/dnsmasq.d/10-internal-epaflix.conf

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
- `local-zone: "internal.epaflix.com." static` — unknown internal names never reach public DNS
- `filter-rr=HTTPS` — blocks DNS type 65 (HTTPS record) queries from reaching public DNS, preventing browsers from receiving Cloudflare ECH/ipv4hint overrides that would bypass local A record resolution
- DNSSEC: **disabled** (`dnssec = false` in `pihole.toml`) — can be enabled if needed

---

## External References

- [Pi-hole v6 Documentation](https://docs.pi-hole.net/)
- [Pi-hole v6 Config Reference](https://docs.pi-hole.net/reference/config/)
- [dnsmasq address= directive](https://thekelleys.org.uk/dnsmasq/docs/dnsmasq-man.html)
- [Unbound Documentation](https://unbound.docs.nlnetlabs.nl/)
- [Pi-hole + Unbound Setup Guide](https://docs.pi-hole.net/guides/dns/unbound/)