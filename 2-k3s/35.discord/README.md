# Discord agents

A private Discord server with one AI agent per channel. Argo CD app `discord` deploys this directory.

| Channel | Who sees it | What the agent does |
| --- | --- | --- |
| `#media` | Authentik group `Servarr users` | Finds, requests, adds and deletes movies, series and music, and fetches subtitles, through the Seerr, Radarr, Sonarr, Lidarr, Bazarr and Jellyfin APIs. Deletes run without the owner's approval. Radarr, Sonarr and Lidarr keep deleted files in their recycling bin for 7 days. |
| `#windmill` | Authentik group `Screener users` | Changes the Windmill workspace. It pushes a `bots/windmill/...` branch, opens a PR and turns on auto-merge. CI fails any bot PR that touches files outside `2-k3s/34.windmill/workspace/`. After the merge, `windmill-deploy` pushes it to Windmill. `2-k3s/34.windmill/README.md` has the details. |

## Access

Authentik decides access. To add someone, put them in the Authentik group, invite them to the Discord server, and have them link Discord in their Authentik account. To remove someone, take them out of the group or deactivate them. Never hand out the Discord roles by hand. The role sync removes any mapped role Authentik does not grant, within 10 minutes. Only direct group members count, not members of child groups. Members with no linked Authentik account get no role and raise `DiscordUnlinkedMember`; nobody is kicked automatically.

## Namespaces

- `discord-media`: the `#media` OpenClaw Gateway in container `gateway`, its state PVC, Secret `openclaw` and a Reloader.
- `discord-windmill`: the `#windmill` Gateway in container `openclaw`, the `gh-token` sidecar that mints a 1-hour GitHub App token every 30 minutes, a clone of this repo on the PVC, Secrets `openclaw` and `github-app`, and a Reloader.
- `discord-sync`: CronJob `role-sync`, every 10 minutes, which copies Authentik group membership to the Discord roles with its own Manage Roles bot. Secret `role-sync`.

Neither Gateway nor the role sync has a ServiceAccount token. A Gateway's NetworkPolicy allows DNS, CLIProxy, SearXNG, for `#media` the media apps, and any public address on 443 with private ranges excluded, which is how it reaches Discord and GitHub. NetworkPolicy cannot filter by hostname.

## Config

On every start the `render-config` init container copies `openclaw.json` and `AGENTS.md` from the ConfigMap over the PVC copy, so Git always wins. Changes made in OpenClaw's Control UI do not survive a restart. A change in Git renames the ConfigMap, so the Deployment rolls on its own. To reseed without a change, delete the pod.

## Logs and audit

- Gateways: `{namespace="discord-media"}` and `{namespace="discord-windmill"}`. OpenClaw prints one JSON object per line; add `| json`.
- Role sync: `{namespace="discord-sync"}`, one JSON line per event: `role_added`, `role_removed`, `unlinked_member`, `aborted`, `group_not_found`, `http_error`, `network_error`.
- Model conversations, including tool calls: CLIProxy transcripts, `{namespace="remote-pi", container="transcripts"}`.
- Audit ledger: `kubectl -n discord-media exec deploy/openclaw -c gateway -- openclaw audit`. In `discord-windmill`, use `-c openclaw`. It records who asked and which run, as metadata only, in the pod's state database. It does not reach Loki.

Alerts: `DiscordUnlinkedMember`, `DiscordRoleSyncAborted`, `DiscordAgentErrors` at 5 or more error lines in 15 minutes, and `WindmillDeployFailed` in `2-k3s/10.observability/alertmanager-config/loki-log-alerts.yaml`. Crash loops and failed role-sync Jobs raise the cluster-wide `KubePodCrashLooping` and `KubeJobFailedLastRun`. A bot PR blocked by the path check shows as a failed `validate` check on GitHub, which notifies the owner; it raises no Loki alert, because that CI job runs on GitHub's runners.

## Accepted risks

- Each agent can read every secret in its own pod, including its Discord bot token and CLIProxy key, because OpenClaw's exec tool sees the Gateway's environment and files. The NetworkPolicy does not stop it: an agent can send what it holds to any public HTTPS host. The limit that matters is what each pod holds.
- The Seerr key is Seerr's admin key. The agent could change Seerr settings or read the Radarr, Sonarr and Jellyfin keys stored there, which it already holds.
- The `#windmill` agent can read its 1-hour GitHub token. The token works only on this repo, rulesets let it push only `bots/` branches, and the CI path check fails its PRs outside the Windmill workspace.
- Code the bot merges runs inside `windmill-deploy`, so it could copy the Windmill deploy token into the screener workspace. That is why the token belongs to a non-admin user with write access to `f/screener` only.

## Rotating a secret

Store the new value in the vault first. Write it into the SOPS file with `sops set` inside `vault run`, without printing it, then merge the PR. Reloader restarts the Gateway when its Secret changes; the CronJobs read theirs on the next run. Revoke the old value last.

| Secret | File | Keys |
| --- | --- | --- |
| `discord-media/openclaw` | `media/secrets.enc.yaml` | `DISCORD_BOT_TOKEN`, `CLIPROXY_API_KEY`, `SEERR_API_KEY`, `RADARR_API_KEY`, `SONARR_API_KEY`, `LIDARR_API_KEY`, `BAZARR_API_KEY`, `JELLYFIN_API_KEY`, plus the Discord IDs |
| `discord-windmill/openclaw` | `windmill/secrets.enc.yaml` | `DISCORD_BOT_TOKEN`, `CLIPROXY_API_KEY`, plus the Discord IDs |
| `discord-windmill/github-app` | `windmill/github-app.enc.yaml` | `app-id`, `installation-id`, `private-key.pem` |
| `discord-sync/role-sync` | `role-sync/secrets.enc.yaml` | `DISCORD_TOKEN`, `AUTHENTIK_TOKEN`, `DISCORD_GUILD_ID`, `ROLE_MAP` |
| `windmill/windmill-deploy` | `2-k3s/34.windmill/windmill-secrets.enc.yaml` | `WINDMILL_TOKEN` |

A new `CLIPROXY_API_KEY` must be added to CLIProxy's client key list (a live change through its management API) before the SOPS edit, and the old one removed after the pod restarts.

Resetting a Discord bot token kills the old one at once, so that agent stays offline until Argo syncs the new one.

The media keys are copies, and they go stale when an app's key changes:

- Seerr: copied from Secret `servarr/seerr-config-seed`, which Seerr reads only on first start. Regenerating the key in Seerr's UI breaks the copy.
- Radarr and Sonarr: copied from Secret `servarr/unpackerr-secret`. The live keys are in each app's `config.xml`.
- Lidarr: copied from Secret `servarr/music-stack`, key `lidarr-api-key`.
- Bazarr: copied from Secret `servarr/bazarr-autotranslate-secret`, key `BAZARR_API_KEY`.

A stale key makes that app's calls fail with HTTP 401.

## Upgrading OpenClaw

Renovate opens one PR, group `openclaw-discord image`, that moves the base image and both `@openclaw` plugins in `images/openclaw-discord/` together. It never auto-merges. After the merge, `build-openclaw-discord.yml` pushes the image and prints its digest. Put that digest in `images:` of `media/kustomization.yaml` and `windmill/kustomization.yaml` in a second PR. If the new version rejects the config, the `render-config` init container fails and the agent stays down until it is fixed. The Deployments use `Recreate`, so the old pod is already gone.

## One-time owner setup

1. Create the Discord server, then three Discord applications: the `media` and `windmill` bots with Message Content Intent, and a `role-sync` bot with Server Members Intent, Manage Roles and Manage Channels. Neither agent bot gets Manage Roles or Administrator. Store the three tokens, and the `role-sync` OAuth2 client ID and secret for Authentik's Discord login, in the vault.
2. Create the Windmill GitHub App with contents and pull requests write, and checks, commit statuses and actions read. Give it no workflows or administration permission. Install it on this repo only and store its private key in the vault.
3. In Windmill workspace `screener`, create a token for a non-admin user with write access to `f/screener` only, and store it in the vault.
4. Link your own Discord account in Authentik, then invite family members.

## Follow-ups

- No per-key model usage alert yet. CLIProxy's own log does not say which client key made a request, and the transcripts label only keys whose first 4 characters are listed in `CLIENT_BACKENDS` in `2-k3s/17.remote-pi/cliproxy/files/payload_lib.py`. Both Discord keys start with `disc`, so their masked headers look the same. The fix is to reissue the two keys with distinct 4-character prefixes, add both to `CLIENT_BACKENDS`, then alert at more than 300 requests per key per day.
- After the first rollout, check which Gateway JSON lines carry the Discord sender and the tool calls, and update the Logs section.
