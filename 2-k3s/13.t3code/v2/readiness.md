# V2 readiness investigation

Status: prerequisites partially verified. No v2 workload has been deployed. Task 3 remains blocked on the readiness addendum.

## Live inventory

Inspected `t3code-0` read-only on 2026-09-30. The live pod differs from this worktree's older manifests: it mounts only `/home/spyros`; `/home/t3env-0` and `/home/t3env-1` do not exist. Its configured instances are OpenCode enabled, Codex enabled and Claude disabled. Do not recreate unused historical homes from stale manifests.

| Ref | Capability | Maintained source or private state | Provisioning and acceptance |
| --- | --- | --- | --- |
| I1 | T3 and provider tools | `env/tools/package.json`, lockfile and runtime Dockerfile | Separate v2 server artifact; same provider/tool pins. Live pins: OpenCode 1.18.33, Codex 0.158.0, Claude 2.1.283, OpenCode Loop 0.5.38. |
| I2 | MCP hub | `one/files/hub_clients.py`, startup script; runtime hub URL/token | Configure notion, keepass, searxng, gmail, kubernetes-epaflix and jev, all enabled in live OpenCode. Verify discovery and read-only calls from v2 agents. |
| I3 | OpenCode plugins | Maintained cliproxy-models, jev-auto, jev-guard and Loop scripts; configured superpowers and cc-safety-net 2.4.11 | Reinstall maintained scripts and plugin declarations; test routing, guards and command discovery. |
| I4 | Commands and skills | Private-config bundle, installed skill links, Loop package | Preserve private instructions and skills via existing installer. Live command folder contains Loop commands and the agent folder contains opencode-loop-local. |
| I5 | Provider settings | Private OpenCode/Codex config and T3 provider settings | Selectively provision configuration, including Codex launch arguments/custom models and OpenCode binary path. Do not copy transcript/session stores. |
| I6 | Proxy and GitHub HTTPS | Existing `cliproxy-secrets` references; credential helper | Reuse runtime secret references and env-based helper; test proxy turns and repository access without printing credentials. |
| I7 | GitHub SSH | Existing `t3code-github-ssh` mount and known hosts | Reuse read-only secret and helper behavior; verify private repository access. |
| I8 | Jev key | Existing `t3code-jev` mounted file | Runtime mount only; validate Jev plugin operation. |
| I9 | Cluster and Azure access | Existing private `.kube/config`, `.azure` authentication state, `.local/bin/aks-auth` and `aks-auth-central` | Selective secure provisioning must cover kubeconfig dependencies and complete Azure auth/cache/helper dependencies. Presence was checked; expiry and effective access still require validation. |
| I10 | Additional GitHub auth | Existing private `.config/gh/hosts.yml` | Inventory credential references before selective transfer; env GitHub token already provided. Do not assume all file state is redundant. |
| I11 | T3 operations | Branch runtime | Test terminals, files, preview, worktrees and PR linking independently of external MCP. |
| I12 | PVC-installed CLIs | `.local/bin/terraform`, jq, shellcheck, uv/uvx, tflint, kubelogin and kubectl | These resolve before image binaries in the live environment and would be lost on a fresh home without explicit provisioning. Observed Terraform 1.11.4, TFLint 0.64.0, kubelogin v0.2.19, jq 1.7.1, ShellCheck 0.10.0, uv/uvx 0.12.19 and kubectl 1.37.0. Reproduce the approved tool set as maintained inputs. |

The MCP registration helper currently special-cases the name `t3code` and its v1 secret destination. Calling it with `t3code-v2` would follow its PC path, not create a Kubernetes secret. Add explicit destination support with tests before minting the v2 client. Do not run the helper unchanged and assume v2 provisioning works.

Private instruction and skill symlinks point into the revisioned private-config installation; the mounted bundle contains `files` and `sourceHome`. Reinstall that bundle rather than copy dangling symlinks. The primary kubeconfig uses embedded certificate/key data for context `epaflix`. The separate `davidhornkubeconfig` has five AKS contexts, all using `/home/spyros/.local/bin/aks-auth` as their exec helper. Azure authentication depends on its token/HTTP caches and profile/config files; exclude logs, telemetry and stale lock files from selective provisioning. Claude credentials and settings exist even though its T3 instance is disabled; preserve required configuration without enabling it or copying session/history directories.

## Initial projects

`projects.json` records nine sanitized remotes and original destination paths. Existing directories total approximately 1.3 GiB, including current worktree content; this is not a prediction of fresh clone size.

Spyros approved excluding the seven exception entries: local-only `Davidhorn/obscure-data`, `temp`, the home directory itself, and four missing project/home paths under `/home/t3env-0` and `/home/t3env-1`. No local files or history are to be copied. A synthetic repository was registered through `POST /api/projects/mutate` with `project.create`, a fresh command/project UUID, title and workspaceRoot; `GET /api/projects` returned that workspace. Both endpoints were called with the temporary bearer credential and protocol-2 header. This validates the registration interface, not access to the nine private/public remotes.

## Capacity and routing

- F1. Worker 65 has 16 allocatable CPUs and about 21.5 GiB allocatable memory. Metrics showed 21% CPU and 22% memory usage during inspection. These are observations, not reserved capacity.
- F2. Worker 65's root filesystem had about 18 GB free. Other workers had approximately 15.1 GB, 13.9 GB and 12.4 GB free. A nominal PVC size is not reserved local-path capacity; fresh clones, agent caches, retained images and backups need headroom.
- F3. Start sizing from one fresh home rather than three cloned homes. Node/volume selection remains unapproved until disk headroom and retention limits are established. Do not modify a node to create capacity without authorization.
- F4. Inside the current environment, `t3code.epaflix.com` resolves to internal `192.168.10.102` and returns HTTPS 200. `t3code-v2.epaflix.com` resolves to public Cloudflare IPv6 addresses. Internal DNS mapping is a prerequisite; an ingress manifest alone is insufficient.

A later worker-65 filesystem check reported 31,306,379,264 bytes available, about 29.2 GiB. The earlier 18 GB figure is stale. Placement on worker 65 now appears plausible for the selected small trial, but final clone/image/backup measurements and scheduling requests remain required.

## Source build evidence

Tested checkout `0a42dd3921b12ff9bbc89058a15e51c21764b430` from upstream v2 branch. Node 24.21.0 was available. Used pnpm 11.10.0 through `npm exec` without global installation.

Commands in `/tmp/opencode/t3code-src`:

```sh
npm exec --yes --package=pnpm@11.10.0 -- pnpm install --frozen-lockfile
npm exec --yes --package=pnpm@11.10.0 -- pnpm exec vp run --filter t3 build:bundle
npm exec --yes --package=pnpm@11.10.0 -- pnpm exec vp run --filter @t3tools/web build
node apps/server/dist/bin.mjs start --help
```

Dependency installation and server bundle succeeded. The initial web build exceeded a 120-second tool timeout; rerunning with a 600-second timeout succeeded in 4m 46s, with upstream chunk-size and plugin-timing warnings. No source changes were needed. The Node bundle path did not require Rust or Docker locally; this does not prove native executable packaging.

A disposable probe ran the built server with scrubbed credential environment, loopback port 13773 and a temporary HOME/T3CODE_HOME. Descriptor reported protocol 2, version 0.0.44 and a fresh identity. `/health` and `/` returned 200. Shutdown exited 130. Startup flags include `--base-dir`, `--no-browser`, `--host`, `--port` and `--auto-bootstrap-project-from-cwd`.

Minimal production artifact assembly remains to be tested. The bundle has external native dependencies; copying only `dist/bin.mjs` is insufficient. Upstream's dependency selector returns `@cursor/sdk`, `@ff-labs/fff-node`, `@napi-rs/keyring` and `node-pty`, whose transitive/native dependencies also need preservation. Reuse upstream's external-package packaging definitions or retain a verified minimal production dependency tree.

The authorized GitHub-hosted [container probe](https://github.com/SpyrosPsarras/epaflix/actions/runs/36736258343) passed in 4m 2s. It built the pinned source in Node 24, retained the full dependency tree, and ran the runtime as an unprivileged user with no network or production credentials. Protocol 2, fresh identity, web/health and live clients 0/1/0 passed. This proves container feasibility, not optimized locked production packaging, provider execution or full tool parity.

Native single-executable packaging has a different floor: upstream's `build-exe` help requires Node 25.7+ for `--build-sea`. Do not infer that Node 24 can produce that artifact from the successful bundle test. A Node-bundle-based v2 runtime can retain our existing Node 24 base if standalone assembly succeeds.

Spyros selected the upstream native executable for production. The follow-up [native archive probe](https://github.com/SpyrosPsarras/epaflix/actions/runs/36739997478) passed in 3m 38s. It built the resource monitor with locked Cargo dependencies, built the single-executable using Node 26, and called upstream `build-cli-archive.ts`. The archive was 85,243,343 bytes. The final container ran `/runtime/t3`, not the JavaScript bundle, and passed protocol, fresh identity, web/health and live clients 0/1/0. Production should pin the tested builder images/tool versions and record the archive's resolved native dependencies; upstream staging performs a fresh production install rather than copying the source lockfile.

A separate production dependency stage was assembled with `pnpm --filter t3 deploy --legacy --prod .probe-stage` inside the disposable source checkout, then the built web directory was copied to `.probe-stage/dist/client`. The resulting directory was 356 MB and its server passed the same isolated boot, client-count, project-registration and fixture probes. The first deployment attempt timed out; an outside-workspace forced retry was refused by pnpm. The successful in-workspace command avoids that refusal. This proves a staged directory can boot, not container behavior or exact frozen dependency reproduction: legacy deployment re-resolved dependencies and emitted peer/deprecation warnings.

A later `pnpm exec node ...cli.ts build` attempt failed because pnpm tried to remove the workspace modules directory under production-mode state without a TTY. No production modules were removed. The existing bundle/stage probes still passed. CI needs a reproducible build/stage sequence rather than assuming this legacy deployment command preserves the original lockfile.

## Live connection observation

Verified `GET /api/auth/clients` on the running synthetic server using a temporary bearer credential kept inside the probe process. It reported zero connected clients before WebSocket connection, one during connection and zero after close. HTTP polling itself did not count as a connected WebSocket client.

Spyros selected a read-scoped monitoring credential. An isolated probe created a pairing token through `POST /api/auth/pairing-token` with `access:read` and `orchestration:read`, then exchanged it through `POST /oauth/token`. The resulting session reported exactly those scopes and read `/api/auth/clients` and `/api/orchestration/shell` with HTTP 200. Bootstrap issuance required the temporary administrative session; the steady monitoring session did not. Secure provisioning, expiry handling and renewal remain implementation requirements. The first exchange used the incorrect `/api/auth/token` URL and returned 404; the contract-defined `/oauth/token` succeeded.

The CLI `auth session list` creates its own auth runtime and in-memory connection map, so it is not a live connection-count mechanism.

Renewal investigation found the ordinary bearer session lasts about 30 days. Reusing the consumed pairing token or using the session itself as bootstrap returned HTTP 401; the read-only session attempting pairing-token issuance returned 403. It cannot renew itself. A token-free `/proc/net/tcp*` probe found an established connection remaining after a completed HTTP request, so socket counting cannot distinguish client WebSockets from idle backend HTTP connections. No unauthenticated live-client count endpoint was found in the inspected source. Spyros selected returning to scoped monitoring after this investigation; the local renewal design still requires approval and validation.

Spyros subsequently authorized validation of local renewal. The isolated probe issued a one-minute admin session, minted/exchanged a new read-scoped session, atomically replaced a mode-0600 credential file, and revoked the admin session in `finally`. The revoked admin received HTTP 401 and the renewed read session received 200. This proves the successful issuance/revocation flow. Interrupted issuance, failed replacement and obsolete read-session revocation still need explicit checks before production scheduling.

## Activity evidence and remaining checks

Focused upstream tests ran against the pinned checkout:

```sh
npm exec --yes --package=pnpm@11.10.0 -- pnpm exec vp test run apps/server/src/auth/SessionStore.test.ts apps/server/src/auth/EnvironmentAuthAdmin.test.ts apps/server/src/orchestration-v2/ProviderRuntimeRecoveryService.test.ts
npm exec --yes --package=pnpm@11.10.0 -- pnpm exec vp test run packages/shared/src/orchestrationV2PendingBackgroundWork.test.ts apps/server/src/scheduledTasks/Schedule.test.ts apps/server/src/terminal/Manager.test.ts
```

Results: 36 tests passed across the first three files; 126 passed across the second three. These are upstream behavior checks, not proof of our complete rollout gate.

The draft read-only idle SQL ran against the actual fresh v2 database and returned eight zero counts for runs, runnable queues, requests, background items, provider background tasks, process-bound effects, dispatching schedules and imminent schedules. This proves schema compatibility and the empty-state case at this revision, not active-state detection. Its draft 15-minute schedule window is an unapproved candidate value.

Fourteen synthetic blocker cases then ran against an in-memory backup of the actual initialized schema: each active-run status, runnable versus held queues, pending requests, three background-item types, provider background tasks, process-bound effects, schedule dispatch and due schedules. All assertions passed. These rows were synthetic; they do not prove providers populate every field as expected during real work.

An actual T3 terminal was opened through WebSocket RPC on the staged runtime, sent `sleep 4`, then closed. The draft descendant-process check reported zero for the idle shell, one while the command ran and zero after completion. Follow-up nested `sh -c "sleep 4"` and background `sleep 4 &` commands each reported one busy terminal, then returned to zero. Process-observation failure handling remains implementation work.

After explicit authorization, a real OpenCode task ran through CLIProxy in a disposable repository and isolated temporary home. It used the maintained model plugin and MCP registration helper, with runtime-only inherited proxy/hub credentials and no GitHub credential. The task requested only `kubernetes-epaflix_namespaces_list`; the probe approved that exact read-only tool once through `runtime-request.respond`. The run completed and the assistant returned `PROBE-OK`. Projection observations included `starting`, `running`, `waiting`, `completed`, a running dynamic tool and a pending approval that later completed. This directly validates one MCP hub service through the v2 OpenCode adapter, not every service or the Codex adapter.

The first provider probe attempted to register the already auto-bootstrapped working directory and received `invalid_command`; it was corrected to use the existing project from the HTTP snapshot. A subsequent invalid `creationSource: desktop` failed schema validation; the defined value `web` succeeded. Initial runs stopped at the read-only tool's approval until that specific approval flow was added. These were probe setup errors, not evidence of upstream incompatibility.

A separate real Codex turn used the existing proxy launch-argument pattern and `codex/codex-auto-review`. It completed with `PROBE-OK` and projected `starting`, `running`, `completed`. A follow-up configured Codex MCP through the existing hub helper and requested the same read-only namespace-list call. Its dynamic tool completed without failure and the assistant returned `PROBE-OK`; the draft gate counted background tool activity and returned idle afterward. This establishes one hub-call path through both enabled providers.

Malformed JSON and a missing activity table caused the draft query to error in the actual-schema fixture, rather than return idle. The production collector must translate those errors into blocked/unknown. Complete combined gate evaluation and remaining MCP/provider tool integrations still need validation. The imminent-schedule margin remains dependent on measured restart time.

The draft SQL was also sampled during subsequent real Codex and OpenCode turns. It counted active runs and pending process-bound effects, then the OpenCode tool/approval request, and returned eight zero counts after each completed turn. The client-count and terminal observations were separately validated. Combining those observations into one fail-closed gate is still implementation work, not a finished gate.

Spyros selected a 15-minute sustained disconnected/idle interval. Any blocker or failed observation resets it. The imminent-schedule window must cover this interval plus measured restart time; the draft 15-minute SQL window alone does not establish that margin.

## GitOps promotion

GitHub main protection requires `validate` and `no-merge-commits`, strict checks and enforcement for admins. Auto-merge is enabled; squash and rebase merge are disabled. Actions default permissions are read-only and Actions cannot approve reviews. Spyros selected bot PRs plus auto-merge for validated v2 image bumps. The bot credential, CI-triggering behavior and compatible merge method still need validation. A scheduled workflow must not assume it can push directly to main. No workflow or branch protection was changed.

Spyros selected reusing the Renovate token. A read-only GitHub repository request using that token, consumed directly from its Kubernetes secret without printing it, reported admin/maintain/push permissions. GitHub returned no classic OAuth scope header. This confirms repository visibility/role, not every token permission or workflow-trigger capability. Installing a hosted workflow secret and performing a bot PR/auto-merge test remain separate authorized operations. Only merge commits are currently enabled at repository level; the required no-merge check examines commits on the PR branch, not the eventual integration commit.

After separate authorization, the existing Renovate token was installed as repository Actions secret `T3_V2_BOT_TOKEN`. `gh secret list` confirmed its metadata at 2026-09-30T17:53:58Z. No token value was printed or committed. Bot PR/CI trigger and merge behavior remain untested.

Spyros authorized the internal DNS entry, but SSH-key retrieval for Pi-hole was blocked by CC Safety Net rule `secret.basename.id-ed25519`. The tool forbade retries or workarounds; no retry occurred and DNS was not changed. Spyros selected reviewing the guard separately. DNS remains a deployment prerequisite until a permitted access path is established. Repository guidance places records in tracked dnsmasq `address=` lines plus corresponding unbound static zones, not Pi-hole API host entries.

Spyros selected limited root-disk placement rather than larger storage or disk expansion. A small initial allocation needs explicit free-space checks and cache/image/backup retention before deployment. This does not authorize cleanup of existing node data. Earlier free-space readings are time-sensitive and must be repeated before allocating storage.

The selected initial home claim is 10 GiB, subject to a fresh measured capacity check leaving at least 8 GiB free after runtime images, initial clones and backup allowance. This is sizing metadata, not a local-path quota. Retention should keep the current and prior v2 images and bounded recovery backups; cleanup must be scoped to v2-owned artifacts and never delete other workloads' data. Exact enforcement and backup bounds belong in the readiness addendum.

Spyros selected a selective SOPS-encrypted provisioning bundle for private tool configuration/authentication, alongside existing runtime secret mounts and the maintained private instruction bundle. Exclude histories, provider session databases, logs and full-home archives. Define the allowlist and refresh policy before collecting encrypted values.

Configuration refresh must preserve v2 user edits: seed private configuration once, then manage only explicitly owned keys/files and report conflicts. Authentication caches remain writable. Spyros selected two pre-upgrade recovery backups, subject to measured size and the 8 GiB floor. Retain only v2-owned copies and block upgrades when both recoverability and free-space requirements cannot be met.

## Readiness gate

- P1. Finish secure selective provisioning dependency inventory and verify config/skill sources, not just file presence.
- P2. Test a minimal, lock-preserving production artifact. Source and full-tree container boot have passed.
- P3. Prove the combined activity gate, monitoring credential lifecycle and project registration on an isolated runtime.
- P4. Resolve internal DNS, storage headroom and image/backup retention.
- P5. Validate the selected bot-PR/auto-merge mechanism and credential permissions.
- P6. Review the exact implementation addendum before Task 3. Native Arch packaging remains a separate pending decision.

## Evidence locations

The disposable probe is `/tmp/opencode/t3-v2-probe.py`; it creates and removes only its own temporary home. The source checkout and installed build dependencies remain at `/tmp/opencode/t3code-src`. The successful web-build log is in the harness output file `tool_0f285f6b7001V2Z1IiDz4uTzN4`. Keep these until the readiness investigation is reviewed; they are not production scripts.
