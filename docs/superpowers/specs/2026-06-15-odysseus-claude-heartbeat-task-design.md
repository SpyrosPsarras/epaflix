# Odysseus Claude subscription heartbeat — design

**Date:** 2026-06-15
**Status:** Approved (Design 1)
**Tier:** `2-k3s/13.odysseus/`

## Purpose

Keep the Claude Code **subscription OAuth token warm** by exercising it on a
fixed daily cadence, and catch breakage early. A scheduled Odysseus Task runs
`claude -p "just reply pong"` **3×/day at 04:00, 09:00, 14:00 Europe/Oslo**.
Success is silent; a failed run surfaces through Odysseus's own task
Activity/notification channel.

This is purpose **A** (token warm-keeping), not a health probe of the wider
stack and not scaffolding for larger automations.

## Why Odysseus Tasks (not a standalone CronJob)

Odysseus has a built-in **Tasks** scheduler (cron-style, per-task timezone).
For **System** actions (`run_local` / `run_script` / `ssh_command`) the task's
*prompt field is passed verbatim as the shell command*, executed via
`subprocess(shell=True)` with a **300s timeout**. This is the native, intended
mechanism for "Odysseus runs a command on a schedule", so we use it instead of
introducing a parallel Kubernetes `CronJob`.

### Execution target: the Odysseus pod (`run_local`)

`run_local` runs the command **inside the odysseus pod** (uid 1000, `HOME=/app`).
Chosen over the bastion VM (`ssh_command` → 192.168.10.43) because:

- The requirement is "subscription **on the pod**, claude installed **on every
  restart of the pod**" — ephemeral-pod semantics, not a persistent VM.
- The bastion VM lives outside this GitOps repo (TrueNAS app), so claude + token
  there would be un-versioned VM config.

## Architecture

```
Odysseus Tasks scheduler (croniter, tz=Europe/Oslo)
        │  cron "0 4,9,14 * * *"
        ▼
action=run_local  →  subprocess(shell=True, timeout=300s)  [inside odysseus pod, uid 1000]
        │  script: export PATH=/opt/claude/.local/bin:$PATH; claude -p "just reply pong"
        ▼
claude CLI (native binary, installed fresh each pod start by initContainer)
        │  auth: CLAUDE_CODE_OAUTH_TOKEN (pod env, from SOPS secret)
        ▼
api.anthropic.com  →  "pong"
```

## Components

### 1. `install-claude` initContainer (`odysseus.yaml`)

- Base image: **the odysseus image itself** (`ghcr.io/spyrospsarras/odysseus:73673258`).
  Confirmed `python:3.12-slim` (Debian, **glibc**) and it **already ships `curl`** — so
  there is no second image to pull (already cached on the node) and the installed glibc
  binary is guaranteed compatible with the main container.
- Command: `curl -fsSL https://claude.ai/install.sh | bash` with `HOME=/opt/claude`,
  so the **native installer** (the documented recommended path) drops a standalone
  binary at `/opt/claude/.local/bin/claude`. No Node, no npm.
- Runs on **every pod start** → always-latest claude, no prebuilt image to maintain
  (satisfies the "don't miss updates / no prebuilt image" requirement).
- Mirrors the existing `seed-data` / `seed-ssh` initContainer pattern in the same file.
- Writes into a shared `emptyDir` volume `claude-bin` mounted at `/opt/claude`.

### 2. odysseus container changes (`odysseus.yaml`)

- Mount the `claude-bin` emptyDir at `/opt/claude` (readOnly).
- Prepend `/opt/claude/.local/bin` to `PATH` (env), plus the task script sets it
  explicitly (belt-and-suspenders).
- `CLAUDE_CONFIG_DIR=/app/data/.claude` — `/app` is root-owned (entrypoint only chowns
  `/app/data`+`/app/logs` to uid 1000), so claude's config/credentials cache lives on
  the writable data PVC, not the default `~/.claude` under `/app`.
- `DISABLE_AUTOUPDATER=1` (reinstalled each start; no in-place self-update needed).
- New env `CLAUDE_CODE_OAUTH_TOKEN` from the odysseus secret (below).

### 3. Subscription token (`claude-oauth-token.enc.yaml`)

- **DEVIATION FROM ORIGINAL PLAN (option B):** the token lives in a **standalone
  SOPS Secret `claude-oauth-token`** (`claude-oauth-token.enc.yaml`), added to the
  ksops generator `files:` list, with the deployment `secretKeyRef` pointing at it.
  Reason: the cluster age **private key was not present on the implementation
  machine**, so editing the existing `odysseus-secrets.enc.yaml` (which requires
  decrypt → re-encrypt) was impossible. Encrypting a *new* file needs only the
  **public** recipient (`age1586…` from `.sops.yaml`), so a standalone file
  unblocked the work and is fully reproducible.
- Token is minted once locally by the owner: `claude setup-token` (needs an active
  Pro/Max login). Never committed in plaintext; the SOPS+age pre-commit guard
  enforces this.

### 4. The Task (runtime, created in the Odysseus Tasks UI by the admin)

Not a manifest — Task definitions live in `app.db` (runtime state) and System
actions are admin-only **and not exposed on the `/api/codex/*` integration token**
(verified: `/api/codex/tasks` → 404). So the owner creates it in **Tasks → Add**:

| Field | Value |
|---|---|
| Action | `run_local` |
| Prompt / script | `export PATH=/opt/claude/.local/bin:$PATH; claude -p "just reply pong"` |
| Schedule | cron `0 4,9,14 * * *` |
| Timezone | `Europe/Oslo` |

## Failure visibility

`run_local` task results land in a dedicated Odysseus session and the task
Activity tab; a non-zero exit is surfaced via Odysseus's configured task
notification channel (ntfy/browser/email per Odysseus settings). We reuse this
built-in channel rather than adding a Prometheus `kube_job_failed` rule (there is
no Kubernetes Job to scrape — the run is in-process inside the long-lived pod).

## Risks / verification

- **libc compatibility — RESOLVED:** the odysseus image is `python:3.12-slim`
  (Debian, glibc), confirmed from the upstream Dockerfile, so the claude native glibc
  binary runs. No musl build needed. (Bonus: the image already ships `curl`, so the
  initContainer reuses the odysseus image — see component 1.)
- **`HOME=/app` writability — RESOLVED:** `/app` is root-owned (the entrypoint only
  chowns `/app/data`+`/app/logs` to uid 1000), so the default `~/.claude` would fail to
  write. `CLAUDE_CONFIG_DIR=/app/data/.claude` (writable, on the data PVC) handles it.
- **Network egress:** pod must reach `claude.ai` (install) + `api.anthropic.com`
  (inference). The pod already has internet egress (Ollama, GHCR).
- **300s timeout:** a `-p "pong"` round-trip is well under 300s; the first run also
  includes init install time, but install happens in the initContainer, not the task.

## Follow-ups (open as GitHub issues per repo policy)

- Harden the install path / pin a claude minimum version if desired.
- Confirm Odysseus task-notification channel actually reaches the owner (so a
  failed heartbeat is seen, not just logged).

## Out of scope

- The bastion VM execution path (Design 2).
- A standalone Kubernetes `CronJob` + Prometheus alert (earlier discarded design).
- OIDC/provider wiring of Claude as an Odysseus model (Odysseus talks
  OpenAI-compatible + Ollama; a subscription is not an API key).
