# Discord agents

A private Discord server with one AI agent per channel. Argo CD app `discord` deploys this directory.

| Channel | Who sees it | What the agent does |
| --- | --- | --- |
| `#media` | Discord role `media` | Uses the Seerr, Radarr, Sonarr, Lidarr, Bazarr and Jellyfin APIs. Finds and deletes movies, series and music, and fetches subtitles. Requests movies and series only through Seerr. Adds music in Lidarr. Deletes run without the owner's approval. Radarr, Sonarr and Lidarr keep deleted files in their recycling bin for 7 days. |
| `#windmill` | Discord role `windmill` | Changes the Windmill workspace. It pushes a `bots/windmill/...` branch, opens a PR and turns on auto-merge. CI fails any bot PR that touches files outside `2-k3s/34.windmill/workspace/`. After the merge, `windmill-deploy` pushes it to Windmill. `2-k3s/34.windmill/README.md` has the details. |

## Notifications channel

`#notifications` receives homelab alerts through one Discord webhook. No agent reads it. The notifications vault item holds the webhook URL as its password and the `@notifications` role ID in field `roleID`.

Loud messages mention `@notifications`: Alertmanager alerts firing with severity critical, every PVE notification, PBS notifications with severity warning, error or unknown, and script messages with priority `high` or `urgent`. Everything else posts without a mention. Members set the channel to "only @mentions".

Each sender keeps its own copy of the URL:

| Sender | Where the URL lives |
| --- | --- |
| Alertmanager | SOPS `2-k3s/10.observability/secrets/alertmanager-config-secret.enc.yaml`, receiver `discord`, with the role ID |
| Unpackerr | SOPS `2-k3s/08.servarr/_shared/secrets/unpackerr-secret.enc.yaml`, key `UN_WEBHOOK_0_URL` |
| PVE | Target `discord-pve` in `1-proxmox/pbs/notifications.cfg`. Secrets `id`, `token` and `role` in `/etc/pve/priv/notifications.cfg`. The vzdump body override `1-proxmox/pbs/notification-templates/vzdump-body.txt.hbs` goes to `/etc/pve/notification-templates/default/` |
| PBS | Targets `discord-pbs` (loud) and `discord-pbs-quiet` in `1-proxmox/pbs/server/notifications.cfg`. Secrets `id` and `token`, plus `role` on `discord-pbs`, in `/etc/proxmox-backup/notifications-priv.cfg`. The overrides `verify-err-body.txt.hbs` and `package-updates-body.txt.hbs` in `1-proxmox/pbs/server/notification-templates/` go to `/etc/proxmox-backup/notification-templates/default/` |
| TrueNAS | `0-truenas/scripts/keystatus-check.sh` reads the env file named by `DISCORD_ENV_FILE` (default `/root/.config/discord-webhook.env`). A root TrueNAS cron job (`midclt call cronjob.create`) runs it every 15 minutes with `DISCORD_ENV_FILE` set. Keep the script and env file on a non-encrypted pool path, because TrueNAS replaces the boot pool on upgrade |
| PBS guest | `1-proxmox/pbs/datastore-usage-alert.sh` reads `/root/.config/discord-webhook.env` |

Both env files are mode 600 and set `DISCORD_WEBHOOK_URL` and `DISCORD_ROLE_ID`.

Jellysweep still posts to ntfy.

## Access

The owner gives the `media` or `windmill` Discord role by hand in Discord, and removes it to revoke access. Each Gateway checks the role ID on every message, so removal takes effect at once. Authentik does not control the bots: removing someone from Authentik does not remove their Discord access.

## Namespaces

- `discord-media`: the `#media` OpenClaw Gateway in container `gateway`, its state PVC, Secret `openclaw` and a Reloader.
- `discord-windmill`: the `#windmill` Gateway in container `openclaw`, the `gh-token` sidecar that mints a 1-hour GitHub App token every 30 minutes, a clone of this repo on the PVC, Secrets `openclaw` and `github-app`, and a Reloader.

Neither Gateway has a ServiceAccount token. A Gateway's NetworkPolicy allows DNS, CLIProxy, SearXNG, for `#media` the media apps, and any public address on 443 with private ranges excluded, which is how it reaches Discord and GitHub. NetworkPolicy cannot filter by hostname.

## Config

On every start the `render-config` init container copies `openclaw.json` and `AGENTS.md` from the ConfigMap over the PVC copy, so Git always wins. Changes made in OpenClaw's Control UI do not survive a restart. A change in Git renames the ConfigMap, so the Deployment rolls on its own. To reseed without a change, delete the pod.

## Logs and audit

- Gateways: `{namespace="discord-media"}` and `{namespace="discord-windmill"}`. OpenClaw prints one JSON object per line; add `| json`.
- Model conversations, including tool calls: CLIProxy transcripts. Each line is a timestamp and a JSON record, so strip the timestamp before `| json`. One agent's requests: ``{namespace="remote-pi", container="transcripts"} | pattern `<_> <record>` | line_format `{{.record}}` | json | client_backend="discord-media"``, or `discord-windmill`. The labels come from the key prefixes `dc-media-` and `dc-windmill-` (`CLIENT_BACKENDS` in `2-k3s/17.remote-pi/cliproxy/files/payload_lib.py`), so a new key must keep its prefix.
- Audit ledger: `kubectl -n discord-media exec deploy/openclaw -c gateway -- openclaw audit`. In `discord-windmill`, use `-c openclaw`. It records who asked and which run, as metadata only, in the pod's state database. It does not reach Loki.

Alerts: `DiscordAgentErrors` at 5 or more error lines in 15 minutes, `DiscordModelUsageHigh` when one agent's CLIProxy key makes more than 300 requests in 24 hours, and `WindmillDeployFailed` in `2-k3s/10.observability/alertmanager-config/loki-log-alerts.yaml`. Crash loops raise the cluster-wide `KubePodCrashLooping`. A bot PR blocked by the path check shows as a failed `validate` check on GitHub, which notifies the owner; it raises no Loki alert, because that CI job runs on GitHub's runners.

## Accepted risks

- Each agent can read every secret in its own pod, including its Discord bot token and CLIProxy key, because OpenClaw's exec tool sees the Gateway's environment and files. The NetworkPolicy does not stop it: an agent can send what it holds to any public HTTPS host. The limit that matters is what each pod holds.
- The Seerr key is Seerr's admin key. The agent could change Seerr settings or read the Radarr, Sonarr and Jellyfin keys stored there, which it already holds.
- The `#windmill` agent can read its 1-hour GitHub token. The token works only on this repo, rulesets let it push only `bots/` branches, and the CI path check fails its PRs outside the Windmill workspace.
- Code the bot merges runs inside `windmill-deploy`, so it could copy the Windmill deploy token into the screener workspace. That is why the token belongs to a non-admin user with write access to `f/screener` only.

## Rotating a secret

Store the new value in the vault first. Write it into the SOPS file with `sops set` inside `vault run`, without printing it, then merge the PR. Reloader restarts the Gateway when its Secret changes; the `windmill-deploy` CronJob reads its Secret on the next run. Revoke the old value last.

| Secret | File | Keys |
| --- | --- | --- |
| `discord-media/openclaw` | `media/secrets.enc.yaml` | `DISCORD_BOT_TOKEN`, `CLIPROXY_API_KEY`, `SEERR_API_KEY`, `RADARR_API_KEY`, `SONARR_API_KEY`, `LIDARR_API_KEY`, `BAZARR_API_KEY`, `JELLYFIN_API_KEY`, plus the Discord IDs |
| `discord-windmill/openclaw` | `windmill/secrets.enc.yaml` | `DISCORD_BOT_TOKEN`, `CLIPROXY_API_KEY`, plus the Discord IDs |
| `discord-windmill/github-app` | `windmill/github-app.enc.yaml` | `app-id`, `installation-id`, `private-key` |
| `windmill/windmill-deploy` | `2-k3s/34.windmill/windmill-deploy.enc.yaml` | `WINDMILL_TOKEN` |
| `#notifications` webhook | Every place in the sender table above | Webhook URL and role ID |

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

1. Create the Discord server, the roles `media` and `windmill`, and the channels `#media` and `#windmill`, each visible only to its role, with View Channel denied for @everyone. Create two Discord applications, the `media` and `windmill` bots, with Message Content Intent. Neither bot gets Manage Roles or Administrator. Store both tokens in the vault, and send the two channel IDs and two role IDs to the agent.
2. Create the Windmill GitHub App with contents and pull requests write, and checks, commit statuses and actions read. Give it no workflows or administration permission. Install it on this repo only and store its private key in the vault.
3. In Windmill workspace `screener`, create a token for a non-admin user with write access to `f/screener` only, and store it in the vault.
4. Give yourself both roles, then invite family members and give each the roles they need.

## Follow-ups

- After the first rollout, check which Gateway JSON lines carry the Discord sender and the tool calls, and update the Logs section.
