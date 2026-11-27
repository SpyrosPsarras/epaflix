# Homelable for the homelab: what it is and how it would run on k3s

Sources are the website, the GitHub repository at tag `v3.8.0` (commit `0c0418f2588dd34922354603dc11066f9129fb5b`), its issues and pull requests, the GitHub REST API and the GHCR registry API. I fetched them with the pod clock reading 2026-11-27 UTC; the file name uses the requested date. Star and issue counts will drift.

In the links below, `T` means `https://github.com/Pouzor/homelable/blob/v3.8.0`.

## 1. Verdict

Homelable is one central web app: a FastAPI backend with SQLite, an nginx frontend and an optional MCP server. There is no agent and no DaemonSet. "Install on the whole cluster" can only mean one backend pod that can reach the LAN. On k3s that means a single-replica Deployment, `Recreate`, a PVC for `/app/data`, and `hostNetwork: true` pinned to one node if you want MACs and mDNS from `192.168.10.0/24`. There is no official Helm chart or Kubernetes manifest, so you would write the kustomize folder yourself.

It overlaps with RackPeek on inventory and notes, but it is a different tool. Homelable draws and live-checks the network and racks, and finds devices itself with ping, nmap, ARP and mDNS. RackPeek, as run here, is a Git-backed YAML index fed by the owner's own Kubernetes and SSH sync. Homelable has no Kubernetes import in v3.8.0.

Two security points matter before any deployment. The images run as root, and the maintainer declined to change the `admin/admin` and `svc_changeme` defaults. The MCP service key authenticates every backend endpoint, and the frontend's nginx forwards that header. Treat it as a root credential and set it from SOPS.

## 2. What it is and does

The README calls it "Self-hosted homelab infrastructure visualization, documentation, scanning & live monitoring" ([README](https://github.com/Pouzor/homelable/blob/v3.8.0/README.md)). The website lists 15 node types, a network canvas, a rack canvas, an electrical canvas, live status, a document space, Zigbee and Z-Wave mesh import, Proxmox import, an MCP server, PNG/SVG export and a Home Assistant (HACS) variant ([homelable.net](https://homelable.net/)).

Feature list from [FEATURES.md](https://github.com/Pouzor/homelable/blob/v3.8.0/FEATURES.md):

- Zones, groups and nesting (VMs and LXCs inside a Proxmox node), text labels, several canvases, themes.
- Rack canvas: 19" and 10" racks, 1 to 48 U, faceplates, port-to-port patching, mounts that follow a node's status check ([rack-canvas.md](https://github.com/Pouzor/homelable/blob/v3.8.0/docs/rack-canvas.md)).
- Floor-plan background images.
- Network scanner, Zigbee2MQTT, Z-Wave JS UI and Proxmox VE imports. v3.7.0 and v3.8.0 added Unraid and UniFi imports ([changelog](https://homelable.net/changelog), [.env.example](https://github.com/Pouzor/homelable/blob/v3.8.0/.env.example)).
- Device Inventory: every discovered device with IP, MAC, hostname and services, to approve, hide or ignore.
- Documentation: a markdown page per device, generated once from scan facts, plus free pages. Wiki links, backlinks, full-text search (SQLite FTS5), up to 50 revisions per document ([documentation.md](https://github.com/Pouzor/homelable/blob/v3.8.0/docs/documentation.md), [homelable.net](https://homelable.net/)).
- Live status checks per node: `ping`, `http`, `https`, `tcp`, `ssh`, `prometheus` (GET `/metrics`), `health` (GET `/health`). Default interval 60 s, pushed to the browser over WebSocket. `STATUS_CHECKER_ENABLED=false` turns all probes off ([FEATURES.md §14](https://github.com/Pouzor/homelable/blob/v3.8.0/FEATURES.md)).
- Export: PNG, SVG, canvas YAML (re-importable), inventory as a Markdown table ([FEATURES.md §15](https://github.com/Pouzor/homelable/blob/v3.8.0/FEATURES.md)).
- Live View and Documentation View: read-only, no-login pages behind a URL key ([README](https://github.com/Pouzor/homelable/blob/v3.8.0/README.md)).
- Git sync: pushes documentation and diagrams to a Git repo on an explicit Sync, never on its own ([.env.example](https://github.com/Pouzor/homelable/blob/v3.8.0/.env.example), [git-sync.md](https://github.com/Pouzor/homelable/blob/v3.8.0/docs/git-sync.md)).
- Standalone mode: frontend only, canvases in browser `localStorage`, no scanning or login ([FEATURES.md](https://github.com/Pouzor/homelable/blob/v3.8.0/FEATURES.md)).

### Discovery methods and the privileges they need

Network scans run only when someone starts them, from the UI, the REST API or the MCP tool. The scheduler has jobs for status checks, service checks and the Proxmox, Unraid, Zigbee, Z-Wave and UniFi syncs, but no network-scan job ([core/scheduler.py](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/core/scheduler.py)).

| Method | What it does | Privileges and network position | Source |
|---|---|---|---|
| Manual | Draw nodes, add inventory entries, or create them through the REST API or MCP | A logged-in user or API credential | [FEATURES.md](https://github.com/Pouzor/homelable/blob/v3.8.0/FEATURES.md), [api.md](https://github.com/Pouzor/homelable/blob/v3.8.0/docs/api.md) |
| Ping sweep (scan phase 1) | `ping -c 1 -W 1` to every host in each `SCANNER_RANGES` CIDR, 50 at a time | ICMP raw socket. Compose adds `cap_add: NET_RAW`, commented "Required for ping-based status checks". Routed reach to the CIDR | [scanner.py L272-300](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/services/scanner.py#L272-L300), [docker-compose.prebuilt.yml](https://github.com/Pouzor/homelable/blob/v3.8.0/docker-compose.prebuilt.yml) |
| ARP cache | Reads `/proc/net/arp` after the sweep to attach MACs | Layer 2 adjacency. On a Docker bridge "the scan will never report a MAC address". Fix: `network_mode: host` (Linux only) or macvlan. `NET_RAW` "does not change this" | [scanner.py L217-250](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/services/scanner.py#L217-L250), [INSTALLATION.md](https://github.com/Pouzor/homelable/blob/v3.8.0/INSTALLATION.md#scanning-from-docker--mac-addresses) |
| nmap port scan (phase 2) | Pass A `-sS` (if root) or `-sT`, `--open -T4 -Pn` on 87 curated ports plus `SCANNER_HTTP_RANGES`; pass B `-sV` on the ports found | `-sS` is chosen when `geteuid() == 0`. The image runs as root, so SYN scan needs `NET_RAW`. As non-root it falls back to TCP connect | [scanner.py L360-L415](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/services/scanner.py#L360-L415), [INSTALLATION.md, non-root service](https://github.com/Pouzor/homelable/blob/v3.8.0/INSTALLATION.md#scanning-as-a-non-root-service) |
| HTTP probe | Optional HTTP request to open ports for service ID (`SCANNER_HTTP_PROBE_ENABLED`) | Plain TCP reach | [README](https://github.com/Pouzor/homelable/blob/v3.8.0/README.md#deep-scan-custom-ports) |
| mDNS | 4 s passive browse of `_http._tcp`, `_shelly._tcp`, `_esphomelib._tcp`, `_hap._tcp`, `_mqtt._tcp`, `_device-info._tcp` during a scan | Multicast on the same segment. This is my inference; the docs do not state it | [scanner.py L164-171, L515-571](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/services/scanner.py#L515-L571) |
| Proxmox VE | REST API on port 8006: hosts, VMs, LXCs, specs, scheduled auto-sync | API token with read-only `PVEAuditor` at `/`, propagate on. QEMU guest agent for VM IPs. Token env-only | [proxmox-import.md](https://github.com/Pouzor/homelable/blob/v3.8.0/docs/proxmox-import.md), [.env.example](https://github.com/Pouzor/homelable/blob/v3.8.0/.env.example) |
| Unraid | Server, Docker containers and VMs over the Unraid API | API key with read access to Docker, VMs and Info | [unraid-import.md](https://github.com/Pouzor/homelable/blob/v3.8.0/docs/unraid-import.md), [.env.example](https://github.com/Pouzor/homelable/blob/v3.8.0/.env.example) |
| UniFi | Devices and clients from the controller's classic API | API key, or a local-only admin account (UI.com accounts with MFA fail) | [unifi_service.py](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/services/unifi_service.py), [.env.example](https://github.com/Pouzor/homelable/blob/v3.8.0/.env.example) |
| Zigbee2MQTT, Z-Wave JS UI | Mesh topology over MQTT | Broker reach, optional MQTT credentials, optional TLS | [zigbee-import.md](https://github.com/Pouzor/homelable/blob/v3.8.0/docs/zigbee-import.md), [zwave-import.md](https://github.com/Pouzor/homelable/blob/v3.8.0/docs/zwave-import.md) |
| Docker API | Not found. Ports 2375/2376 are only fingerprinted as "Docker API" | n/a | [service_signatures.json](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/data/service_signatures.json) |
| Kubernetes API | Not in v3.8.0. Draft PR [#473](https://github.com/Pouzor/homelable/pull/473) (read-only topology), open since 2026-09-11. The maintainer wrote "I can't test myself" on [#330](https://github.com/Pouzor/homelable/issues/330) | n/a | same |
| SNMP, LLDP/CDP | Not in v3.8.0. Open PR [#432](https://github.com/Pouzor/homelable/pull/432), issue [#367](https://github.com/Pouzor/homelable/issues/367) | n/a | same |
| Agents | None. Feature request [#465](https://github.com/Pouzor/homelable/issues/465) has no replies | n/a | same |
| Tailscale | Not in v3.8.0. Open PR [#627](https://github.com/Pouzor/homelable/pull/627) | n/a | same |

Two doc mismatches. The README says the scanner runs `nmap -sV --open`, but the code runs the ping sweep and two separate nmap passes ([scanner.py L331-L415](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/services/scanner.py#L331-L415)). The docs mention OS detection (`-O`), but I found no `-O` in the v3.8.0 scanner.

The bare-metal unit suggests `AmbientCapabilities=CAP_NET_RAW CAP_NET_ADMIN` for raw scans ([INSTALLATION.md](https://github.com/Pouzor/homelable/blob/v3.8.0/INSTALLATION.md#scanning-as-a-non-root-service)). The Docker compose files only add `NET_RAW`.

## 3. Install

### Methods

The website lists three paths: Docker ("recommended", "One compose file. Frontend + backend + SQLite."), the community-scripts Proxmox LXC, and HACS for Home Assistant ([homelable.net](https://homelable.net/)). INSTALLATION.md adds bare metal (`scripts/install-baremetal.sh`, systemd plus nginx, Debian 12+ or Ubuntu 22.04+) and build from source ([INSTALLATION.md](https://github.com/Pouzor/homelable/blob/v3.8.0/INSTALLATION.md)). The README also links a RepoCloud one-click deploy. The LXC script lives in the third-party `community-scripts/ProxmoxVE` repo, which I did not read.

The Docker quick start runs `install.sh`, which writes `docker-compose.prebuilt.yml` as `docker-compose.yml` and pulls prebuilt images ([INSTALLATION.md](https://github.com/Pouzor/homelable/blob/v3.8.0/INSTALLATION.md#pre-built-docker-images)).

### Images

| Image | Base | Port | Contents |
|---|---|---|---|
| `ghcr.io/pouzor/homelable-backend` | `python:3.13-slim` plus `nmap`, `iputils-ping`, `curl`, `git`, `openssh-client` | 8000 | API, scanner, status checker, scheduler |
| `ghcr.io/pouzor/homelable-frontend` | `nginx:alpine` | 80 | SPA, proxies `/api`, `/ws`, `/docs`, `/redoc`, `/openapi.json` to `${BACKEND_UPSTREAM}` (default `backend:8000`) |
| `ghcr.io/pouzor/homelable-frontend-standalone` | `nginx:alpine` | 80 | SPA with no backend |
| `ghcr.io/pouzor/homelable-mcp` | `python:3.13-slim` | 8001 | MCP server over Streamable HTTP at `/mcp` |

Sources: [Dockerfile.backend](https://github.com/Pouzor/homelable/blob/v3.8.0/Dockerfile.backend), [Dockerfile.frontend](https://github.com/Pouzor/homelable/blob/v3.8.0/Dockerfile.frontend), [docker/nginx.conf](https://github.com/Pouzor/homelable/blob/v3.8.0/docker/nginx.conf), [mcp/Dockerfile.mcp](https://github.com/Pouzor/homelable/blob/v3.8.0/mcp/Dockerfile.mcp), [INSTALLATION.md](https://github.com/Pouzor/homelable/blob/v3.8.0/INSTALLATION.md#pre-built-docker-images).

- Tags: `latest` and `main` follow the tip of `main`. Releases get `X.Y.Z` and `X.Y`, without a `v` prefix ([docker-publish.yml L47-55](https://github.com/Pouzor/homelable/blob/v3.8.0/.github/workflows/docker-publish.yml#L47-L55)). The registry lists `3.8.0`, `3.8`, `main` and `latest`, and older releases back to at least `3.1.1`.
- Architectures: `linux/amd64` and `linux/arm64` ([docker-publish.yml L67](https://github.com/Pouzor/homelable/blob/v3.8.0/.github/workflows/docker-publish.yml#L67)), confirmed in the 3.8.0 index.
- Pinnable digests for `3.8.0`, read anonymously from the GHCR v2 API. All three carry `org.opencontainers.image.revision` set to commit `0c0418f`:
  - backend `sha256:f4f8b8061af27084c793803614e0009f3440fbbf7cc12033438868a86452d60a`
  - frontend `sha256:629a18054f13b54e9c6dfbab059aca27bbf4bcc083a8366d43706d59aa5c6520`
  - mcp `sha256:995aa4bdf0c1f28a651cf4f025cb26bb325df70c06d5066f998dafdc522a5a1c`
- Image user: none set in any image config, so all three run as root (UID 0). No `USER` line in the Dockerfiles. A PR that switched backend and MCP to UID 10001 had that part reverted. The maintainer said it breaks existing root-owned volumes and drops scans from `-sS` to `-sT` ([#513 comment](https://github.com/Pouzor/homelable/pull/513)).
- Healthcheck in compose: `curl -f http://localhost:8000/api/v1/health` ([docker-compose.yml](https://github.com/Pouzor/homelable/blob/v3.8.0/docker-compose.yml)).

### Data, database and backups

- Database: SQLite only, via `sqlite+aiosqlite`, with a 30 s lock timeout ([database.py L161-173](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/db/database.py#L161-L173)). No Postgres or Redis support found. The backend dependencies have no Postgres driver ([requirements.txt](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/requirements.txt)).
- Data path: the `backend_data` volume at `/app/data`, with `SQLITE_PATH=/app/data/homelab.db` ([docker-compose.prebuilt.yml](https://github.com/Pouzor/homelable/blob/v3.8.0/docker-compose.prebuilt.yml)). The same directory holds `uploads/`, `scan_config.json` and the `git-sync/` working copy ([config.py L390-403](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/core/config.py#L390-L403)).
- `scan_config.json` holds settings saved in the UI (scan ranges, intervals, auto-sync switches). It is loaded after the environment at start and wins over it ([config.py L404](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/core/config.py#L404)). For GitOps, settings changed in the UI will not show up in the manifests.
- Backups: on start, the backend copies the DB to `homelab.db.back-<version>` once per version ([database.py L184-195](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/db/database.py#L184-L195)). The bare-metal docs say "Back up this folder" for the data dir ([INSTALLATION.md](https://github.com/Pouzor/homelable/blob/v3.8.0/INSTALLATION.md#afterwards)). No scheduled backup or dump command found.

### Environment variables

All in [.env.example](https://github.com/Pouzor/homelable/blob/v3.8.0/.env.example). The ones that matter here:

- Core: `SECRET_KEY` (required, no default in code, [config.py L98](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/core/config.py#L98)), `SQLITE_PATH`, `UPLOAD_DIR`, `CORS_ORIGINS` (must include the browser origin behind TLS).
- Auth: `AUTH_MODE` (`local` or `oidc`), `AUTH_USERNAME`, `AUTH_PASSWORD_HASH` (bcrypt), `ACCESS_TOKEN_EXPIRE_MINUTES`, `OIDC_*`.
- Scanner and checks: `SCANNER_RANGES`, `SCANNER_HTTP_RANGES`, `SCANNER_HTTP_PROBE_ENABLED`, `SCANNER_HTTP_VERIFY_TLS`, `STATUS_CHECKER_INTERVAL`, `STATUS_CHECKER_ENABLED`.
- Keys: `MCP_API_KEY`, `MCP_SERVICE_KEY`, `LIVEVIEW_KEY`, `DOCS_VIEW_KEY`, `HOMEPAGE_API_KEY`.
- Integrations: `PROXMOX_*` or `PROXMOX_ENDPOINTS`, `UNRAID_*`, `UNIFI_*`, `ZIGBEE_*`, `ZWAVE_*`, `GIT_SYNC_*`.
- Frontend: `BACKEND_UPSTREAM` at runtime; `VITE_BASE_PATH` only at build time, so the prebuilt images serve from `/` ([INSTALLATION.md](https://github.com/Pouzor/homelable/blob/v3.8.0/INSTALLATION.md#serving-under-a-subpath)).
- MCP container: `MCP_API_KEY`, `MCP_SERVICE_KEY`, `BACKEND_URL` ([mcp/app/config.py](https://github.com/Pouzor/homelable/blob/v3.8.0/mcp/app/config.py)).

## 4. Kubernetes

- No Helm chart, no Kubernetes manifests and no Kubernetes install docs in the repo at v3.8.0. Searching the repo for `kubernetes`, `k8s`, `helm` and `kubectl` only hits a code comment in the Proxmox importer. The issue search for "helm" returns no chart work.
- No documented Kubernetes caveats. The Docker caveats carry over, and these points are my inference from the code:
  - One replica. Scans run on a background thread in the backend process, and startup marks any scan still "running" as orphaned ([main.py](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/main.py)). APScheduler runs in-process, and the store is a single SQLite file. Two replicas would double every check and sync and contend for the DB. Use `Recreate`.
  - LAN position. A pod on flannel reaches `192.168.10.0/24` only through the node's NAT, so ARP gives no MACs and mDNS multicast will not arrive. That matches the Docker bridge case in [INSTALLATION.md](https://github.com/Pouzor/homelable/blob/v3.8.0/INSTALLATION.md#scanning-from-docker--mac-addresses). `hostNetwork: true` on a node with `eth0` on the LAN fixes both, at the cost of binding 8000 on the node. With host networking, the mDNS browse would also see `eth1` (10.0.0.0/24) unless the library limits interfaces.
  - Capabilities. Add `NET_RAW` for ping and SYN scans, as compose does. With a non-root `runAsUser` the scanner falls back to `-sT`, and the root-owned volume problem from [#513](https://github.com/Pouzor/homelable/pull/513) applies.
  - Probes. Backend `GET /api/v1/health` on 8000 returns `{"status":"ok"}` with no DB check ([main.py L112-114](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/main.py#L112-L114)). MCP has `GET /health` on 8001 ([mcp/app/main.py L46](https://github.com/Pouzor/homelable/blob/v3.8.0/mcp/app/main.py#L46)). The frontend has no dedicated health path; `/` serves the SPA.
  - Traefik. Either keep the nginx frontend and point `BACKEND_UPSTREAM` at the backend Service, or route `/api` and `/ws` to the backend in the IngressRoute. The WebSocket path is `/api/v1/status/ws/` ([docker/nginx.conf](https://github.com/Pouzor/homelable/blob/v3.8.0/docker/nginx.conf)).

## 5. Auth and security

- Local mode (default): one user, `AUTH_USERNAME` plus a bcrypt `AUTH_PASSWORD_HASH`. The default is `admin`/`admin` ([.env.example](https://github.com/Pouzor/homelable/blob/v3.8.0/.env.example), [INSTALLATION.md](https://github.com/Pouzor/homelable/blob/v3.8.0/INSTALLATION.md)). Login returns a bearer JWT, valid 24 h by default ([README](https://github.com/Pouzor/homelable/blob/v3.8.0/README.md#session-duration)).
- OIDC mode: Authorization Code with PKCE, a confidential client, an HttpOnly `__Host-` session cookie, CSRF and Origin checks. Authentik has a worked example. OIDC replaces local login (`/api/v1/auth/login` refuses outside `local` mode). Sessions are stateless JWTs with no server-side revocation. Logout does not end the IdP session ([oidc-auth.md](https://github.com/Pouzor/homelable/blob/v3.8.0/docs/oidc-auth.md), [routes/auth.py](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/api/routes/auth.py)). I found no group or role check, so access control has to sit in the Authentik application policy.
- Header or forward auth: not supported. The backend accepts only the MCP service key header, a bearer JWT or the OIDC cookie ([deps.py L22-62](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/api/deps.py#L22-L62)). Authentik forward auth can still gate the site in front, but Homelable would then ask for its own login as well.
- No per-user roles. Local mode has one account. OIDC users all get the same full access.
- API keys: no user-managed API keys. Scripts log in for a bearer token ([api.md](https://github.com/Pouzor/homelable/blob/v3.8.0/docs/api.md)). Fixed keys exist for single purposes: `HOMEPAGE_API_KEY` (stats only), `LIVEVIEW_KEY`, `DOCS_VIEW_KEY`, `MCP_API_KEY` (clients to MCP server), `MCP_SERVICE_KEY` (MCP server to backend).

Security notes:

- `MCP_SERVICE_KEY` "authenticates against every endpoint via the `X-MCP-Service-Key` header ... does not expire. Treat it as a root credential" ([api.md, Security](https://github.com/Pouzor/homelable/blob/v3.8.0/docs/api.md#security)). The README says it is "internal Docker network only, never exposed", but the frontend nginx forwards all of `/api/` with request headers intact ([docker/nginx.conf](https://github.com/Pouzor/homelable/blob/v3.8.0/docker/nginx.conf)). It works in OIDC mode too ([deps.py L28](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/api/deps.py#L28)). If left at `svc_changeme`, anyone who can reach the UI has full API access. I read this from code and did not test it.
- The maintainer declined to remove default credentials: "removing the default installation credentials is not wanted" ([#521](https://github.com/Pouzor/homelable/pull/521), closed unmerged 2026-10-02). The MCP image defaults are `mcp_sk_changeme` and `svc_changeme` ([mcp/app/config.py](https://github.com/Pouzor/homelable/blob/v3.8.0/mcp/app/config.py)). Nothing in the backend config rejects the default `SECRET_KEY` in local mode; only OIDC mode enforces 32 bytes ([config.py L140-L151](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/core/config.py#L140-L151)).
- `/docs`, `/redoc` and `/openapi.json` are unauthenticated. The login endpoint has no rate limit. "Homelable is meant for a LAN. Do not expose it to the internet." ([api.md](https://github.com/Pouzor/homelable/blob/v3.8.0/docs/api.md#security)).
- With `DOCS_VIEW_KEY` set, "uploaded media is served without authentication" ([README](https://github.com/Pouzor/homelable/blob/v3.8.0/README.md#documentation-view-read-only-public-documentation)).
- Hardening merged in [#513](https://github.com/Pouzor/homelable/pull/513) (2026-09-23): MCP path-segment validation, Proxmox tokens bound to their configured host, deep scans limited to one validated IP.
- CVEs and advisories: 0 published GitHub security advisories on the repo. A SearXNG search for "homelable CVE" found none. I did not query NVD directly. No `SECURITY.md` in the repo.
- Open issues: 21 issues and 13 PRs. None is labelled as a security issue. The newest are canvas bugs ([#629](https://github.com/Pouzor/homelable/issues/629), [#630](https://github.com/Pouzor/homelable/issues/630)) and duplicate inventory rows ([#622](https://github.com/Pouzor/homelable/issues/622)).

## 6. Maturity

From the GitHub REST API and the release list:

- License: MIT ([LICENSE](https://github.com/Pouzor/homelable/blob/v3.8.0/LICENSE)).
- Repo created 2026-03-06. First release v1.0.0 on 2026-03-09.
- Latest release v3.8.0 on 2026-11-24 ([releases](https://github.com/Pouzor/homelable/releases)).
- 59 releases in under 9 months. Per month: Mar 13, Apr 12, May 9, Jun 5, Jul 4, Aug 7, Sep 5, Oct 1, Nov 3. The last three were 3.7.0 (11-07), 3.7.1 (11-15) and 3.8.0 (11-24).
- 5,488 stars, 248 forks. 21 open issues, 13 open PRs, 243 closed issues.
- 25 contributors. `Pouzor` has 928 of 1,015 commits (about 91 %). The next is 24 commits.
- It is a one-person project in practice. The website says "Free forever. Built solo." and the footer names Remy Jardinet ([homelable.net](https://homelable.net/)).
- The repo has CI workflows for Docker builds, an install smoke test, quality and security checks ([.github/workflows](https://github.com/Pouzor/homelable/tree/v3.8.0/.github/workflows)).

Telemetry and phone-home:

- The website says "Self-hosted, zero telemetry" ([homelable.net](https://homelable.net/)). I found no analytics or telemetry library in backend, frontend or MCP code.
- The browser does make outside calls. The sidebar fetches `https://api.github.com/repos/Pouzor/homelable/releases/latest` for an update badge, with no setting to turn it off ([useLatestRelease.ts L18](https://github.com/Pouzor/homelable/blob/v3.8.0/frontend/src/hooks/useLatestRelease.ts#L18)). Brand icons load from `cdn.jsdelivr.net` ([nodeIcons.ts L243](https://github.com/Pouzor/homelable/blob/v3.8.0/frontend/src/utils/nodeIcons.ts#L243)). Swagger UI and ReDoc load from a public CDN ([api.md](https://github.com/Pouzor/homelable/blob/v3.8.0/docs/api.md)). The backend itself makes no such calls that I found.

## 7. Integrations and APIs

- REST: everything under `/api/v1`, with OpenAPI at `/openapi.json`, Swagger at `/docs` and ReDoc at `/redoc` ([api.md](https://github.com/Pouzor/homelable/blob/v3.8.0/docs/api.md)).
- MCP: a separate service on 8001 at `/mcp/`, Streamable HTTP, `X-API-Key` header. Read and write tools for nodes, edges, zones, inventory, scans, racks and documents, with `readOnlyHint` and `destructiveHint` annotations ([README](https://github.com/Pouzor/homelable/blob/v3.8.0/README.md#mcp-server-ai-integration-optional), [FEATURES.md §18](https://github.com/Pouzor/homelable/blob/v3.8.0/FEATURES.md)).
- Import and export: canvas YAML export and import, PNG, SVG, inventory as Markdown, all documents as a zip of `.md` files (`GET /api/v1/documents/export`), and Git push of documents and diagrams ([FEATURES.md §15](https://github.com/Pouzor/homelable/blob/v3.8.0/FEATURES.md), [routes/documents.py L538](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/api/routes/documents.py#L538), [git-sync.md](https://github.com/Pouzor/homelable/blob/v3.8.0/docs/git-sync.md)). No whole-database export found besides copying the SQLite file.
- Webhooks and notifications: not found. Push notifications are an open request ([#59](https://github.com/Pouzor/homelable/issues/59)).
- Prometheus metrics: not found. Homelable can check a target's `/metrics`, but it exposes no metrics endpoint of its own. An "Observatory monitoring integration" PR is open ([#520](https://github.com/Pouzor/homelable/pull/520)).
- Stats: `GET /api/v1/stats/summary` with `X-API-Key: $HOMEPAGE_API_KEY` returns node, online, offline, pending and zigbee counts, for gethomepage ([README](https://github.com/Pouzor/homelable/blob/v3.8.0/README.md#gethomepage-widget-read-only-stats)).
- Health: `/api/v1/health` (backend) and `/health` (MCP), as in section 4.
- Logs: plain text `LEVEL:logger:message` to stderr ([main.py](https://github.com/Pouzor/homelable/blob/v3.8.0/backend/app/main.py)). No JSON logging option found.

## 8. Overlap with RackPeek

RackPeek facts here come from [2-k3s/37.rackpeek/README.md](../../2-k3s/37.rackpeek/README.md) only. I did not read RackPeek upstream.

Both keep an inventory of devices with free-text notes, offer a REST API and an MCP server, and can push their data to Git.

Homelable has, and RackPeek as deployed here does not:

- A drawn network canvas and a rack canvas with U positions and port-to-port patching.
- Live status checks with WebSocket updates.
- Built-in discovery: ping sweep, nmap, ARP and mDNS.
- Proxmox, Unraid, UniFi, Zigbee2MQTT and Z-Wave importers with scheduled sync.
- Markdown documents with 50 revisions, wiki links, backlinks and full-text search.
- Its own login, local or OIDC.

RackPeek, as deployed here, has and Homelable does not:

- Data as one YAML file, committed to Forgejo with built-in Git sync. Homelable's data lives in SQLite; its Git sync pushes documents and diagrams one way, on a manual Sync.
- A fixed `X-Api-Key` for `/api` and `/mcp`. Homelable's only non-expiring full-access key is the MCP service key.
- A non-root, read-only-root image (UID 1654). Homelable's images run as root.
- Bulk replace of all items through the API (`mode: Replace`), which the nightly `rackpeek-sync` CronJob uses. That job also reads the Kubernetes API and scans 17 hosts over SSH. Homelable has no Kubernetes import in v3.8.0 and no host-level scanner.

## 9. Does it need something on every node?

No. It is one central web app: backend, frontend and optional MCP. There is no agent, DaemonSet or per-node collector in v3.8.0. A "lightweight agent" is only an open feature request with no replies ([#465](https://github.com/Pouzor/homelable/issues/465)). Every discovery runs from the backend's own network position. On this cluster, "the whole cluster" in practice means one backend pod that can see the LAN, plus Proxmox API tokens for the hosts.

## 10. Open questions

1. Q1: Whether nmap SYN scans in a k3s pod need `NET_ADMIN` as well as `NET_RAW`. Compose adds only `NET_RAW`; the bare-metal unit suggests both. Not tested.
2. Q2: Whether `hostNetwork` mDNS browsing picks up `eth1` and the flannel interfaces, and whether that adds noise. Inferred, not tested.
3. Q3: The exact header-forwarding risk of `X-MCP-Service-Key` through the frontend. Read from code, not tested against a running instance.
4. Q4: How the community-scripts LXC installer handles credentials and updates. That script lives outside the Homelable repo and was not read.
5. Q5: Whether Traefik in front of Homelable can carry the OIDC callback and WebSocket without extra middleware. The docs cover nginx only.
6. Q6: The HACS variant ([Pouzor/homelable-hacs](https://github.com/Pouzor/homelable-hacs)) was not researched. It runs inside Home Assistant and does not apply to k3s.
