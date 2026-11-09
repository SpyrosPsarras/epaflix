# Discord agent for the homelab: tool choice

Researched 2026-11-09. Sources are official docs, source files, GitHub release lists and GitHub Security Advisories, fetched on that date. Star counts and advisory counts come from the GitHub REST API on that date and will drift.

## 1. Verdict

Use OpenClaw, with one Gateway Deployment per tier (media, sites, ops). Each Deployment gets its own namespace, Discord bot application, ServiceAccount, Secrets and NetworkPolicy. Do not run one shared process. OpenClaw's own trust model says one Gateway is one trust boundary and that exec approvals "are guardrails for operator intent, not hostile multi-tenant isolation" ([trust model](https://docs.openclaw.ai/gateway/security/trust-model)). Its advisory history backs that up: 722 published advisories, many of them bypasses of exec allowlists and approvals. So the real boundary has to be the Kubernetes pod and the credentials it holds, and OpenClaw only supplies the Discord front end, routing, approval buttons and audit. OpenClaw beats the alternatives here because it is the only candidate with official Kubernetes manifests, role-ID allowlists and role-based routing, allowlist-plus-ask exec approvals with Discord buttons, and a built-in audit ledger. Hermes Agent comes a close second.

## 2. Comparison

| Candidate | Discord channel/role gating | Per-channel routing | Discord approval buttons | Custom model endpoint | Sandbox in k8s without Docker | Secrets handling | Audit logging | Maturity | Known security issues |
|---|---|---|---|---|---|---|---|---|---|
| OpenClaw | Guild, channel, user-ID and role-ID allowlists; channel map is an allowlist | Yes: `bindings[]` by peer/channel/guild/role, one Gateway can host many agents | Yes: `execApprovals` with approver list, DM or channel target | Yes: `models.providers.*.baseUrl` with `openai-completions` or `anthropic-messages` | No in-pod sandbox. Backends are docker, podman, ssh, openshell, crabbox. Run with sandbox off and use the pod as the boundary, or SSH to a separate pod | SecretRefs (env, file, store, Vault, 1Password); store is unencrypted SQLite; egress proxy substitutes secrets on HTTPS only | Metadata-only audit ledger (`openclaw audit`), file logs, OpenTelemetry export | ~394k stars, MIT, several stable releases per week, official image `ghcr.io/openclaw/openclaw` | 722 GHSAs (14 critical, 249 high), most recent batch 2026-09-11, many in exec approvals/allowlists |
| NanoClaw | Users and roles live in NanoClaw's own DB; Discord adapter installed by a skill. Discord role gating not found | Channel to agent-group wiring | Yes for credential-gateway holds and package installs, sent to an approver DM. Not a general shell-command gate | Anthropic-compatible `ANTHROPIC_BASE_URL`; others via OpenCode/Ollama skills | No. Only a Docker driver exists; runtime binary hard-coded to `docker` | Credential gateway (OneCLI Agent Vault) injects keys at request time; agent never holds keys | Not verified | ~31k stars, MIT, monthly-ish releases, customise-by-forking model | 9 GHSAs (all 2026-10-13), 6 high |
| Hermes Agent | `DISCORD_ALLOWED_USERS`, `DISCORD_ALLOWED_ROLES`, `DISCORD_ALLOWED_CHANNELS`, ignored channels | One bot per profile; run many profile gateways. Per-channel prompts only inside one profile | Yes: dangerous-command approvals as Discord buttons; `approvals.approvers` list | Yes: "Custom endpoint", any OpenAI-compatible URL | `local` backend runs in the pod. Docker/Modal/SSH/Daytona/Vercel/singularity plugin otherwise. No Kubernetes backend found | `.env` per profile; secret-source plugins for Bitwarden, 1Password, command | Session DB and hooks; dedicated audit ledger not found (unverified) | ~262k stars, MIT, releases every few days, image `nousresearch/hermes-agent` | 0 published GHSAs. SECURITY.md says only OS isolation is a boundary |
| nanobot (HKUDS) | `allowFrom` user IDs and `allowChannels`; no role gating found | Not found | Not found (pairing approval only) | Not verified in detail | `bwrap` exec sandbox on Linux; whether it works in an unprivileged pod is unverified | Env vars / Docker secret | Not found | ~50k stars, MIT, created 2026-02, releases every few weeks | 8 GHSAs (2 critical, 6 high), 3 published 2026-11-08 |
| Botkube | Discord supported | Per-channel bindings (not verified in detail) | Not verified | Not an LLM agent | Runs in-cluster by design | Kubernetes Secrets | Not verified | ~2.3k stars, last release v1.14.0 on 2024-11-13, last push 2024-12-11 | Not checked; project looks inactive |
| Thin Discord bot + pi (RPC/SDK) | You write it | You write it | pi extension `ctx.ui.confirm` becomes an RPC `extension_ui_request`; your bot renders the button | Yes: `providers.<id>.baseUrl` + `api` | pi has no sandbox of its own; run the whole process in the pod | Whatever you build | pi session files and RPC event stream; audit is yours to build | pi ~121k stars, MIT; the bot is custom code | Custom code risk; pi says it "does not ask for approval before every tool call" |

## 3. Candidate notes

### 3.1 OpenClaw

Discord gating and routing:

- `channels.discord.groupPolicy: "allowlist"` requires the guild to match `channels.discord.guilds`. Per guild you can set `users` and `roles` (role IDs only). Adding any `channels` entry turns the map into an allowlist. Name matching is off by default ([Discord access control](https://docs.openclaw.ai/channels/discord/access-control)).
- `bindings[].match.roles` routes guild members to different agents by role ID ([same page, Role-based agent routing](https://docs.openclaw.ai/channels/discord/access-control)). Bindings map channel accounts and peers to agents, and each agent has its own workspace, `agentDir`, auth profiles and session store ([multi-agent routing](https://docs.openclaw.ai/concepts/multi-agent)).
- Channel entries can carry tool policy, and channel ID entries bind tool policy to that ID ([Discord access control](https://docs.openclaw.ai/channels/discord/access-control)).
- Two accounts with the same bot token start only one monitor ([Discord setup](https://docs.openclaw.ai/channels/discord/setup)). So each tier Gateway needs its own Discord application and token.

Approvals:

- Discord exec approvals: `channels.discord.execApprovals.enabled`, `.approvers`, `.target` (`dm`, `channel`, `both`). Only resolved approvers can press the buttons; others get an ephemeral denial. Approvals expire after 30 minutes by default ([Discord components and approvals](https://docs.openclaw.ai/channels/discord/rich-messages)).
- Exec approvals combine policy, allowlist and optional ask. With no approval UI available, the ask fallback defaults to `deny`. They are "not a per-user auth boundary". The trusted single-operator default is `security="full"`, `ask="off"`, so you must set this explicitly ([exec approvals](https://docs.openclaw.ai/tools/exec-approvals), [trust model](https://docs.openclaw.ai/gateway/security/trust-model)).
- Approvals are per command, not per "destructive" class. For ops, the practical setup is an allowlist of read-only commands with ask on miss. That setup is inferred from the docs and not tested.

Tool policy:

- `tools.profile` (`minimal`, `coding`, `messaging`, `full`) plus `tools.allow`/`tools.deny`, per-agent `agents.entries.*.tools`, `tools.byProvider` and `tools.toolsBySender` ([tool policy](https://docs.openclaw.ai/gateway/config-tools/tool-policy)).
- By default, session tools reach across the whole Gateway: `tools.sessions.visibility` is `all` and `tools.agentToAgent.enabled` is `true` ([trust model](https://docs.openclaw.ai/gateway/security/trust-model)). That is another reason to run one Gateway per tier.

Sandbox and Kubernetes:

- Backends: `docker` (default), `podman`, `ssh`, `openshell`, `crabbox`. Mode defaults to `off` ([modes, scope and backend](https://docs.openclaw.ai/gateway/sandboxing/modes-scope-and-backend)). SSH needs a reachable host with `/bin/sh`, `python3`, GNU `stat`/`readlink` ([SSH backend](https://docs.openclaw.ai/gateway/sandboxing/ssh-backend)). OpenShell delegates to an NVIDIA OpenShell gateway that uses Docker, Podman or virtualization ([OpenShell](https://docs.openclaw.ai/gateway/openshell)). Crabbox leases cloud machines, initially Daytona ([Crabbox backend](https://docs.openclaw.ai/gateway/sandboxing/crabbox-backend)). None of these is a Kubernetes-native sandbox.
- Official Kustomize manifests: single-pod Deployment, `replicas: 1`, `Recreate`, PVC on local or block storage (not NFS/SMB), `readOnlyRootFilesystem`, `drop: ALL`, UID 1000, no cluster-scoped resources. The page calls itself "a minimal starting point ... not a production-ready deployment". There is no Helm chart ([Kubernetes](https://docs.openclaw.ai/install/kubernetes)).
- The init container seeds `openclaw.json` only when missing, and the PVC copy wins after first boot. That clashes with GitOps unless you reseed on purpose ([Kubernetes](https://docs.openclaw.ai/install/kubernetes)).
- Images: `ghcr.io/openclaw/openclaw`, version tags, `latest`, `extended-stable` and `-slim` variants ([Docker install](https://docs.openclaw.ai/install/docker)).

Models, secrets, audit:

- Custom provider: `baseUrl` plus `api` (`openai-completions`, `openai-responses`, `anthropic-messages`, and others). Setting a custom `baseUrl` also allows that exact origin through the guarded fetch path ([custom providers](https://docs.openclaw.ai/gateway/config-tools/custom-providers)).
- Shared secret store: `secret` entries are write-only and never injected into subprocess env. `env` entries are agent-readable. The store is not encrypted at rest. The egress proxy substitutes secrets only for named HTTPS hosts and refuses to put secrets on plain HTTP ([secret store and egress proxy](https://docs.openclaw.ai/gateway/secrets/secret-store-and-egress)). In-cluster *arr APIs over plain HTTP will not benefit, so the media pod's agent will be able to read those API keys.
- Audit ledger: on by default, metadata only (no prompts, arguments or output). Message metadata is off by default (`logging.audit.messages`). File logs do not mask tool arguments or transcripts. OTel export is off by default ([audit history](https://docs.openclaw.ai/gateway/audit), [observability config](https://docs.openclaw.ai/gateway/config-observability)). "Who asked what" therefore needs `audit.messages` on plus the session transcripts. "What ran" comes from the ledger and OTel spans.

Maturity and advisories:

- Repo `openclaw/openclaw`: 394,246 stars, MIT, created 2025-11-24. Stable releases v2026.11.2 (2026-11-08), v2026.11.1 (2026-11-05), v2026.10.10 (2026-11-03), plus betas ([GitHub API repo](https://api.github.com/repos/openclaw/openclaw), [releases](https://github.com/openclaw/openclaw/releases)).
- [Security advisories](https://github.com/openclaw/openclaw/security/advisories): 722 published (14 critical, 249 high, 390 medium, 69 low), 39 with a CVE ID. By month: Feb 220, Mar 204, Apr 108, May 67, Jun 45, Sep 75. Recent examples relevant here: GHSA-74gc-hg2m-79p9 "Reusable exec approvals could authorize changed arguments", GHSA-ghpx-6xwq-2w4w "Exec wrapper allowlists could trust arbitrary inner commands", GHSA-35wj-hm2r-ghgw "Discord guild reads could skip target allowlists" (all 2026-09-11), GHSA-3pmr-x9g8-m55r "Discord guild actions could skip cross-provider requester authorization" (2026-06-30).

### 3.2 NanoClaw

- Repo moved from `qwibitai/nanoclaw` to `nanocoai/nanoclaw`: 31,099 stars, MIT, created 2026-01-31. Releases include v2026.10.0 (2026-10-09) and v2026.11.0-beta.1 (2026-11-09) ([GitHub API](https://api.github.com/repos/nanocoai/nanoclaw), [releases](https://github.com/nanocoai/nanoclaw/releases)).
- Requires Docker. One host process spawns per-session agent containers ([README](https://github.com/nanocoai/nanoclaw/blob/main/README.md)). `src/drivers/` contains only `docker-driver.ts`, and `CONTAINER_RUNTIME_BIN = 'docker'` ([container-runtime.ts](https://github.com/nanocoai/nanoclaw/blob/main/src/container-runtime.ts)). It does not fit a cluster with no Docker daemon.
- "Customization = code changes", with no config files ([README](https://github.com/nanocoai/nanoclaw/blob/main/README.md)). That works against "avoid building it myself".
- Approvals cover agent self-modification (`install_packages`, `add_mcp_server`) and credential-gateway holds, sent to an admin DM ([approvals module](https://github.com/nanocoai/nanoclaw/blob/main/src/modules/approvals/project.md), [SECURITY.md](https://github.com/nanocoai/nanoclaw/blob/main/docs/SECURITY.md)).
- Discord comes from a skill copying the adapter in from the `channels` branch ([add-discord skill](https://github.com/nanocoai/nanoclaw/blob/main/.claude/skills/add-discord/SKILL.md)).
- [Advisories](https://github.com/nanocoai/nanoclaw/security/advisories): 9, all published 2026-10-13, 6 high (path traversal, symlink following, an installer fetched over plain HTTP).

### 3.3 Hermes Agent (Nous Research)

- Discord: `DISCORD_ALLOWED_USERS`, `DISCORD_ALLOWED_ROLES` (OR semantics), `DISCORD_ALLOWED_CHANNELS`, `DISCORD_IGNORED_CHANNELS`. Deny-all if no allowlist is set. Per-user sessions in shared channels by default. Per-channel prompts ([Discord](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/discord)).
- Approval buttons on Discord for dangerous commands. `approvals.mode` is `smart` (an auxiliary LLM judges risk), `manual` or `off`, with an `approvals.approvers` list. `/yolo` toggles approvals off per session ([Security](https://hermes-agent.nousresearch.com/docs/user-guide/security)). The gate matches a list of dangerous patterns. It is not an allowlist.
- Separation: each profile has its own bot tokens, sessions and memory, and each can run as its own gateway ([Running many gateways](https://hermes-agent.nousresearch.com/docs/user-guide/multi-profile-gateways)).
- Terminal backends: local, docker, ssh, modal, daytona, vercel_sandbox, and the singularity and sprites plugins ([Tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools)). No Kubernetes backend or Kubernetes install guide found. There is an official image `nousresearch/hermes-agent` ([Docker](https://hermes-agent.nousresearch.com/docs/user-guide/docker)).
- Custom OpenAI-compatible endpoint via `hermes model` → "Custom endpoint" ([Providers](https://hermes-agent.nousresearch.com/docs/integrations/providers)).
- [SECURITY.md](https://github.com/NousResearch/hermes-agent/blob/main/SECURITY.md): "The only security boundary against an adversarial LLM is the operating system. Nothing inside the agent process constitutes containment ... not the approval gate ..." That matches the per-pod design.
- Repo: 262,030 stars, MIT. Releases v0.23.0 (2026-11-05), v0.22.7 (2026-11-03), v0.22.6 (2026-11-02). 0 published GHSAs via the API ([advisories](https://github.com/NousResearch/hermes-agent/security/advisories)). Zero advisories may mean fewer bugs or less disclosure through GHSA; I cannot tell which.

### 3.4 nanobot (HKUDS), brief

- Discord config: `allowFrom` (user IDs), `allowChannels`, `groupPolicy` ([chat apps](https://github.com/HKUDS/nanobot/blob/main/docs/chat-apps.md)). No role-ID gating and no approval buttons found.
- `tools.exec.sandbox: "bwrap"` on Linux ([SECURITY.md](https://github.com/HKUDS/nanobot/blob/main/SECURITY.md)). Whether bubblewrap works in an unprivileged k3s pod is unverified.
- 49,974 stars, created 2026-02-01, v0.4.1 on 2026-11-06. 8 advisories including 2 critical; 3 high published 2026-11-08, one titled "Sandbox escape through caller-selected shell executable" ([advisories](https://github.com/HKUDS/nanobot/security/advisories)).

### 3.5 Baselines, brief

- Botkube supports Discord and runs in-cluster ([README](https://github.com/kubeshop/botkube/blob/main/README.md)). Last release v1.14.0 on 2024-11-13, last push 2024-12-11 ([releases](https://github.com/kubeshop/botkube/releases)). It is not an LLM agent, and the project looks inactive. Not a fit.
- Thin bot + pi: pi RPC mode is a JSONL subprocess protocol ([rpc.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc.md)). Extensions can block tool calls and ask `ctx.ui.confirm` ([extensions.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)). In RPC mode, confirm becomes an `extension_ui_request` that the client answers ([rpc-extension-ui.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/rpc-extension-ui.md)). Custom providers take `baseUrl` + `api` ([models-configuration.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models-configuration.md)). pi says safety comes from OS or container limits, not from watching the transcript ([security.md](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md)). Content was read from the installed package v1.5.0. The upstream paths for `rpc.md` and `security.md` returned HTTP 200 on `main`; the others assume the same folder. This route means writing and maintaining the Discord gating, routing, approvals and audit yourself, which is what the owner wants to avoid.

### 3.6 Suggested shape (inference, not from a source)

- Three namespaces, each with one OpenClaw Deployment, its own Discord application, Gateway `groupPolicy: allowlist` and a single channel entry with the tier's role ID.
- media: tools limited to an exec wrapper or MCP server for the *arr/Jellyfin/Navidrome APIs, holding only those API keys. NetworkPolicy egress limited to those services, the model proxy and Discord.
- sites: a fine-grained GitHub token for the three repos only. The CI path check and branch protection are the real limit, not the agent.
- ops: ServiceAccount with the RBAC you choose, `execApprovals` with only the owner as approver, `ask: "on-miss"` with a read-only allowlist. Treat approvals as a guardrail, given the advisory history.
- All tiers: sandbox `off`, `tools.sessions.visibility` narrowed, `logging.audit.messages` on, OTel export to the cluster collector, image pinned by version tag.

## 4. Open questions

1. Q1: Whether OpenClaw approvals can gate MCP or plugin tool calls for the native (non-Codex) runtime. The MCP grant docs cover Codex runs only ([exec approvals, MCP tool grants](https://docs.openclaw.ai/tools/exec-approvals)). This only matters if media deletes need approval.
2. Q2: Whether OpenClaw's ConfigMap-seeded config can be run fully GitOps-managed without drift from Control UI or `doctor --fix` writes. The docs say the PVC copy wins after first boot.
3. Q3: Whether the Anthropic-compatible path through the in-cluster proxy works with OpenClaw's `anthropic-messages` adapter. Not tested.
4. Q4: Hermes audit logging beyond session storage and hooks. I did not find a dedicated audit ledger page.
5. Q5: Whether bubblewrap (nanobot) or any user-namespace sandbox runs in the k3s pods without extra privileges. Not tested.
6. Q6: The VentureBeat article (2026-08-31) was not read. Rules required primary sources, and NanoClaw was assessed from its repo.
7. Q7: SearXNG worked but was degraded: brave, duckduckgo and startpage reported rate limits or CAPTCHA, and results were noisy. Repos were located by exact name through the GitHub API.
