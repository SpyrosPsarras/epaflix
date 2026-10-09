# t3code: replace OpenCode with Pi — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Writer: Sol 6.1 through T3 `delegate_task`. Reviewer: Opus 5.5 under review-gate. Steps use checkbox (`- [ ]`) syntax.

**Goal:** The t3code pod runs every T3 thread, main thread and subagent, on Pi instead of OpenCode, using existing Pi packages in place of our OpenCode plugins.

**Architecture:** T3 0.0.46 nightly drives Pi through `pi --mode rpc` and loads its own MCP bridge extension, which provides `delegate_task`. A wrapper script fixes Pi's environment. Pi loads pinned packages from `/tools/node_modules` and reads its config from `~/.pi/agent`, which the entrypoint writes on every start. OpenCode, its plugins and Jev Auto are removed.

**Tech Stack:** Pi 1.1.0 (`@earendil-works/pi-coding-agent`), T3 0.0.46 nightly, CLIProxyAPI, Bash/Python entrypoint, kustomize + ArgoCD.

**Spec:** this conversation (decisions D1–D4 below); findings in thread 14245a50.

## Decisions (from Spyros)

- D1 Pi everywhere. OpenCode is removed from the pod. Jev Auto is dropped.
- D2 Jev guard = upstream `jev-guard` plus an upstream PR that adds `JEV_API_URL` and `JEV_API_KEY_FILE`. Until the PR is merged, the same change ships as a hash-checked patch applied at install time.
- D3 There is no check for secrets inside tool arguments. Output masking (pi-redact) and the env-print denies stay.
- D4 homePC's three Pi profiles (`agent`, `dh`, `epaflix`) switch to `pi-cliproxyapi-provider` in this round.

## Global Constraints

- Pins: `@earendil-works/pi-coding-agent` 1.1.0, `pi-cliproxyapi-provider` 0.15.58, `@gotgenes/pi-permission-system` 40.1.2, `@spences10/pi-redact` 0.0.15, `@juicesharp/rpiv-todo` 2.12.0, `jev-guard` 0.3.1, superpowers `github:obra/superpowers#v6.4.2`, cc-safety-net ≥ 2.4.12 (the first release with `dist/pi/index.js`).
- All of these are pinned in `2-k3s/13.t3code/{env,one}/tools/package.json` plus the lockfiles; Renovate tracks them. Pi's `settings.json` lists them as absolute paths under `/tools/node_modules`, never as `npm:` sources.
- Provider name `cliproxy`. Model slugs become `cliproxy/claude/claude-opus-5-5`, `cliproxy/codex/gpt-6.1-sol` and so on. The reasoning option id is `thinking`.
- No secret value goes into any file, ConfigMap or commit. Keys stay as env references or `/run/...` files.
- The work company's name never appears in this repo.
- Pi must not see `ANTHROPIC_AUTH_TOKEN`. If it does, it registers 17 direct-Anthropic models (measured).

## Review Focus

1. Pi's process env holds `CLIPROXYAPI_API_KEY` and `MCP_HUB_TOKEN`. `env`, `printenv`, `/proc/*/environ` and `set` must be denied (Task 3 test).
2. jev-guard "ask" prompts would stall unattended subagents. Run it deny-only: `JEV_GUARD_ASK_SCORE=9` and `JEV_GUARD_ASK_P=1.01` (Task 3 test: an ask-level call is allowed and logged, not prompted).
3. T3 never drops a Pi model it has seen once. Startup deletes `~/.t3/caches/pi.json` once, at the switch (Task 4 test).
4. Existing OpenCode threads lose their provider. T3 must start, and an old thread must show a provider-missing state, not crash (Task 6 verification).
5. `/run/jev` (the OpenRouter key) must be unreadable through `read`, `bash` and `grep` (Task 3 test).

---

### Task 1: jev-guard — SUPERSEDED (no code)

Upstream `leepokai/jev-guard` main (`cda06c4`, unreleased; npm is still 0.3.1) already has an OpenRouter backend. It reads `openRouterApiKey` from the config file named by `JEV_GUARD_CONFIG`. The plan therefore pins jev-guard as `github:leepokai/jev-guard#cda06c49ceaac207904142ec92bfab793bbd18b1` (Task 2). In Task 3, `pi.sh` sets `JEV_GUARD_CONFIG=/run/jev/jev-guard.json`, a key added to the SOPS secret `one/jev-secret.enc.yaml` with content `{"openRouterApiKey": "<same key as openrouter-key>"}`. Agents can't read `/run/jev`, so the key never enters Pi's environment. There is no patch and no upstream PR. Our fork branch `SpyrosPsarras/jev-guard:feat/api-url-and-key-file` is unused.

Original text (superseded):

**Files:**
- Fork `leepokai/jev-guard` → `SpyrosPsarras/jev-guard`, branch `feat/api-url-and-key-file`, change `src/jev.js` and its test.
- Create: `2-k3s/13.t3code/one/files/jev-guard.patch` (the same diff). `env/Dockerfile` applies it to `/tools/node_modules/jev-guard` after `npm ci` and fails the build if the result's sha256 doesn't match the pinned value. The step is removed once a jev-guard release contains the change.

- [ ] Test (upstream repo, `node:test` with the fake Jev it already uses): with `JEV_API_URL=http://fake/v1/systemone` and `JEV_API_KEY_FILE=<tmpfile>`, `ask()` posts to that URL with `Authorization: Bearer <file contents trimmed>`. With neither set, behaviour is unchanged.
- [ ] Implement: `backend()` reads `JEV_API_KEY_FILE` when `JEV_API_KEY` is unset; `ask()` uses `env.JEV_API_URL ?? TYPESAFE_URL` for the typesafe backend.
- [ ] `npm test` passes. Open the PR upstream (approved by Spyros in D2) and link it in the epaflix PR.

### Task 2: Image pins and cc-safety-net for Pi

**Files:** `2-k3s/13.t3code/{env,one}/tools/package.json` + lockfiles (`one/tools/sync-shared.sh` keeps them in sync), `one/files/cc-safety-net-install.py`, `one/files/cc-safety-net.patch`, `one/files/cc-safety-net-hashes.json`, `one/tools/test-cc-safety-net-install.py`.

**Produces:** `/tools/node_modules/<pkg>` for every pin. The reviewed cc-safety-net build is at `$HOME/.local/share/pi/cc-safety-net/<version>/` with its Pi entry `dist/pi/index.js`.

- [ ] Add the pins and remove `@opencode/cli` and `@opencode/client`.
- [ ] Rebase `cc-safety-net.patch` (budget.ts, secret-protection.ts, python-print.ts) onto the newest release that has the Pi entry. Regenerate the hashes. The installer drops its OpenCode registration (`--config`) and prints the Pi entry path.
- [ ] `python3 one/tools/test-cc-safety-net-install.py` passes. On the built tree, `node -e "import('<entry>')"` loads.

### Task 3: Pi config, wrapper and guard policy

**Files:**
- Create: `one/files/pi.sh` (T3 `binaryPath`), `one/files/pi-setup.py` (writes `~/.pi/agent` on every start), `one/files/pi-setup.test.py`.
- Modify: `one/files/hub_clients.py` (`pi` target writes `~/.pi/agent/mcp.json`, header `"Authorization": "Bearer ${MCP_HUB_TOKEN}"`), `one/files/private-config.py` (links `.pi/agent/AGENTS.md` → `instructions.md`).

**Interfaces:**
- `pi.sh`: exports `CLIPROXYAPI_BASE_URL="$ANTHROPIC_BASE_URL/v1"`, `CLIPROXYAPI_API_KEY="$ANTHROPIC_AUTH_TOKEN"`, `CLIPROXYAPI_PROVIDER_NAME=cliproxy`, `JEV_GUARD_CONFIG=/run/jev-guard/config.json`. `pi-setup.py jev-config <key-file> <out-file>` writes `{"openRouterApiKey": <trimmed key>}` there at startup, mode 0600, atomically, and never prints the key. `/run/jev-guard` is a memory emptyDir that Task 4 adds to the statefulset. No SOPS edit, `JEV_GUARD_ASK_SCORE=9`, `JEV_GUARD_ASK_P=1.01`, `JEV_GUARD_SKIP_TOOLS=<vaultwarden tools>`. Unsets `ANTHROPIC_AUTH_TOKEN`, then `exec /tools/node_modules/.bin/pi "$@"`.
- `pi-setup.py write <home>` writes:
  - `settings.json`: the packages list as absolute paths (including `@dietrichgebert/ponytail`), `"skills": ["!**/.agents/skills/ponytail/**"]` (the package's newer ponytail skill wins in Pi; the bundled copy stays for Claude Code and Codex; verified on homePC), plus `"pi-cliproxyapi-provider": {"gpt56ContextWindow": "canonical"}`. Keys the user set are kept; `packages` and `skills` are always rewritten.
  - `pi-cliproxyapi-provider/config.json` (path is `$HOME/.pi/agent/...`; the package ignores `PI_CODING_AGENT_DIR`): `providerName: "cliproxy"`, `baseUrl` and `modelAliases`. Aliases are built at every start from cliproxy's live `GET $ANTHROPIC_BASE_URL/v1/models`: `claude/<x>` → `anthropic/<x>` and `codex/<x>` → `openai/<x>`, plus fixed OpenRouter aliases `openrouter/or-glm-5.3-flash` → `openrouter/z-ai/glm-5.3-flash`, `openrouter/or-deepseek-v4-flash` → `openrouter/deepseek/deepseek-v4-flash`, and `openrouter/or-minimax-m3:free` and `or-minimax-m3:free` → `openrouter/minimax/minimax-m3`. If the fetch fails, the previous file is kept. These aliases give the picker names like "Claude Opus 5.5" and "GPT-6.1 Sol" (verified on homePC).
  - `pi-permissions.jsonc`: `bash` denies `mkfs*`, `dd *of=/dev/*`, `kubectl delete|drain|cordon *`, `helm uninstall *`, `reboot*`, `shutdown*`, `poweroff*`, `qm stop|shutdown *`, `env`, `env *`, `printenv*`, `set`, `export`, `declare*`, `*/proc/*/environ*`, `*/run/jev*` (also matches `/run/jev-guard`); `tools` denies `read:`, `grep:`, `find:` and `ls:` for `/run/jev/*` and `/run/jev-guard/*`; every default is `allow`.
  - `JEV_GUARD_SKIP_TOOLS`: the Pi names of the vaultwarden MCP tools (`mcp__<server>__<tool>`, server names from `hub_clients.py`), because they handle secrets by design. Check whether `@spences10/pi-redact` can exempt the same tools. If it can't, accept masking of vault output and note it.
  - `AGENTS.md` = bundle instructions + `/scripts/homelab-ssh.md`, written whole or not at all, like the Codex override.

- [ ] Failing tests in `pi-setup.test.py`: the policy denies `printenv HOME`, `cat /proc/1/environ` and `cat /run/jev/openrouter-key`, and allows `git status`. `settings.json` keeps a user key such as `theme`. A rerun gives byte-identical output.
- [ ] Implement until they pass. `python3 hub_clients.py --selftest` passes with the new `pi` case.
- [ ] In the built image: `pi.sh --list-models` lists only `cliproxy` models. `pi.sh -p "printenv"` comes back blocked.

### Task 4: Entrypoint and T3 settings

**Files:** `one/files/entrypoint.sh`, `one/statefulset.yaml` / `one/kustomization.yaml` (ConfigMap file list).

- [ ] Replace the OpenCode block: run `pi-setup.py`, `hub_clients.py pi`, and the cc-safety-net installer.
- [ ] Seed or migrate `settings.json`: add a `pi` instance (`binaryPath: /scripts/pi.sh`, enabled), remove the `opencode` instance, and set `defaultModelSelection` and `textGenerationModelSelection` to `pi` / `cliproxy/claude/claude-opus-5-5` when they point at OpenCode. Codex is untouched.
- [ ] Delete `~/.t3/caches/pi.json` once, using a marker file.
- [ ] Startup check: `pi.sh list` must show every pinned package, otherwise startup aborts (replaces `opencode-plugin-check.mjs`).
- [ ] Handoffs from earlier reviews:
  - (T2-M4) The cc-safety-net installer runs under `env -i`. Pass `HOME` or an explicit destination under the PVC home, then hand its last stdout line (the Pi entry) to `pi-setup.py write <home> <entry>`.
  - (T3-D4) Ship `pi-setup.py` and `hub_clients.py` together in `/scripts`.
  - Add `pi.sh` and `pi-setup.py` to the ConfigMap (`pi.sh` mode 0755).
  - Add a memory emptyDir `jev-guard` mounted at `/run/jev-guard`. Run `pi-setup.py jev-config <the existing openrouter key file> /run/jev-guard/config.json` before `exec t3`.
  - Do not set `PI_CODING_AGENT_DIR`: pi-setup writes under `$HOME/.pi/agent`.
  - Run `hub_clients.py pi "$HOME/.pi/agent/mcp.json" …` in the existing hub block, next to the opencode/claude/codex calls. Task 5 removes the OpenCode ones.
  - After editing anything under `env/` or `one/`, run `bash 2-k3s/13.t3code/env/tools/sync-runtime-image.sh` and `bash 2-k3s/13.t3code/one/tools/sync-shared.sh`. CI fails if `statefulset.yaml` or the copies drift.
- [ ] Image-level checks happen in PR CI (`build-t3-runtime.yml` builds the image and runs `env/tools/smoke.sh`). Task 5 updates `smoke.sh`.

### Task 5: Remove OpenCode

**Delete:** `one/files/{cliproxy-models.js,jev-auto.js,jev-auto.test.mjs,jev-guard.js,jev-guard.test.mjs,jev-guard.live.mjs,jev-steps.test.mjs,opencode-compat.js,opencode-compat.test.mjs,opencode-fast-version.sh,opencode-refresh.mjs,opencode-plugin-check.mjs,opencode-plugin-check.test.mjs,opencode-v1-backup.py}`, `one/tools/test-opencode-v1-backup.py`, `files/cliproxy-models.js`, `files/cliproxy-models.test.mjs`, `one/jev-auto.md`.

**Modify:** `env/tools/smoke.sh`, `env/tools/opencode-parity.mjs` → `pi-parity.mjs`, `.github/workflows/ci.yml`, `.github/renovate.json`, `one/jev-guard.md` (rewritten for the Pi layers), `env/README.md`, `migration/` docs only where they describe current behaviour.

- [ ] `rg -il opencode 2-k3s/13.t3code .github` lists only historical migration notes.

#### Task 5 addition: drop the second npm lockfile copy (`one/tools/package*.json`) — approved by Spyros in another thread

Why: 5,162 of the branch's 6,125 added lines are the two identical `package-lock.json` copies, and the `one/` copy has no reader. `one/tools/package*.json` reach the pod only as ConfigMap `t3code-tools` at `/runtime-pins` (`one/statefulset.yaml:154`, volume :273), and nothing reads it (`rg runtime-pins`); its only effect is rolling the pod on pin changes. The image tag already does that: `env/tools/runtime-tag.sh` hashes `env/tools/package.json` + `package-lock.json`, so a pin change gives a new `inputs-<hash>` image in `one/statefulset.yaml`. The copy has also broken Renovate before (`.github/renovate.json` disables `one/tools/package.json`; PRs #1488, #1497, #1498, #1502).

- [ ] 1. Delete `2-k3s/13.t3code/one/tools/package.json` and `one/tools/package-lock.json`.
- [ ] 2. `one/kustomization.yaml`: remove the `t3code-tools` configMapGenerator entry, and fix the header comment (lines 8-9) so it no longer says `tools/` holds copies.
- [ ] 3. `one/statefulset.yaml`: remove the `lock` volume and its `/runtime-pins` mount.
- [ ] 4. `one/tools/sync-shared.sh`: remove the two `env/tools/package*.json` pairs; keep private-config, git-credential, hub_clients and homelab-ssh.
- [ ] 5. `.github/renovate.json`: in the first t3code rule, drop `bash 2-k3s/13.t3code/one/tools/sync-shared.sh` from `postUpgradeTasks.commands` and the two `one/tools/package*.json` entries from `fileFilters`; delete the rule that disables `one/tools/package.json`; keep `sync-runtime-image.sh`.
- [ ] 6. `env/tools/rollout-hash.test.sh`: remove the `t3code-tools-*` check (line 23) and the "lockfile change changes lock ConfigMap" step. Replace them with a check that a lockfile change changes `runtime-tag.sh` output, and keep the checks that a script change rolls `t3code-scripts` and an unrelated edit rolls nothing.
- [ ] 7. Optional: `2-k3s/12.renovate/cronjob.yaml:41` may drop `sync-shared.sh` from the allowed post-upgrade commands (harmless if left).
- [ ] 8. Docs: `migration/README.md:200` stays (historical); `23.mcp-hub/README.md` stays (still correct for `hub_clients.py`).
- [ ] Checks: `bash 2-k3s/13.t3code/one/tools/sync-shared.sh --check`; `bash 2-k3s/13.t3code/env/tools/rollout-hash.test.sh`; `kustomize build 2-k3s/13.t3code/one` renders (synthetic-secret fixture); `rg -n "runtime-pins|t3code-tools|one/tools/package" . .github` returns nothing outside `migration/`.
- [ ] Note for the implementer: the T4 fix round's step "run sync-shared.sh after edits" still applies to the other pairs; T2's earlier copies of the lockfile are deleted here, not re-synced.
- [ ] CI-equivalent local run: `bash env/tools/smoke.sh` passes.

### Task 6: Private instructions (homePC, SOPS)

- [ ] On homePC, edit the instructions source: the subagent section uses `providerInstanceId: pi`, models `cliproxy/codex/gpt-6.1-sol`, `cliproxy/claude/claude-opus-5-5` and `cliproxy/claude/claude-haiku-5-5`, and the option `thinking`. Drop the OpenCode Browser line.
- [ ] `python3 private-config.py pack one/private-agent-config.enc.yaml` on homePC. Only the ciphertext changes go into the epaflix PR.

### Task 7: homePC Pi profiles (ops, not git) — DONE 2026-10-09 for `~/.pi/agent` only

Done: `pi-cliproxyapi` was replaced by `pi-cliproxyapi-provider`. The wrapper `~/.local/bin/pi-t3` reads the key from `secret-tool`. homePC's T3 `pi.binaryPath` points at it, and the cache was cleared. The `dh` and `epaflix` profiles were not changed. Backups: `~/.pi/agent/backup-before-cliproxyapi-provider-202610091639/` and `/tmp/t3-settings.before-pi-t3.json`.

Original steps:

- [ ] Back up `~/.pi/{agent,profiles/dh,profiles/epaflix}/settings.json`.
- [ ] In each, replace `npm:pi-cliproxyapi` with `npm:pi-cliproxyapi-provider`. Write `~/.pi/agent/pi-cliproxyapi-provider/config.json` (shared by every profile, because the package reads `$HOME`) with `baseUrl` `https://cliproxy.epaflix.com/v1`, `providerName` `cliproxy` and the Claude aliases. The key comes in through the same mechanism the old config used (`!command` or `/login cliproxy`), never as a literal.
- [ ] `pi --list-models cliproxy` in each profile shows Claude with reasoning on. Delete homePC's `~/.t3/caches/pi.json` with T3 stopped.

### Task 8: Rollout and live checks

- [ ] review-gate passes on the whole branch. Ask Spyros, then merge; ArgoCD syncs and restarts `t3code-0`.
- [ ] In the pod:
  - (a) T3 lists Pi models with Thinking Off–Max for Codex.
  - (b) `delegate_task` to Pi with `cliproxy/codex/gpt-6.1-sol` completes.
  - (c) the synthetic AGENTS.md code word comes back through Claude.
  - (d) `rm -rf /`, `printenv` and `cat /run/jev/openrouter-key` are blocked.
  - (e) an old OpenCode thread opens without crashing T3.
- [ ] Rollback: revert the merge commit. OpenCode's data on the PVC is not deleted.
