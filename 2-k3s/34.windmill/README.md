# Windmill

Daily stock screener for members of `Screener users`. Design: `docs/superpowers/specs/2026-11-08-screener-design.md`. Plan: `docs/superpowers/plans/2026-11-08-screener.md`.

Hostname https://windmill.epaflix.com on Traefik `websecure` at `192.168.10.101`. The official Helm chart `windmill` 4.0.313 renders through Kustomize from `values.yaml`. It runs the free Community Edition (CE): one `windmill-app` server and two `windmill-workers-default` workers. The database is `windmill` in `postgres-cluster`.

## Access

Two Authentik gates:

1. Traefik forward auth with the Authentik proxy provider `windmill`. Anyone outside `Screener users` and `authentik Admins` gets Authentik's login or denial and never reaches Windmill. This also hides Windmill's own password login and API from the internet.
2. Windmill's built-in `authentik` login provider with the Authentik OAuth2 provider `windmill-sso`. Windmill creates the user on the first login.

Both Authentik applications bind `Screener users` and `authentik Admins`. The NetworkPolicy lets only Traefik and the `windmill-deploy` CronJob below reach `windmill-app`. Every other request passes forward auth. The CronJob skips it and authenticates with a Windmill API token.

CE allows 10 users with SSO, 50 users, 3 workspaces and 4 groups (https://www.windmill.dev/pricing).

### First login, once

Each step needs the owner's confirmation.

1. Open https://windmill.epaflix.com, pass forward auth, then log in with Windmill's default `admin@windmill.dev` account and password from the Windmill docs. Create a short-lived token.
2. With that token, set the `authentik` login provider through the Windmill API. The command reads the client id and secret of `windmill-sso` from a decrypted temp copy of `2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml` and prints neither.
3. Log in through Authentik, make that user superadmin, and create workspace `screener`.
4. Delete `admin@windmill.dev` and the token.

### Adding and removing a user

The owner creates the person's Authentik account and adds it to `Screener users`. On their first visit Windmill creates their user. A superadmin adds them to workspace `screener`.

Removing someone from `Screener users` stops forward auth at their next Authentik check, within about an hour, when the proxy session's access token expires. Also disable the user in Windmill's instance settings, so their API tokens stop working.

## Screener

Script `f/screener/run_screen` in workspace `screener`. Its source and tests are in `workspace/`. CI runs the tests. The CronJob in [Deploy](#deploy) pushes it to Windmill.

The Discord agent in `#windmill` (GitHub App `homelab-windmill-bot`) changes the screener for members. It pushes a branch under `bots/windmill/`, opens a PR and turns on auto-merge. The CI job `bot-paths`, part of the required check `validate`, fails any such PR that changes a path outside `2-k3s/34.windmill/workspace/` or changes `workspace/wmill.yaml`. The check reads file names, not file contents. The job lives in `ci.yml`, which the App cannot change because it has no Workflows permission. Within 10 minutes of the merge, `windmill-deploy` pushes the change to Windmill.

The script reads the Finviz screener URL, scrapes the matching rows with the `finviz` package, works out the levels and sends one email with a table of the matches and a CSV file. With no matches the email says so. A failed run sends an email with the error and also shows as failed in Windmill's run history. Three failures show only in the run history: a run that Windmill kills for a timeout or out of memory, a failed dependency install, and a run whose email cannot be sent. SMTP settings come from resource `f/screener/smtp`, so every workspace member can read that password.

### Rules

Write a rule as a small formula over the Finviz columns, with the column name in square brackets:

| Field | Example | Meaning |
| --- | --- | --- |
| `entry` | `[Price] * 1.01` | buy level |
| `stop` | `[Price] - 2 * [ATR]` | stop loss |
| `target` | `[Price] + 3 * [ATR]` | optional take profit |
| `condition` | `[RSI] < 30 and [Price] > 5` | optional, keeps only rows where it is true |

Allowed are numbers, columns, `+ - * /`, `< <= > >= == !=`, `and`, `or`, `not`, and the functions `min` and `max` with two or more values, `abs` with one, and `round` with one or two. A rule can be up to 500 characters. The script rejects anything else before it scrapes. `12.5%` counts as `12.5` and `1.2B` as `1200000000`. A row where a used column shows `-`, or where a rule divides by zero or overflows, is skipped, and the email lists it with the reason.

The column names are the headers of the Finviz view in the URL. View `v=171` (Technical) includes `Price`, `ATR`, `RSI`, `SMA20`, `SMA50`, `SMA200`, `52W High`, `52W Low`, `Change %`, `Gap` and `Volume`. When Finviz returns rows, a rule naming a column the view lacks fails with the list of columns it has. `SMA20`, `SMA50`, `SMA200`, `52W High` and `52W Low` are the distance from the price in percent, not price levels. To turn `[SMA50]` into a price, write `[Price] / (1 + [SMA50] / 100)`.

### Setting up a screen

1. Build the screen on finviz.com and copy the address from the browser, for example `https://finviz.com/screener.ashx?v=171&f=cap_mega`.
2. In Windmill, open workspace `screener`, find `run_screen` and open it.
3. Fill in the URL, your email address and the rules, then press Run. Check the email.
4. To run it every trading day, add a schedule for `f/screener/run_screen` with cron `0 30 22 * * Mon-Fri` and time zone `Europe/Oslo`, with the same arguments. Windmill's cron starts with a seconds field. Check that the next runs Windmill shows fall on Monday to Friday. 22:30 Oslo time is after the US close all year, also in the weeks when US and European daylight saving time differ.

Finviz has no free API, so the script reads its public pages. It can break when Finviz changes them. The free data is delayed, which is fine after the close.

## Deploy

CronJob `windmill-deploy` runs `deploy.mjs` every 10 minutes. It downloads `main` from GitHub, installs `windmill-cli` with `npm ci --ignore-scripts` from the lock in `deploy-cli/`, and runs `wmill sync push --yes --auto-metadata --keep-deleted` from `workspace/` into workspace `screener`. `workspace/wmill.yaml` limits the push to scripts, flows and apps under `f/`. It leaves variables, secrets, resources, schedules, folders, users' `u/` folders and workspace settings alone. `--keep-deleted` means the push never removes anything from Windmill. Add and change shared scripts through Git, not in the Windmill editor. To retire one, delete it in Git and archive it in Windmill by hand.

The pod holds only the Windmill token: key `WINDMILL_TOKEN` of Secret `windmill-deploy`, passed to the CLI as `WM_TOKEN`, so it never appears in a command line. It has no ServiceAccount token, and its NetworkPolicy allows only DNS, `windmill-app` on 8000 and public addresses on 443. It does not run on the GitHub runner because the CLI can run code and read files from what it pushes, and the runner holds homelab SSH keys. Until the Secret exists, each run waits with `secret "windmill-deploy" not found` in the pod events and fails after 5 minutes.

Each run ends with one JSON line, for example `{"commit":"<sha>","outcome":"deployed"}`, or `"outcome":"failed"` with an `error`. Read it in Loki with `{namespace="windmill", container="windmill-deploy"}`, or with `kubectl -n windmill logs job/<job name>`. To deploy now, run `kubectl -n windmill create job --from=cronjob/windmill-deploy windmill-deploy-manual-$(date +%s)` right after a scheduled run has finished: a manual Job does not wait for a running one.

`deploy-cli/package.json` and its lock pin `windmill-cli` to the server version. When you bump the Windmill image in `values.yaml`, set the same version in `deploy-cli/package.json` and run `npm install --package-lock-only --ignore-scripts` in `deploy-cli/`. `deploy.test.py` fails while they differ. Renovate updates the `node` image digest in `deploy-cronjob.yaml`.

## Jobs

Workers run every job in nsjail, set by the chart's `isolationSecurity: capabilities`. The worker container is not privileged. It runs as root with capabilities `SYS_ADMIN`, `SETPCAP` and `SYS_RESOURCE`, and seccomp and AppArmor are `Unconfined` because the runtime defaults block nsjail. The database URL is a file at `/run/secrets/windmill/database-url`, a path nsjail does not expose to jobs. A job that runs out of memory restarts its worker pod.

Workers use their own ServiceAccount `windmill-worker` without a token. Their NetworkPolicy allows DNS, the database and public addresses only, so jobs cannot reach the LAN or any other cluster service. They call the Windmill API on their worker's own localhost, so workers need no route to `windmill-app`.

## Secrets

`windmill-secrets.enc.yaml` holds Secret `windmill-db-role` in `postgres-system` and Secret `windmill-database` in `windmill`, both at sync wave `-2`. The database password appears in both. It was generated at random when the file was encrypted and exists only there. To rotate it, change it in both Secrets in one PR. CNPG updates the role. Then restart `windmill-app` and the workers so they read the new URL.

The `windmill-sso` client id and secret exist only in the Authentik blueprint and in Windmill's database. To rotate them, change the blueprint in a PR, then repeat step 2 of the first login.

## DNS

Public DNS resolves through the Cloudflare wildcard and tunnel. The LAN needs `windmill.epaflix.com` in `1-proxmox/pihole/dnsmasq.d/10-epaflix.conf` and `1-proxmox/pihole/unbound-no-aaaa-leak.conf`, applied on the Pi-hole as `2-k3s/08.servarr/navidrome/README.md` records.

## Observability

- `WindmillDown` fires when `windmill-app` has no available replica for 15 minutes or is absent.
- `WindmillWorkersDown` fires the same way for `windmill-workers-default`. Scheduled screens do not run then.
- Blackbox target `windmill` probes `https://192.168.10.101` with host `windmill.epaflix.com` for the route and certificate. Forward auth answers before Windmill does, so the probe passes even when Windmill is down. The two alerts above cover Windmill itself.
- Logs reach Loki through Alloy. CE has no Prometheus metrics.
