# SearXNG web search for Pi — design

> Date: 2026-06-23
> Status: design approved, pending spec review
> Repo for cluster work: `SpyrosPsarras/epaflix` (this repo)
> Pi config lives outside this repo: `~/.pi/agent/` on the workstation (`192.168.10.177`)

## Purpose

Give the `pi` coding agent (installed at `/opt/pi-coding-agent`, using local Ollama models on TrueNAS `192.168.10.200:30068`) a `web_search` tool. Pi ships no built-in web search and no built-in MCP, so the capability is added as a Pi extension that calls a self-hosted SearXNG. SearXNG runs on the K3s cluster the Epaflix GitOps way (ArgoCD app), so nothing leaves the LAN except the outbound searches SearXNG itself makes.

## Why SearXNG self-hosted (vs hosted API / Ollama cloud search)

- No third-party API key, no per-query cost.
- Queries do not go through a SaaS search provider - only SearXNG's own upstream engine calls leave the network.
- Fits the existing private-infra setup.

## Architecture / data flow

```
pi (workstation 192.168.10.177)
  -> web_search tool (extension, IPv4-forced fetch)
  -> https://searxng.epaflix.com  (DNS A -> 192.168.10.101, Traefik LB)
  -> Traefik IngressRoute (websecure, wildcard *.epaflix.com cert)
  -> Service searxng:8080 (ClusterIP)
  -> SearXNG pod -> upstream search engines
```

The model then reads result URLs with the existing built-in `bash`+`curl` tool. A dedicated `fetch_url` tool is out of scope for v1 (see Out of scope).

## Component 1 — K3s app `2-k3s/13.searxng/`

Mirrors `2-k3s/09.filebrowser/` conventions (kustomize + ksops + Traefik IngressRoute + ArgoCD app).

Files:

- `namespace.yaml` — namespace `searxng`.
- `configmap.yaml` — SearXNG `settings.yml`. Required settings:
  - `search.formats: [html, json]` — enables the JSON API (default is html-only; without this Pi cannot parse results).
  - `server.limiter: false` and `server.public_instance: false` — the bot limiter blocks non-browser requests and needs Valkey/Redis; disabling it keeps the deployment single-pod with no datastore.
  - `server.bind_address: "0.0.0.0"`, port `8080`.
  - `server.secret_key` — not stored here; injected from the Secret via the `SEARXNG_SECRET` env the official image reads.
  - `general.instance_name: "epaflix-searxng"`, conservative default engine set, `language: en`.
- `searxng-secret.enc.yaml` — SOPS+age encrypted `kind: Secret` holding `SEARXNG_SECRET` (a random 32+ byte key). Single cluster age recipient, same as other `*.enc.yaml`. Pre-commit hook (`check-sops-encrypted.sh`) must accept it (never commit plaintext).
- `ksops-generator.yaml` — inflates `searxng-secret.enc.yaml` at render time (same shape as `filebrowser` ksops generator).
- `deployment.yaml`:
  - image `searxng/searxng:<pinned-tag>` — pin to the current latest release tag, confirmed against the registry at apply time; Renovate (`12.renovate`) then manages bumps.
  - 1 replica, `strategy: Recreate`.
  - env `SEARXNG_SECRET` from the Secret; env `SEARXNG_BASE_URL: https://searxng.epaflix.com/`.
  - `settings.yml` mounted from the ConfigMap at `/etc/searxng/settings.yml`.
  - liveness/readiness probes on `/healthz`.
  - resource requests `cpu: 100m`, `memory: 256Mi`, no limits (repo convention).
  - `securityContext` runAsNonRoot where the image allows.
- `service.yaml` — ClusterIP, port `8080` named `http`.
- `ingress.yaml` — two Traefik `IngressRoute`s, identical pattern to filebrowser:
  - `websecure`, `Host(\`searxng.epaflix.com\`)` -> svc `searxng:8080`, `tls.certResolver: cloudflare`, domains `epaflix.com` + `*.epaflix.com`.
  - `web`, same host, middleware `redirect-https@traefik-system` -> svc.
- `kustomization.yaml` — lists the resources above + `generators: [ksops-generator.yaml]`.
- `README.md` + `QUICKSTART.md` — per repo convention (what it is, how to verify, how to roll back).

No Valkey/Redis, no PVC (SearXNG is stateless with the limiter off).

## Component 2 — ArgoCD + DNS wiring

- `2-k3s/11.argocd/apps/app-searxng.yaml` — ArgoCD `Application`:
  - `repoURL: https://github.com/SpyrosPsarras/epaflix.git`, `targetRevision: main`, `path: 2-k3s/13.searxng` (kustomize).
  - `destination` namespace `searxng`.
  - `syncPolicy.automated: { selfHeal: true, prune: false }` for adoption. Flip `prune: true` after a soak window (tracked by a follow-up `gh issue`).
  - `ignoreDifferences` on Service `clusterIP`/`clusterIPs`/`status` (repo convention).
- Add `app-searxng.yaml` to `2-k3s/11.argocd/apps/kustomization.yaml`.
- **Adoption order** (repo CLAUDE.md): merge the aligned git first so the app-of-apps creates the Application; do not create the Application before the manifests are on `main`, or selfHeal reverts live state.
- **DNS** (`192.168.10.30`, Pi-hole): add a per-host A record `searxng.epaflix.com -> 192.168.10.101` to `/etc/dnsmasq.d/10-epaflix.conf` over SSH, then reload FTL. Edit dnsmasq.d only, never the Pi-hole UI. The `*.epaflix.com` zone is per-host records, not a wildcard, so this record is required; without it the name resolves to the Cloudflare-proxied public IPs (no tunnel exists -> unreachable).

## Component 3 — Pi `web_search` extension (workstation)

- Location: `~/.pi/extensions/pi-searxng/` (a small pi package dir: `package.json` + `extensions/web-search.ts`).
- Registers tool `web_search` via `pi.registerTool()`:
  - input `{ query: string, count?: number }`, default `count = 5`.
  - request `GET ${SEARXNG_URL}/search?q=<query>&format=json&language=en&safesearch=1`.
  - **Forces IPv4** (undici Agent `connect: { family: 4 }` / `autoSelectFamily: false`) so it uses the internal Traefik path. IPv6 AAAA for the host points at Cloudflare (no internal IPv6 LB exists), so IPv6 must be avoided.
  - parses `results[]`, returns top `count` as compact text lines: `title — url — snippet`.
  - `promptSnippet` (one-line "Available tools" entry) + `promptGuidelines` ("Use web_search when the user asks about current events or facts not in the codebase") so the local Qwen models reach for it. Guidelines name the tool explicitly (`web_search`, not "this tool").
  - on non-200 or empty results, returns a clear error string (no silent empty success).
- Config: `SEARXNG_URL` env var, default `https://searxng.epaflix.com`.
- TLS: the wildcard Let's Encrypt cert is valid for `searxng.epaflix.com`, so no cert-skipping.
- Install: `pi install ~/.pi/extensions/pi-searxng` (writes `~/.pi/agent/settings.json`).

## Out of scope (YAGNI)

- Authentik / OIDC SSO on SearXNG — Pi cannot run a browser OIDC flow; the service is internal-only (per-host internal A record, no Cloudflare tunnel), so it is not publicly reachable.
- Valkey/Redis + the SearXNG limiter — only needed for public/abused instances.
- HA / multiple replicas / persistent storage — SearXNG is stateless here.
- A `fetch_url` page-reader tool — built-in `bash`+`curl` covers reading a result URL for v1. Add later if Pi struggles with it (follow-up issue).

## Gates and guardrails

- **Cluster apply** is a deploy gate: work goes branch -> PR -> rebase onto `origin/main` -> merge (semi-linear, `gh pr merge --merge`) -> ArgoCD sync. Never merged automatically; stop for sign-off.
- **Pi-hole edit** is an infra change: confirm before applying.
- **`settings.json` write** on the workstation: a second Claude session was active in `/home/spy` during design; coordinate / confirm it is idle before writing, to avoid clobbering.
- **Secrets**: `SEARXNG_SECRET` only as SOPS `*.enc.yaml`; never plaintext (pre-commit hook enforces).
- **Follow-ups**: open a `gh issue` on `SpyrosPsarras/epaflix` for each deferred item (soak->prune flip, optional `fetch_url`) before closing the thread.

## Verification (run during implementation)

1. `kustomize build --enable-alpha-plugins --enable-exec 2-k3s/13.searxng` renders (with KSOPS).
2. After merge + sync: ArgoCD app `searxng` Synced/Healthy.
3. `curl --resolve searxng.epaflix.com:443:192.168.10.101 'https://searxng.epaflix.com/search?q=test&format=json'` returns JSON `results`.
4. From the workstation after the DNS record: `getent ahostsv4 searxng.epaflix.com` -> `192.168.10.101`.
5. `pi -p "search the web for the latest k3s release"` triggers `web_search` and returns results.
