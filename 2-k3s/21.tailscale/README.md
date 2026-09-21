# Tailscale private access

Tailscale replaces NetBird on spylinux and the Pixel 8. The homelab subnet router advertises `192.168.10.0/24` and `10.0.0.0/24`. Pi-hole `192.168.10.30` is the sole global DNS upstream, with DNS override enabled and MagicDNS disabled. Internet traffic stays on the local connection; no exit node is configured.

Cloudflare public ingress remains in place. The NetBird client profiles and the existing `pihole-hop` container remain available for rollback.

## Deploy and enroll

```sh
kubectl --context epaflix apply -k 2-k3s/21.tailscale
kubectl --context epaflix -n tailscale exec deploy/subnet-router -- tailscale status
```

On first deployment, open the login URL from `tailscale status` and enroll under the same account as the clients. The one-hour boot timeout allows time for this step. In the Tailscale admin console:

- D1. Apply `policy.json` as the access policy for this dedicated personal tailnet. It replaces the policy; merge it if the tailnet later has other users or services.
- D2. Approve both advertised routes on `epaflix-subnet-router` and disable that device's key expiry.
- D3. Configure DNS from `dns.json`: sole nameserver `192.168.10.30`, override local DNS, no split DNS, and MagicDNS off. These JSON files are admin/API inputs, not Kubernetes resources.

The equivalent API endpoints are `POST /api/v2/tailnet/-/acl`, `POST /api/v2/tailnet/-/dns/configuration`, `POST /api/v2/device/DEVICE_ID/routes` with `{"routes":["192.168.10.0/24","10.0.0.0/24"]}`, and `POST /api/v2/device/DEVICE_ID/key` with `{"keyExpiryDisabled":true}`. Read the API credential from KeePass; do not put it in manifests or shell history.

The router uses Tailscale's userspace forwarding for TCP, UDP and ping. It needs no privileged container, host networking or kernel forwarding changes. It stores its enrolled identity on the `subnet-router-state` PVC. Do not delete that claim during upgrades. The local-path volume pins this single replica to one worker; loss of that worker stops remote private access and DNS until it returns or the router is recovered and enrolled again. This is not a highly available deployment.

Readiness and metrics are available inside the cluster on pod port 9002 at `/healthz` and `/metrics`. Pi-hole query logs show the worker's LAN address after source NAT, so use unique test domains to attribute queries. They do not identify the original client by source IP.

## Clients

On Arch, install the official `tailscale` package. Install `10-no-portmapper.conf` under `/etc/systemd/system/tailscaled.service.d/` before starting the daemon, then:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now tailscaled
sudo tailscale up --accept-routes=false --accept-dns=false
# Complete browser enrollment before switching routes and DNS.
netbird down
sudo systemctl disable --now netbird@default.service
sudo tailscale set --accept-routes=true --accept-dns=true
```

On Android, keep JetBird disconnected, install the official Tailscale app, approve VPN access, and enroll under the same account. Enable Tailscale DNS and select no exit node. Enable Android's Always-on VPN for Tailscale and allow it through battery optimization. Leave Android's "Block connections without VPN" off because ordinary internet traffic uses split routing. Disable Android Private DNS and Chrome Secure DNS. Other apps with their own DNS-over-HTTPS settings must also use the system resolver.

For Android 1.102.3, open App split tunneling, open its menu, choose "Switch to including", and leave the selection empty. Despite the "Included apps (0)" label, this version routes every app through the VPN's configured routes and DNS. Android reports the complete primary-user UID range `0-99999`. The default exclusion mode instead bypasses Messages, Android Auto, Google Home, Sonos and Adaptive Connectivity Services on this phone, including their DNS. The empty inclusion behavior follows [the installed version's VPN builder](https://github.com/tailscale/tailscale-android/blob/aea8f60c0/android/src/main/java/com/tailscale/ipn/IPNService.kt#L185). It is version-dependent and contradicts the UI wording, so verify the UID range after upgrades. Selecting even one app changes this to an allowlist and excludes every unselected app. The regression script checks the complete UID range on every probe. The upstream exclusions address app compatibility problems; RCS, casting, Sonos and Android Auto functionality still need normal-use validation.

Linux and the subnet router have `TS_DISABLE_PORTMAPPER=true`. The home Archer AX90 already had UPnP off, and its NAT-PMP/PCP port refused discovery probes. No router settings or port forwards were changed. Android has no configured equivalent of that environment flag: the attempted `only-tcp-443` tailnet attribute was rejected by this account and is not deployed. Automatic mappings are prevented at the home router, not proven disabled inside Android on arbitrary Wi-Fi networks. Keep automatic mappings disabled on any other router you administer.

## Phone regression test

The USB connection runs through the existing `adbbox` container on spylinux. Preserve that container and its ADB authorization. Copy `phone-roam-test.py` to spylinux and run:

```sh
python3 /tmp/phone-roam-test.py | tee phone-roam.jsonl
```

Start with Wi-Fi enabled, Tailscale connected, and working mobile data. The script checks ten Wi-Fi-off/on cycles, requires the expected validated Wi-Fi or cellular VPN underlay, polls fresh DNS and private HTTP access every five seconds, and requires recovery within 30 seconds. It checks the connection again five seconds after recovery and restores Wi-Fi in `finally`. It never enables roaming or mobile data. A blocked domain is checked by its resolved address; successful loopback ping does not mean an ad bypass.

Correlate each `probe_domain` with `/var/log/pihole/pihole.log`. A returned public wildcard address alone does not prove which resolver handled the query. A and AAAA and DNS-failure tests require the additional checks recorded below.

## Validation on 2026-09-09

- T1. Ten cycles, 20 transitions passed on the Pixel 8 with the final all-app inclusion setting and no Tailscale restart during the loop. Complete mobile probes recovered in 6.48 to 17.63 seconds; Wi-Fi probes in 5.23 to 5.58 seconds. All 58 successful fresh public probes appeared in Pi-hole logs. Every settled transition passed direct internet, blocked DNS, internal DNS and private HTTP checks. Every probe retained the complete app UID range.
- T2. With Wi-Fi off, Android reported validated Telenor cellular data, subscription 4, not roaming. The phone reached the router through `DERP(hel)`. Chrome's external address matched its cellular IPv6 address, while home Wi-Fi and laptop traffic used the home WAN address. No exit node was selected.
- T3. System A and AAAA queries returned allowed public answers and Pi-hole block answers. The phone returned `127.0.0.1` for blocked A and `::` for blocked AAAA; the laptop returned `0.0.0.0` and `::`. Pi-hole internal DNS and its HTTP API were reachable, and authenticated laptop kubectl listed all seven Ready nodes through Tailscale routes.
- T4. An isolated laptop firewall test blocked Pi-hole, producing SERVFAIL with zero answers for fresh A and AAAA queries while direct internet still worked. Removing the test rule restored DNS. Removing only the phone's private access in the tailnet policy while it was on cellular also stopped fresh DNS and private HTTP; restoring the policy recovered without restarting the app. All temporary rules were removed.
- T5. The laptop recovered fresh DNS and private HTTP within 5.12 seconds of a short suspend/resume and remained usable at the follow-up check. The phone passed a short screen-off/wake test on cellular while the router restarted. The router retained its node ID and IP from the PVC. This is a short wake test while USB-connected, not a long Android deep-idle test.
- T6. Android Private DNS, Chrome Secure DNS and Firefox Android DNS-over-HTTPS were verified off. After the final loop, restarting the Tailscale app preserved the all-app UID range and passed DNS, private HTTP and direct internet probes. The phone finished connected with Wi-Fi on. NetBird's laptop service was disabled to prevent reconnecting at boot; its profile was retained.

Raw command output is retained locally under `.history/tailscale-migration-2026-09-09/`, which is git-ignored. `phone-roam-final.jsonl` and `pihole-final.log` contain the final loop and query provenance. `phone-roam.jsonl` records the earlier passing loop before the all-app change. The first test attempt stopped on an overly short probe timeout; the corrected test retries within the same 30-second recovery limit. An intermediate run was deliberately stopped to check a policy setting. Neither is counted as a completed ten-cycle run.

A several-day soak on both devices remains outstanding. Wi-Fi-to-hotspot was not tested. Keep NetBird deployed until that soak passes. A single Pi-hole or home outage intentionally prevents uncached system DNS resolution. This config does not enforce DNS for arbitrary applications that bypass the system resolver, or while the VPN is disconnected.

## Rollback

On the phone, turn off Tailscale Always-on VPN, disconnect Tailscale and open JetBird to reconnect its saved profile. On spylinux, run `sudo tailscale down`, `sudo systemctl enable --now netbird@default.service`, then `netbird up`. Confirm DNS and private access before disabling the Tailscale service. Do not run both clients' private routes and DNS together. The NetBird service is disabled during the migration because its saved profile still permits automatic connection after boot.

The cluster router can stay deployed while clients roll back. If it must be stopped, scale the deployment to zero and retain the PVC. Do not delete NetBird, change Cloudflare public ingress, or retire the old WireGuard forwards as part of this rollback.
