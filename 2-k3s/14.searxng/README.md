# SearXNG

Self-hosted metasearch engine. Sole consumer is the `pi` coding agent on the
maintainer workstation, which calls the JSON API for its `web_search` tool.

## Design
- ArgoCD-managed (app-of-apps `app-searxng`), kustomize + ksops.
- `secret_key` injected from SOPS Secret `searxng-secret` via a render
  initContainer into `/etc/searxng/settings.yml` (the SOPS-seed +
  render-initContainer pattern; filebrowser was the original example, since
  decommissioned).
- `limiter: false`, `public_instance: false` → no Valkey/Redis, stateless,
  single replica. JSON API enabled via `search.formats: [html, json]`.
- Exposed at `https://searxng.epaflix.com` (Traefik websecure, wildcard
  `*.epaflix.com` Let's Encrypt cert). Internal-only: Pi-hole A record
  `searxng.epaflix.com → 192.168.10.101`; no Cloudflare tunnel, not public.

## Verify
    curl --resolve searxng.epaflix.com:443:192.168.10.101 \
      'https://searxng.epaflix.com/search?q=test&format=json' | jq '.results | length'

## Roll back
Revert the `14.searxng` manifests on `main`; ArgoCD prunes after the
soak-window prune flip, or delete the Application + namespace manually.

## Two routes on purpose (#547)

SearXNG answers on two entry points, and the difference is deliberate.

| Entry point | LB | Auth | Who uses it |
|---|---|---|---|
| `websecure` (`searxng-https`) | 192.168.10.101 | **Authentik forward-auth** | the public internet, including anyone reaching the origin IP directly around Cloudflare |
| `internal` (`searxng-internal`) | 192.168.10.102 | none | LAN clients and API/automation queries |

Pi-hole resolves `searxng.epaflix.com` to **192.168.10.102**, so anything on the LAN
lands on the unauthenticated route. Public DNS goes through Cloudflare to the origin,
which is the gated route.

Why not just allow-list the LAN at the edge: Traefik cannot see client IPs. Its access
log records `10.42.0.0` for every request, because both LoadBalancers run
`externalTrafficPolicy: Cluster` and no forwarded-headers config exists (#560). A
separate entry point is the only way to tell the two apart today.

Forward-auth needs a per-app Authentik proxy provider. The `searxng` provider,
its application and its embedded-outpost membership live in
`2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml`. There is no policy
binding, so any authenticated Authentik user may use it.

Querying the JSON API from a script:

```bash
curl -s --resolve searxng.epaflix.com:443:192.168.10.102 \
  'https://searxng.epaflix.com/search?q=example&format=json'
```

Point that at `.101` and it returns an Authentik redirect instead of results.
