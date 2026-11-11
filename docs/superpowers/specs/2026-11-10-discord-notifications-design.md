# Discord notifications instead of ntfy

## Intent

Spyros has a Discord server now. Every homelab alert that goes to ntfy today
should arrive in one Discord channel, `#notifications`, and ntfy should be
removed. Long messages become a short useful summary with a link to where the
full log lives.

## Goals

- G1. Every sender that posts to ntfy today posts to the `#notifications`
  webhook instead:
  1. Alertmanager, receiver `ntfy` (secret `alertmanager-config-secret`).
  2. Unpackerr extract failures (`2-k3s/08.servarr/unpackerr/unpackerr.yaml`).
  3. PVE notification target `ntfy-pve` (`1-proxmox/pbs/notifications.cfg`).
  4. `1-proxmox/pbs/datastore-usage-alert.sh` on the PBS guest (LXC 1031).
  5. `0-truenas/scripts/keystatus-check.sh` on TrueNAS.
  6. PBS server webhook target `ntfy-pbs` in
     `/etc/proxmox-backup/notifications.cfg` on the PBS guest. It is not in
     Git today; PR 1 adds a repo copy without secrets, plus a `verify-err`
     body override that prints a count instead of the snapshot list.
  `keystatus-check.sh` is not installed on TrueNAS today. Spyros approved
  installing it as a 15-minute TrueNAS cron job during rollout.
- G2. No message is rejected for size. Discord limits plain message content to
  2000 characters and an embed description to 4096.
- G3. Every message ntfy made loud mentions the Discord role `@notifications`
  (`<@&ROLE_ID>`, with `allowed_mentions` limited to that role); everything
  else posts without a mention. Members of the role set the channel to "only
  @mentions" and keep the loud/quiet split. A role mention notifies only
  members who can see `#notifications`. Loud means: Alertmanager firing with severity critical
  (ntfy priority 5), every PVE notification (priority 4), and script messages
  sent as `high` or `urgent` (TrueNAS locked or unreadable, datastore 85% and
  95%, datastore unreadable). Recovery messages stay quiet. Builder's call
  from the current priorities.
- G4. ntfy is gone: Deployment, Service, IngressRoute, kustomization entries,
  Pi-hole and Unbound records, the CoreDNS comment, the `ntfy-canary-check`
  CronJob, alert `NtfyCanaryPublishFailing` and its tests, and ntfy mentions in
  docs and test hooks.

## Non-goals

- No relay service between senders and Discord.
- No shipping of PVE logs to Loki.
- No Argo CD notifications (they are disabled today and stay disabled).
- No change to which alerts fire or how Alertmanager groups and repeats them.
- No alarm outside Discord for a broken webhook. Spyros accepts that if the
  webhook breaks, notifications stop until he notices. The ntfy canary goes
  without a replacement.

## Anti-goals

- A sender that fails silently. #1076 ran twelve days of failed backups with a
  dead notification path. Any sender change that can drop a message without a
  trace is wrong.
- The webhook URL or the `@notifications` role ID in Git as plain text. The repo is
  public.

## Constraints and facts

- The webhook URL is a secret. It contains the webhook token.
- Alertmanager runs v0.34.2. Its `discord_configs` supports `webhook_url_file`,
  `title`, `message`, `content`, `username` and `avatar_url`. It truncates the
  title to 256, the description to 4096 and content to 2000 runes itself
  (`notify/discord/discord.go` at v0.34.2).
- Unpackerr runs 0.15.2. It has a built-in Discord template, chosen when the
  URL contains `discord.com` or when the template is set to `discord`. The
  template includes the path, app, queue and error fields
  (`pkg/unpackerr/webhooks_templates.go` at v0.15.2).
- PVE runs 9.2.25 on takaros, evanthoulaki, patroklos and shypowder, one
  cluster. Webhook targets support `secret` entries stored in
  `/etc/pve/priv/notifications.cfg` and used as `{{ secrets.<name> }}` in the
  URL, headers and body. The `escape` helper makes a value safe inside a JSON
  string (PVE wiki, Notifications).
- PVE renders vzdump messages from `vzdump-body.txt.hbs`. An override in
  `/etc/pve/notification-templates/default/` replaces it, and `/etc/pve` is
  shared by the cluster, so one file covers all nodes. The default body ends
  with `{{logs}}`, the full task log, which is what makes messages large. The
  template data has `error`, `guest-table`, `logs`, `status-text`,
  `total-size`, `total-time` and common data such as `fqdn`. It has no task
  UPID (`PVE/VZDump.pm` on takaros), so a link cannot point to one task.
- The current matcher routes only severity warning and error. vzdump is the only
  notification type with a long body; fencing and replication bodies are short.
- ntfy stores only an emptyDir cache. Removing it deletes no persistent data.
- The vault item `discord epaflix notifications` holds the webhook URL as its
  password, Spyros's user ID in field `userID` (unused), and the
  `@notifications` role ID in field `roleID`. A read-only GET to the URL
  returned HTTP 200 on 2026-11-10.

## Approaches

- O1, picked. Each sender posts straight to the Discord webhook in its own
  format. No new service. The URL is stored in four places: SOPS, PVE's
  private notification config, and a file on TrueNAS and on the PBS guest.
- O2, rejected. An in-cluster relay that every sender posts to. One place for
  the URL and for truncation, but it is a new service that can fail the same
  way ntfy did.

For the PVE log link:

- L1, picked. Link to the PVE web UI of the node in `fields.hostname`, plus the
  time. The full log is in that node's Task History. Works on the LAN or over
  Tailscale.
- L2, rejected. Ship PVE logs to Loki and link a Grafana query. Needs an agent
  on every PVE host.

## Design

### Secrets

- One Discord webhook for `#notifications`. Its URL lives in the vault item
  `discord epaflix notifications`. The `@notifications` role ID is in the same item,
  field `roleID`, because it is an account identifier.
- `alertmanager-config-secret` (SOPS) gets the Discord receiver with the URL
  and the role ID. Alertmanager reads them from its config.
- PVE: two `secret` entries on the webhook target, the URL and the role ID,
  written through `pvesh` inside `vault run`, never committed. The repo copy of
  `notifications.cfg` shows the target without the secret value.
- Unpackerr: the URL comes from a SOPS Secret through `secretKeyRef`.
- TrueNAS and the PBS guest: each script reads the URL and the role ID from a
  root-only file (for example `/root/.config/discord-webhook.env`), which the
  install steps write inside `vault run`. The scripts fail loudly if the file
  is missing.

### Message shape

- Alertmanager: `discord_configs` with `send_resolved: true`. Title and message
  keep today's ntfy logic (FIRING:N or RESOLVED, namespace, first five alerts,
  one line each, Grafana alert-list link). `content` is the role mention only
  when the group is firing with severity critical. Replaces the `ntfy`
  receiver; the route tree only renames the receiver.
- Unpackerr: built-in Discord template, event 3 only, as today.
- PVE: webhook target `discord-pve` posting JSON with the mention in `content`
  and an embed whose title is `{{ escape title }}` and whose description is
  `{{ escape message }}` plus the node link. Embed description allows 4096
  characters. A vzdump body override that keeps the error, one short line per
  failed guest (VMID, name, status, no error text), total time and total size,
  and drops `{{logs}}`. At about 40 characters a line, 30 failed guests (the
  #1075 case, every guest fails) stay near 1200 characters. If PVE's
  Handlebars cannot filter `guest-table` rows by status, list every guest the
  same short way; that still fits. Builder's call. The body points the reader to Task
  History on the linked node. Repo copy of the override lives in
  `1-proxmox/pbs/` next to `notifications.cfg`.
- Scripts: `curl` posts `{"content": "**<title>**\n<body>"}` built with `jq`
  or a printf that escapes JSON safely. High priority messages include the
  mention. Exit codes and state-file behaviour stay as they are, so a failed
  post still exits 2 and retries on the next run.

### Delivery failures

`AlertmanagerWebhookDeliveryFailing` (`custom-alerts.yaml`) filters on
`integration="webhook"`. It must also count `integration="discord"`, or
Discord delivery failures go uncounted. It still helps when Discord rejects
one payload while the webhook works.

Rollout step 4 tests PVE's copy of the URL with PVE's built-in target test.
PVE logs a failed post only in its task log.

### Removal

Delete the ntfy manifests and every reference listed in G4 in a second PR,
after every sender is confirmed on Discord. Pi-hole and Unbound records are applied on the Pi-hole
guest; Spyros or the agent applies those after merge. Spyros removes the ntfy
app from his phone.

## Rollout order

1. Done: the vault item is in this pod's collection with the URL; Spyros adds field `roleID`.
2. PR 1: Alertmanager, Unpackerr, delivery-failure alert, script and PVE repo files,
   docs. Argo syncs the cluster parts. ntfy keeps running.
3. Live, after merge, with Spyros's go-ahead: PVE secret, target, matcher and
   template override; TrueNAS and PBS URL files and script copies.
4. Send one test notification per sender and confirm each in `#notifications`.
5. PR 2: ntfy removal (G4). Then remove the Pi-hole and Unbound records.

ntfy stays up until step 4 passes, so no sender ever posts to a dead host.

## Left to the builder

- Confirm TrueNAS, LXC 1031 and the PVE nodes can reach `discord.com:443`
  before changing them.

- Exact PVE UI link form. Verify whether a URL hash can open the node's Task
  History; if not, link the node page.
- Exact Discord JSON layout (plain content or an embed) per sender, within the
  limits above.
- How the TrueNAS and PBS scripts get reinstalled, following their existing
  install notes.
- Test updates for `.github/hooks/test-keystatus-check.sh` and
  `.github/hooks/test-datastore-usage-alert.sh`.
