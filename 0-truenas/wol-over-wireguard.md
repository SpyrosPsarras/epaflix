# Wake-on-LAN over WireGuard (homePC)

Wake the home PC `192.168.10.177` from any WireGuard client (phone, laptop) with a plain WoL broadcast - no SSH hop, no relay. The fix lives on the TrueNAS WG endpoint.

```
[phone / laptop on wg-easy tunnel]
        │ WireGuard (subnet 10.0.8.0/24, client iface "spyros-arch")
        ▼
[TrueNAS 192.168.10.200]                     ← wg-easy app, network_mode: host
        │  wg0 (10.0.8.1) in the HOST netns
        │  • net.ipv4.ip_forward = 1
        │  • net.ipv4.conf.{all,default,enp8s0}.bc_forwarding = 1   ← the fix
        │  • POSTROUTING: -s 10.0.8.0/24 -o enp8s0 MASQUERADE
        ▼ enp8s0 (192.168.10.200) - directed broadcast flooded onto LAN
[home LAN 192.168.10.0/24 L2]
        ▼
[homePC 192.168.10.177, MAC d8:43:ae:20:7d:23] wakes
```

## The problem

Wake-on-LAN is a Layer-2 broadcast. A magic packet sent to `192.168.10.255` over the VPN arrives at the TrueNAS WG endpoint, but Linux **drops directed broadcasts on forward by default**, so it never floods the LAN segment. Result: Moonlight's wake (and any WoL app pointed at the broadcast) silently does nothing from outside the house.

Unicast to `.177` does not help either - the home router has no ARP entry for a powered-off NIC, so a routed unicast can't be delivered. On the local segment WoL works because the switch floods the broadcast to every port regardless of ARP; that flooding is exactly what the routed path loses.

## The fix

Enable directed-broadcast forwarding on the TrueNAS host (the WG endpoint). `wg0` is in the **host** network namespace (the wg-easy container runs `network_mode: host`), so `/proc/sys/net` is the host's - the knob must be set on the host, not the container (see "Why not the container" below).

`bc_forwarding` is checked per-interface (setting `conf.all` alone is **not** enough - verified by capture). Needed:

| sysctl | value | why |
|--------|-------|-----|
| `net.ipv4.ip_forward` | 1 | already on (TrueNAS sets it for WG/docker) |
| `net.ipv4.conf.all.bc_forwarding` | 1 | global component of the per-iface check |
| `net.ipv4.conf.enp8s0.bc_forwarding` | 1 | egress LAN interface |
| `net.ipv4.conf.wg0.bc_forwarding` | 1 | ingress (WG) interface |

`wg0` is created late by the wg-easy app, so persist `conf.default.bc_forwarding=1` instead of `conf.wg0` - any interface created after boot inherits it (verified: a dummy iface created after `default=1` came up with the flag set).

### Live (test / one-off)

```bash
# on TrueNAS as root
echo 1 > /proc/sys/net/ipv4/ip_forward
echo 1 > /proc/sys/net/ipv4/conf/all/bc_forwarding
echo 1 > /proc/sys/net/ipv4/conf/default/bc_forwarding
echo 1 > /proc/sys/net/ipv4/conf/enp8s0/bc_forwarding
echo 1 > /proc/sys/net/ipv4/conf/wg0/bc_forwarding
```

### Persistent (survives reboot) - TrueNAS SYSCTL tunables

```bash
# truenas_admin sudo password: sops -d --extract '["truenas_admin_password"]' .github/instructions/secrets.enc.yaml
for var in net.ipv4.conf.all.bc_forwarding \
           net.ipv4.conf.default.bc_forwarding \
           net.ipv4.conf.enp8s0.bc_forwarding; do
  midclt call -j tunable.create \
    "{\"type\": \"SYSCTL\", \"var\": \"$var\", \"value\": \"1\", \"enabled\": true, \"comment\": \"WoL over WireGuard: forward directed broadcasts onto LAN\"}"
done
midclt call tunable.query '[["type","=","SYSCTL"]]'   # confirm (also visible in UI: System > Advanced > Sysctl)
```

`conf.default` applies at boot before the wg-easy app starts, so `wg0` inherits `bc_forwarding=1` when it is created.

## How to wake

Connect the **TrueNAS** wg-easy tunnel (`10.0.8.0/24`), then broadcast to the LAN:

- **Phone:** any WoL app → MAC `d8:43:ae:20:7d:23`, target/broadcast `192.168.10.255`, port `9`.
- **Laptop:** `wol -i 192.168.10.255 d8:43:ae:20:7d:23` (`pacman -S wol`), or the raw socket:
  ```bash
  python3 - <<'PY'
  import socket
  pkt = b'\xff'*6 + bytes.fromhex("d843ae207d23")*16
  s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
  s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
  s.sendto(pkt, ("192.168.10.255", 9))
  PY
  ```

Do **not** point the app at `wg-hop` - that tunnel (`10.0.9.0/24`) is PPTP egress only, not LAN access.

## Verification

```bash
# from a WG client, with homePC off:
wol -i 192.168.10.255 d8:43:ae:20:7d:23     # or the python above
sleep 20
nc -z 192.168.10.177 47989 && echo "Sunshine up - homePC awake"
```

Proven 2026-06-26: homePC woke in ~10s from a broadcast sent over the tunnel, no SSH hop.

## Why not do it "only in the container"

The wg-easy container (`ix-wg-easy-wg-easy-1`) runs `network_mode: host`, so `wg0` and `/proc/sys/net` belong to the **host** - there is no separate container netns to scope the change to. Docker also refuses to set namespaced `net.*` sysctls on a host-networked container ("not allowed with host network namespace"). Same limitation the wg-hop LXC hit with `src_valid_mark` (see `1-proxmox/wg-hop/README.md`). So the host tunable is the only clean place; baking it into the app would mean dropping host networking, which just moves the directed-broadcast problem to the docker-bridge → LAN boundary instead of removing it.

## Gotchas

- TrueNAS has only `truenas_admin` (no root SSH key). Elevate with `sudo` and the `truenas_admin_password` credential from the credential store: `PW=$(sops -d --extract '["truenas_admin_password"]' .github/instructions/secrets.enc.yaml)`. Over SSH, feed it with `sudo -S` (stdin), since the password prompt needs a tty - and because stdin keeps the value off `argv`, so it never lands in the process list or shell history.
- `conf.all.bc_forwarding=1` alone does nothing - the ingress/egress interfaces need their own flag (confirmed by `tcpdump` on `enp8s0` showing 0 packets with only `all=1`).
- The home PC's NIC + BIOS WoL must already be armed - see the homePC WoL notes (r8125 driver, ErP disabled, Resume By PCI-E enabled).

## Related

- `1-proxmox/wg-hop/README.md` - the *other* WireGuard endpoint (PPTP egress, not LAN access).
- TrueNAS access / `midclt`: `.github/instructions/truenas.instructions.md`.
