# Discord agents for the family

## Intent

Family members want to ask for things in
plain language instead of opening several apps or waiting for Spyros. A
private Discord server holds one channel per job. In each channel an AI
agent acts with only the rights that channel needs: media requests and
deletes in `#media`, changes to the Windmill workspace in `#windmill`.

We reuse OpenClaw for the Discord side, CLIProxy for the models, Discord
roles that Spyros assigns for who may do what, GitHub CI and Argo CD for
what reaches the cluster, and Alloy and Loki for logs. The new code is
the CI path check and the post-merge Windmill push.

Research and sources: `docs/research/2026-11-09-discord-agent.md`.

## Goals

- G1. A private Discord server with two locked channels. Only people
  Spyros invites join. Spyros owns the server.
- G2. `#media`: people with the `media` role ask the agent to request, add,
  find and delete movies, TV, anime and music, through the apps' HTTP
  APIs: Seerr, Radarr, Sonarr, Lidarr, Bazarr, Jellyfin. Lidarr covers
  music; Navidrome is out (no admin access to create its user).
  Deletes run without asking Spyros.
- G3. `#windmill`: people with the `windmill` role ask the agent to change or
  extend the Windmill workspace. The agent opens a PR. It merges on its
  own when CI passes and reaches Windmill without Spyros.
- G4. Spyros assigns the `media` and `windmill` roles in Discord.
  Removing a role removes access at once, because each Gateway checks the
  role ID on every message.
- G5. Every request is traceable in Grafana through Loki: who asked, in
  which channel, which model, which commands and API calls ran, and the
  result.

## Non-goals

- No ops or admin agent. No agent gets Kubernetes API rights.
- No public server and no open channels.
- No changes to bread, deal-finder or any other app from Discord.
- No infra changes from `#windmill`: no manifests, Secrets, RBAC, CI
  workflows, network policies or Argo Applications.
- No repo or Kubernetes changes from `#media`.
- No third-party OpenClaw skills or plugins. Only the first-party Discord
  and SearXNG plugins load (`plugins.allow`), with the skill the Discord
  plugin ships.
- No access to Gmail, Drive or any MCP hub path.
- No voice channels, no Discord Activities.

## Anti-goals

These would make it a failure even if it works:

- A1. One agent, token or pod that serves both channels. A prompt
  injection in `#media` must not reach GitHub.
- A2. Any agent that can reach the Kubernetes API, the work namespaces or
  another app's credentials.
- A3. A `#windmill` change that edits files outside its allowlist and
  still merges.
- A4. Secrets in the public repo, in Discord messages or in logs.
- A5. Anyone but Spyros assigning a channel role. A Discord role is the
  only gate, so only Spyros assigns them.

## Constraints

- The repo is public. Bot PRs, branch names and commit text are public.
- No Docker in the cluster. OpenClaw's sandbox backends all need Docker,
  Podman, SSH, OpenShell or Crabbox, so sandbox stays off and the pod is
  the boundary.
- OpenClaw's trust model says approvals "are guardrails for operator
  intent, not hostile multi-tenant isolation", and it has hundreds of
  published advisories, many of them approval and allowlist bypasses.
  Every limit that matters must hold even if OpenClaw's checks fail:
  credentials held, no ServiceAccount token, NetworkPolicy, GitHub rules
  and CI.
- OpenClaw needs one Discord bot token per Gateway, `replicas: 1`,
  `Recreate`, and a PVC on block storage with a local filesystem.
  `truenas-iscsi` fits.
- OpenClaw seeds `openclaw.json` from a ConfigMap only when the file is
  missing; after that the PVC copy wins.
- In-cluster *arr APIs use plain HTTP. OpenClaw's secret proxy fills in
  secrets on HTTPS calls only, so the media agent can read its API keys.
- Radarr, Sonarr and Lidarr web pages allow only `authentik Admins` today.
  The media agent deliberately gives holders of the `media` role request
  and delete rights there through Discord.
- Lidarr's NetworkPolicy admits only Traefik and named servarr pods. Each
  media app's ingress policy must admit the media agent's pod.
- GitHub ruleset `owner-only` blocks creating any branch except `main` for
  everyone but the repo Admin role. Ruleset `PR required` allows only merge
  commits and requires 0 reviews.
- Windmill's NetworkPolicy lets only Traefik reach `windmill-app`, behind
  Authentik forward auth.
- Secrets reach the cluster as SOPS files loaded through ksops. Tokens are
  created by Spyros and stored in the vault first.
- Argo CD owns the manifests. Changes reach the cluster by merged PR.

## Approaches

- Chosen: OpenClaw, one Gateway per channel, each in its own namespace
  with its own Discord application, Secrets and NetworkPolicy, and no
  ServiceAccount token. OpenClaw gives Discord allowlists, conversation
  history and an audit ledger. Kubernetes and GitHub give the real limits.
- Rejected: a small Discord bot of our own in front of pi in RPC mode.
  Same limits, but we would write and maintain the Discord gating,
  history and audit ourselves.
- Rejected: one shared OpenClaw for both channels, by A1.
- Rejected: Hermes Agent (no Kubernetes guide), NanoClaw (Docker only),
  Botkube (not an AI agent, last release 2024-11).
- Dropped: role sync from Authentik groups to Discord roles was built,
  then removed by Spyros's choice. It was simpler for him to assign the
  roles himself.

## Design

### Shape

| | media | windmill |
| --- | --- | --- |
| Namespace | `discord-media` | `discord-windmill` |
| Discord channel | `#media` | `#windmill` |
| Discord role | `media` | `windmill` |
| Model via CLIProxy | Claude Haiku 5.5 | Claude Opus 5.5 |
| Holds | Seerr, Radarr, Sonarr, Lidarr, Bazarr, Jellyfin API keys | GitHub App key, installed on epaflix only |
| Egress | DNS, CLIProxy, SearXNG, the media apps, public 443 | DNS, CLIProxy, SearXNG, public 443 |

Each Gateway reaches Discord and GitHub through public internet on 443
(private ranges excluded); NetworkPolicy cannot filter by hostname.

Both: our image built on the official OpenClaw image, pinned by digest,
sandbox off, `automountServiceAccountToken: false`, only the first-party
Discord and SearXNG plugins (`plugins.allow`), SearXNG pointed at
in-cluster SearXNG, Discord
`groupPolicy: allowlist` with one channel entry keyed by channel ID that
also allows only the channel's role ID, DMs off, role and moderation
actions off, `requireMention: false` in its own channel, no exec
approvals. Neither Discord bot has Manage Roles or Administrator.

### Models

Each Gateway gets its own CLIProxy client key, so cost and usage show per
channel. CLIProxy supports several client keys through its management API
(`2-k3s/13.t3code-work/tools/provision.py`). Model IDs come from the
CLIProxy catalogue at build time. Provider format is Anthropic messages
against `http://cliproxy.remote-pi.svc.cluster.local:8317`. No usage cap
at first; `DiscordModelUsageHigh` fires when one key passes 300 requests
in 24 hours (see Audit and logs).

### Access

Spyros gives the `media` or `windmill` role by hand in Discord and
removes it to revoke access. Each Gateway allows only its channel's role
ID. Authentik does not control the bots: removing someone from Authentik
does not remove their Discord access.

### Media

- The agent calls the apps' APIs with `curl` through OpenClaw's exec tool.
  The workspace `AGENTS.md` lists each app's base URL and the calls the
  agent may use. API keys come from env vars set from the namespace
  Secret.
- NetworkPolicy egress allows only the targets in the table. Each media
  app's ingress policy admits the `discord-media` pod on its API port.
- Seerr requests use Seerr's admin API key, an accepted risk: the agent
  could change Seerr settings. Who asked is in the audit log.
- Radarr, Sonarr and Lidarr get the Recycling Bin on, with 7 days of
  cleanup, so a wrong delete can be undone for a week.

### Windmill

- A GitHub App, `homelab-windmill-bot`, installed on epaflix only, with
  contents and pull requests write, checks, commit statuses and actions
  read (to see why CI failed), and no workflows permission, so
  GitHub rejects any push from it that changes `.github/workflows/`. Its
  PRs carry its own author, so CI knows a PR came from the bot.
- OpenClaw's GitHub App support only reads. A sidecar container holds the
  App key, mints a 1-hour installation token for epaflix every 30 minutes
  and shares only the token with the agent container.
- Ruleset `owner-only` excludes `bots/**`. The App gets no bypass, so it
  can push only `bots/...` branches and reaches `main` only through a PR.
- The agent works in a clone on its PVC, pushes a `bots/windmill/...`
  branch, opens a PR and turns on auto-merge.
- A job inside `ci.yml`, part of the required `validate` check, fails a
  bot PR if any changed path, old or new name, is outside
  `2-k3s/34.windmill/workspace/**`. It reads the file list from the GitHub
  API without checking out PR code, and the bot cannot edit `ci.yml`.
- A CronJob `windmill-deploy` in namespace `windmill`, every 10 minutes,
  downloads `main` and runs `wmill sync push --keep-deleted` for
  `2-k3s/34.windmill/workspace/f/` into workspace `screener`. It holds only
  the Windmill token, has no ServiceAccount token, and reaches only
  `windmill-app`, GitHub and npm. The Windmill CLI can run code and read
  files from what it pushes, so it must not run where other secrets live:
  the in-cluster GitHub runner holds homelab SSH keys. Code it runs reaches
  only the Windmill workspace, which the bot already controls through the
  scripts it merges. This replaces today's manual API update.
- A Windmill change that needs a new resource, secret or network access
  touches files outside the allowlist, so CI blocks the merge. The agent
  says so in the channel and leaves the PR for Spyros.

### Config ownership

The ConfigMap in Git is the source of truth. The init container copies
`openclaw.json` and `AGENTS.md` from the ConfigMap on every start,
overwriting the PVC copy, so changes made in OpenClaw's Control UI do not
survive a restart. Reloader restarts the pod when the ConfigMap or Secret
changes. The Control UI is not exposed outside the pod.

### Audit and logs

- OpenClaw audit ledger on with `logging.audit.messages` on. It records
  metadata (who asked, which run) in the pod's state database, read with
  `openclaw audit`; it does not reach Loki and holds no tool arguments.
- Logs to stdout as JSON. Alloy ships them to Loki, labelled by namespace.
  The build confirms which log lines carry the sender and the tool calls;
  if they do not, G5 in Loki narrows to what they carry and the README
  says so. CLIProxy's transcript sidecar already ships the model
  conversations, including tool calls, to Loki.
- Loki alerts: `DiscordAgentErrors`, `DiscordModelUsageHigh` and
  `WindmillDeployFailed`. Crash loops raise the cluster-wide
  `KubePodCrashLooping`. A bot PR blocked by the path check shows as a
  failed GitHub check.
- The two Discord keys have distinct prefixes listed in `CLIENT_BACKENDS`,
  so each transcript carries `client_backend` `discord-media` or
  `discord-windmill`. `DiscordModelUsageHigh` counts that parsed field per
  key over 24 hours and fires above 300.

### Owner steps

Spyros does these himself, from a list in the plan, no script:

1. Create the Discord server, the roles `media` and `windmill`, and the
   channels `#media` and `#windmill`, each visible only to its role, with
   View Channel denied for @everyone.
2. Create two Discord applications, the `media` and `windmill` bots, with
   Message Content Intent. Invite each to the server and store each token
   in the vault. Send the controller the two channel IDs and two role IDs.
3. Create the GitHub App `homelab-windmill-bot`, install it on epaflix and
   store its private key in the vault.
4. Give himself both roles.
5. Create a Windmill token for the post-merge job and store it in the
   vault. The agent encrypts it into `2-k3s/34.windmill/windmill-secrets.enc.yaml`.
6. Invite family members and give each the roles they need.

The agent then encrypts the tokens
into SOPS files and opens the PRs. The ruleset
change in C1 is a PR-free GitHub setting, done after Spyros confirms.

## Calls made in this design

- C1. `bots/**` excluded from `owner-only`; no bypass for the App.
- C2. Seerr requests use Seerr's admin API key (accepted risk).
- C3. No CLIProxy cap at first; the per-key usage alert `DiscordModelUsageHigh` instead.
- C4. No sync from Authentik; Spyros assigns Discord roles by hand (see
  Approaches).
- C5. Neither agent bot can grant roles.
- C6. `#media` role holders get deletes (Q8, assumed O12).

## Left to the builder

- Exact OpenClaw config keys, version and resource limits.
- The `AGENTS.md` text per channel, including the allowed API calls.
- How the post-merge Windmill job reaches Windmill past forward auth and
  the NetworkPolicy, for example from the in-cluster `gh-runner`.
- Alert thresholds.
