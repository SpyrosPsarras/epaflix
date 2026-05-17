## [2026-05-17] - wg-hop: public Cloudflare A record (unproxied)

### Problem
External users could not reach `wg-hop.epaflix.com:51820` — no public DNS record pointed to the home IP, and the existing ddns-updater wildcard entry forced `proxied: true` (Cloudflare orange-cloud), which cannot carry WireGuard UDP.

### Goal
Resolve `wg-hop.epaflix.com` to the home public IP from public DNS resolvers, with `proxied: false`, while preserving the LAN-side split-DNS (`.30` → `.45`).

### Design
Add a second entry to the TrueNAS `ddns-updater` app's `ddns.config[]` schema:
- `domain: wg-hop.epaflix.com`
- `ip_version: ipv4` (pin to A-record)
- `cloudflare_proxied: false`
- `cloudflare_ttl: 60` (fast dyn-IP propagation; the wildcard entry uses `1` = auto)
- Same `cloudflare_token` and `cloudflare_zone_id` as the wildcard entry.

The router port-forward (UDP 51820 → 192.168.10.45:51820) is the user's job — not done in this session.

### Changes
1. **TrueNAS** — pushed updated config via `midclt call -j app.update ddns-updater "$(cat /tmp/ddns-update.json)"`. App redeployed; ddns container picked up the new entry, logged `Found 8 settings`, pushed `wg-hop.epaflix.com → <HOME_PUBLIC_IP>` to Cloudflare.
2. **Repo** — `1-proxmox/wg-hop/README.md` gained a "Public exposure" section.

### Verification
- `dig +short wg-hop.epaflix.com @1.1.1.1` → `<HOME_PUBLIC_IP>` ✓
- `dig +short wg-hop.epaflix.com @192.168.10.30` → `192.168.10.45` ✓ (split-DNS intact)
- Cloudflare API: `A wg-hop.epaflix.com → <HOME_PUBLIC_IP> proxied=False ttl=60` ✓
- End-to-end from cellular: pending router port-forward.

### Gotchas (worth knowing later)
- **`/mnt/apps/ddns-updater/config.json` is not authoritative.** It is re-rendered each redeploy from the TrueNAS app schema; the container reads its config from the `CONFIG` env var injected by the app layer. Editing the file in-place wastes time. Always edit via `midclt app.update`.
- **Two different Cloudflare tokens exist in this estate**:
  - `secrets.yml:cloudflare-api-token` → returns `Authentication error` (10000) against `/zones/.../dns_records`. Likely the old / Traefik-scoped one, or revoked.
  - The token inside the ddns app schema → works for record read+write.
  Did **not** rotate `secrets.yml` because Traefik/cert-manager may use a different scope; left a TODO note in `1-proxmox/wg-hop/README.md`.
- **ddns thinks current IP is `192.168.10.45`** when resolving the FQDN, because TrueNAS resolves through Pi-hole (LAN override). Harmless: each poll cycle sees the mismatch and pushes the public IP to Cloudflare again. To stop the noise, set the ddns `Public IP fetching` resolver to bypass Pi-hole, or just leave it.

### Still TODO at end of session
- ~~After forward exists, retest from an external network and confirm `curl ifconfig.me` returns `188.117.218.213`.~~ Done in the second-addendum verification (cellular hotspot test, see below).

### Addendum (same day): router port-forward had a typo — `.4` vs `.45`

After the port move to UDP 51822, cellular tests still failed: zero packets reached the LXC. Hairpin test from LAN to public IP also got zero packets, but that was a red herring (TP-Link Archer has no NAT loopback on this firmware — would have been zero even with a correct rule).

Root cause: the `wireguard-k3s` rule in TP-Link Port Forwarding had **Device IP `192.168.10.4`** instead of `192.168.10.45`. Single-digit typo. Packets were forwarded to a non-existent host and silently dropped at the LAN switch.

Fix: edit rule → set Device IP to `192.168.10.45` → save.

Verification after fix (cellular hotspot (SSID redacted), carrier IPv4 `<EXTERNAL_TEST_IP>`):
- `nmcli con up epaflix-wg-hop-greece` with kill-switch
- `curl -s --max-time 8 -4 ifconfig.me` → `188.117.218.213` ✓
- LXC `wg show wg0` → `endpoint: <EXTERNAL_TEST_IP>:51345 latest handshake: 30 seconds ago` ✓

End-to-end public path complete: cellular → ISP → home public IP `<HOME_PUBLIC_IP>:51822` → TP-Link NAT → `192.168.10.45:51822` → wg-easy → ppp0 → `hcn.libero.fm` egress `188.117.218.213`.

### Diagnostic notes worth keeping
- TP-Link Archer port-forward rules are AES-encrypted in the web UI's POST body (per-session key); reading them via curl requires reimplementing TP-Link's login handshake (RSA pubkey fetch → AES key/IV → encrypted body). Faster: ask the operator for a UI screenshot.
- TP-Link Archer (this firmware) has **no NAT loopback** — so a "LAN → public IP" hairpin probe cannot validate a port-forward. The only authoritative external test is from a different network (cellular, VPS).
- Detached `tcpdump` on the LXC across a `nmcli con up <other-wifi>` flip: works iff started with `nohup ... & disown` AND writing to file. Keep the SSH session that started it short — long-lived SSH dies with the wifi flip even if tcpdump survives, which is what we want.

### Addendum (same day): port collision with TrueNAS wg-easy → moved to UDP 51822

UDP 51820 is already forwarded to TrueNAS wg-easy (used by the user's phone autoconnect). WireGuard has no SNI, so two services cannot share a UDP port on the public IP. Resolution:

- wg-hop's wg-easy was moved to **UDP 51822** by editing the SQLite DB inside the LXC:
  ```bash
  pct exec 1045 -- docker stop wg-easy
  pct exec 1045 -- sqlite3 /etc/wg-easy/wg-easy.db \
    "UPDATE interfaces_table SET port=51822, updated_at=CURRENT_TIMESTAMP WHERE name='wg0';
     UPDATE user_configs_table SET port=51822, updated_at=CURRENT_TIMESTAMP WHERE id='wg0';"
  pct exec 1045 -- docker start wg-easy
  ```
  wg-easy regenerates `/etc/wg-easy/wg0.conf` from the DB on start; new `ListenPort = 51822`. Backup at `/etc/wg-easy/wg-easy.db.bak-2026-05-17`.
- Existing `spyros-arch-laptop.conf` was edited in-place (Endpoint port `51820` → `51822`); the NetworkManager keyfile `/etc/NetworkManager/system-connections/wg-hop.nmconnection` was edited the same way, then `sudo nmcli connection reload`. `nmcli con modify` does **not** support the `wireguard-peer.<pk>.endpoint` property — keyfile edit is the only path.
- Router forward (added by user): UDP 51822 → 192.168.10.45:51822. The previous UDP 51820 → TrueNAS rule is untouched.
- Verified `wg show wg0` on the LXC shows handshake within seconds of bringing the client up; `curl ifconfig.me` from hostOS returns `188.117.218.213`.

This addendum was triggered by a self-lockout near-miss — changing the server port before the client made the VPN unreachable. Captured the failsafe lesson in `memory/feedback_vpn_failsafe.md`.
