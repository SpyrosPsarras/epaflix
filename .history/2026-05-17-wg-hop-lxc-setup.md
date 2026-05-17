## [2026-05-17] - wg-hop LXC: WireGuard → PPTP hop on takaros

### Problem
A workstation needs to egress through the upstream PPTP VPN `hcn.libero.fm`. PPTP requires kernel modules + privileged setup; pushing that into every client is bad UX. K3s was considered as the host but k8s pods with PPTP + `/dev/ppp` + GRE are fragile.

### Goal
Single hop LXC: clients connect over WireGuard, traffic egresses via PPTP. Mirror the TrueNAS `wg-easy` deployment scheme so it feels familiar.

### Design
- Privileged Debian-13 LXC `1045 / wg-hop / 192.168.10.45` on **takaros**.
- `/dev/ppp` (108:0) and `/dev/net/tun` (10:200) passed through via `lxc.cgroup2.devices.allow` + `lxc.mount.entry`.
- `pptp-linux` dials `hcn.libero.fm` (MPPE-128, auto-reconnect via `pptp-hcn-libero.service`).
- WireGuard via `wg-easy v15` Docker container in host-network mode, subnet `10.0.9.0/24` (parallel to TrueNAS' `10.0.8.0/24`), UDP 51820 / TCP 51821 UI.
- Policy routing: `ip rule from 10.0.9.0/24 lookup vpn` → `default dev ppp0`. iptables `MASQUERADE` on `ppp0` for source `10.0.9.0/24`. Plumbing applied/torn-down by `/etc/ppp/ip-{up,down}.d/01-wg-hop`.
- LXC's own egress stays on `eth0` (LAN reachability preserved); only the WG subnet is forced through `ppp0`.

### Changes
1. **Proxmox** — `pct create 1045 ... --features nesting=1,keyctl=1 --unprivileged 0`. Edited `/etc/pve/lxc/1045.conf` to bind `/dev/ppp` and `/dev/net/tun`.
2. **Inside LXC** — installed `wireguard-tools`, `pptp-linux`, `iptables-persistent`, Docker CE. PPTP peer at `/etc/ppp/peers/hcn-libero` with creds in `/etc/ppp/chap-secrets`. wg-easy compose at `/etc/wg-easy/docker-compose.yml`.
3. **Pi-hole** — added `address=/wg-hop.epaflix.com/192.168.10.45` to `/etc/dnsmasq.d/10-epaflix.conf`; `systemctl restart pihole-FTL`.
4. **Repo** — `1-proxmox/wg-hop/README.md` documents the full procedure.

### Verification
From inside the LXC:
- `ppp0 = 10.107.14.254`, peer `10.107.14.1`.
- `curl --interface ppp0 ifconfig.me` → `188.117.218.213` (VPN gateway).
- `curl ifconfig.me` (default route) → `<HOME_PUBLIC_IP>` (real ISP) — proves LXC management traffic stays on eth0.
- `ip rule` shows `from 10.0.9.0/24 lookup vpn`; `ip route show table vpn` has `default dev ppp0`.
- `iptables -t nat -S POSTROUTING | grep ppp0` shows the MASQUERADE rule.

### Gotchas
- `cap_add: SYS_MODULE` is **not allowed** in PVE-9 privileged LXC — Docker rejects it. Drop it; modules are already loaded on the Proxmox host kernel.
- `sysctls: net.ipv4.conf.all.src_valid_mark=1` cannot be set in `network_mode: host` containers — set it on the LXC via `/etc/sysctl.d/` instead.
- Debian-13 LXC template ships without `/etc/iproute2/` — create the dir + `rt_tables` file manually.
- `pihole reloaddns` failed with a `readonly variable FTL_PID_FILE` shell error; `systemctl restart pihole-FTL` worked.
- Installing `iptables-persistent` (via recommends) pulled in `linux-image-rt-amd64` — ~600 MB of wasted kernel inside the LXC. Safe to `apt purge linux-image-rt-amd64` afterwards.

### Still TODO at end of session
- Complete wg-easy first-boot wizard via browser at `http://wg-hop.epaflix.com:51821/`.
- Add a peer `spyros-arch`, import config into the workstation's NetworkManager.
- Verify hostOS egress IP becomes `188.117.218.213` once peer is up.
