# SearXNG searches leave only through AirVPN

Approved by Spyros on 2026-11-19.

## Intent

Search engines rate-limit and CAPTCHA the home IP, so SearXNG returns junk or nothing. Every outbound SearXNG request must leave through AirVPN. If the tunnel is down, searches fail. They never fall back to the home IP.

## Behavior

1. D1. A Deployment `airvpn-proxy` in namespace `searxng` runs gluetun (`qmcgaw/gluetun`, tag pinned in the kustomization `images:` block so Renovate tracks it). It connects to AirVPN over WireGuard and serves an HTTP proxy on port 8888. Its firewall drops traffic outside the tunnel, and its health check restarts the tunnel when it fails.
2. D2. The WireGuard config belongs to the AirVPN device `searxng` (not `Default`, which qBittorrent uses). It is fetched from the AirVPN generator API and stored only as a SOPS-encrypted Secret in `2-k3s/14.searxng/`.
3. D3. Servers come from the Netherlands, Germany and Sweden, the countries Bluetit uses.
4. D4. SearXNG sends all outgoing requests through `http://airvpn-proxy:8888` (`outgoing.proxies`).
5. D5. A NetworkPolicy allows SearXNG egress only to `airvpn-proxy` on 8888 and to cluster DNS. A NetworkPolicy allows `airvpn-proxy` ingress on 8888 only from SearXNG.
6. D6. Before the PR, a scratch copy proves: the proxy's exit IP is AirVPN's, a search returns results, a direct request from the SearXNG pod is blocked, and with the proxy stopped a search fails without traffic leaving through the home IP.

## Non-goals

- Sharing the proxy with Prowlarr or other apps (#1022).
- New alerts.
- Excluding the servers Bluetit blacklists (Cygnus, Hassaleh, Kajam). gluetun can only include servers, not exclude them.
- Changing the Bing engine or adding API-key engines.

## Constraints

- No key material in chat, logs, Git history or PR text. Only the `.enc.yaml` file reaches Git.
- Only one tunnel per device key may run. The scratch copy must be gone before Argo syncs the real one.
- The SearXNG ingress stays as it is. The Discord bots, both MCP hubs and remote-pi search-guard keep reaching SearXNG on 8080.
