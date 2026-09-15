# TLS monitoring

Issue #1122. The observability ArgoCD application renders the pinned blackbox
exporter chart and its ServiceMonitors through Kustomize. The monitors discover
the in-cluster exporter Service through EndpointSlice and pass each remote URL
as a probe parameter. The `instance` label identifies the endpoint.

Probes run every minute. Expiry below 14 days warns, below 3 days is critical,
and failed probes, failed scrapes or missing certificate dates are critical
after five minutes. Both expiry alerts can fire below three days.

## Targets

| Endpoint | Reason |
| --- | --- |
| `192.168.10.10:8006` | takaros PVE certificate |
| `192.168.10.11:8006` | evanthoulaki PVE certificate |
| `192.168.10.101:443` | External Traefik wildcard, SNI `grafana.epaflix.com` |
| `192.168.10.102:443` | Internal Traefik wildcard, same SNI, separate listener |
| `192.168.10.200:8443` | TrueNAS UI certificate, bypasses Traefik |

PBS on port 8007 is excluded for now. Neither the tracked configuration nor the
vault inventory identified its address during implementation. Add its direct
URL to `blackbox-values.yaml` once the PBS address is confirmed.

The probes skip CA and hostname verification so private certificates and expired
certificates still yield expiry metrics. SNI selects the intended certificate.
Redirects are not followed, so an authentication redirect cannot substitute
another server's certificate. HTTP redirects and authentication responses count
as reachable endpoints. This checks certificate dates, not trust-chain validity.

## Delivery check

Before closing #1122, deploy the chart and rules, confirm all five
`probe_success{job="tls-probe"}` values are 1 and certificate dates exist, then
temporarily raise an expiry threshold on a uniquely named test rule. Observe
Prometheus firing and ntfy `k8s-alertmanager` receiving FIRING. Restore the
expression, observe RESOLVED on ntfy, then remove the test rule. Record timestamps
and notification IDs in the issue. Never expire a real certificate for this test.

Verified on 2026-09-15 with `TLSCertificateExpiryCriticalTest1122`, a 365-day
threshold on the live takaros metric. ntfy recorded FIRING at 14:59:49 UTC,
ID `GqU5vVNeqaLn`, and RESOLVED at 15:04:49 UTC, ID `ZD1FF5s99axD`, after
restoring the three-day threshold. Prometheus reported firing then an empty
alert vector. The temporary rule was deleted. All five probes returned 1 and
remaining certificate lifetimes were 66.78 days for both PVE hosts, 59.43 days
for both Traefik listeners and 253.19 days for TrueNAS.
