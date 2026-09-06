# HANDOFF — T3 Code server on Epaflix (implementation session)

Working directory: `/home/spy/Documents/Epaflix/k3s-swarm-proxmox` (Spyros' infra-as-code repo).
Previous session: research + design interview, ALL decisions settled. This doc is the only artifact — deliberately no CONTEXT.md/ADRs/tickets exist. Do not create any.
This doc lives at `2-k3s/13.t3code/handoff.md` — the number 13 slot is deliberate: `13.odysseus` is being decommissioned this weekend and t3code replaces it.

## ⚠️ ROUTE — ENFORCE THIS ORDER (user's CRITICAL explicit instruction)

1. **`/implement`** — build the repo artifacts (Phase A below).
2. **`/wizard`** — inline, at each human-only wall, in build order (Phase B below). Do not front-load; wizard fires when the implement work actually hits that wall.
3. **`/code-review`** — gate before done/commit (also `review-gate` per global AGENTS.md: a second agent reviews the actual diff + command output).

The design interview is DONE (decisions locked below). Do NOT re-grill, do NOT re-open settled decisions, do NOT run wayfinder / to-spec / to-tickets. If a genuinely NEW decision arises that this doc doesn't cover, ask Spyros one minimal question directly and continue. No paper trail: no CONTEXT.md, no ADRs, no ticket files, no specs in the repo (this handoff doc is the one exception, placed here by Spyros' explicit request).

Always-in-force per global AGENTS.md: `unslop` (all prose), `ponytail` (lazy-minimal code), `review-gate` (before done).

## Locked decisions (interview Q1–Q9 — settled, do not re-open)

- **Q1 host**: NEW clean unprivileged LXC named `t3code` — Debian 13, 4 vCPU, 8 GB RAM, 64 GB disk, static IP in `192.168.10.x`, pihole DNS record. NOT the existing bastion (`192.168.10.43`); if anything on bastion is needed, replicate it fresh.
- **Q2 access**: HTTPS via Traefik at `https://t3code.epaflix.com` (existing wildcard cert) → enables hosted web app `app.t3.codes` AND desktop app over WireGuard. No cloudflared public route, no authentik fronting (T3 Code has its own pairing/session auth).
- **Q3 providers**: ALL providers through cliproxy (`https://cliproxy.epaflix.com`). No provider OAuth/logins on the guest. Client api-key = the EXISTING key in sops secret `cliproxy-secrets` (evidence: git-durable as `omp-api-key`). Never create new keys.
- **Q4 vault**: ONE vault only — Spyros' existing personal KeePassXC KDBX. No second agent vault.
- **Q5 GitHub**: `gh auth login` (device flow) on the guest as Spyros. No new PAT.
- **Q6 Azure**: `az login --use-device-code` on the guest as Spyros. No service principal.
- **Q7 backups**: Proxmox Backup Server (PBS) job covering the whole guest. No file-level cron.
- **Q8 name**: `t3code` → `t3code.epaflix.com`.
- **Q9 vault passphrase delivery**: sops on the cluster, injected into the keepass service (reboots self-heal unattended; trust-domain tradeoff accepted by user).

## Verified facts (evidence)

- **T3 Code** (https://github.com/pingdotgg/t3code, MIT): headless server = `t3 serve --host <ip>`; auth = one-time pairing token → device sessions; `t3 auth` manages/revokes; Linux background = systemd **user** unit + lingering; Node requirement `^22.16 || ^23.11 || >=24.10` (use 24.x). Key doc: `docs/user/remote-access.md` in the repo. Default port 3773.
- **Repo conventions**: numbered top dirs (`0-truenas`, `1-proxmox`, `2-k3s`, `3-docker-swarm`); sops+ksops secrets as `*.enc.yaml` (rules in root `.sops.yaml`); ArgoCD + Renovate + Reloader throughout; guest/VM stuff under `1-proxmox/` (has `pbs`, `pihole`, `ssh`, `user-vms` subdirs), cluster manifests under `2-k3s/<NN>.<name>/`.
- **Traefik**: two LoadBalancers — default + `internal` (192.168.10.102:8443, LAN-only). Wildcard cert `*.epaflix.com` via `certificatesresolvers.cloudflare` (Cloudflare DNS-01). Non-k8s-backend pattern to copy: `2-k3s/05.traefik-deployment/ingress/pegaprox-proxy.yaml` (headless Service + static-IP Endpoints + IngressRoute; `truenas-proxy.yaml` is a second example). cliproxy's IngressRoute uses the `internal` entrypoint — t3code must too (LAN/WG-only posture).
- **cliproxy**: CLIProxyAPI (`docker.io/eceasy/cli-proxy-api`) in namespace `remote-pi`, ClusterIP:8317, exposed LAN-only at `https://cliproxy.epaflix.com` via Traefik `internal` entrypoint. Anthropic-compatible at root, OpenAI-compatible at `/v1`. Client auth = api-keys from live config (config lives in Postgres, reconciled by `files/reconcile-config.psql`; keys in sops `2-k3s/17.remote-pi/cliproxy/cliproxy-secrets.enc.yaml` — encrypted, decrypt only during wizard with Spyros).
- **Provider/model names** Spyros uses through cliproxy (from `/home/spy/.config/zed/settings.json`): `claude-opus-5`, `claude-sonnet-5`, `claude-fable-5*` (anthropic-compat, root URL); `gpt-5.6-*` and `or-*` openrouter models (openai-compat, `/v1`). Zed's default agent model: `or-glm-5.3-flash`.
- **Security flag (F0, told to user)**: plaintext GitHub PAT at `/home/spy/.config/zed/settings.json` → `lsp.gh-actions-language-server.initialization_options.sessionToken`. Phase B optional step: rotate it on GitHub, store in KeePassXC, update settings.
- **Cluster access**: kubernetes MCP tools available with default context `epaflix` (pods verified running: argocd, traefik, authentik, cloudflared, cnpg postgres, syncthing, etc.).

## Phase A — repo artifacts (implement; top-to-bottom)

- **A1** `2-k3s/05.traefik-deployment/ingress/t3code-proxy.yaml` — copy pegaprox pattern: headless Service + Endpoints (guest IP, port 3773) + IngressRoute `Host(\`t3code.epaflix.com\`)` on `internal` entrypoint, TLS `certResolver: cloudflare` with wildcard domains block. Guest IP unknown until B1 — check how pegaprox handles its static IP and follow suit (placeholder is acceptable if that's the house way).
- **A2** `1-proxmox/` — guest provisioning artifact per whatever convention exists in that dir (inspect first): unprivileged Debian 13 LXC spec (hostname `t3code`, 4c/8G/64G, static IP, nesting off).
- **A3** `1-proxmox/pihole/` — DNS record `t3code.epaflix.com` → 192.168.10.102 (Traefik internal LB), matching how existing records are managed there.
- **A4** `1-proxmox/pbs/` — add guest to PBS backup job per existing convention.
- **A5** Guest provisioning script (runs on the LXC after B1): base packages; Node 24; T3 Code (`npx t3@latest` or pinned global); Claude Code CLI; `gh`; `az cli`; `syncthing`; workspace layout (`~/projects`); systemd **user** unit `t3code.service` running `t3 serve --host <guest-ip>` with lingering enabled; env file (root-only) with `ANTHROPIC_BASE_URL=https://cliproxy.epaflix.com` + `ANTHROPIC_AUTH_TOKEN=<existing cliproxy client key>`; keepass MCP server registered in Claude Code config pointing at the Syncthing-synced KDBX, passphrase sourced from the sops secret (A6).
- **A6** sops secret (new `*.enc.yaml`) holding the vault master passphrase; delivery to guest = decrypt at provision time into root-only env file (simplest consistent with repo; verify against `.sops.yaml` creation rules).
- Validate manifests with the repo's own patterns (kubectl dry-run / argocd conventions) — not unit tests; infra config.
- Implementation artifacts that belong to the t3code build may live in `2-k3s/13.t3code/` alongside this handoff (namespace manifests, kustomization, secrets scaffolding) — follow how `13.odysseus` structures its dir as the reference.

## Phase B — wizard (human-only walls, fire IN THIS ORDER as implement reaches them)

- **B1** Create LXC on Proxmox + static IP (user runs the A2 artifact or clicks the Proxmox UI).
- **B2** sops: decrypt `cliproxy-secrets` → extract client api-key into the guest env file; encrypt vault passphrase into the A6 secret. Requires Spyros' sops key.
- **B3** On guest: `gh auth login` (device flow), `az login --use-device-code`, enable lingering + start `t3code.service`.
- **B4** Syncthing: share the guest `secrets-vault` folder with the k3s Syncthing hub (2-k3s/15.syncthing, device H4I72HH, 192.168.10.101:22000), the same hub every other machine syncs through. Done 2026-09-06.
- **B5** Client pairing from Spyros' PC over WireGuard: desktop app or `https://app.t3.codes` pair against `https://t3code.epaflix.com` with the one-time token from the server.
- **B6** Optional (recommended): rotate the F0 PAT, move it into KeePassXC, update Zed settings.

## Phase C — validation

1. From a paired client: thread that runs a shell command on the guest, reads one KeePassXC entry via MCP, clones + pushes a repo via `gh`, and runs `az account show`.
2. Reboot guest → `t3code.service` and vault unlock must come back unattended (Q9a).
3. Run `/code-review` + review-gate. Only then done.

## Do NOT

- Do not touch `2-k3s/13.odysseus` — Spyros decommissions it himself this weekend. t3code replaces it; the shared slot number is intentional.
- No SSH from agents to Spyros' PCs; no cloudflared exposure for t3code; no new PATs/keys/vaults; no authentik in front of t3code.
- Never print or commit secret values; refer to them by sops key name only.
