# SearXNG

Self-hosted metasearch engine. Sole consumer is the `pi` coding agent on the
maintainer workstation, which calls the JSON API for its `web_search` tool.

## Design
- ArgoCD-managed (app-of-apps `app-searxng`), kustomize + ksops.
- `secret_key` injected from SOPS Secret `searxng-secret` via a render
  initContainer into `/etc/searxng/settings.yml` (filebrowser pattern).
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
