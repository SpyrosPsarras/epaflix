# Authentik Identity Provider for epaflix.com

This deployment configures Authentik as an identity provider for Single Sign-On (SSO) and authentication across services at epaflix.com.

**Deployment**: Helm (official `authentik/authentik` chart)

## Architecture

- **Domain**: `auth.epaflix.com`
- **TLS**: Let's Encrypt via Traefik with Cloudflare DNS-01 challenge
- **Namespace**: `app-authentik`
- **Server Replicas**: 1
- **Worker Replicas**: 3
- **Database**: CloudNativePG PostgreSQL cluster at `192.168.10.105:5432`
- **Storage**: `local-path` PVC (10Gi, ReadWriteOnce) for media files
- **Redis**: Not required (removed in Authentik 2024+)
- **Ingress**: Traefik IngressRoute at `192.168.10.101`
- **Version**: 2026.5.3 (pinned in [kustomization.yaml](kustomization.yaml) — `helmCharts.version` + `images` `newTag`, the single source of truth)

## Prerequisites

1. **Traefik deployed** at `192.168.10.101` with Cloudflare certResolver
2. **CloudNativePG cluster** running with database `authentik` created
3. **DNS**: `auth.epaflix.com` pointing to router (or directly to `192.168.10.101` for LAN)
5. **Router**: Port forwarding 80/443 to `192.168.10.101`
6. **Helm 3** installed

## Deployment (Helm)

### Fresh Installation

Deploy a new Authentik instance:

```bash
./deploy.sh
```

The script will:
1. Create namespace `app-authentik`
2. Add Authentik Helm repository
3. Install Authentik via Helm with [helm-values.yaml](helm-values.yaml)
4. Wait for pods to be ready

Media storage is provisioned automatically via `local-path` StorageClass.

### Manual Deployment Steps

```bash
# Create namespace
kubectl --context epaflix apply -f namespace.yaml

# Add Helm repository and install
helm repo add authentik https://charts.goauthentik.io
helm repo update
helm install authentik authentik/authentik \
  --namespace app-authentik \
  --values helm-values.yaml
```

### Initial Setup (First Time)

1. Wait 1-2 minutes for Let's Encrypt certificate to be issued
2. Access: **https://auth.epaflix.com/if/flow/initial-setup/** (trailing slash required!)
3. Follow the setup wizard to create admin user
4. Configure SMTP settings (already in configuration, but verify in UI)
5. Configure authentication flows, policies, and applications

## Backup

Create a backup of Authentik data (database + media files):

```bash
./backup.sh
```

This backs up:
- PostgreSQL database (`authentik-db.sql`)
- Media files (`authentik-media.tar.gz`)

Backup location: `/tmp/authentik-backup-YYYYMMDD-HHMMSS/`

**Important**: Copy backups to a safe location for disaster recovery!

### Manual Backup

```bash
# Backup database
PGPASSWORD='<AUTHENTIK_DB_PASSWORD>' pg_dump \
  -h 192.168.10.105 \
  -U authentik \
  -d authentik \
  --no-owner --no-acl \
  > authentik-db-$(date +%Y%m%d).sql

# Backup media files (from local-path PVC on the worker node)
MEDIA_POD=$(kubectl --context epaflix get pod -n app-authentik -l app.kubernetes.io/name=authentik -o jsonpath='{.items[0].metadata.name}')
kubectl --context epaflix exec -n app-authentik $MEDIA_POD -- tar czf - /media > authentik-media-$(date +%Y%m%d).tar.gz
```

## Restore

Restore a backup to a fresh Authentik installation:

```bash
./restore.sh /path/to/backup-directory
```

**Example**:
```bash
./restore.sh /tmp/authentik-backup-20260120-120000
```

The script will:
1. Verify Authentik is deployed
2. Scale down pods
3. Drop and restore PostgreSQL database
4. Restore media files to PVC
5. Scale up pods
6. Wait for pods to be ready

**Use case**: Restore after cluster rebuild or disaster recovery.

### Manual Restore

```bash
# 1. Deploy fresh Authentik
./deploy.sh

# 2. Scale down pods
kubectl --context epaflix -n app-authentik scale deployment/authentik-server --replicas=0
kubectl --context epaflix -n app-authentik scale deployment/authentik-worker --replicas=0

# 3. Restore database
PGPASSWORD='<AUTHENTIK_DB_PASSWORD>' psql \
  -h 192.168.10.105 \
  -U authentik \
  -d authentik \
  -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

PGPASSWORD='<AUTHENTIK_DB_PASSWORD>' psql \
  -h 192.168.10.105 \
  -U authentik \
  -d authentik \
  < authentik-db-20260120.sql

# 4. Restore media files
cat authentik-media-20260120.tar.gz | \
  ssh truenas_admin@192.168.10.200 \
  "sudo tar xzf - -C /"

# 5. Scale up pods
kubectl --context epaflix -n app-authentik scale deployment/authentik-server --replicas=2
kubectl --context epaflix -n app-authentik scale deployment/authentik-worker --replicas=1
```

## Upgrading

Upgrade to a new version:

```bash
./upgrade.sh
```

The script will:
1. Show current version
2. Update Helm repository
3. Prompt for version selection
4. Create backup before upgrade
5. Perform Helm upgrade
6. Wait for rollout completion
7. Display upgrade status

### Manual Upgrade

```bash
# Update Helm repository
helm repo update authentik

# Check available versions
helm search repo authentik/authentik --versions

# Upgrade to latest
helm upgrade authentik authentik/authentik \
  --namespace app-authentik \
  --values helm-values.yaml

# Or upgrade to specific version
helm upgrade authentik authentik/authentik \
  --namespace app-authentik \
  --values helm-values.yaml \
  --version 2026.5.3
```

### Rollback Helm Upgrade

If an upgrade fails or causes issues:

```bash
# View upgrade history
helm history authentik -n app-authentik

# Rollback to previous version
helm rollback authentik -n app-authentik

# Rollback to specific revision
helm rollback authentik 2 -n app-authentik
```

### Re-validate the IaC blueprint on each chart MINOR bump (#232)

The declarative IaC service-account blueprint (#185, `authentik-iac-blueprint.enc.yaml` — see
[Durable service-account token](#1-durable-service-account-token--the-sanctioned-path-for-iac--automation-185))
relies on goauthentik schema features (custom YAML tags, blueprint-secret mounting, token-`key`
honoring) that can shift across **MINOR** chart bumps and **silently** break automation at the next
apply. Patch bumps are exempt (Renovate auto-merges them). Run this checklist on every
MINOR/MAJOR `authentik` upgrade:

- [ ] **Before merging** the Renovate `authentik` MINOR/MAJOR PR, skim the goauthentik release notes + blueprint-schema docs for breaking changes (patch bumps auto-merge and are exempt).
- [ ] `!Find` / `!KeyOf` custom-tag semantics unchanged (the blueprint uses `attrs.groups: !Find` and `!KeyOf` references in `authentik-iac-blueprint.enc.yaml`).
- [ ] Explicit token `key` still honored for `intent: api` (the `ak-iac-token` value stays the one mirrored in the credential store `.github/instructions/secrets.enc.yaml` - not regenerated by the upgrade).
- [ ] `blueprints.secrets` still consumed as a plain string list (the chart mounts each named Secret and the worker auto-discovers `*.yaml`); verify the rendered shape with `kustomize build 2-k3s/07.authentik-deployment --enable-helm`.
- [ ] **Post-upgrade apply confirmation:** the `iac-service-account` BlueprintInstance reports `status: successful`; if it shows 403 / not-applied, trigger blueprint discovery or `kubectl --context epaflix -n app-authentik rollout restart deploy/authentik-worker` (the #185 worker-discovery gotcha). Confirm the `ak-iac` user + token are present via `GET /api/v3/core/users/me/` -> 200.
- [ ] `state: created` still skips an existing instance WITHOUT saving it (grep `Instance exists, skipping` in `authentik/blueprints/v1/importer.py`) and still populates `entry._state` so `!KeyOf` resolves off a skipped entry — if either changes, the ArgoCD entries stop being inert and can rotate the live client credentials (#1040).
- [ ] `oauth2provider.redirect_uris` is still a list of `{matching_mode, url, redirect_uri_type}` objects (`RedirectURISerializer`), not the pre-2024 newline-separated string.

Cross-references: **#185** (the durable declarative token this guards); **#230** /
**#339** (the scoped-RBAC flip landed 2026-08-10, so the post-upgrade expectation
is now the scoped `ak-iac IaC` role plus `is_superuser: false`, not superuser).

#### In-cluster blueprint check (#883)

`blueprint-check-cronjob.yaml` ships a daily CronJob in `app-authentik` that reads the
live `authentik-iac-blueprint` Secret through the kube API and runs
`files/check_authentik_blueprint.py` over every YAML-suffixed key in it. Two classes are
caught:

1. **Payload YAML syntax** (#876). The Secret file is valid YAML; the blueprint nested
   inside `stringData` is a second document that nothing parsed, so a misindented entry
   merged green and stopped *all* entries from reconciling. The checker loads the payload
   with the ten Authentik tags registered. A tag outside those ten also fails, on purpose:
   a new Authentik tag has to be added to the checker deliberately.
2. **`!KeyOf` / `!Find` resolution and absent-entry hygiene** (#940). The payload that
   failed every apply for two days parsed *cleanly* - a `present` entry's `!KeyOf` pointed
   at a `state: absent` entry, which `KeyOf.resolve` cannot resolve, so the importer
   aborted the run. Layer 1 provably cannot see that. So every `!KeyOf` must name a
   declared, non-absent entry; every `!Find` must be shaped `[model, [attr, value]]` and
   must not resolve onto a sibling entry declared absent; and `state: absent` plus `attrs`
   is a hard fail (attrs are ignored on a delete, and a tag inside them silently skips it).
   `state: created` counts as resolvable, not absent - a skipped `created` entry still
   populates `entry._state` (#1040).

What it does **not** catch: every other semantic error - wrong model paths, bad attr
names, permissions Authentik would reject at apply. Only Authentik's own importer knows
those.

**The trade-off, stated rather than glossed.** This is a detector, not a gate. A
pre-commit hook would have refused a broken blueprint *before* it reached `main`; this
finds it *after* merge, up to a day later. That is later than #883's framing wanted, and
it was accepted knowingly: a hook would have to decrypt `authentik-iac-blueprint.enc.yaml`
at commit time, which means `sops`, which means the age key, which on this workstation
lives behind KeePassXC - a personal password manager on the owner's own machines. A
repo-wide commit path must not depend on it. In the cluster the Secret is already
decrypted, so the Job needs no age key and no `sops`, only RBAC to `get` one Secret. And
a day is still far earlier than the status quo, where the only signal was `failed to parse
blueprint` in a worker log nothing reads - #940 sat unnoticed for two days that way.

This also covers #883's third box (alert on `failed to parse blueprint` in the
`authentik-worker` log): the Job parses the same payload on a schedule and exits non-zero,
which reaches the same conclusion earlier and without log scraping.

Operational notes:

- **RBAC is one Secret wide.** The Role grants `get` on `resourceNames:
  [authentik-iac-blueprint]` only - no `list`, no `watch` (both ignore `resourceNames` and
  would hand the Job every Secret in the namespace, including `authentik-app-secrets`).
- **Alerting is inherited, not reinvented.** No ntfy URL is hardcoded in the CronJob.
  `KubeJobFailedLastRun` in `2-k3s/10.observability/alertmanager-config/custom-alerts.yaml`
  already covers CronJob-owned Jobs cluster-wide, so a failing run reaches Alertmanager
  and ntfy on the existing route.
- **One copy of the checker, two consumers.** `files/check_authentik_blueprint.py` is
  built into the `authentik-blueprint-check-script` ConfigMap by the `configMapGenerator`
  in `kustomization.yaml`, and the same file is run against synthetic plaintext fixtures
  by `.github/hooks/test-check-authentik-blueprint.sh`, which CI executes. The fixtures
  need no age key and no cluster. A fixture case asserts the ConfigMap is generated from
  that same path, so the suite cannot drift into testing a copy nobody deploys.
- **It refuses to pass vacuously.** If the Secret carries no `.yaml`/`.yml` key - say the
  blueprint key gets renamed - the check exits non-zero instead of reporting a clean run
  over zero payloads.
- **Baseline on `main` today**, so a future drop in coverage is visible:
  `entries=56 ids=56 absent=4 !KeyOf refs checked=32 !Find refs checked=135
  (sibling-matched=17)`. Measured identically through both paths - `sops -d` of the
  committed `.enc.yaml` (`stringData`) and `kubectl --context epaflix -n app-authentik get
  secret authentik-iac-blueprint -o json` (base64 `data`, the CronJob's own input).
- **Reading a run:**
  `kubectl --context epaflix -n app-authentik logs job/<authentik-blueprint-check-...>`.
  The checker reports YAML errors as `problem` plus line/column and locates violations by
  `entries[i]:<id>`; it never prints payload content, because `str(yaml.YAMLError)` embeds
  the offending source line and that line is Secret material (#602).

## Configuration

All configuration is managed via [helm-values.yaml](helm-values.yaml):

- **Global settings**: Image repository, tag, pull policy
- **Authentik config**: Secret key, database, email, logging
- **Server settings**: Replicas, resources, health probes, volumes
- **Worker settings**: Replicas, resources, volumes
- **Ingress**: Traefik IngressRoute via `additionalObjects`

### Updating Configuration

1. Edit [helm-values.yaml](helm-values.yaml)
2. Apply changes:
   ```bash
   helm upgrade authentik authentik/authentik \
     --namespace app-authentik \
     --values helm-values.yaml
   ```
3. Wait for rollout:
   ```bash
   kubectl --context epaflix rollout status deployment/authentik-server -n app-authentik
   kubectl --context epaflix rollout status deployment/authentik-worker -n app-authentik
   ```

**Warning**: Do NOT change `authentik.secret_key` after initial deployment - this will break sessions and user authentication.

## Authorization & Application Integration

Authentik provides centralized authentication and authorization for services. This section describes the authorization model, standard groups, and how to integrate applications.

### Authorization Model

Authentik separates **authentication** (who can sign in) from **authorization** (who can access which application):

1. **Authentication Sources**: Users can sign in via multiple methods (local password, Google OAuth, etc.)
2. **User Accounts**: Once authenticated, user account is created in Authentik
3. **Groups**: Users are assigned to groups (e.g., "Jellyseerr Users", "Grafana Admins")
4. **Applications**: Each application has policies that check group membership
5. **Authorization**: Only users in the required groups can access specific applications

**Key Principle**: Signing in with Google OAuth (or any source) creates an account but does NOT grant access to applications. Access requires explicit group membership.

### Standard Authorization Groups

The following groups are used for service access control. Create these in Authentik UI at **Directory → Groups** — **except** the rows marked *blueprint-declared*, which `authentik-iac-blueprint.enc.yaml` creates on apply. Do not hand-create those: the blueprint owns them, and a hand-made duplicate of the same name makes the blueprint's `!Find` ambiguous, which fails the entry and aborts the whole apply.

| Group Name | Slug | Purpose | Applications |
|------------|------|---------|--------------|
| `Servarr Users` | `servarr-users` | Access to all media services | Jellyseerr, Sonarr, Radarr, Prowlarr, Jellyfin, qBittorrent, etc. |
| `Grafana Admins` | `grafana-admins` | Grafana administrator access | Grafana (Admin role) |
| `Grafana Editors` | `grafana-editors` | Grafana editor access | Grafana (Editor role) |
| `Monitoring Users` | `monitoring-users` | Access to monitoring tools | Beszel, Grafana (Viewer) |
| `ArgoCD Admins` | `argocd-admins` | ArgoCD UI admin — maps to `role:admin` via `argocd-rbac-cm` `policy.csv` | ArgoCD (blueprint-declared, #1040) |

**Creating Groups:**
1. Navigate to **Directory → Groups** in Authentik UI
2. Click **Create**
3. Enter **Name** (slug auto-generated)
4. Click **Create**

### OAuth2/OIDC Providers

Applications integrate with Authentik via OAuth2/OIDC providers. Each application needs:

1. **Provider**: OAuth2/OIDC configuration (client ID, secret, redirect URIs, scopes)
2. **Application**: Binds provider to URL and policies
3. **Policy**: Group membership or custom authorization logic

**Standard OIDC Endpoints** (replace `<app-slug>` with application slug):
- **Issuer**: `https://auth.epaflix.com/application/o/<app-slug>/`
- **Authorization**: `https://auth.epaflix.com/application/o/authorize/`
- **Token**: `https://auth.epaflix.com/application/o/token/`
- **UserInfo**: `https://auth.epaflix.com/application/o/userinfo/`
- **Logout**: `https://auth.epaflix.com/application/o/<app-slug>/end-session/`
- **JWKS**: `https://auth.epaflix.com/application/o/<app-slug>/jwks/`

**Existing Providers:**
- **Jellyseerr**: OIDC for Jellyseerr/Seerr (see [08.servarr/seerr/authentik-provider-config.md](../../08.servarr/seerr/authentik-provider-config.md))
- **Grafana Monitor**: OAuth for Grafana (see [10.observability/grafana-config/](../../10.observability/grafana-config/))
- **Beszel Monitoring**: OAuth for Beszel monitoring dashboard
- **ArgoCD**: OIDC for the ArgoCD UI. Slug `argocd`, issuer `https://auth.epaflix.com/application/o/argocd/`, confidential, `sub_mode: user_uuid`, one regex redirect URI `https://argocd\.epaflix\.com/auth/callback`, 3 scope mappings (openid/profile/email — `groups` rides inside `profile`, it is not its own mapping). Blueprint-declared since #1040 in `authentik-iac-blueprint.enc.yaml`. Credentials live in three places that must move together: this blueprint, `argocd-secret`'s `oidc.authentik.clientId`/`clientSecret`, and `argocd_oidc_client_id`/`argocd_oidc_client_secret` in `.github/instructions/secrets.enc.yaml`. Consumer config: `2-k3s/11.argocd/helm-values.yaml` `configs.cm.oidc.config`.

### Rebuilding the ArgoCD login path from scratch (#1040)

ArgoCD's local `admin` account is **disabled** (`2-k3s/11.argocd/helm-values.yaml:59`, #1039), so this
Authentik provider is the only door into the tool that deploys Authentik. If SSO is broken rather than
missing, do not rebuild anything — go to `2-k3s/11.argocd/README.md` → "Break-glass: restoring local
admin login", which needs only `kubectl`.

What the blueprint reproduces with no UI clicks and no remembered values: the OAuth2 provider, the
application, the `ArgoCD Admins` group and the policy binding that gates the application on that group.
It works from nothing because every reference is a natural key rather than a captured UUID — flow slugs,
scope-mapping `managed` keys, the certificate name — and because the client credentials travel inside the
encrypted payload. All you need is the age key.

**The akadmin password no longer needs the setup flow (#1064).** `authentik-app-secrets` carries
`AUTHENTIK_BOOTSTRAP_PASSWORD_HASH` and `AUTHENTIK_BOOTSTRAP_EMAIL`, so a freshly built Authentik comes
up with a usable `akadmin` login and the rebuild never visits `/if/flow/initial-setup/`.

- **The plaintext lives in the credential store** under `authentik_bootstrap_password` in
  `.github/instructions/secrets.enc.yaml`. Read it with
  `sops -d --extract '["authentik_bootstrap_password"]'` and never echo it. The Secret holds only the
  pbkdf2 hash, so no usable password ever becomes a pod environment variable where `kubectl describe`
  or any env dump would surface it; upstream supports the hash on its own
  (`authentik/core/tests/test_setup.py:167`). A hash alone would leave nobody able to log in, which is
  why the plaintext is stored too.
- **Both variables are inert on an existing install.** `post_startup_setup_bootstrap`
  (`authentik/core/setup/signals.py`) skips every tenant where `Setup.get(tenant)` is true and logs
  "Tenant is already setup, skipping". Measured on this cluster: `tenant=public ready=True
  Setup.get=True`, and `akadmin.has_usable_password()` is `False` and stays `False`. They fire on a
  fresh install only. Adding them repairs nothing here and rotating them changes nothing here either.
- **No `AUTHENTIK_BOOTSTRAP_TOKEN`, deliberately.** The IaC API token `ak-iac` is already created by
  `authentik-iac-blueprint.enc.yaml`, so a bootstrap token would be a second standing superuser API
  credential with no consumer. `/blueprints/system/bootstrap.yaml` gates its token entry on
  `!If [!Context token]`, so omitting the variable skips that entry cleanly and no
  `authentik-bootstrap-token` Token is ever created.
- **Writing this Secret bounces SSO.** Both Deployments carry
  `secret.reloader.stakater.com/reload: "authentik-app-secrets"`, so any change to it rolls
  `authentik-server` and `authentik-worker` and new SSO logins fail for the length of the restart.

**The one step it still cannot do.** Named here so nobody has to rediscover it under pressure:

1. **Put an identity into `ArgoCD Admins`.** The blueprint creates the group but declares no members.
   Live membership is a single user that the blueprint does not declare, and declaring it means setting
   `attrs.groups` on that user, which is authoritative-replace across all five of its live groups — four
   of which the blueprint does not declare, so a `!Find` miss there rolls back the entire apply (#295).
   Tracked in #1065.

Until that is done, a rebuilt cluster has a complete ArgoCD login *path* with nobody authorised to walk it.

**Verify after a rebuild.** Run the two mechanical checks, then the login — the login is the only one
that settles anything:

```bash
# discovery answers for the argocd slug, and a bogus slug 404s (without the control the probe
# proves nothing - #541)
curl -4 -s -o /dev/null -w 'argocd: http=%{http_code}\n' --resolve auth.epaflix.com:443:192.168.10.101 \
  https://auth.epaflix.com/application/o/argocd/.well-known/openid-configuration
curl -4 -s -o /dev/null -w 'bogus:  http=%{http_code}\n' --resolve auth.epaflix.com:443:192.168.10.101 \
  https://auth.epaflix.com/application/o/zzz-bogus-nope/.well-known/openid-configuration

# the client_id still matches the other two stores (hash only, never the value - #602)
CID=$(kubectl --context epaflix -n argocd get secret argocd-secret \
        -o jsonpath='{.data.oidc\.authentik\.clientId}' | base64 -d)
printf 'len=%s sha=%s\n' "${#CID}" "$(printf %s "$CID" | sha256sum | cut -c1-12)"   # expect len=40 sha=d0789e2f448f
```

Then do **one real SSO login** to `argocd.epaflix.com` as a member of `ArgoCD Admins` and record whether
the session lands `role:admin` or `role:readonly`. Never conclude anything from `scopes_supported`:
misreading that field is what produced #1040's original premise, which was then retracted (#541, and the
rbac comment at `2-k3s/11.argocd/helm-values.yaml:107-112`).

### Forward Auth Integration

For applications that don't support OIDC, use Authentik's Forward Auth (Proxy Provider):

**Middleware**: [05.traefik-deployment/middleware/authentik-forwardauth.yaml](../../05.traefik-deployment/middleware/authentik-forwardauth.yaml)

**Setup Pattern**:
1. Create **Proxy Provider** in Authentik (Forward auth mode)
2. Create **Application** with group policy
3. Add `authentik-forwardauth` middleware to Traefik IngressRoute
4. Create outpost IngressRoute for `/outpost.goauthentik.io/` path

**Example**: [05.traefik-deployment/examples/protected-app-with-sso.yaml](../../05.traefik-deployment/examples/protected-app-with-sso.yaml)

**Current Forward Auth Applications**:
- **Traefik Dashboard**: `traefik.epaflix.com`
- **Newtarr**: `newtarr.epaflix.com` (group `Servarr users`; in-app login disabled — see issue #134 and [08.servarr/newtarr/ingressroute.yaml](../../08.servarr/newtarr/ingressroute.yaml))
- **Servarr UIs** (group `Servarr users`; rolled out in #176 — providers pk 126-135, Stage A blueprint PR #289 / Stage B IngressRoutes PR #291):
  - `sonarr.epaflix.com`, `sonarr2.epaflix.com`, `radarr.epaflix.com`, `prowlarr.epaflix.com`, `bazarr.epaflix.com` - fully gated on every path. These carried a **priority-20 `/api` bypass** (no middleware) from #176 until #296 deleted all six on 2026-08-10: every inter-app caller had already moved to internal Service DNS (#465, #466, #468), so the bypass had no consumer and published six full APIs to the internet.
  - `qbittorrent.epaflix.com` is the **exception, and it is not gated at all**. #296 deleted its `/api` bypass too, but the name does not resolve to the public LB: `/etc/dnsmasq.d/10-epaflix.conf` points it at `192.168.10.102`, the `traefik-internal` LoadBalancer, where `servarr/qbittorrent-internal` serves `Host(qbittorrent.epaflix.com)` on the `internal` entry point with **no middleware**. Measured 2026-08-10 from the LAN and from inside a servarr pod: root returns `200`, `/api/v2/app/version` returns `403` from qBittorrent's own auth, never an Authentik redirect. qBittorrent's built-in login (`WebUI\LocalHostAuth=true`) is the only control on that path, which is why its single-login half of #296 was declined - see [08.servarr/qbittorrent/ingressroute.yaml](../../08.servarr/qbittorrent/ingressroute.yaml) and #937. The gated `websecure` route still exists and still redirects, but no client reaches it by name.
  - `cleanuparr.epaflix.com`, `homarr.epaflix.com`, `lingarr.epaflix.com` - fully gated (never had an `/api` bypass)
  - `wizarr.epaflix.com` was in this set until wizarr was decommissioned (#295); its provider and routes are gone.
- **Admin UIs** (rolled out in #548 + #552; providers/applications named `<host>-forwardauth`, all fully gated with no `/api` bypass):

  | Host | Provider + application | Group | Why the gate is safe |
  |---|---|---|---|
  | `pegaprox.epaflix.com` | `pegaprox-forwardauth` | `PegaProx Admins` | Nothing calls PegaProx inbound; it is a *client* of the Proxmox API |
  | `truenas.epaflix.com` | `truenas-forwardauth` | `authentik Admins` | All TrueNAS automation is `ssh` + `midclt`, never the HTTP API via Traefik |
  | `minio-console.epaflix.com` | `minio-console-forwardauth` | `authentik Admins` | Console only — the S3 API on `minio.epaflix.com` is a **separate host** and stays ungated for CNPG/Barman SigV4 |
  | `grafana.epaflix.com` | `grafana-forwardauth` | `Grafana Admins` | Browser-only inbound; Prometheus scrapes `/metrics` via the in-cluster Service |

  These providers set **`intercept_header_auth: false`** (the servarr ones set it
  `true`). They are browser UIs that carry their own bearer/API-key headers after
  login — letting Authentik intercept `Authorization` risks it rejecting the
  app's own header and 401-ing a logged-in user.

  Each host needs a **distinct** provider + application because an Authentik
  application can carry only **one** provider, and `pegaprox`, `grafana-monitor`
  already have applications bound to their **OAuth2** providers
  for their own native OIDC login. Those are untouched.

  **`argocd.epaflix.com` is deliberately excluded** — the `argocd` CLI talks
  gRPC-web to that host and cannot do browser SSO, and ArgoCD is what deploys
  everything else. Rationale is recorded in
  [11.argocd/ingress.yaml](../../11.argocd/ingress.yaml).

#### Embedded outpost provider membership — now blueprint-declared (#293)

The embedded outpost's `providers` array (which providers it fronts) used to be
a **one-time imperative PATCH**: during the servarr rollout (#176) the 10 new
provider pks were appended to the outpost by hand, invisible to git and
clobber-prone on any future blueprint apply or Authentik reconcile. As of #293,
membership is declared in `authentik-iac-blueprint.enc.yaml` as an
`authentik_outposts.outpost` entry (`identifiers.name: "authentik Embedded
Outpost"`), listing every provider by `!Find [<model>, [name, "<name>"]]`
rather than by pk, so the reference survives pk churn and stays readable.

**This is authoritative-replace**: the outposts API treats `providers` as a
full-replacement list on every apply, not a merge. If the declared list is
wrong or incomplete, the missing providers silently drop out of the outpost
and lose their SSO gate on internet-reachable hosts. Keep the blueprint list
in **exact** sync with the outpost's live membership — after any change,
diff the declared names against `GET /api/v3/outposts/instances/` for the
embedded outpost's `providers[]`.

Cross-references: **#176/#289** (rollout that introduced the imperative
PATCH this replaces); **#404** (tracks removing the orphaned `syncthing`
provider from this list once its app/provider are torn down — it is
currently included because it is still live on the outpost).

**This failure mode already bit once.** The `Traefik Forward Auth` provider
(pk 6, `traefik.epaflix.com`) was never added to the declared list, so the
authoritative-replace dropped it from the outpost and
`https://traefik.epaflix.com/` served the outpost's **404** instead of a login
redirect — a documented forward-auth app that was quietly down. #548/#552
re-added it. Treat a 404 (not a 302) from a supposedly gated host as "this
provider is missing from the outpost", and always diff the declared names
against `GET /api/v3/outposts/instances/` after editing this list.

### Granting Service Access

**Workflow for adding users to applications:**

1. **User signs in**: User accesses application and clicks "Sign in with Authentik"
2. **Authentication**: User authenticates via Google OAuth (or other source)
3. **Account created**: User account created in Authentik (if first time)
4. **Access denied**: Application shows "Access Denied" (user not in required group)
5. **Admin grants access**:
   - Admin logs into Authentik at https://auth.epaflix.com
   - Navigate to **Directory → Users**
   - Search for and select the user
   - Go to **Groups** tab
   - Click **Add to existing group**
   - Select appropriate group (e.g., "Servarr Users" for media services)
   - Click **Add**
6. **User gains access**: User refreshes application or signs in again

**Alternative (bulk)**: Add users from group view:
1. Navigate to **Directory → Groups**
2. Select group (e.g., "Servarr Users")
3. Go to **Users** tab
4. Click **Add existing user**
5. Select multiple users
6. Click **Add**

### Removing Service Access

1. Admin logs into Authentik
2. Navigate to **Directory → Users** → select user
3. Go to **Groups** tab
4. Find the group (e.g., "Servarr Users")
5. Click **Remove** (trash icon)
6. User loses access to all services in that group on next authentication

### Admin / Automation API tokens

This is the single source of truth for how Authentik API tokens are issued and
used. Three distinct paths exist; pick the right one:

#### 1. Durable service-account token — the sanctioned path for IaC / automation (#185)

Any automation that **mutates Authentik objects** (providers, outpost provider
arrays, groups, bindings) during deploys uses a durable machine identity, **not**
an ad-hoc human token. This identity is created **declaratively** by a blueprint
shipped inside a SOPS-encrypted Secret:

- **Source of truth**: `authentik-iac-blueprint.enc.yaml` (this directory),
  decrypted by ksops on `argocd-repo-server`, mounted via the chart's
  `blueprints.secrets` (`helm-values.yaml`). The Authentik worker auto-applies
  the `*.yaml` key on sync.
- **What the blueprint creates**:
  - `ak-iac` — service account (`authentik_core.user`, `type: service_account`,
    path `users/service-accounts`).
  - the scoped **`authentik_rbac.role`** `ak-iac`, the non-superuser
    **`ak-iac IaC`** group bound to it, and the user's membership in that group
    only. The built-in **`authentik Admins`** membership this used to grant was
    removed in #339.
  - `ak-iac-token` — a **non-expiring** `intent: api` token whose `key` lives
    only in the SOPS Secret and (mirrored, for break-glass) in the credential
    store `.github/instructions/secrets.enc.yaml` as
    `authentik_iac_service_account_token`.

> **The quote trap is gone (#545).** The old plaintext read handed back the value
> with its double quotes attached, and a quoted token in an
> `Authorization: Bearer` header makes Authentik answer **403** - which looks
> exactly like a stale or revoked token. That is what caused the #293 `403`: it
> concluded the mirror had drifted. It had not. Verified 2026-08-03: the mirror
> is **byte-identical** to the blueprint's `key:` and both return `200`.
> `sops -d --extract` returns the YAML-parsed value, so quotes and escaping are
> handled for you and that failure mode cannot be reproduced.
> ```bash
> TOK=$(sops -d --extract '["authentik_iac_service_account_token"]' .github/instructions/secrets.enc.yaml)
> echo "${#TOK}"   # expect 64 - print the length, never the value
> curl -s -o /dev/null -w '%{http_code}\n' -m 10 \
>   -H "Authorization: Bearer $TOK" https://auth.epaflix.com/api/v3/core/users/me/   # expect 200
> ```
> Same check straight from the cluster, no age key needed - this is the real
> break-glass path, since the token is only useful while the API is up anyway:
> ```bash
> BP=$(kubectl --context epaflix -n app-authentik get secret authentik-iac-blueprint -o jsonpath='{.data.iac-service-account\.yaml}' \
>      | base64 -d | awk '/identifiers:/,0' | awk -F': *' '/^[[:space:]]*key:/{print $2; exit}')
> ```

Because the token is declared in git-as-SOPS, it survives Authentik DB rebuilds
(re-applied from the blueprint) and never silently expires mid-deploy — which is
exactly the failure #185 fixed (the old personal token expired twice mid-run
during the Odysseus SSO bring-up #183).

> **On every chart MINOR/MAJOR bump**, re-validate this blueprint against the new
> goauthentik schema — see
> [Re-validate the IaC blueprint on each chart MINOR bump (#232)](#re-validate-the-iac-blueprint-on-each-chart-minor-bump-232).

##### Scoped RBAC role - `ak-iac IaC` (#230 phase 1, #339 phase 2/3, flip landed)

`ak-iac` is **not a superuser**. Its only privilege comes from the `ak-iac IaC`
group and the scoped `ak-iac` role that the same blueprint provisions:

- `authentik_rbac.role` **`ak-iac`** - `attrs.permissions` lists only the
  **global** permissions the IaC and the token's own calls exercise:
  view/add/change/**delete** proxy providers, view/add/change OAuth2 providers,
  view/change outposts, view/add/change/**delete** applications,
  view/add/change groups, view/add/**delete** policy bindings, view/add users,
  view flows.
- `authentik_core.group` **`ak-iac IaC`** - non-superuser group bound to that
  role via the group `roles` relation.
- The `ak-iac` user is a member of **that group only**. Its built-in
  **`authentik Admins`** membership was removed in #339.

Because the role is now the *only* grant, a missing codename surfaces as a
**403** instead of being masked by superuser. That is the point of the flip.

> **The `authentik Admins` group itself is untouched** - it keeps
> `is_superuser: true` and its human members. Nothing else depended on
> `ak-iac`: **Grafana** authorizes off `Grafana Admins` / `Grafana Editors`, and
> **Jellyfin / servarr** off their own groups (e.g. `Servarr users`).

**Rollback is one line.** Re-add the Admins entry to the membership block in
`authentik-iac-blueprint.enc.yaml`:

```yaml
    - !Find [authentik_core.group, [name, "authentik Admins"]]
```

then re-apply. The apply lever needs **no token** (it runs inside the worker, so
it still works even if the role is broken):

```bash
kubectl --context epaflix -n app-authentik exec deploy/authentik-worker -- \
  ak apply_blueprint mounted/secret-authentik-iac-blueprint/iac-service-account.yaml
```

**What #339 verified.** The blueprint importer runs inside the worker and is not
RBAC-gated, so the only thing the role has to cover is the **token's own API
surface**. Measured from tracked files rather than from memory, that surface is:

| Call | Post-flip |
|---|---|
| `GET /core/users/me/` | 200 |
| `GET /admin/version/` | 200 |
| `GET /outposts/instances/` (+ one instance by UUID) | 200 |
| `POST /core/tokens/` → `GET .../view_key/` → `DELETE` | created / key readable / 204 |
| `GET /providers/proxy/`, `/core/applications/`, `/core/groups/`, `/policies/bindings/`, `/flows/instances/` | 200 |

Negative controls confirm the demotion is real and not cosmetic:
`GET /managed/blueprints/` → **403**, `GET /rbac/permissions/roles/` → **403**.

> `GET /providers/all/` also returns 403, and that is expected. It is the
> polymorphic base-`Provider` endpoint, called only from git-ignored
> `artifacts/` scratch scripts and never from tracked IaC - the base model has
> no `view_provider` codename to grant in the first place.

**Use it** (read the value from the SOPS credential store; never paste a literal
token into git):

```bash
# Verify the service-account token is live and correctly de-privileged:
curl -s -H "Authorization: Bearer <AUTHENTIK_IAC_SERVICE_ACCOUNT_TOKEN>" \
  https://auth.epaflix.com/api/v3/core/users/me/ | jq '.user.username, .user.is_superuser'
# Expect: "ak-iac"  false
```

##### Rotation

**Last rotated:** 2026-06-14 (issued #185) — bump this line every rotation.

**Cadence:** rotate **annually**, plus a **mandatory out-of-cycle rotation on
any suspected exposure** (age-key leak, credential-store leak, lost/stolen device,
or any reason to believe the token value escaped its at-rest stores). The
due-date is tracked via the **#169 periodic-review cadence** — there is no
CronJob or alert by design, because there is no in-cluster token-age signal to
fire one off.

**What "rotating" means here.** The token is **authoritative in the blueprint**:
the `key:` field of the `ak-iac-token` entry in
`authentik-iac-blueprint.enc.yaml`, which the Authentik worker **upserts** on the
stable `ak-iac-token` identifier. It is **mirrored** (for break-glass only) in the
credential store `.github/instructions/secrets.enc.yaml` as
`authentik_iac_service_account_token`. Rotating
therefore means **setting a NEW value in the blueprint** — do **NOT** mint a fresh
token in the Authentik UI, because a UI-minted value diverges from git and the next
blueprint apply would overwrite it.

**No consumer rollout-restart is required.** The token is **not** consumed by any
pod via a `secretKeyRef`/env var — it is used only ad-hoc by humans/IaC and by the
Authentik worker's blueprint engine — so the #299 env-var-secret rollout gotcha
(stale env value pinned until `rollout restart`) does **not** apply here.

**Re-key recipe** (use the CORRECT in-place SOPS form — never the broken
`sops -e <plaintext> > <enc>` redirect; see
[sops.instructions.md](../../.github/instructions/sops.instructions.md)):

```bash
# 1. Generate a new value (do NOT paste any real value into git):
NEW=$(openssl rand -hex 32)   # held in the shell for step 3, never printed

# 2. Edit the blueprint IN PLACE and replace ONLY the ak-iac-token `key:` value
#    (keep the `ak-iac-token` identifier so the worker UPSERTS rather than
#    creating a second token). Save re-encrypts via .sops.yaml:
sops 2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml

# 3. Update the credential-store mirror to the SAME value. `sops set` rewrites
#    only that one value, leaving the other entries byte-identical.
#    --value-stdin keeps the value out of argv; `jq -Rs .` JSON-encodes it,
#    which --value-stdin requires. Argument order is <file> then <index>:
printf %s "$NEW" | jq -Rs . \
  | sops set --value-stdin .github/instructions/secrets.enc.yaml '["authentik_iac_service_account_token"]'

#    Confirm the mirror took the same value the blueprint got, by hash only:
sops -d --extract '["authentik_iac_service_account_token"]' \
  .github/instructions/secrets.enc.yaml | tr -d '\n' | sha256sum | cut -c1-12
printf %s "$NEW" | sha256sum | cut -c1-12   # the two prefixes must match

#    Do NOT `git grep` the old token value to hunt stale copies. That puts it on
#    argv and into shell history, and a value in a retained transcript is burned
#    whatever the encoding (#602). A committed plaintext copy is already blocked
#    by .github/hooks/check-sops-encrypted.sh, which CI runs in --full-tree mode;
#    step 6 below is the real proof, since the old value stops working.

# 4. Branch, rebase onto origin/main, push --force-with-lease, wait for the
#    `validate` check, then:
gh pr merge <n> --merge

# 5. ArgoCD: sync app-authentik (Synced/Healthy), then make the worker re-apply
#    the blueprint:
argocd app sync app-authentik
kubectl --context epaflix -n app-authentik rollout restart deploy/authentik-worker
#    Confirm the BlueprintInstance reconciled: status = successful.

# 6. Validate the swap against live Authentik:
#    NEW token -> 200, username "ak-iac":
curl -s -H "Authorization: Bearer <NEW_AUTHENTIK_IAC_SERVICE_ACCOUNT_TOKEN>" \
  https://auth.epaflix.com/api/v3/core/users/me/ | jq '.user.username'
#    OLD token -> 401/403 (mint/use/revoke mechanics per #227).
```

Cross-references: **#185** (issued the durable declarative token); **#230** /
**#339** (the scoped-RBAC flip, landed 2026-08-10 - `ak-iac` is no longer a
superuser, so the validate step above expects `is_superuser: false` plus the
scoped `ak-iac IaC` role); the in-place SOPS recipe lives in
[sops.instructions.md](../../.github/instructions/sops.instructions.md).

#### 2. Personal superuser admin token — RETIRED (#175)

The standing, long-lived **personal** superuser admin API token (formerly the
`authentik_admin_api_token` key) has been **retired**. It exists nowhere: not in
the credential store `.github/instructions/secrets.enc.yaml`, not in any SOPS
file, not in cluster Secrets.
`grep -c '^authentik_admin_api_token:' .github/instructions/secrets.enc.yaml`
returns `0`. The live token
object was deleted in Authentik by the owner. The durable service-account token
above (#185) is its supervised replacement for automation.

#### 3. On-demand scoped + expiring token — for one-off human use

When a human needs a one-off privileged API call (and does not want to use the
service-account token), mint a short-lived, scoped token by hand:

1. **Mint**: Authentik UI → **Directory → Tokens → Create** (or create a
   dedicated service account and issue its token). Set an explicit **short
   expiry** and a **least-privilege** intent — only the access the task needs.
2. **Use it** for the single task at hand.
3. **Delete it immediately** afterward: **Directory → Tokens** → trash icon. Do
   not leave the token at rest or copy it into a file.

**Verify** it is live (and later that it is gone):

```bash
curl -s -o /dev/null -w "%{http_code}" -m 10 \
  -H "Authorization: Bearer <SCOPED_TOKEN>" \
  https://auth.epaflix.com/api/v3/core/users/me/
```

`200` while valid; `401`/`403` once deleted or expired.

##### API-scriptable path

The same lifecycle is fully scriptable against the admin API — useful when an
operator wants to drive it from a shell rather than the UI. Authenticate the
mint / revoke calls with an existing admin credential (e.g. the durable `ak-iac`
service-account token read from the credential store, key
`authentik_iac_service_account_token` - never paste a literal token into git;
read it with the `sops -d --extract` one-liner above, which returns the
YAML-parsed value, never a bare `grep` that could echo it):

```bash
BASE=https://auth.epaflix.com
H=(-H "Authorization: Bearer <ADMIN_TOKEN>" -H "Content-Type: application/json")

# 1. Mint a scoped, expiring token (Authentik applies a default expiry):
curl -s "${H[@]}" -X POST $BASE/api/v3/core/tokens/ \
  -d '{"identifier":"my-task","intent":"api","expiring":true,"description":"one-off"}'   # -> 201

# 2. Retrieve the key — ONLY via the view_key/ sub-resource (the token object
#    itself never returns the key):
curl -s "${H[@]}" $BASE/api/v3/core/tokens/my-task/view_key/   # -> {"key":"..."}

# 3. Use the key for the task, then revoke:
curl -s -o /dev/null -w "%{http_code}" "${H[@]}" \
  -X DELETE $BASE/api/v3/core/tokens/my-task/   # -> 204
```

The minted token's `key` is retrievable **only** through
`GET /api/v3/core/tokens/{identifier}/view_key/`; the `GET .../tokens/{id}/`
object never includes it.

**Least-privilege option**: pass an explicit `"user": <pk>` in the mint body to
issue the token against a service account scoped to exactly the objects the task
touches, instead of inheriting `ak-iac`'s own scoped role. Since #339, `ak-iac`
is not a superuser, so this is no longer a demotion, only a narrowing.

> Validated end-to-end 2026-06-14 against live Authentik: mint `201` → retrieve
> key via `view_key/` → admin read (`GET /api/v3/admin/version/`) `200` → revoke
> `204` → post-revoke read `403`.

> Issue #134 created the original standing personal admin token; #175 retired it;
> #185 introduced the durable declarative service-account token as the standing
> automation identity. This section supersedes the standalone runbook drafted in
> PR #225.

### Google OAuth Configuration

To allow users to sign in with Google (creates accounts but doesn't grant app access):

1. Create Google OAuth2 credentials in Google Cloud Console
2. In Authentik, navigate to **Directory → Federation & Social login**
3. Click **Create** → **Google OAuth2 Source**
4. Configure:
   - **Name**: `Google`
   - **Slug**: `google`
   - **Consumer Key**: Google Client ID
   - **Consumer Secret**: Google Client Secret
   - **Scopes**: `openid email profile`
   - **Provider Type**: `google`
5. Configure **Flow Settings**:
   - **Authentication flow**: `default-authentication-flow`
   - **Enrollment flow**: `default-enrollment-flow` (or custom flow)
6. Click **Create**

**Important**: Signing in with Google creates an account in Authentik but does NOT grant access to any applications. Users must be added to groups manually.

### Security Best Practices

1. **Always require group membership**: Bind group policies to all applications
2. **Monitor new sign-ups**: Regularly review **Directory → Users** for new accounts
3. **Principle of least privilege**: Only grant necessary access
4. **Audit regularly**: Review group memberships periodically
5. **Disable unused sources**: Remove authentication sources you don't use
6. **Enable MFA**: Configure multi-factor authentication for sensitive access
7. **Review event logs**: Check **Events → Logs** for suspicious activity

### Application Integration Guides

Detailed integration instructions for specific applications:

- **Jellyseerr/Seerr (OIDC)**: [08.servarr/seerr/authentik-provider-config.md](../../08.servarr/seerr/authentik-provider-config.md)
- **Grafana (OAuth)**: [10.observability/grafana-config/](../../10.observability/grafana-config/)
- **Traefik Dashboard (Forward Auth)**: [05.traefik-deployment/ingress/traefik-dashboard-sso.yaml](../../05.traefik-deployment/ingress/traefik-dashboard-sso.yaml)
- **Protected App Template (Forward Auth)**: [05.traefik-deployment/examples/protected-app-with-sso.yaml](../../05.traefik-deployment/examples/protected-app-with-sso.yaml)

## Verification

### Check Deployment Status

```bash
# Check Helm release
helm list -n app-authentik
helm status authentik -n app-authentik

# Check all resources
kubectl --context epaflix -n app-authentik get all

# Check pods
kubectl --context epaflix -n app-authentik get pods -o wide

# Check PVC binding
kubectl --context epaflix -n app-authentik get pvc

# Check IngressRoute
kubectl --context epaflix -n app-authentik get ingressroute
```

### Check Logs

```bash
# Server logs
kubectl --context epaflix -n app-authentik logs -l app.kubernetes.io/name=authentik,app.kubernetes.io/component=server -f

# Worker logs
kubectl --context epaflix -n app-authentik logs -l app.kubernetes.io/name=authentik,app.kubernetes.io/component=worker -f

# All Authentik logs
kubectl --context epaflix -n app-authentik logs -l app.kubernetes.io/name=authentik -f --max-log-requests=10
```

### Verify Certificate

```bash
# Check Traefik logs for ACME/Let's Encrypt
kubectl --context epaflix -n traefik-system logs -l app.kubernetes.io/name=traefik | grep -i acme

# Test HTTPS
curl -I https://auth.epaflix.com
```

## Scaling

### Scale Server Replicas

```bash
# Edit helm-values.yaml and change server.replicas
# Then apply:
helm upgrade authentik authentik/authentik \
  --namespace app-authentik \
  --values helm-values.yaml

# Or use kubectl (temporary, will revert on next Helm upgrade):
kubectl --context epaflix -n app-authentik scale deployment/authentik-server --replicas=3
```

### Scale Worker Replicas

```bash
# Edit helm-values.yaml and change worker.replicas
# Then apply:
helm upgrade authentik authentik/authentik \
  --namespace app-authentik \
  --values helm-values.yaml

# Or use kubectl (temporary):
kubectl --context epaflix -n app-authentik scale deployment/authentik-worker --replicas=2
```

**Note:** Media PVC uses `local-path` (ReadWriteOnce). Multi-replica access shares via the pod's mounted PVC.

## Troubleshooting

### Pods Not Starting

```bash
# Check pod events
kubectl --context epaflix -n app-authentik describe pods

# Check logs
kubectl --context epaflix -n app-authentik logs -l app.kubernetes.io/name=authentik --tail=100

# Common issues:
# - PVC not bound: Check local-path provisioner is running
# - Database connection: Verify CloudNativePG cluster is running at 192.168.10.105
# - Image pull: Verify ghcr.io is accessible
```

### Certificate Not Issued

```bash
# Check Traefik logs
kubectl --context epaflix -n traefik-system logs -l app.kubernetes.io/name=traefik | grep -i acme

# Common issues:
# - Cloudflare API token invalid
# - DNS not propagated
# - Rate limit hit (Let's Encrypt has rate limits)
```

### Database Connection Issues

```bash
# Test database connection from within cluster
kubectl --context epaflix run -it --rm psql-test --image=postgres:16 --restart=Never -- \
  psql "postgresql://authentik:<AUTHENTIK_DB_PASSWORD>@192.168.10.105:5432/authentik" -c "SELECT version();"

# Check CloudNativePG cluster status
kubectl --context epaflix -n postgres-system get cluster
kubectl --context epaflix -n postgres-system get pods
```

### Cannot Access https://auth.epaflix.com

```bash
# Check IngressRoute
kubectl --context epaflix -n app-authentik get ingressroute
kubectl --context epaflix -n app-authentik describe ingressroute authentik-https

# Check service
kubectl --context epaflix -n app-authentik get svc

# Check Traefik is running
kubectl --context epaflix -n traefik-system get pods,svc

# Check DNS (from local machine)
nslookup auth.epaflix.com

# Check router port forwarding: 80/443 → 192.168.10.101
```

### SMTP Issues

```bash
# Test SMTP from within a pod
kubectl --context epaflix -n app-authentik exec -it deployment/authentik-server -- ak test_email admin@example.com

# Check logs for SMTP errors
kubectl --context epaflix -n app-authentik logs -l app.kubernetes.io/name=authentik,app.kubernetes.io/component=server | grep -i smtp
```

## Uninstall

```bash
# Uninstall Helm release (keeps namespace, PVC, and PV)
helm uninstall authentik -n app-authentik

# Delete namespace and storage (optional)
kubectl --context epaflix delete -f storage/pv-pvc.yaml
kubectl --context epaflix delete -f namespace.yaml

# Clean media PVC (optional — will be recreated on next deploy)
kubectl --context epaflix delete pvc authentik-media-pvc -n app-authentik
```

**Note:** This does NOT delete the PostgreSQL database. To clean the database:

```bash
PGPASSWORD='<AUTHENTIK_DB_PASSWORD>' psql \
  -h 192.168.10.105 \
  -U authentik \
  -d authentik \
  -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
```

## File Structure

```
07.authentik-deployment/
├── helm-values.yaml          # Helm configuration
├── namespace.yaml            # Namespace definition
├── storage/
│   └── pv-pvc.yaml          # Storage definitions (historical, now uses local-path)
├── upgrade.sh                # Helm version upgrade
└── README.md                 # This file
```

## Disaster Recovery Workflow

**Scenario**: Cluster rebuild or complete data loss

1. **Deploy infrastructure**:
   - Deploy Traefik at 192.168.10.101
   - Deploy CloudNativePG cluster
   - Create `authentik` database

2. **Deploy fresh Authentik**:
   ```bash
   ./deploy.sh
   ```

3. **Restore from backup**:
   ```bash
   ./restore.sh /path/to/backup-directory
   ```

4. **Verify**:
   - Test login at https://auth.epaflix.com
   - Verify users and settings restored
   - Test authentication flows

## Connection Information

### Database Connection

```bash
Host: 192.168.10.105
Port: 5432
Database: authentik
User: authentik
Password: <AUTHENTIK_DB_PASSWORD>
```

### SMTP Configuration

Transport settings live in [helm-values.yaml](helm-values.yaml) (`authentik.email`):
port `587`, `use_tls: true` (STARTTLS), `use_ssl: false`, `timeout: 30`.

Host, username, from and password are **not in git** — the repo is public, so
they are only in the SOPS-encrypted `authentik-app-secrets`
([authentik-app-secrets.enc.yaml](authentik-app-secrets.enc.yaml)) as
`AUTHENTIK_EMAIL__HOST` / `__USERNAME` / `__FROM` / `__PASSWORD`, mirrored in
the credential store `.github/instructions/secrets.enc.yaml` under the
`auth_email_*` keys - **three** of them (`hostname`, `username`, `password`).
There is deliberately no `auth_email_from` key: `__FROM` reuses the
`auth_email_username` value, per the relay constraint below (#979). Env vars override
the chart's own config, which is what makes the split work.

`from` is set to the **same mailbox as `username`**: the relay rejects a sender
that is not the authenticated mailbox, which is why the old
`noreply@epaflix.com` could never have worked even with a reachable host.

> **Do not put `mail.epaflix.com` back (#461).** That name has no record of its
> own, so the proxied `*.epaflix.com` Cloudflare wildcard answers for it with
> `104.21.59.155` / `172.67.179.219` — hosts that serve 443 and carry no SMTP.
> Port 587 times out. Measured from a pod in `app-authentik`, 2026-08-04. The
> real relay node is the one the domain's own MX and SPF records already name —
> the DirectAdmin DNS-only carve-out from the wildcard — and its TLS cert
> matches that hostname, so STARTTLS verifies with no skip-verify. Alertmanager
> had the identical pair of breaks; PR #684 fixed that half, and
> `alertmanager-config-secret.enc.yaml` holds the same smarthost value.

Rotation: the Secret is consumed as **env vars** via `global.envFrom`, and env
values are resolved once at pod start, so a changed value does not reach a
running pod on its own (#299). Since #780 that restart is automatic - both
Deployments carry
`secret.reloader.stakater.com/reload: "authentik-app-secrets"` (set through
`global.deploymentAnnotations` in [helm-values.yaml](helm-values.yaml)) and the
`reloader-app-authentik` instance in
[16.reloader](../16.reloader/kustomization.yaml) rolls
`authentik-server` + `authentik-worker` when the Secret data changes.

Reloader is content-hash driven, not sync driven: it injects a
`STAKATER_AUTHENTIK_APP_SECRETS_SECRET=<hash>` env var into the pod template
and only rolls when that hash moves. A routine ArgoCD sync re-applying the same
bytes rolls nothing, so the restart happens only at the moment a value actually
changes. That restart still briefly interrupts SSO - including forward-auth for
the 10 servarr UIs (#176) - so rotate when you can watch it.

Manual fallback, if Reloader is down or you need the restart now:

```bash
kubectl --context epaflix rollout restart deploy/authentik-server deploy/authentik-worker -n app-authentik
```

Verify a real send (not just config):

```bash
kubectl --context epaflix exec deploy/authentik-worker -n app-authentik -- \
  sh -c 'ak test_email "$AUTHENTIK_EMAIL__USERNAME"'   # self-send, no address in argv
```

### Admin Credentials

```bash
Username: akadmin
Password: credential store key `authentik_bootstrap_password`
```

That password is what a **fresh** install bootstraps into, via
`AUTHENTIK_BOOTSTRAP_PASSWORD_HASH` in `authentik-app-secrets` (#1064). On this cluster `akadmin`
predates that seeding and `has_usable_password()` is `False`, so it cannot log in at all; of the three
`authentik Admins` members exactly one has a usable password, and that per-person account is the admin
login here. See "Rebuilding the ArgoCD login path from scratch".

## Additional Resources

- [Authentik Documentation](https://docs.goauthentik.io/)
- [Authentik Kubernetes Installation](https://docs.goauthentik.io/install-config/install/kubernetes/)
- [Authentik Helm Chart on ArtifactHub](https://artifacthub.io/packages/helm/goauthentik/authentik)
- [Authentik Configuration](https://docs.goauthentik.io/install-config/configuration/)
- [Traefik Documentation](https://doc.traefik.io/traefik/)
- [CloudNativePG Documentation](https://cloudnative-pg.io/documentation/)

## Support

For issues specific to this deployment:
1. Check logs: `kubectl --context epaflix -n app-authentik logs -l app.kubernetes.io/name=authentik`
2. Check pod status: `kubectl --context epaflix -n app-authentik get pods`
3. Review troubleshooting section above
4. Check Helm release: `helm status authentik -n app-authentik`
5. Check Authentik documentation for application-specific issues
