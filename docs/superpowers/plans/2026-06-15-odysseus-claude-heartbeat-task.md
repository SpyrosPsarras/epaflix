# Odysseus Claude subscription heartbeat — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run `claude -p "just reply pong"` 3×/day (04:00/09:00/14:00 Europe/Oslo) from inside the Odysseus pod via a built-in `run_local` Task, to keep the Claude Code subscription OAuth token warm.

**Architecture:** An `install-claude` initContainer reinstalls the Claude CLI fresh on every pod start (native installer → shared emptyDir on PATH). The subscription token is injected as `CLAUDE_CODE_OAUTH_TOKEN` from the existing SOPS `odysseus-secrets`. The schedule itself is an Odysseus Task (`run_local`, cron, tz=Europe/Oslo) created in the Tasks UI — System actions are admin-only and not on the `/api/codex` token.

**Tech Stack:** Kustomize + ArgoCD (selfHeal) + ksops/SOPS+age; Odysseus app (`python:3.12-slim`, glibc, already ships `curl`); Claude Code native installer.

**Spec:** `docs/superpowers/specs/2026-06-15-odysseus-claude-heartbeat-task-design.md`

**Refinements since spec (resolved during planning):**
- Image is `python:3.12-slim` (glibc) and **already has `curl`** → initContainer reuses the **odysseus image itself** (no debian-slim, no second image to pull, glibc-compatible binary).
- `/app` is root-owned (entrypoint only chowns `/app/data`+`/app/logs` to 1000) → set `CLAUDE_CONFIG_DIR=/app/data/.claude` (writable, on the data PVC).

---

### Task 1: Mint and encrypt the subscription token

**Files:**
- Modify: `2-k3s/13.odysseus/odysseus-secrets.enc.yaml` (add one SOPS-encrypted key)

**Pre-req (owner action, local machine, NOT committed):**
Mint a long-lived token from an active Pro/Max login:
```bash
claude setup-token   # opens browser; copy the printed token (starts sk-ant-oat...)
```

- [ ] **Step 1: Confirm the age key + sops are available**

Run: `which sops && cat .sops.yaml | head -5`
Expected: sops path printed, and `.sops.yaml` shows the cluster age recipient (the `creation_rules`). If `sops` is missing, install per `.github/instructions/sops.instructions.md`.

- [ ] **Step 2: Decrypt-edit the secret and add the key**

Run: `sops 2-k3s/13.odysseus/odysseus-secrets.enc.yaml`
In the editor, under `stringData:` add a new line alongside the existing keys (`ODYSSEUS_ADMIN_PASSWORD`, `SEARXNG_SECRET`, `OPENAI_API_KEY`, `HF_TOKEN`, `ODYSSEUS_BASTION_SSH_KEY`):
```yaml
  CLAUDE_CODE_OAUTH_TOKEN: "<PASTE_THE_sk-ant-oat_TOKEN>"
```
Save and exit — sops re-encrypts on write.

- [ ] **Step 3: Verify the key is present and still encrypted**

Run: `sops -d 2-k3s/13.odysseus/odysseus-secrets.enc.yaml | grep -c CLAUDE_CODE_OAUTH_TOKEN`
Expected: `1`

Run: `grep -c "sk-ant-oat" 2-k3s/13.odysseus/odysseus-secrets.enc.yaml`
Expected: `0` (the plaintext token must NOT appear — only the SOPS ciphertext/`enc` blob).

- [ ] **Step 4: Verify the pre-commit guard accepts it**

Run: `./.github/hooks/check-sops-encrypted.sh 2-k3s/13.odysseus/odysseus-secrets.enc.yaml || echo "HOOK FAILED"`
Expected: no `HOOK FAILED` (file is recognized as encrypted). If the hook script takes no args, run `git add` then `git commit` is deferred to Task 3 — do NOT commit yet.

---

### Task 2: Wire the CLI into the odysseus Deployment

**Files:**
- Modify: `2-k3s/13.odysseus/odysseus.yaml` (add volume, initContainer, container mount + env)

- [ ] **Step 1: Add the shared `claude-bin` volume**

In `2-k3s/13.odysseus/odysseus.yaml`, under `spec.template.spec.volumes:` (the list that ends with `ssh-home`), append:
```yaml
        # Claude CLI is (re)installed fresh on every pod start by the
        # install-claude initContainer into this emptyDir; the main container
        # runs it from /opt/claude/.local/bin. Ephemeral on purpose — always
        # pulls the latest CLI, no prebuilt image to maintain.
        - name: claude-bin
          emptyDir: {}
```

- [ ] **Step 2: Add the `install-claude` initContainer**

In the same file, under `spec.template.spec.initContainers:` after the `seed-ssh` initContainer block, append:
```yaml
        # Install the Claude Code CLI via the official native installer on every
        # pod start (no prebuilt image; always latest). Reuses the odysseus
        # image (already ships curl + glibc) so there is no second image to pull.
        # The standalone binary lands at /opt/claude/.local/bin/claude in the
        # shared claude-bin emptyDir; world-executable so the uid-1000 app can
        # run it. See docs/superpowers/specs/2026-06-15-odysseus-claude-heartbeat-task-design.md
        - name: install-claude
          image: ghcr.io/spyrospsarras/odysseus:73673258
          securityContext:
            runAsUser: 0
          env:
            - name: HOME
              value: /opt/claude
          command:
            - sh
            - -c
            - |
              set -e
              curl -fsSL https://claude.ai/install.sh | bash
              chmod -R a+rX /opt/claude
              /opt/claude/.local/bin/claude --version
          resources:
            requests:
              memory: "128Mi"
              cpu: "100m"
            limits:
              memory: "512Mi"
              cpu: "1000m"
          volumeMounts:
            - name: claude-bin
              mountPath: /opt/claude
```

- [ ] **Step 3: Mount `claude-bin` and add env on the odysseus container**

In the `containers:` → `name: odysseus` block, add to its `volumeMounts:` (after the `ssh-home` mount):
```yaml
            # Claude CLI installed by the install-claude initContainer.
            - name: claude-bin
              mountPath: /opt/claude
              readOnly: true
```
And add to that container's `env:` list (after the `HF_TOKEN` block):
```yaml
            # Claude subscription heartbeat (run_local Task). Token is the
            # long-lived OAuth token from `claude setup-token`.
            - name: CLAUDE_CODE_OAUTH_TOKEN
              valueFrom:
                secretKeyRef:
                  name: odysseus-secrets
                  key: CLAUDE_CODE_OAUTH_TOKEN
            # Put the CLI on PATH for the run_local Task shell.
            - name: PATH
              value: "/opt/claude/.local/bin:/usr/local/bin:/usr/bin:/bin"
            # /app is root-owned; point claude config at the writable data PVC.
            - name: CLAUDE_CONFIG_DIR
              value: "/app/data/.claude"
            # Reinstalled each pod start; no in-place self-update needed.
            - name: DISABLE_AUTOUPDATER
              value: "1"
```

> NOTE: setting an explicit `PATH` overrides the image default. The value above
> matches the Debian default plus the claude dir. If the app relies on extra PATH
> entries (e.g. for `npx`/`ssh`), confirm `/usr/local/bin:/usr/bin:/bin` covers
> them (it does for `nodejs`/`npm`/`git`/`ssh`, all in `/usr/bin`).

- [ ] **Step 4: Render the kustomization to validate structure**

Run: `kustomize build 2-k3s/13.odysseus --enable-alpha-plugins --enable-exec 2>&1 | head -40`
Expected: YAML renders without error; the `odysseus` Deployment shows the new `install-claude` initContainer, the `claude-bin` volume, and the four new env entries. (This invokes the ksops exec plugin to decrypt; requires the local age key. If ksops is not installed locally, skip and rely on the `validate` CI gate in Task 3.)

- [ ] **Step 5: Sanity-check the rendered Deployment with a dry-run**

Run: `kustomize build 2-k3s/13.odysseus --enable-alpha-plugins --enable-exec | kubectl --context epaflix apply --dry-run=client -f - 2>&1 | grep -i "odysseus\|error"`
Expected: `deployment.apps/odysseus configured (dry run)` (or `created`), no errors.

---

### Task 3: Commit, PR, merge (Epaflix semi-linear policy)

**Files:** none new — commits Task 1 + Task 2 changes.

- [ ] **Step 1: Review the diff**

Run: `git diff --stat && git diff 2-k3s/13.odysseus/odysseus.yaml`
Expected: only `odysseus.yaml` (and the binary-diff `odysseus-secrets.enc.yaml`) changed; no plaintext token anywhere.

- [ ] **Step 2: Commit**

```bash
git add 2-k3s/13.odysseus/odysseus.yaml 2-k3s/13.odysseus/odysseus-secrets.enc.yaml
git commit -F - <<'EOF'
feat(odysseus): install Claude CLI + subscription token for heartbeat Task

Adds an install-claude initContainer (native installer, fresh each pod
start, reuses the odysseus image) that drops the claude binary into a
shared emptyDir on PATH, and injects CLAUDE_CODE_OAUTH_TOKEN from the
SOPS odysseus-secrets. Enables a run_local Task (created in the Odysseus
UI) to run `claude -p "just reply pong"` 3x/day, keeping the subscription
token warm. CLAUDE_CONFIG_DIR points at the writable /app/data.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
```

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin odysseus-claude-heartbeat-task
gh pr create --repo SpyrosPsarras/epaflix --base main \
  --title "feat(odysseus): Claude subscription heartbeat (install CLI + token)" \
  --body "$(cat <<'EOF'
## Summary
Keeps the Claude Code subscription OAuth token warm via an Odysseus
`run_local` Task running `claude -p "just reply pong"` 3x/day
(04:00/09:00/14:00 Europe/Oslo).

This PR is the GitOps half: install the CLI fresh each pod start
(initContainer, native installer) + inject `CLAUDE_CODE_OAUTH_TOKEN`
from SOPS `odysseus-secrets`. The Task itself is created in the Odysseus
Tasks UI (System actions are admin-only, not on the `/api/codex` token).

Spec: `docs/superpowers/specs/2026-06-15-odysseus-claude-heartbeat-task-design.md`

## Test plan
- [ ] `validate` CI green (kustomize build renders)
- [ ] After merge: ArgoCD `odysseus` Synced/Healthy
- [ ] `install-claude` initContainer logs show `claude --version`
- [ ] Odysseus Task created (`run_local`, cron `0 4,9,14 * * *`, tz Europe/Oslo)
- [ ] Manual "Run" of the Task returns `pong`, exit 0

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Rebase onto origin/main and merge**

Run:
```bash
git fetch origin && git rebase origin/main && git push --force-with-lease
```
Wait for the `validate` check to pass, then:
```bash
gh pr merge --repo SpyrosPsarras/epaflix --merge
```
Expected: `Merge pull request #N` marker on main.

---

### Task 4: Verify the deploy (post-merge, ArgoCD selfHeal auto-applies)

**Files:** none — live verification.

- [ ] **Step 1: Confirm ArgoCD synced the change**

Run: `kubectl --context epaflix -n argocd get application odysseus -o jsonpath='{.status.sync.status} {.status.health.status}{"\n"}'`
Expected: `Synced Healthy` (may take a minute; selfHeal is automatic, no manual sync).

- [ ] **Step 2: Confirm the pod rolled with the initContainer**

Run: `kubectl --context epaflix -n odysseus get pod -l app=odysseus -o jsonpath='{.items[0].spec.initContainers[*].name}{"\n"}'`
Expected: includes `install-claude` (alongside `seed-data`, `seed-ssh`).

- [ ] **Step 3: Confirm the CLI installed cleanly**

Run: `kubectl --context epaflix -n odysseus logs -l app=odysseus -c install-claude --tail=20`
Expected: installer output ending with a version line like `2.x.x (Claude Code)`. (`kubectl exec` is blocked here by the auto-classifier — rely on initContainer logs, not exec.)

---

### Task 5: Create and verify the Odysseus Task (owner, Tasks UI)

**Files:** none — runtime state in `app.db`, created via UI (System actions are admin-only and not exposed on the `/api/codex` token).

- [ ] **Step 1: Add the Task**

In Odysseus → **Tasks → Add**, set:
- **Action:** `run_local`
- **Prompt / script:** `export PATH=/opt/claude/.local/bin:$PATH; claude -p "just reply pong"`
- **Schedule:** cron `0 4,9,14 * * *`
- **Timezone:** `Europe/Oslo`

Save.

- [ ] **Step 2: Trigger a manual run**

Click **RUN** on the new task.

- [ ] **Step 3: Verify the result**

Open the task's dedicated session / **Activity** tab.
Expected: output contains `pong`, run marked success (exit 0). If it errors:
- "command not found: claude" → PATH/initContainer issue (re-check Task 4 Step 2/3).
- auth/login error → token not minted from an active Pro/Max plan, or wrong key (re-check Task 1).
- a write/permission error mentioning `~/.claude` → `CLAUDE_CONFIG_DIR` not applied (re-check Task 2 Step 3); the run may need `--dangerously-skip-permissions` only if a first-run prompt blocks (it should not for `-p`).

- [ ] **Step 4: Confirm the schedule shows the next fire time**

In the Tasks list, the task shows `Cron: 0 4,9,14 * * *` and a next-run timestamp in Oslo wall-clock (04:00, 09:00, or 14:00).

---

### Task 6: Open follow-up issues (repo policy: an issue for every follow-up)

- [ ] **Step 1: Notification-reaches-owner verification**

```bash
gh issue create --repo SpyrosPsarras/epaflix \
  --title "Verify Odysseus task-failure notification actually reaches the owner" \
  --body "$(cat <<'EOF'
## Finding
The Claude heartbeat relies on Odysseus's built-in task-failure
notification (ntfy/browser/email) to surface a dead token.

## Current state
Notification channel for failed Tasks is assumed but unverified end-to-end.

## Desired outcome
Force a heartbeat Task failure (e.g. temporarily break the token) and
confirm the owner is actually notified through the configured channel.

## Notes
Relates to the Claude subscription heartbeat (spec
docs/superpowers/specs/2026-06-15-odysseus-claude-heartbeat-task-design.md).
EOF
)"
```

- [ ] **Step 2: (Optional) Pin a minimum Claude CLI version**

Only if reproducibility becomes a concern (install-on-restart always pulls latest):
```bash
gh issue create --repo SpyrosPsarras/epaflix \
  --title "Optionally pin Claude CLI version in odysseus install-claude initContainer" \
  --body "$(cat <<'EOF'
## Finding
install-claude installs the latest CLI on every pod start (by design, to
avoid a prebuilt image). A bad upstream release could break the heartbeat
silently on next restart.

## Desired outcome
If desired, pin via `curl -fsSL https://claude.ai/install.sh | bash -s X.Y.Z`
and let Renovate bump it, trading always-latest for reproducibility.

## Notes
Spec: docs/superpowers/specs/2026-06-15-odysseus-claude-heartbeat-task-design.md
EOF
)"
```

---

## Self-review

- **Spec coverage:** initContainer (Task 2.2) ✓; reinstall-each-start (emptyDir, Task 2.1) ✓; token via SOPS (Task 1) + env (Task 2.3) ✓; `run_local` Task with cron+tz (Task 5) ✓; failure visibility via Odysseus channel (Task 6.1 verifies) ✓; libc risk resolved (glibc image, noted in header) ✓; HOME writability resolved (`CLAUDE_CONFIG_DIR`, Task 2.3) ✓.
- **Placeholders:** none — every YAML block and command is concrete. The token value is the one intentional `<PASTE...>` (a secret the owner supplies; never committed).
- **Consistency:** volume `claude-bin`, mount `/opt/claude`, binary `/opt/claude/.local/bin/claude`, and `PATH` all agree across Tasks 2 and 5.
