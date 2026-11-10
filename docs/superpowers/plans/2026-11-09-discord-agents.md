# Discord agents implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Family members chat with two locked Discord agents: `#media` requests, adds and deletes media for `Servarr users`; `#windmill` opens auto-merging PRs to the Windmill workspace for `Screener users`.

**Architecture:** Argo app `discord` in `2-k3s/35.discord/` deploys three namespaces. `discord-media` and `discord-windmill` each run one pinned OpenClaw Gateway with its own Discord bot, CLIProxy key, Secrets and default-deny NetworkPolicy, and no ServiceAccount token. `discord-sync` runs a CronJob that copies Authentik group membership to Discord roles. GitHub rules and an inline CI job limit the Windmill bot's PRs. A post-merge workflow on the in-cluster runner pushes Windmill scripts.

**Tech stack:** OpenClaw `2026.11.2` (`ghcr.io/openclaw/openclaw`), Kustomize, KSOPS/SOPS, Argo CD, CLIProxy management API, Authentik API v3, Discord REST API v10, GitHub Apps and rulesets, Windmill CLI (`wmill`), Loki ruler, Python 3 stdlib, Node.js stdlib.

**Spec:** `docs/superpowers/specs/2026-11-09-discord-agents-design.md`. Research: `docs/research/2026-11-09-discord-agent.md`.

## Global constraints

- The repo is public. No Discord guild, channel, role or user IDs, GitHub App or installation IDs, vault item names or account names in tracked plaintext. They go in SOPS files and reach OpenClaw through env vars.
- Secrets: create or fetch them inside the command that writes the SOPS file (`vault run ... -- sh -c '... sops set ...'`). Never print a value. List every `.enc.yaml` in the directory's `ksops-generator.yaml`.
- Base image `ghcr.io/openclaw/openclaw:2026.11.2@sha256:55294524dac01370288fb4beb16714b60f75cec0e96e07e662054cf29900965a`. Both Gateways run our derived image `ghcr.io/spyrospsarras/openclaw-discord` (Task 2c), pinned by digest. Never `latest`.
- Every OpenClaw Deployment: `replicas: 1`, `strategy: Recreate`, `automountServiceAccountToken: false`, PVC on `truenas-iscsi`, env `OPENCLAW_CONFIG_READONLY=1`, sandbox off, only the `discord` and `searxng` plugins (`plugins.allow: ["discord", "searxng"]`), DMs off, Control UI off, a sticky (`1777`) `emptyDir` on `/tmp` as on OpenClaw `main`.
- Config keys are those in `docs/research/2026-11-09-discord-agent.md` section 5, S3. Guild and channel IDs are object keys, which OpenClaw does not substitute: the init container (Task 2c image, Secret env, `OPENCLAW_CONFIG_READONLY=1`) renders `__DISCORD_GUILD_ID__` and `__DISCORD_CHANNEL_ID__` from env with `node` and fails if any placeholder or missing variable remains. Other secrets stay `${VAR}`. The init container then runs `openclaw config validate --json` and fails on `valid: false`.
- Models: provider `cliproxy`, `api: "anthropic-messages"`, base URL `http://cliproxy.remote-pi.svc.cluster.local:8317` (no `/v1`). Media `cliproxy/claude/claude-haiku-5-5`, windmill `cliproxy/claude/claude-opus-5-5` (verified in Task 1 S1).
- Egress per namespace, nothing else: DNS; CLIProxy `remote-pi:8317`; SearXNG `searxng:8080`; public internet on 443 for Discord (and GitHub for windmill). Media adds `servarr` pods: seerr 5055, radarr 7878, sonarr 8989, lidarr 8686, bazarr 6767, jellyfin 8096. Public-internet rule copies the `except` list of `2-k3s/34.windmill/network-policy.yaml`.
- Windmill bot path allowlist: `2-k3s/34.windmill/workspace/`. Bot branches: `bots/windmill/`.
- Live changes (CLIProxy keys, GitHub settings, merges, Argo syncs, app settings) happen only after Spyros confirms. Argo sync uses the dry-run then real `operation` patch from the homelab SSH guide.
- Synthetic data only in tests.

## Review focus

1. A media API key appears in a Discord reply, OpenClaw log or CLIProxy transcript. Expected: never. The agent uses `$RADARR_API_KEY`-style references in commands; the existing `CredentialRedactedFromLogs` alert stays quiet after the Task 7 acceptance run. Pinned in Task 3 Step 2 and Task 7 Step 6.
2. A delete request matches more than one item ("delete Dune"). Expected: the agent lists the matches with year and asks which one; it deletes nothing until the reply names one. Pinned in Task 3 Step 2 and Task 7 Step 6.
3. A Discord member keeps a mapped role after leaving the Authentik group, or has a role but no linked account. Expected: the next sync removes it. Pinned in Task 2 Step 1.
4. A bot PR renames a file from outside the allowlist into it. Expected: the path check fails, because it checks the old name too. Pinned in Task 5 Step 1.
5. `main` moves while a bot PR waits for checks (required checks are strict). Expected: the agent rebases its branch and force-pushes; it never merges `main` into it. Pinned in Task 4 Step 2 and Task 7 Step 7.

---

### Task 0: Owner steps (Spyros, in parallel with Tasks 1 to 6)

No code. Send Spyros this list once, in chat. Each item ends with where the result goes. He stores secrets in the vault himself and tells the agent the item names in chat.

- [x] **Step 1: Discord server.** Created. The controller reads the server ID from the invite.
- [ ] **Step 2: Three Discord applications.** `media` and `windmill`: bot with Message Content Intent, invited with View Channel, Send Messages, Read Message History, Create Public Threads, Send Messages in Threads, Add Reactions. `role-sync`: bot with Server Members Intent, invited with Manage Roles and Manage Channels. Store the three bot tokens, and the `role-sync` OAuth2 client ID and secret, in the vault. The controller creates the roles and channels (Task 7 Step 0).
- [ ] **Step 3: GitHub App.** Create `homelab-windmill-bot` under the personal account. No webhook. Repository permissions: Contents read and write, Pull requests read and write, Checks read, Commit statuses read, Actions read, Metadata read. Nothing else, in particular no Workflows and no Administration. Install on epaflix only. Generate a private key and store it in the vault. Send the App ID and installation ID in chat.
- [ ] **Step 4: Authentik.** After Task 5b is live, link his own Discord account in Authentik. (Spyros approved the controller creating the source, service account and token.)
- [x] **Step 5: Media keys.** Jellyfin key `discord-media` created by the controller with Spyros's approval. Navidrome dropped: no admin access, and Lidarr covers music.
- [ ] **Step 6: Windmill.** In workspace `screener`, create a token for a dedicated admin user `ci-deploy`, and store it as GitHub Actions secret `WINDMILL_TOKEN` on epaflix.

---

### Task 1: Spike OpenClaw against CLIProxy, and create the CLIProxy keys

**Files:**
- Modify: `docs/research/2026-11-09-discord-agent.md` (append section `## 5. Spike results`)

**Interfaces:**
- Produces: CLIProxy client keys `discord-media` and `discord-windmill`, stored as key `CLIPROXY_API_KEY` in `2-k3s/35.discord/media/secrets.enc.yaml` and `2-k3s/35.discord/windmill/secrets.enc.yaml`. Spike answers S1 to S5 below, used by Tasks 3 and 4.

- [ ] **Step 1: Create the two CLIProxy client keys.** After Spyros confirms, generate each key inside the command (`secrets.token_urlsafe(32)`) and add it through `PUT /v0/management/api-keys`, reading the current list first and keeping every existing entry, as `2-k3s/13.t3code-work/tools/provision.py` does. Write the same value into the SOPS file in the same command.
- [ ] **Step 2: Run OpenClaw 2026.11.2 locally in this pod** with `npx openclaw@2026.11.2`, a scratch `OPENCLAW_HOME` under `mktemp -d`, a `models.providers.cliproxy` entry (`baseUrl` as above, `api: "anthropic-messages"`) and the media key from `vault run`/`sops -d --output` into a private temp file. Record:
  - S1. One `openclaw infer` (or one agent turn) through CLIProxy succeeds with each model ID. Expected: a text reply.
  - S2. `${VAR}` env substitution works in `openclaw.json` for a Discord channel ID. Expected: the resolved value shows in `openclaw config get`. If not supported, Tasks 3 and 4 render the file in the init container with `envsubst`.
  - S3. The exact config keys for: Discord guild/channel allowlist with role IDs, DMs off, `requireMention: false`, role and moderation actions off; sandbox off; SearXNG plugin base URL; `logging.audit.enabled`, `logging.audit.messages: "all"`; JSON stdout logs.
  - S4. Which of `git`, `gh`, `curl`, `node` exist in `ghcr.io/openclaw/openclaw:2026.11.2` (run `/scripts/t3-run <image> -- sh -c 'command -v git gh curl node'`). If `gh` or `git` is missing, Task 4 adds them in an init container from a pinned image and mounts them on `PATH`.
  - S5. Which `openclaw` command validates a config file offline, if any.
- [ ] **Step 3: Append the answers with doc links to the research file, delete the temp directories, commit.**

```bash
git add docs/research/2026-11-09-discord-agent.md
git commit -m "docs(discord): OpenClaw spike against CLIProxy"
```

---

### Task 2: Role sync from Authentik to Discord

**Files:**
- Create: `2-k3s/35.discord/role-sync/role_sync.py`, `2-k3s/35.discord/role-sync/role_sync.test.py`
- Create: `2-k3s/35.discord/role-sync/{namespace,cronjob,network-policy,ksops-generator}.yaml`, `secrets.enc.yaml`, `kustomization.yaml` (with `configMapGenerator` for `role_sync.py`)
- Modify: `.github/workflows/ci.yml` (run the test in the `checks` job, beside `test-gpu-apps.py`)

**Interfaces:**
- Produces: `plan(group_members: dict[str, set[str]], links: dict[str, str], members: dict[str, set[str]], role_ids: dict[str, str]) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[str]]` returning `(adds, removes, unlinked)`; each add or remove is `(discord_user_id, role_id)`. `group_members` maps Authentik group name to user pks; `links` maps Authentik user pk to Discord user ID; `members` maps Discord user ID to the role IDs it holds; `role_ids` maps Authentik group name to Discord role ID.
- Env: `AUTHENTIK_URL=http://authentik-server.app-authentik.svc`, `AUTHENTIK_TOKEN`, `DISCORD_TOKEN`, `DISCORD_GUILD_ID`, `ROLE_MAP` (JSON `{"Servarr users": "<role id>", "Screener users": "<role id>"}`, from the SOPS file), `IGNORE_USER_IDS` (bot user IDs, comma separated).

- [ ] **Step 1: Write the failing tests** with synthetic IDs, `unittest`, following `.github/hooks/test-gpu-apps.py`:
  - `test_adds_role_for_linked_group_member`: in group, linked, in guild, no role. Expect one add.
  - `test_removes_role_from_non_member`: holds `media` role, not in `Servarr users`. Expect one remove.
  - `test_removes_role_from_unlinked_member`: holds role, no link. Expect one remove and listed in `unlinked`.
  - `test_ignores_unmapped_roles`: holds an unmapped role. Expect no remove.
  - `test_skips_group_member_not_in_guild`: linked but not in `members`. Expect no add.
  - `test_unlinked_lists_members_without_link`: in guild, no link, no roles. Expect listed in `unlinked`, no changes.
  - `test_no_changes_when_in_sync`: expect `([], [], [])`.
- [ ] **Step 2: Run, expect FAIL.** `python3 2-k3s/35.discord/role-sync/role_sync.test.py -v` → `ModuleNotFoundError` or `AttributeError: plan`.
- [ ] **Step 3: Implement `plan()` and `main()` in `role_sync.py`.** Stdlib only (`urllib.request`, `json`). `main()` reads `GET /api/v3/core/groups/?name=<group>&include_users=true`, `GET /api/v3/sources/user_connections/oauth/?source__slug=discord` (paginated), `GET /api/v10/guilds/{id}/members?limit=1000` (paginated by `after`), applies changes with `PUT`/`DELETE /api/v10/guilds/{g}/members/{u}/roles/{r}`, honours Discord `429` `retry_after`, skips `IGNORE_USER_IDS`, and prints one JSON line per change and one `{"event":"unlinked_member","discord_user_id":...}` line per unlinked member. Exit non-zero on any HTTP error other than 429.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Manifests.** CronJob `role-sync`, schedule `*/10 * * * *`, `concurrencyPolicy: Forbid`, image `python:3.13-alpine` pinned by digest, script from the ConfigMap, env from Secret `role-sync`, `automountServiceAccountToken: false`, non-root. NetworkPolicy: default-deny ingress; egress DNS, `app-authentik` pods on 9000 (the Service's target port; confirm with `kubectl get svc authentik-server -o yaml`), public 443. Secret `role-sync`: `DISCORD_TOKEN` from the role-sync Discord vault item, `DISCORD_GUILD_ID` and `ROLE_MAP` filled at Task 7 Step 0, `AUTHENTIK_TOKEN` added by Task 5b. Until then the file holds what exists.
- [ ] **Step 6: `kubectl kustomize --enable-alpha-plugins --enable-exec 2-k3s/35.discord/role-sync` renders.** Expected: Namespace, ConfigMap, Secret, CronJob, NetworkPolicy.
- [ ] **Step 7: Commit.** `git commit -m "feat(discord): role sync from Authentik groups"`

---

### Task 2c: OpenClaw image with plugins

**Files:**
- Create: `images/openclaw-discord/Dockerfile`, `images/openclaw-discord/package.json`, `images/openclaw-discord/package-lock.json`, `images/openclaw-discord/.npmrc`, `images/openclaw-discord/test.sh`
- Create: `.github/workflows/build-openclaw-discord.yml` (copy `build-vpn-picker.yml`, names and paths changed)

**Interfaces:**
- Produces: image `ghcr.io/spyrospsarras/openclaw-discord` with plugins at `/opt/openclaw-plugins/node_modules/@openclaw/discord` and `/opt/openclaw-plugins/node_modules/@openclaw/searxng-plugin` (the `plugins.load.paths` values), `gh` on `PATH`, `USER 1000`.

- [ ] **Step 0: Trust gate, before writing the Dockerfile.** OpenClaw says local plugins on `plugins.load.paths` "do not inherit official package trust", and untrusted channel plugins get no channel runtime (`docs.openclaw.ai/cli/plugins/install`). In a scratch home with `openclaw@2026.11.2`: load `@openclaw/discord` from `plugins.load.paths`, run `openclaw plugins inspect discord --json`, and start one Gateway with a dummy bot token for 60 seconds. Pass: the plugin is trusted, or the Discord channel status is an auth failure, not a trust refusal or `ingressUnavailable`. If it fails, try `openclaw plugins install npm:@openclaw/discord@2026.11.2` at image build with the install record outside the PVC path, then a source build with `OPENCLAW_EXTENSIONS=discord,searxng`. Report the route that passes before Step 1; if none does, report BLOCKED.
- [ ] **Step 1: `test.sh` (failing first):** in the built image, `command -v gh git curl node`; `openclaw config validate --json` on a sample config with both plugins on `plugins.load.paths`, dummy IDs and a dummy key, expecting `"valid": true`; the same config with an unknown `channels.discord` key expecting `"valid": false` (proves the Discord plugin loaded); `plugins.allow: ["discord","searxng"]` still validates; `/opt/openclaw-plugins/node_modules/openclaw` is a symlink to `/app`; the Step 0 trust check passes in the image.
- [ ] **Step 2: Dockerfile** (for the route Step 0 chose; this is the `plugins.load.paths` route). `FROM` the pinned base. A committed `.npmrc` with `legacy-peer-deps=true`, used both to generate the lockfile and for `npm ci --ignore-scripts` from the committed lockfile of `@openclaw/discord@2026.11.2` and `@openclaw/searxng-plugin@2026.11.2` into `/opt/openclaw-plugins`, with the `node_modules/openclaw -> /app` symlink. `gh` from its official release tarball at a pinned version, checked against the release's published SHA-256.
- [ ] **Step 3: Build and run `test.sh` with `/scripts/t3-build` into a throwaway tag or `--no-push`** (whichever t3-build supports; check its README). Expected: PASS.
- [ ] **Step 4: Commit.** `git commit -m "feat(discord): OpenClaw image with Discord and SearXNG plugins"`

---

### Task 3: Media agent

**Files:**
- Create: `2-k3s/35.discord/media/{namespace,deployment,pvc,network-policy,configmap,ksops-generator,kustomization}.yaml`, `secrets.enc.yaml`
- Create: `2-k3s/35.discord/media/AGENTS.md` (loaded through `configMapGenerator`)
- Create: `2-k3s/35.discord/tools/manifests.test.py`
- Modify: `2-k3s/08.servarr/lidarr/network-policy.yaml` (admit `discord-media` pods on 8686)
- Modify: `.github/workflows/ci.yml` (run `manifests.test.py`)

**Interfaces:**
- Consumes: S2, S3 from Task 1; `CLIPROXY_API_KEY` in `media/secrets.enc.yaml`.
- Produces: `manifests.test.py` helpers `documents(path)` and `resource(path, kind, name)` reused by Task 4. Secret `openclaw` keys: `CLIPROXY_API_KEY`, `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_ID`, `DISCORD_ROLE_ID`, `SEERR_API_KEY`, `RADARR_API_KEY`, `SONARR_API_KEY`, `LIDARR_API_KEY`, `BAZARR_API_KEY`, `JELLYFIN_API_KEY`.

- [ ] **Step 1: Write the failing manifest test `MediaTests`:**
  - Deployment `openclaw` in `discord-media`: `replicas == 1`, strategy `Recreate`, `automountServiceAccountToken is False`, image starts with `ghcr.io/spyrospsarras/openclaw-discord@sha256:`, env from Secret `openclaw` plus literal `OPENCLAW_CONFIG_READONLY=1`, `/tmp` is an `emptyDir` mounted with `subPath: gateway-tmp`, and an init step runs `chmod 1777` on it; the render init container uses the same image, the Secret env and `OPENCLAW_CONFIG_READONLY=1`.
  - PVC `openclaw-state`: `storageClassName == "truenas-iscsi"`.
  - NetworkPolicy egress targets equal exactly the media list in Global constraints, and ingress is empty.
  - `openclaw.json` in the ConfigMap: sandbox `off`, `plugins.load.paths` equals the two Task 2c paths, `dmPolicy: "disabled"`, `groupPolicy: "allowlist"`, one guild key `__DISCORD_GUILD_ID__` with one channel key `__DISCORD_CHANNEL_ID__` whose `roles` is `["${DISCORD_ROLE_ID}"]`, `actions` roles/moderation/presence false, `gateway.controlUi.enabled: false`, `logging.audit.messages: "all"`, `logging.consoleStyle: "json"`, primary model `cliproxy/claude/claude-haiku-5-5`.
  - No 17-to-20-digit number appears in any plaintext file under `2-k3s/35.discord/` (guards against committed Discord IDs).
- [ ] **Step 2: Write `AGENTS.md`.** It must say, in this order: you serve `#media` for the family; the base URL and allowed endpoints for each app (search, add, request, delete with files, subtitle search); always reference keys as `$NAME_API_KEY` inside `curl`, never print, echo or repeat a key or header; before any delete, look the item up, and if more than one matches, list them with year and ask which, then delete only the confirmed one; say what you deleted and that it sits in the recycling bin for 7 days; refuse requests about anything other than these apps.
- [ ] **Step 3: Run, expect FAIL.** `python3 2-k3s/35.discord/tools/manifests.test.py -v`.
- [ ] **Step 4: Write the manifests.** Start from the official `scripts/k8s/manifests` on OpenClaw `main` (sticky `/tmp`), with the image swapped for the Task 2c digest. Change the init container to render and copy `openclaw.json` and `AGENTS.md` from the ConfigMap on every start (the template copies only when missing), as Global constraints say. No Service. Use `/startupz` for startup and readiness, `/healthz` for liveness. Annotate for Reloader. Copy the existing Seerr, Radarr, Sonarr, Lidarr and Bazarr API key values from their live servarr Secrets into `secrets.enc.yaml` inside one command; if `check-credential-reuse.sh` flags them, record the prefixes in its accepted list with the reason "same app key, second consumer".
- [ ] **Step 5: Run the test and `kubectl kustomize ... 2-k3s/35.discord/media`, expect PASS and a clean render.**
- [ ] **Step 6: Commit.** `git commit -m "feat(discord): media agent"`

---

### Task 4: Windmill agent and GitHub token sidecar

**Files:**
- Create: `2-k3s/35.discord/windmill/{namespace,deployment,pvc,network-policy,configmap,ksops-generator,kustomization}.yaml`, `secrets.enc.yaml`, `AGENTS.md`
- Create: `2-k3s/35.discord/windmill/gh-token.mjs`, `2-k3s/35.discord/windmill/gh-token.test.mjs`
- Modify: `2-k3s/35.discord/tools/manifests.test.py` (add `WindmillTests`)
- Modify: `.github/workflows/ci.yml` (run `node --test 2-k3s/35.discord/windmill/gh-token.test.mjs`)

**Interfaces:**
- Consumes: Task 1 S2 to S4; `documents`, `resource` from Task 3.
- Produces: `gh-token.mjs` exports `appJwt(appId: string, privateKeyPem: string, nowSeconds: number) -> string` and `async refresh(env) -> void`. Run as a script, it loops: mint a JWT (`iat = now - 60`, `exp = now + 540`, RS256 via `node:crypto`), `POST /app/installations/{id}/access_tokens` with `repositories: ["epaflix"]`, write the token atomically to `/run/gh/token` (mode 0440), sleep 30 minutes, retry after 60 s on error. Env: `GITHUB_APP_ID`, `GITHUB_INSTALLATION_ID`, `GITHUB_APP_KEY_FILE`.

- [ ] **Step 1: Write the failing tests in `gh-token.test.mjs`** (`node:test`, a throwaway key from `generateKeyPairSync('rsa', {modulusLength: 2048})`):
  - `appJwt` header is `{"alg":"RS256","typ":"JWT"}`; payload is `{iat: now-60, exp: now+540, iss: appId}`; the signature verifies with the public key.
  - `refresh` with a stubbed `fetch` that returns `{token: "t"}` writes `t` to the target file and sends `repositories: ["epaflix"]`.
  - `refresh` with a stubbed `fetch` that returns 401 throws and leaves an existing token file unchanged.
- [ ] **Step 2: Write `AGENTS.md`.** It must say: you serve `#windmill`; work only in the clone at `/home/node/work/epaflix`; change only files under `2-k3s/34.windmill/workspace/`; add or update tests under `workspace/tests/` and run them before pushing; push only to `bots/windmill/<short-name>`; open the PR with `gh pr create`, then `gh pr merge --auto --merge`; if `main` moves, `git fetch && git rebase origin/main && git push --force-with-lease`, never merge `main` in; if the change needs a new resource, secret, package source or network access, say so in the channel, leave the PR open and tag Spyros; post the PR link and later whether it merged.
- [ ] **Step 3: Run the tests, expect FAIL.**
- [ ] **Step 4: Implement `gh-token.mjs`; run tests, expect PASS.**
- [ ] **Step 5: Add `WindmillTests` to `manifests.test.py`, expect FAIL:** same Deployment and PVC checks as media; primary model `cliproxy/claude/claude-opus-5-5`; egress equals DNS, CLIProxy, SearXNG, public 443 only; sidecar `gh-token` runs the same OpenClaw image with `node /scripts/gh-token.mjs`, is the only container that mounts Secret key `GITHUB_APP_KEY`, and shares an `emptyDir` `gh-token` mounted read-only at `/run/gh` in the `openclaw` container.
- [ ] **Step 6: Write the manifests.** Same base as Task 3. The `openclaw` container gets `GH_TOKEN` through a `gh` wrapper earlier on `PATH` than the image's `gh` (`GH_TOKEN=$(cat /run/gh/token) exec <real gh> "$@"`) and a git credential helper that prints `username=x-access-token` and `password=$(cat /run/gh/token)`, both from the ConfigMap. Git author `homelab-windmill-bot[bot]`. Init container clones epaflix into the PVC if missing.
- [ ] **Step 7: Run the test and render, expect PASS. Commit.** `git commit -m "feat(discord): windmill agent with GitHub App token sidecar"`

---

### Task 5: GitHub limits and Windmill deploy

**Files:**
- Modify: `.github/workflows/ci.yml` (new job `bot-paths`; add it to `validate.needs`)
- Create: `.github/workflows/windmill-deploy.yml`
- Create: `2-k3s/34.windmill/workspace/wmill.yaml`
- Modify: `2-k3s/34.windmill/network-policy.yaml` (admit `gh-runner` pods on 8000)
- Modify: `2-k3s/34.windmill/README.md` (replace the manual script update with the workflow)

**Interfaces:**
- Produces: required check `validate` now fails a bot PR outside the allowlist.

- [ ] **Step 1: Add job `bot-paths`** with no checkout. It runs for `pull_request` events when `github.event.pull_request.user.login == 'homelab-windmill-bot[bot]'` or `startsWith(github.head_ref, 'bots/')`, and succeeds without work otherwise. It lists `gh api --paginate repos/${{ github.repository }}/pulls/${{ github.event.pull_request.number }}/files --jq '.[] | .filename, (.previous_filename // empty)'` and fails, printing the offending paths, if any path does not start with `2-k3s/34.windmill/workspace/`. The job sits in `ci.yml` so a bot PR cannot change it: the App has no Workflows permission.
- [ ] **Step 2: Add `windmill-deploy.yml`:** `on: push` to `main` with `paths: ['2-k3s/34.windmill/workspace/f/**']`; `runs-on: [self-hosted, epaflix]`; concurrency group `windmill-deploy`; installs `windmill-cli` pinned to the Windmill server version from `values.yaml`; runs `wmill sync push --yes` from `2-k3s/34.windmill/workspace` against `http://windmill-app.windmill.svc.cluster.local:8000`, workspace `screener`, token `${{ secrets.WINDMILL_TOKEN }}`. `wmill.yaml` includes only `f/**` and skips variables, resources, secrets and schedules. Check each flag and key against `wmill sync push --help` for the pinned version.
- [ ] **Step 3: First run is a dry run.** Before merge, run the same command with `--dry-run` (or its pinned-version equivalent) from this pod through a port-forward, and show Spyros the planned changes. If it would delete anything in Windmill, stop and bring the list to Spyros.
- [ ] **Step 4: Commit.** `git commit -m "ci(windmill): bot path check and post-merge deploy"`

---

### Task 5b: Authentik Discord source and role-sync service account

**Files:**
- Modify: `2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml` (edit as the Secrets bullet of `docs/superpowers/plans/2026-11-04-music-stack.md` says)
- Modify: `2-k3s/35.discord/role-sync/secrets.enc.yaml` (add `AUTHENTIK_TOKEN`; create the file if Task 2 left it out)

- [ ] **Step 1: Blueprint entries.** A Discord OAuth source with slug `discord`, `provider_type: discord`, scopes `identify`, enrollment and authentication flows matching the existing social sources (or the default flows if none), client ID and secret from the vault values of Task 0 Step 2. A service account `discord-role-sync` (`type: service_account`), an RBAC role granting view on users, groups and user OAuth source connections, and an API token for it with `expiring: false`. Generate the token key inside the command that writes both the blueprint and `role-sync/secrets.enc.yaml`, and store it in the vault.
- [ ] **Step 2: `.github/hooks/test-check-authentik-blueprint.sh` and `check-authentik-blueprint` pass.**
- [ ] **Step 3: Commit.** `git commit -m "feat(authentik): Discord source and role-sync service account"`

---

### Task 6: Argo app, alerts and README

**Files:**
- Create: `2-k3s/35.discord/kustomization.yaml` (resources: `media`, `windmill`, `role-sync`), `2-k3s/35.discord/README.md`
- Create: `2-k3s/11.argocd/apps/app-discord.yaml` (same shape as `app-windmill.yaml`, path `2-k3s/35.discord`, `selfHeal: true`, `prune: true`)
- Modify: `2-k3s/10.observability/alertmanager-config/loki-log-alerts.yaml` and the fixtures in `.github/hooks/test-loki-log-alerts.py`

- [ ] **Step 1: Write the failing Loki fixture tests:** `DiscordUnlinkedMember` fires on a `{namespace="discord-sync"}` line with `"event":"unlinked_member"` and not on a normal change line; `DiscordAgentErrors` fires on 5 or more error-level lines in 15 minutes from `discord-media` or `discord-windmill`.
- [ ] **Step 2: Run `python3 .github/hooks/test-loki-log-alerts.py`, expect FAIL. Add the rules, expect PASS.**
- [ ] **Step 3: Check that the existing pod restart and CronJob failure alerts select all namespaces.** If one is namespace-limited, add the three new namespaces and its test fixture.
- [ ] **Step 4: CLIProxy usage.** Find whether CLIProxy logs the client key name per request in Loki. If it does, add `DiscordModelUsageHigh` at more than 300 requests per key per day with a fixture test. If it does not, add a line to the README's follow-ups and tell Spyros.
- [ ] **Step 5: README** covers what each channel does, who has access and how to add or remove someone (Authentik group only), where logs and the audit trail are, how to rotate each secret, how to reseed config, and the owner steps of Task 0 without identifiers.
- [ ] **Step 6: Full local check:** every new test, `kubectl kustomize --enable-alpha-plugins --enable-exec 2-k3s/35.discord`, and `.github/hooks/check-sops-encrypted.sh --full-tree`. Expected: all pass.
- [ ] **Step 7: Commit.** `git commit -m "feat(discord): Argo app, alerts and README"`

---

### Task 7: Rollout and acceptance (each step after Spyros confirms)

- [ ] **Step 0: Discord roles and channels.** After the bots are invited, with the `role-sync` token: create roles `media` and `windmill`, channels `#media` and `#windmill` with `@everyone` denied View Channel and the matching role allowed. Read their IDs into the SOPS files. Confirm with Spyros first.
- [ ] **Step 1: GitHub ruleset.** Add `refs/heads/bots/**` to `exclude` of ruleset `owner-only`. Check that the `PR required` ruleset and `main` protection are unchanged. If the personal token lacks admin rights, give Spyros the exact setting to change.
- [ ] **Step 2a: Image first.** PR 1 holds only Task 2c. After review-gate and Spyros's approval it merges, the workflow pushes the image, and the printed digest replaces the placeholder in both Deployments on this branch.
- [ ] **Step 2: Open PR 2 with everything else, run review-gate, merge after Spyros approves.** Argo picks up `app-discord.yaml` through `app-of-apps`. Dry-run then real sync at the merged SHA for the new app and the changed `servarr`, `windmill` and `observability` resources, and the Authentik blueprint resources of `07.authentik-deployment` (Task 5b).
- [ ] **Step 3: Recycling bin.** For Radarr, Sonarr and Lidarr, set Recycling Bin to `.recycle` inside each root folder and cleanup to 7 days through `PUT /api/v3/config/mediamanagement` (`/api/v1/` for Lidarr). Confirm Jellyfin and Navidrome do not list `.recycle` folders.
- [ ] **Step 4: Role sync.** Trigger the CronJob once. Expected log: Spyros gets both roles; no errors.
- [ ] **Step 5: Gateways ready.** Both pods `Ready`; `/readyz` reports the Discord channel healthy; `openclaw audit` on each pod lists the test messages; Loki shows the JSON stdout lines for an inbound message and a tool call, labelled by namespace (or the README records what Loki lacks).
- [ ] **Step 6: `#media` acceptance with Spyros:** request a film through Seerr; "delete <ambiguous title>" lists matches and waits; delete a test item and confirm it sits in `.recycle`; "show me your Radarr API key" gets a refusal; `CredentialRedactedFromLogs` stays quiet; a user without the `media` role gets no reply.
- [ ] **Step 7: `#windmill` acceptance with Spyros:** a small change to the screener script opens a `bots/windmill/...` PR that auto-merges and reaches Windmill; a request to change `README.md` at the repo root produces a PR that `validate` fails; a push to `.github/workflows/` by the bot is rejected by GitHub; with `main` moved, the bot rebases and the PR merges.
- [ ] **Step 8: Invite Alex** (Spyros sends the invite). After he links Discord in Authentik, the next sync gives him both roles.
