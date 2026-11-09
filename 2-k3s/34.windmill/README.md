# Windmill

Daily stock screener for members of `Screener users`. Design: `docs/superpowers/specs/2026-11-08-screener-design.md`. Plan: `docs/superpowers/plans/2026-11-08-screener.md`.

Hostname https://windmill.epaflix.com on Traefik `websecure` at `192.168.10.101`. The official Helm chart `windmill` 4.0.313 renders through Kustomize from `values.yaml`. It runs the free Community Edition (CE): one `windmill-app` server and two `windmill-workers-default` workers. The database is `windmill` in `postgres-cluster`.

## Access

Two Authentik gates:

1. Traefik forward auth with the Authentik proxy provider `windmill`. Anyone outside `Screener users` and `authentik Admins` gets Authentik's login or denial and never reaches Windmill. This also hides Windmill's own password login and API from the internet.
2. Windmill's built-in `authentik` login provider with the Authentik OAuth2 provider `windmill-sso`. Windmill creates the user on the first login.

Both Authentik applications bind `Screener users` and `authentik Admins`. The NetworkPolicy lets only Traefik reach `windmill-app`, so no request skips forward auth.

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

Script `f/screener/run_screen` in workspace `screener`. Its source and tests are in `workspace/`. CI runs the tests. After a merge that changes the script, update it in Windmill through the API with the merged file.

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
