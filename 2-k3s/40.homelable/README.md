# Homelable

Homelable is the homelab index: a drawn network and rack canvas with live up/down checks, Markdown pages, and a network scanner. It replaces RackPeek. Design: [docs/superpowers/specs/2026-11-24-homelable-design.md](../../docs/superpowers/specs/2026-11-24-homelable-design.md).

At https://homelable.epaflix.com, sign in through Authentik as a member of `authentik Admins`. The site is on Traefik's internal entrypoint only (LAN and Tailscale). Login is OpenID only (`AUTH_MODE=oidc`): Homelable turns its local login off, and it has no group check of its own, so the policy binding on Authentik application `homelable` is the only gate. The provider and application live in `2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml`; the client id and secret exist there and in `secrets.enc.yaml`, so change them in both in the same commit.

## Pod

One pod with two containers. `backend` is the API on 8000 and runs as root, as the upstream image requires, with only `NET_RAW` added for ping and nmap SYN scans. `frontend` is nginx on 80 and proxies `/api` to `127.0.0.1:8000`. Both have a read-only root; nginx writes only to emptyDirs. The Service exposes port 80 only.

The pod sits on the normal pod network, so scans see hosts and open ports on `192.168.10.0/24` but no MAC addresses or mDNS names. The egress policy allows DNS, Traefik (Authentik and `git-internal.epaflix.com`) and the two homelab subnets, nothing else.

## Data

SQLite and uploads are on PVC `homelable-data-pithari` at `/app/data`. On a version change the backend copies the database to `homelab.db.back-<version>` once. Renovate opens Homelable updates for review only, because an upgrade migrates the database one way.

Settings saved in the UI go to `/app/data/scan_config.json` and would override the environment. Init container `reset-settings` deletes that file at every start, so the manifests stay the source of truth. Change settings here, not in the UI.

## Keys

Secret `homelable-secrets` holds `SECRET_KEY`, `MCP_SERVICE_KEY`, the OIDC client and `GIT_SYNC_TOKEN`. `MCP_SERVICE_KEY` opens every API endpoint and never expires. Middleware `strip-service-key` removes the `X-MCP-Service-Key` header from every request through Traefik, so the key only works inside the cluster. `LIVEVIEW_KEY`, `DOCS_VIEW_KEY` and `HOMEPAGE_API_KEY` stay unset on purpose: any value turns on pages that need no login.

Scripts call `http://homelable.homelable.svc` with `X-MCP-Service-Key`. The ingress policy admits only Traefik and pods labelled `app: homelab-sync`. The homelab agent uses `kubectl -n homelable port-forward svc/homelable 8080:80` and reads the key inside the command that sends it.

## Git history

`GIT_SYNC_*` pushes documents and diagrams, including node notes and properties, to the private Forgejo repo `homelab-bot/homelable-config` over `https://git-internal.epaflix.com`, when someone presses Sync or the nightly sync calls it. The token is the bot's `write:repository` token. Forgejo creates the repo on the first push.

## Homelab index

The nightly sync owns the design `Homelab index` and rewrites it every night. Draw your own maps on other designs. The UI saves a whole design at once, so a tab opened on `Homelab index` before the 03:30 sync and saved after it deletes the nodes that run added; the next run puts them back.
