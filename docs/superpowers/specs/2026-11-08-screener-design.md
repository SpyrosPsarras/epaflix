# Daily stock screener on Windmill

## Intent

A friend of the owner screens US stocks on Finviz by hand. He wants a bot
that runs his screen every day after the market closes and emails him the
matching tickers, each with an entry, a stop loss and other levels from
rules he writes. He sets up the screen and the rules himself in a web page
behind Authentik. Several users each keep their own screens.

No ready-made open-source app does this. Windmill, a self-hosted platform
for scripts and schedules, does the hard parts: users, forms, schedules,
run history. The owner adds one shared screening script.

## Goals

- G1. A user pastes a Finviz screener URL, writes entry, stop and optional
  target and condition rules, sets an email address and saves a daily
  schedule. Nobody edits code for that.
- G2. After each scheduled run the user gets an email with one row per
  match: ticker, the Finviz columns, entry, stop and target. A day with no
  matches still sends an email that says so.
- G3. A failed run emails the user who owns the schedule and shows in
  Windmill's run history.
- G4. Only members of the new Authentik group `Screener users`, plus
  `authentik Admins`, reach Windmill.
- G5. Each user's schedules and settings live in their own `u/<user>/`
  folder. Users see the shared script, not each other's schedules.
- G6. Windmill has health alerts, a blackbox probe and logs in Loki.

## Non-goals

- Trading, broker connections, position sizing or backtests.
- Intraday or real-time data. Free Finviz data is delayed, which is fine
  after the close.
- Finviz Elite. A user with Elite can paste his export URL later.
- Windmill Enterprise features. The free Community Edition (CE) is enough.

## Anti-goals

- A user's script reading the Windmill database password and through it
  every user's secrets.
- Windmill's own login page or API reachable from the internet without
  Authentik.
- User scripts reaching homelab services on the LAN or in the cluster.
- Credentials in Git, chat or logs.

## Constraints

- Argo CD owns the app. Changes reach the cluster only through a merged PR.
- CE limits, from https://www.windmill.dev/pricing: 10 users with SSO,
  50 users, 3 workspaces, 4 groups. Custom OAuth and OIDC are Enterprise,
  but the built-in `authentik` login provider works on CE
  (https://www.windmill.dev/docs/advanced/setup_oauth/).
- Database: the shared CNPG cluster `postgres-cluster`. Windmill without
  a Postgres superuser needs roles `windmill_user` and `windmill_admin`
  (`BYPASSRLS`), per `init-db-as-superuser.sql` in the Windmill repo.
- Nodes run Ubuntu 24.04 and k3s v1.36. User-namespace isolation needs an
  AppArmor profile on every node, so it is out of scope.
- Finviz has no free API. The script scrapes the public screener with the
  `finviz` Python package (https://github.com/mariostoev/finviz). It can
  break when Finviz changes its pages, and scraping may break Finviz's
  terms. The owner accepts this.

## Approaches

- O1, picked. Windmill CE with one shared script. Users get forms,
  schedules and history for free. The cost is running Windmill.
- O2. n8n. Its free edition has no OIDC login.
- O3. A small custom app. It would rebuild users, schedules and forms.

## Decisions

- D1. Hostname `windmill.epaflix.com`, namespace `windmill`, directory
  `2-k3s/34.windmill`, Argo app `windmill`.
- D2. The official Helm chart through Kustomize `helmCharts`, like
  `2-k3s/16.reloader`. No bundled Postgres, MinIO, hub or indexer.
- D3. Two Authentik gates (owner's Q4 answer). Traefik forward auth with
  an Authentik proxy provider stops everyone outside the group. Windmill
  then logs the user in through its built-in `authentik` provider, a
  second Authentik OAuth2 provider. Both applications bind
  `Screener users` and `authentik Admins`.
- D4. New Authentik group `Screener users` (owner's Q1 answer). The owner
  creates the friend's Authentik account and adds him.
- D5. Workers run jobs in nsjail with the chart's
  `isolationSecurity: capabilities` (owner's Q3 answer). The database URL
  is a mounted file outside the paths nsjail exposes
  (`databaseUrlAsFile`). A job that runs out of memory restarts its worker.
- D6. A NetworkPolicy lets workers reach DNS, the database and public
  addresses only. Each worker serves the API its jobs call on its own
  localhost (`backend/src/main.rs`, `BASE_INTERNAL_URL` is ignored outside
  agent mode), so workers need no route to the Windmill app. Only Traefik
  reaches the app. The SMTP relay is a public address on port 587.
- D7. No Windmill operator. Its reconciler is behind the `private`
  build feature (`backend/windmill-operator/src/operator_oss.rs` bails
  with "K8s operator is not available in this build"), so the CE image
  cannot run it. The base URL comes from the chart's `BASE_URL`. The
  `authentik` login provider is set once through the Windmill API, with
  the client secret read from the SOPS blueprint inside that command.
  Windmill itself sends no email, so it gets no instance SMTP settings.
- D8. Email uses the SMTP relay Authentik and Vaultwarden already use
  (owner's Q2 answer). The script reads it from a shared resource, so any
  Windmill user can read that SMTP password. The owner accepts this.
- D9. One workspace `screener`. The shared script lives at
  `f/screener/run_screen`, its source in Git under
  `2-k3s/34.windmill/workspace/`, pushed with the `wmill` CLI.
- D10. Rules are small expressions over the Finviz columns, column names
  in brackets: `[Price] * 1.01`, `[Price] - 2 * [ATR]`, `[RSI] < 30`.
  Only numbers, column references, `+ - * /`, comparisons, `and`, `or`,
  `not`, `min`, `max`, `abs` and `round` are allowed. Anything else is
  rejected before the screen runs.

## Left to the builder

- Resource requests and limits, replica counts within one app and two
  workers.
- The exact `authentik` OAuth settings shape, read from the Windmill
  source or from `GET /api/settings/instance_config/yaml` after a test.
- Email layout, as long as it has the G2 columns and a CSV attachment.
