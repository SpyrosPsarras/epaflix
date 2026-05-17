# wg-hop — WireGuard → PPTP hop LXC

A privileged Debian-13 LXC on **takaros** that lets workstations connect via WireGuard and have their egress traffic tunneled out through the upstream PPTP VPN `hcn.libero.fm`.

```
[hostOS, phone, etc.]
        │ WireGuard (UDP 51822, AllowedIPs=0.0.0.0/0)
        ▼
[wg-hop LXC 1045 — 192.168.10.45]       ← takaros, Debian 13, privileged
        │  • wg-easy (web UI :51821, WG :51822 UDP)
        │  • pptp-linux client (peer hcn-libero → hcn.libero.fm)
        │  • ip rule from 10.0.9.0/24 → table vpn → default dev ppp0
        │  • iptables -t nat MASQUERADE -s 10.0.9.0/24 -o ppp0
        ▼
[hcn.libero.fm 188.117.218.213]          ← PPTP server, MPPE-128
```

## Inventory

| Item            | Value                                       |
|-----------------|---------------------------------------------|
| Proxmox host    | takaros (192.168.10.10)                     |
| VMID            | 1045                                        |
| Hostname        | wg-hop                                      |
| IP              | 192.168.10.45/24, GW 192.168.10.1           |
| DNS             | `wg-hop.epaflix.com` → 192.168.10.45        |
| LXC type        | **privileged**, nesting=1, keyctl=1         |
| Resources       | 2 cores / 1024 MB RAM / 512 MB swap / 8 GB  |
| Template        | debian-13-standard_13.1-2_amd64             |
| Storage         | local-raid                                  |
| WireGuard subnet| `10.0.9.0/24` (mirrors TrueNAS `10.0.8.0/24`)|
| WG listen port  | UDP 51822                                   |
| wg-easy UI      | http://wg-hop.epaflix.com:51821             |
| Upstream VPN    | hcn.libero.fm (PPTP+MPPE-128)               |
| Upstream creds  | see `secrets.yml` (`hcn_libero_username` / `hcn_libero_password`) |

## Why this design

- Workstations don't run PPTP locally → kernel modules + privileged config stay on the LXC.
- WireGuard is the only client-side protocol → cleaner UX, mobile clients work.
- Egress IP is the PPTP server's IP (`188.117.218.213`) for any client of the hop.
- Mirrors the TrueNAS `wg-easy` deployment scheme (subnet, port) for operator familiarity.

## Provisioning (one-shot, ran 2026-05-17)

### 1. Create privileged LXC on takaros

```bash
pct create 1045 local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst \
  --hostname wg-hop \
  --cores 2 --memory 1024 --swap 512 \
  --rootfs local-raid:8 \
  --net0 name=eth0,bridge=vmbr0,ip=192.168.10.45/24,gw=192.168.10.1,firewall=0 \
  --nameserver 192.168.10.30 --searchdomain epaflix.com \
  --unprivileged 0 --features nesting=1,keyctl=1 \
  --onboot 1 \
  --password "<HOSTOS_PASSWORD>" \
  --ssh-public-keys /root/.ssh/authorized_keys
```

### 2. Pass through PPTP/TUN devices

Append to `/etc/pve/lxc/1045.conf`:

```
lxc.cgroup2.devices.allow: c 108:0 rwm
lxc.cgroup2.devices.allow: c 10:200 rwm
lxc.mount.entry: /dev/ppp dev/ppp none bind,create=file 0 0
lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file 0 0
```

Then `pct start 1045`. Verify inside the LXC: `ls -la /dev/ppp /dev/net/tun`.

### 3. Inside the LXC — packages

```bash
apt-get update
apt-get install -y wireguard wireguard-tools pptp-linux ppp \
                   iptables iptables-persistent netfilter-persistent \
                   curl ca-certificates gnupg
# Docker (for wg-easy)
install -m0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/debian $(. /etc/os-release; echo $VERSION_CODENAME) stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
```

### 4. PPTP peer config

`/etc/ppp/peers/hcn-libero`:
```
pty "pptp hcn.libero.fm --nolaunchpppd"
name <HCN_LIBERO_USERNAME>
remotename PPTP
require-mppe-128
refuse-eap
refuse-pap
refuse-chap
refuse-mschap
file /etc/ppp/options.pptp
ipparam hcn-libero
```

`/etc/ppp/options.pptp`:
```
lock
noauth
nobsdcomp
nodeflate
```

`/etc/ppp/chap-secrets` (chmod 600):
```
<HCN_LIBERO_USERNAME> PPTP <HCN_LIBERO_PASSWORD> *
```

`/etc/systemd/system/pptp-hcn-libero.service`:
```ini
[Unit]
Description=PPTP tunnel hcn-libero
After=network-online.target docker.service
Wants=network-online.target
[Service]
Type=forking
ExecStart=/usr/bin/pon hcn-libero
ExecStop=/usr/bin/poff hcn-libero
Restart=always
RestartSec=15
[Install]
WantedBy=multi-user.target
```

`systemctl daemon-reload && systemctl enable --now pptp-hcn-libero`.

### 5. Policy routing + NAT

`/etc/iproute2/rt_tables` (file may not exist on minimal template):
```
255	local
254	main
253	default
0	unspec
100	vpn
```

`/etc/sysctl.d/99-wg-hop.conf`:
```
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
```

`/etc/ppp/ip-up.d/01-wg-hop` (chmod +x):
```sh
#!/bin/sh
[ "$PPP_IPPARAM" = "hcn-libero" ] || exit 0
WG_NET=10.0.9.0/24
ip route flush table vpn 2>/dev/null
ip route add default dev "$PPP_IFACE" table vpn
ip rule del from $WG_NET lookup vpn 2>/dev/null
ip rule add from $WG_NET lookup vpn
iptables -t nat -C POSTROUTING -s $WG_NET -o "$PPP_IFACE" -j MASQUERADE 2>/dev/null \
  || iptables -t nat -A POSTROUTING -s $WG_NET -o "$PPP_IFACE" -j MASQUERADE
iptables -C FORWARD -i wg0 -o "$PPP_IFACE" -j ACCEPT 2>/dev/null \
  || iptables -A FORWARD -i wg0 -o "$PPP_IFACE" -j ACCEPT
iptables -C FORWARD -i "$PPP_IFACE" -o wg0 -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null \
  || iptables -A FORWARD -i "$PPP_IFACE" -o wg0 -m state --state RELATED,ESTABLISHED -j ACCEPT
```

`/etc/ppp/ip-down.d/01-wg-hop` (chmod +x) deletes the same rules.

`netfilter-persistent save` to persist the iptables state.

### 6. wg-easy

`/etc/wg-easy/docker-compose.yml`:
```yaml
services:
  wg-easy:
    image: ghcr.io/wg-easy/wg-easy:15
    container_name: wg-easy
    restart: unless-stopped
    network_mode: host
    cap_add:
      - NET_ADMIN
      - NET_RAW
    volumes:
      - /etc/wg-easy:/etc/wireguard
      - /lib/modules:/lib/modules:ro
    environment:
      - INSECURE=true
      - PORT=51821
```

```bash
cd /etc/wg-easy && docker compose up -d
```

Then open `http://wg-hop.epaflix.com:51821/` and run the first-boot wizard:
- Admin user / password — pick anything; record in `secrets.yml`.
- Server settings: subnet `10.0.9.0/24`, port `51822`, host `wg-hop.epaflix.com` (or `192.168.10.45`).
- Optional: DNS for clients = `1.1.1.1` if you want DNS over the tunnel, or `192.168.10.30` (Pi-hole) for LAN DNS.

Notes:
- `SYS_MODULE` cap is **not** allowed in PVE-9 privileged LXC. Don't add it — modules are already loaded on the Proxmox host kernel.
- `sysctls: net.ipv4.conf.all.src_valid_mark=1` cannot be set in `network_mode: host` containers; set it via `/etc/sysctl.d/` on the LXC instead (already done in step 5).

## Adding a client peer

1. wg-easy UI → "New Client" → name (e.g. `spyros-arch`) → download `.conf`.
2. On the client (Linux with NetworkManager) — rename the file to ≤15 chars before `.conf` (kernel ifname limit), e.g. `/tmp/wg-hop.conf`:
   ```bash
   nmcli connection import type wireguard file /tmp/wg-hop.conf
   nmcli connection modify wg-hop connection.id epaflix-wg-hop-greece   # optional friendly name
   nmcli connection up epaflix-wg-hop-greece
   ```
3. Verify egress IP is `188.117.218.213`:
   ```bash
   curl ifconfig.me
   ```

## Public exposure (internet → wg-hop)

The LXC is reachable from the LAN by default. To let external users dial in:

### DNS (Cloudflare, unproxied)

WireGuard is UDP — Cloudflare's HTTP proxy cannot carry it. The `wg-hop.epaflix.com` A record must be **grey-cloud** (`proxied: false`).

The TrueNAS `ddns-updater` app manages our Cloudflare records. Its **config lives in the TrueNAS app schema**, NOT in `/mnt/apps/ddns-updater/config.json` (the file is only re-rendered from the app config; editing it directly does nothing — `ddns-updater` reads its config from the `CONFIG` env var the TrueNAS app layer injects).

To add a new record:

```bash
# Read current schema:
ssh truenas_admin@192.168.10.200 'sudo midclt call app.config ddns-updater' | jq

# Build a values payload (see /tmp/ddns-update.json from the 2026-05-17 session),
# then push it:
ssh truenas_admin@192.168.10.200 \
  'sudo midclt call -j app.update ddns-updater "$(cat /tmp/ddns-update.json)"'
```

For `wg-hop` the relevant `ddns.config[]` entry is:

```jsonc
{
  "domain": "wg-hop.epaflix.com",
  "ip_version": "ipv4",
  "provider": "cloudflare",
  "cloudflare_proxied": false,
  "cloudflare_ttl": 60,
  "cloudflare_token": "<see ddns app config>",
  "cloudflare_zone_id": "fe8661d923f772a0c8fb6ab8375ad93e"
}
```

### Router port-forward

Add **one** rule in the home router:

| Proto | External port | Destination          | Notes                              |
|-------|---------------|----------------------|------------------------------------|
| UDP   | 51822         | 192.168.10.45:51822  | WireGuard data channel             |

Do **NOT** forward:
- TCP/UDP 51821 — wg-easy admin UI is LAN-only (auth is `INSECURE=true`).
- TCP 51822 — WG is UDP-only; TCP forwarding only adds attack surface.

### Verification

```bash
dig +short wg-hop.epaflix.com @1.1.1.1          # → home public IP (the value in secrets.yml / current DDNS)
dig +short wg-hop.epaflix.com @192.168.10.30    # → 192.168.10.45 (split-DNS intact)
# From a network outside the LAN (cellular hotspot, friend's wifi, VPS):
nmcli con up epaflix-wg-hop-greece && curl ifconfig.me
# → 188.117.218.213 (hcn.libero.fm egress)
```

**Authoritative test must come from a non-LAN network.** This TP-Link firmware has no NAT loopback — sending a probe from a LAN client to the home public IP will silently drop, regardless of whether the port-forward is correct. A hairpin failure says nothing about the rule.

If `wg show wg0` on the LXC stays empty after an external client tries to connect, walk the router's Port Forwarding table and verify the **Device IP** field for the `wireguard-k3s` rule literally character-by-character against `192.168.10.45`. A single-digit typo (`.4` vs `.45`) reaches a non-existent host and gets dropped at the LAN switch — silent failure with no logs anywhere.

### Gotcha — Cloudflare token in `secrets.yml` is stale

`secrets.yml:cloudflare-api-token` did not authenticate against `/zones/.../dns_records` during the 2026-05-17 work; the **live working token** is the one inside the ddns-updater app config (`midclt call app.config ddns-updater | jq '.ddns.config[0].cloudflare_token'`). The stale token is still referenced by cert-manager and Traefik manifests — leaving it alone for now since DNS-01 ACME may use a different fine-grained token. If Traefik cert renewals fail, audit both and merge.

## Troubleshooting

- **`ppp0` not appearing**: check `pct config 1045` includes the device-allow + bind-mount lines; check `journalctl -u pptp-hcn-libero` and `pppd` logs.
- **GRE blocked**: PPTP needs GRE (proto 47). Outbound is fine here because the upstream router NATs GRE. If broken, check router firewall.
- **Egress goes via eth0 instead of ppp0**: confirm `ip rule` lists `from 10.0.9.0/24 lookup vpn` and `ip route show table vpn` has `default dev ppp0`. The `ip-up.d` script must have fired.
- **wg-easy can't write to /etc/wireguard**: check volume mount is `/etc/wg-easy:/etc/wireguard`.

## Related

- TrueNAS wg-easy app — same UI, different purpose (LAN access via WG; subnet `10.0.8.0/24`).
- Upstream PPTP server — credentials and details in `secrets.yml` as `hcn_libero_*`.
