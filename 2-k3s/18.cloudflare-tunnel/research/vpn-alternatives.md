# VPN alternative assessment

Assessed 2026-09-09. Spyros stopped the WARP cutover and kept Pi-hole mandatory. This is an assessment, not authorization to migrate to another provider. NetBird remains the current VPN.

## Recommendation

Consider hosted Tailscale with a homelab subnet router and Pi-hole as the global DNS server. This requires changing the Cloudflare-only requirement for private VPN connections. Public `*.epaflix.com` services can continue using the existing Cloudflare Tunnel.

If private VPN connections must also enter through Cloudflare, retain NetBird while investigating its Android recovery problem. No replacement assessed here meets every original constraint without changing the DNS entitlement or the private VPN provider.

## Proposed Tailscale design

| Requirement | Proposed configuration and limit |
| --- | --- |
| Pi-hole on phone and laptop from any network | Set `192.168.10.30` as the sole global nameserver, enable **Override DNS servers**, and have both clients accept Tailscale DNS. Disable MagicDNS if tailnet-name lookups must also reach Pi-hole. No public secondary resolver. |
| Homelab reachability | Advertise `192.168.10.0/24` through one homelab subnet router with IP forwarding enabled. Add `10.0.0.0/24` only if cluster-node access is required. Approve routes, restrict access to the enrolled user, and allow both TCP and UDP port 53 to Pi-hole. |
| Internet goes directly through the current network | Do not select or advertise an exit node. On Linux, enable subnet-route acceptance explicitly. |
| No new inbound forwards or user-operated VPS | Use outbound connectivity, direct peer connections when possible, and Tailscale-hosted DERP relays when necessary. Prevent automatic UPnP/NAT-PMP mappings before the pilot as well as avoiding manual forwards. Do not deploy a custom relay. |
| Cloudflare entrypoint | Existing public web ingress stays on Cloudflare. Private VPN traffic uses Tailscale and therefore does **not** meet a Cloudflare-only private-entrypoint requirement. |
| Traceability | Keep Pi-hole query logging. Default subnet-router source NAT means Pi-hole sees the router's address, not separate client addresses. Per-device attribution would require a different routing arrangement. |
| Cost | Current Personal pricing is $0 for up to six users, unlimited user devices and 50 tagged resources. It requires personal, non-commercial use. Check eligibility before enrollment. |

Tailscale documents [Pi-hole integration](https://tailscale.com/kb/1114/pi-hole), [global DNS override](https://tailscale.com/kb/1054/dns), [subnet routing](https://tailscale.com/kb/1019/subnets), [connection types](https://tailscale.com/kb/1257/connection-types), [firewall requirements](https://tailscale.com/kb/1082/firewall-ports), and [pricing](https://tailscale.com/pricing).

DNS override governs system DNS. Browser or application-specific encrypted DNS can bypass it. Check those settings and Android Private DNS during validation; do not claim that one successful direct query proves all applications use Pi-hole. A single home Pi-hole also makes remote DNS depend on the home connection and that server being available.

## Why not the other paths

- O1. **WARP with Pi-hole for every domain.** Cloudflare's documented [Resolver policies](https://developers.cloudflare.com/cloudflare-one/traffic-policies/resolver-policies/) require Enterprise. During the preceding migration preflight, its API rejected Local Domain Fallback suffixes `.`, `*`, and an empty string with error 2048. The disabled test profile was deleted. This rules out the tested catch-all approach, not every conceivable workaround.
- O2. **Upgrade the official NetBird Android app and assume recovery is fixed.** [Version 0.6.0](https://github.com/netbirdio/android-client/releases/tag/v0.6.0), released 2026-09-09, advertises network-switch improvements. The installed `io.netbird.client` package reports version `v0.6.0-rc.6`, alongside JetBird `1.8.9`. Its APK provenance was not verified. Comparing the published `v0.6.0-rc.6` and `v0.6.0` GitHub source trees found identical Go submodule and file hashes for `EngineRunner`, `NetworkSwitchNotifier`, `VPNService`, and `ConcreteNetworkAvailabilityListener` between those tags. This does not establish installed-source identity or whole-package equivalence. The release notes alone do not establish a new fix for this phone. [Issue 2029](https://github.com/netbirdio/netbird/issues/2029) remains open. Do not treat either release notes or issue status as a substitute for a reproduction test.
- O3. **Headscale behind Cloudflare.** The project's [reverse-proxy guide](https://headscale.net/stable/ref/integration/reverse-proxy/#cloudflare) says Cloudflare Proxy and Tunnel are unsupported because the protocol requires WebSocket POSTs. The page is community-maintained and warns that developers have not verified it. This agrees with the prior estate probe, but is not a claim that every future implementation must fail. It is not a validated substitute for hosted Tailscale here.

Source trees for the NetBird comparison: [release-candidate tag](https://api.github.com/repos/netbirdio/android-client/git/trees/v0.6.0-rc.6?recursive=1), [stable release](https://api.github.com/repos/netbirdio/android-client/git/trees/v0.6.0?recursive=1). This assessment did not switch or reinstall either Android app.

## Acceptance before any migration

- A1. Confirm that Tailscale may handle private VPN connections. Keep NetBird deployed and its device profiles intact. Switch one client at a time; concurrent VPNs can collide on routes and DNS, and Android permits one active VPN service.
- A2. From an external network, prove ordinary system DNS reaches Pi-hole using fresh public and internal queries plus Pi-hole logs. Cover A and AAAA, a blocked domain, a permitted domain and an internal record. Direct `dig @192.168.10.30` is only a reachability check. Confirm internet egress remains direct and required private services work. In an isolated client test, verify fresh system lookups fail when Pi-hole is unreachable rather than using another upstream.
- A3. Run ten Wi-Fi interruption/recovery cycles, polling DNS and private-service access every five seconds. Require recovery within 30 seconds after a working underlying connection returns, without restarting the VPN app. Test Wi-Fi-to-hotspot or cellular only after confirming usable data and any roaming cost. Include a relayed external-network test, sleep/wake, and a several-day soak on both devices. Tailscale documentation is not proof that this Pixel's recovery issue is solved.

No Tailscale deployment, enrollment, cutover, network-hopping test or soak was performed for this assessment.
