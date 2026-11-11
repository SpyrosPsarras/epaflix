# Discord notifications implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every homelab notification that goes to ntfy today posts to the Discord `#notifications` webhook instead, then ntfy is removed.

**Architecture:** Each sender posts straight to the webhook in its own format (Alertmanager `discord_configs`, Unpackerr's built-in Discord template, PVE and PBS webhook targets, `curl` + `jq` in two scripts). Loud messages mention the `@notifications` role. No relay, no new service.

**Tech Stack:** Alertmanager v0.34.2, Unpackerr 0.15.2, PVE 9.2.25, PBS 4.2.10, TrueNAS SCALE cron, bash, jq, SOPS + KSOPS, Argo CD.

**Spec:** `docs/superpowers/specs/2026-11-10-discord-notifications-design.md`

## Global constraints

- The webhook URL and the role ID never appear in Git in plain text, in logs, or in chat. Read them only inside `vault run` on the notifications vault item (password: webhook URL, field `roleID`).
- Mention text is `<@&ROLE_ID>`. Loud = Alertmanager firing with severity critical, every PVE notification, PBS notifications with severity warning, error or unknown, script messages with priority `high` or `urgent`. Everything else has no mention.
- Discord limits: content 2000 characters, embed title 256, embed description 4096.
- Webhook display names: `Alertmanager`, `Unpackerr`, `PVE`, `PBS`, `TrueNAS`, `PBS datastore`.
- A failed post must stay visible: scripts keep exit code 2 and do not write state; Alertmanager failures count in `AlertmanagerWebhookDeliveryFailing`.
- ntfy keeps running until Task 7 passes. ntfy removal is a separate PR (Task 8).
- Live host and vault changes need Spyros's go-ahead first (AGENTS.md S8).
- Before any commit, the branch is rebased onto `origin/main`. No co-author line.

## Review focus

1. Alert text with quotes, backslashes, newlines or emoji must produce valid JSON in every sender. Owned by Tasks 1 and 3 (test with a title holding `"` and `\` and a newline).
2. A missing or empty webhook env file on TrueNAS or the PBS guest must fail loudly and not write state. Owned by Task 3.
3. A PVE backup where every guest fails (about 30 guests) must stay under 4096 characters. Owned by Task 4 (live test with a synthetic long guest table is not possible, so the template must not print per-guest error text).
4. A PBS verify failure listing many snapshots must stay under 4096 characters. Owned by Task 5.
5. A resolved Alertmanager group must never mention the role. Owned by Task 1.

---

### Task 1: Alertmanager Discord receiver

**Files:**
- Modify: `2-k3s/10.observability/secrets/alertmanager-config-secret.enc.yaml` (receiver `ntfy` → `discord`, both routes renamed)
- Modify: `2-k3s/10.observability/alertmanager-config/custom-alerts.yaml` (`AlertmanagerWebhookDeliveryFailing`)
- Modify: `2-k3s/10.observability/alertmanager-config/custom-alerts-tests.yaml`

**Interfaces:**
- Produces: receiver name `discord`.

- [ ] **Step 1: Add the failing promtool test.** In `custom-alerts-tests.yaml`, add a test group with series `alertmanager_notifications_failed_total{integration="discord",pod="alertmanager-0"}` values `0+1x30`, expecting `AlertmanagerWebhookDeliveryFailing` at 30m with label `integration: discord`. Update the existing expected `description` to the new text from Step 3.
- [ ] **Step 2: Run the CI promtool step locally** (copy of the `Alert rule tests (promtool)` script in `.github/workflows/ci.yml`, both shards). Expected: the new group FAILS.
- [ ] **Step 3: Change the rule.** `expr: increase(alertmanager_notifications_failed_total{integration=~"webhook|discord"}[30m]) > 0`. Rewrite the description: covers the `discord` receiver and the `deadman` webhook; a broken Discord webhook means this alert is not seen in Discord, read it in Grafana or the Alertmanager UI; drop every ntfy and `NtfyCanaryPublishFailing` reference.
- [ ] **Step 4: Run promtool again.** Expected: all groups PASS.
- [ ] **Step 5: Edit the SOPS secret.** Decrypt to a private temp file with `sops -d --output` inside `vault run --name sops-age-k3s-cluster`. Replace receiver `ntfy` with:
  - `name: discord`, `discord_configs:` one entry, `send_resolved: true`, `username: Alertmanager`, `webhook_url:` the vault URL.
  - `title`: today's `$title` logic (`FIRING:N alertname [namespace]` or `RESOLVED alertname [namespace]`).
  - `message`: today's `$msg` lines, then a final line with today's Grafana alert-list link.
  - `content`: `{{ if and (eq .Status "firing") (eq .GroupLabels.severity "critical") }}<@&ROLE_ID>{{ end }}`.
  - Rename both `receiver: ntfy` routes to `receiver: discord`. Drop the ntfy comments.
  Write URL and role ID into the temp file inside a `vault run` that also runs `sops -e -i` on the result; never print them.
- [ ] **Step 6: Validate the config.** Download `amtool` v0.34.2 into a temp dir and run `amtool check-config` on the decrypted temp file. Expected: `SUCCESS`, receivers `discord`, `deadman`, `null`. Delete the decrypted file.
- [ ] **Step 7: Check the encrypted file.** Run `./.github/hooks/check-sops-encrypted.sh --full-tree`. Expected: pass.
- [ ] **Step 8: Commit.** `feat(observability): send Alertmanager notifications to Discord`

### Task 2: Unpackerr to Discord

**Files:**
- Modify: `2-k3s/08.servarr/_shared/secrets/unpackerr-secret.enc.yaml` (add key `UN_WEBHOOK_0_URL`)
- Modify: `2-k3s/08.servarr/unpackerr/unpackerr.yaml`
- Modify: `2-k3s/08.servarr/kustomization.yaml` (drop `unpackerr-ntfy-template`)
- Delete: `2-k3s/08.servarr/unpackerr/files/ntfy.json.tmpl`

- [ ] **Step 1: Add the URL to the secret** with `sops set` inside one `vault run` that holds both the age key and the webhook URL. Never print it.
- [ ] **Step 2: Change the Deployment env.** `UN_WEBHOOK_0_URL` from `secretKeyRef` `unpackerr-secret`/`UN_WEBHOOK_0_URL`; `UN_WEBHOOK_0_NAME: Unpackerr`; keep `UN_WEBHOOK_0_EVENTS_0: "3"`; remove `UN_WEBHOOK_0_TEMPLATE_PATH`, the `ntfy-template` volume and mount. The built-in Discord template is chosen because the URL contains `discord.com`. Update the comment above the env block to say Discord.
- [ ] **Step 3: Render.** `kustomize build --enable-alpha-plugins --enable-exec 2-k3s/08.servarr` (or the CI render step for this app). Expected: renders, no `ntfy` string in the output: `... | grep -c ntfy` prints 0.
- [ ] **Step 4: Commit.** `feat(servarr): send Unpackerr extract failures to Discord`

### Task 3: Scripts post to Discord

**Files:**
- Modify: `0-truenas/scripts/keystatus-check.sh`
- Modify: `1-proxmox/pbs/datastore-usage-alert.sh`
- Modify: `.github/hooks/test-keystatus-check.sh`
- Modify: `.github/hooks/test-datastore-usage-alert.sh`

**Interfaces:**
- Produces: env file contract, used by Task 6. Path from `DISCORD_ENV_FILE`, default `/root/.config/discord-webhook.env`, mode 600, two lines `DISCORD_WEBHOOK_URL=...` and `DISCORD_ROLE_ID=...`. Scripts source it.
- Both scripts keep `notify(title, priority, body)` with today's priorities (`default`, `high`, `urgent`).

- [ ] **Step 1: Write the failing tests.** In both test files, write an env file in `$tmp` with `DISCORD_WEBHOOK_URL=https://discord.test/api/webhooks/1/abc` and `DISCORD_ROLE_ID=42`, export `DISCORD_ENV_FILE`. The curl stub saves the URL argument and the `--data`/`--data-binary` value. Assertions:
  - posts go to `https://discord.test/api/webhooks/1/abc`;
  - the body parses with `jq -e .`;
  - `high`/`urgent` posts: `.content` starts with `<@&42>` and `.allowed_mentions.roles == ["42"]`;
  - `default` posts (recoveries): `.content` has no `<@&`;
  - `.content` holds the bold title and the body; `.username` is `TrueNAS` / `PBS datastore`;
  - a title containing `"`, `\` and a newline still parses (Review Focus 1);
  - env file missing: datastore script exits 2 and writes no state; keystatus script exits non-zero and writes no state (Review Focus 2);
  - replace the old ntfy URL and header assertions; rename `ntfy_log`/`NTFY_LOG` to `post_log`/`POST_LOG`.
- [ ] **Step 2: Run them.** `./.github/hooks/test-keystatus-check.sh; ./.github/hooks/test-datastore-usage-alert.sh`. Expected: FAIL on the new assertions.
- [ ] **Step 3: Implement.** In both scripts, remove `NTFY_URL`; source `${DISCORD_ENV_FILE:-/root/.config/discord-webhook.env}` (fail if missing or either variable empty, before any state change); `notify` builds the JSON with `jq -n --arg ...` and posts with `curl -fsS -4 -m 10 -H 'Content-Type: application/json' --data-binary "$json" "$DISCORD_WEBHOOK_URL"`. Mention and `allowed_mentions` only for `high`/`urgent`. Keep the datastore script's exit codes and state logic. Update the install comment in `datastore-usage-alert.sh` and add one at the top of `keystatus-check.sh` describing the env file and the TrueNAS cron job.
- [ ] **Step 4: Run the tests.** Expected: all `ok`, exit 0.
- [ ] **Step 5: Commit.** `feat(alerts): post TrueNAS and PBS datastore alerts to Discord`

### Task 4: PVE target and vzdump template (repo files)

**Files:**
- Modify: `1-proxmox/pbs/notifications.cfg`
- Create: `1-proxmox/pbs/notification-templates/vzdump-body.txt.hbs`

- [ ] **Step 1: Write the template.** Keep `{{error}}`, then one short line per guest from `guest-table` (VMID, name, status; no per-guest error text; filter to failed rows if PVE's Handlebars allows, else all rows), `Total running time: {{duration total-time}}`, `Total size: {{human-bytes total-size}}`, then `Full log: Task History on the node linked below.` No `{{logs}}`.
- [ ] **Step 2: Check the template on a node without installing it.** Copy to `/tmp` on takaros and render it against the last vzdump notification data if PVE offers a way (`perl -MPVE::Notify` one-liner); if not, Task 7's test covers it. Remove the temp file.
- [ ] **Step 3: Change the repo copy of `notifications.cfg`.** Replace `webhook: ntfy-pve` with `webhook: discord-pve`: `method post`, `url {{ secrets.url }}`, header `Content-Type: application/json`, body (base64 as PVE stores it) of:
  `{"username":"PVE","content":"<@&{{ secrets.role }}>","allowed_mentions":{"roles":["{{ secrets.role }}"]},"embeds":[{"title":"{{ escape title }}","description":"{{ escape message }}\n\nNode: https://{{ fields.hostname }}.epaflix.com:8006/"}]}`
  Use the node link form you verify in Task 7 Step 2. Matcher `ntfy-failures` → `discord-failures`, same `match-severity warning,error`, `target discord-pve`. No secret values in the file.
- [ ] **Step 4: Commit.** `feat(proxmox): PVE notifications to Discord, short vzdump body`

### Task 5: PBS target (repo files)

**Files:**
- Create: `1-proxmox/pbs/server/notifications.cfg` (repo copy of `/etc/proxmox-backup/notifications.cfg`, no secrets)
- Create: `1-proxmox/pbs/server/notification-templates/verify-err-body.txt.hbs`
- Create: `1-proxmox/pbs/server/notification-templates/package-updates-body.txt.hbs`

- [ ] **Step 1: Write the repo copy.** Same matcher as live (`default-matcher`, invert prune/info), target `discord-pbs`, same body shape as Task 4 with `"username":"PBS"`, no node line (PBS bodies already link to `#pbsServerAdministration:tasks`).
- [ ] **Step 2: Write the verify-err override.** Job ID, datastore, the number of failed snapshots instead of the list, and the stock tasks link. Add a package-updates override that prints the number of updates and the updates link. Keep the other stock bodies.
- [ ] **Step 3: Commit.** `feat(pbs): PBS notifications to Discord`

### Task 6: Docs in PR 1

**Files:**
- Modify: `2-k3s/10.observability/TLS-monitoring.md` (ntfy → Discord in the test procedure)
- Modify: `2-k3s/08.servarr/research/music_discovery.md` line 147 (point to Discord)
- Modify: `2-k3s/35.discord/README.md` (add the `#notifications` channel, its webhook, the `@notifications` role, the vault item, and where each sender's copy of the URL lives: SOPS `alertmanager-config-secret`, SOPS `unpackerr-secret`, PVE and PBS private notification config, TrueNAS and PBS guest env files; a rotation row)

- [ ] **Step 1: Edit the three files.**
- [ ] **Step 2: Run the CI hooks** that cover docs and secrets: `./.github/hooks/check-no-plaintext-store-values.sh`, `./.github/hooks/check-sops-encrypted.sh --full-tree`. Expected: pass.
- [ ] **Step 3: Commit, rebase on `origin/main`, review gate, open PR 1.**

### Task 7: Live rollout (after PR 1 merges, with go-ahead)

- [ ] **Step 1: Argo sync** `observability` and `servarr` at the merged SHA (dry-run first, per `/scripts/homelab-ssh.md`). Check: Alertmanager pods ready, `kubectl -n observability logs` shows no config error; Unpackerr pod ready.
- [ ] **Step 2: PVE.** Verify the node link form in a browser-free way (curl the UI hash is not possible; pick `https://<node>.epaflix.com:8006/` unless DNS lacks those names, then use the IP from inventory). Install the template to `/etc/pve/notification-templates/default/vzdump-body.txt.hbs`. Create target `discord-pve` and its secrets `id`, `token` (split from the vault URL) and `role` with `pvesh` inside `vault run`, then the matcher, then delete `ntfy-pve` and `ntfy-failures`. `diff` live `/etc/pve/notifications.cfg` against the repo copy: equal.
- [ ] **Step 3: PBS.** Same for targets `discord-pbs` (secrets `id`, `token`, `role`) and `discord-pbs-quiet` (secrets `id`, `token`) and matchers `default-matcher` and `quiet-matcher` from `1-proxmox/pbs/server/notifications.cfg`, with `proxmox-backup-manager notification` commands, the verify-err and package-updates overrides in `/etc/proxmox-backup/notification-templates/default/`, delete `ntfy-pbs`. `diff` against the repo copy.
- [ ] **Step 4: PBS guest script.** Write `/root/.config/discord-webhook.env` (mode 600) inside `vault run`, copy the new `datastore-usage-alert.sh` to `/usr/local/sbin/`.
- [ ] **Step 5: TrueNAS.** Put the script and env file on a non-encrypted pool path that survives TrueNAS updates (the boot pool is replaced on upgrade). Create a cron job with `midclt call cronjob.create`: user root, every 15 minutes, the script with `DISCORD_ENV_FILE` set. Do not hide the job's stdout or stderr, so a missing env file shows up. Because the dataset is locked today, the first run posts a loud LOCKED message; tell Spyros before enabling.
- [ ] **Step 6: One test per sender, confirmed in `#notifications` by Spyros:** Alertmanager (a temporary critical test alert via `amtool alert add` against the live Alertmanager, then expire it), Unpackerr (Spyros confirms when one occurs, or skip with his OK), PVE (`pvesh create /cluster/notifications/targets/discord-pve/test`), PBS (`proxmox-backup-manager notification target test discord-pbs` and `discord-pbs-quiet`), PBS datastore script (`DATASTORE_PATH=/nonexistent` run, expect the loud "cannot be measured" post, then delete the state file), TrueNAS (first cron run). Check the role mention pings; if not, Spyros enables "Allow anyone to @mention this role".

### Task 8: Remove ntfy (PR 2)

**Files:**
- Delete: `2-k3s/10.observability/ntfy.yaml`, `2-k3s/10.observability/ingress/ntfy-ingressroute.yaml`, `2-k3s/maintenance/ntfy-canary-check-cronjob.yaml`
- Modify: `2-k3s/10.observability/kustomization.yaml`, `2-k3s/maintenance/kustomization.yaml`
- Modify: `custom-alerts.yaml` and `custom-alerts-tests.yaml` (drop group `ntfy-canary` and its tests; reword the "ntfy spam fix" comments and the `KubeJobFailedLastRun` owner exclusion at line ~1098)
- Modify: `1-proxmox/pihole/dnsmasq.d/10-epaflix.conf`, `1-proxmox/pihole/unbound-no-aaaa-leak.conf`, `2-k3s/04.coredns/coredns-epaflix-domains.yaml` comment, `2-k3s/maintenance/aaaa-tripwire-cronjob.test.sh` comment

- [ ] **Step 1: Delete and edit.** Afterwards `rg -n -i ntfy --hidden -g '!.git' -g '!docs/superpowers/**'` prints nothing.
- [ ] **Step 2: Run promtool tests and the renders** for `observability` and `maintenance`. Expected: pass, no ntfy resources.
- [ ] **Step 3: Commit, rebase, review gate, PR 2.** After merge: Argo sync with prune for the two apps; apply the Pi-hole and Unbound files on `pihole` the way earlier record changes were applied (backup first, as the `/root/*.bak-*` files show), then reload; `dig ntfy.epaflix.com @pihole` returns no LAN address.
