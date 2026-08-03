# Odysseus TrueNAS GPU deploy — process

Deploy the **Odysseus** self-hosted AI workspace (`github.com/pewdiepie-archdaemon/odysseus`) on the
Epaflix **TrueNAS** host (`192.168.10.200`) as a **Custom App**, with the **NVIDIA RTX 2070 SUPER (8 GB)**
passed through for **local model serving**, then front `odysseus.epaflix.com` with **Authentik SSO**
(Traefik forward-auth) + a Cloudflare DNS-only record.

## Confirmed facts (interview + live probe, 2026-06-06)
- TrueNAS 25.10.0.1, Docker 28.3.1; RTX 2070 SUPER, driver 570.172.08 / CUDA 12.8, modules loaded, GPU idle.
- Odysseus: FastAPI + JS, **builds from a Dockerfile** (no published image). Base compose = odysseus (:7000),
  chromadb, searxng, ntfy. GPU overlay attaches the GPU to the **odysseus** service via
  `deploy.resources.reservations.devices` (driver nvidia, count all, caps [gpu]) + `NVIDIA_VISIBLE_DEVICES`.
- Owner choices: **local models on the GPU**, deploy as a **TrueNAS Custom App**, **Authentik SSO** in front.

## Phases
1. **analyze** (read-only) — Odysseus repo (compose/Dockerfile/env/GPU/AGPL/admin bootstrap + latest SHA),
   a security review of risky mounts/agent script host, the TrueNAS host (nvidia runtime/CDI, Custom-App
   build path, datasets/space), local-serving options for 8 GB, and the existing TrueNAS→Traefik proxy +
   forward-auth + Cloudflare patterns.
2. **plan** — concrete compose/app spec (GPU block, placeholders for secrets, pinned SHA), the local model
   to serve, secrets handling, the auth model (one login, never ungated), the GitOps exposure change set,
   the Authentik objects, order, test plan, risks, rollback. **[Plan gate]**
3. **author-artifacts** — codified compose (placeholders) + k3s Traefik/forward-auth manifests + runbook on
   a branch; validate (`kustomize build`, YAML parse, SOPS guard); local commit, no push.
4. **deploy-truenas** (live) — dataset + on-host secrets + pinned build + Custom App create + serve model on
   the GPU. **[Deploy gate before this]**
5. **verify-deploy** — `nvidia-smi` inside the container, UI reachable, a completion that shows GPU use.
   *(conditional anomaly gate)*
6. **expose** (live) — Authentik Proxy Provider + Application + group binding + outpost append; push + PR +
   rebase + `validate` + `gh pr merge --merge`; Cloudflare DNS-only A record. **[Expose gate before this]**
7. **verify-sso** — `curl` shows a 302 to the Authentik flow, outpost route handled, no double login.
   *(conditional anomaly gate)*
8. **closeout** — tick the PR test plan in the PR body (never a new comment), open follow-up `gh` issues,
   log to `.history`.

## Guardrails honored
- Epaflix merge-commit + mandatory-rebase policy; PR with a `## Test plan`; merge via `--merge`.
- Never commit secrets; placeholders in git, real values on TrueNAS / `secrets.yml`; SOPS `*.enc.yaml`.
- Pin the third-party image to a reviewed commit SHA (no floating `main`).
- Open a `gh issue` for every follow-up.
- Authentik objects created **before** the Traefik middleware goes live.

## Agent / model
All reasoning tasks: `general-purpose` agent on `claude-opus-4-8`. Validation is a `shell` task.
