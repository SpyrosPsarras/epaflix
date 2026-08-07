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
kubectl apply -f namespace.yaml

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
MEDIA_POD=$(kubectl get pod -n app-authentik -l app.kubernetes.io/name=authentik -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n app-authentik $MEDIA_POD -- tar czf - /media > authentik-media-$(date +%Y%m%d).tar.gz
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
kubectl -n app-authentik scale deployment/authentik-server --replicas=0
kubectl -n app-authentik scale deployment/authentik-worker --replicas=0

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
kubectl -n app-authentik scale deployment/authentik-server --replicas=2
kubectl -n app-authentik scale deployment/authentik-worker --replicas=1
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
- [ ] Explicit token `key` still honored for `intent: api` (the `ak-iac-token` value stays the one mirrored in `secrets.yml` — not regenerated by the upgrade).
- [ ] `blueprints.secrets` still consumed as a plain string list (the chart mounts each named Secret and the worker auto-discovers `*.yaml`); verify the rendered shape with `kustomize build 2-k3s/07.authentik-deployment --enable-helm`.
- [ ] **Post-upgrade apply confirmation:** the `iac-service-account` BlueprintInstance reports `status: successful`; if it shows 403 / not-applied, trigger blueprint discovery or `kubectl -n authentik rollout restart deploy/authentik-worker` (the #185 worker-discovery gotcha). Confirm the `ak-iac` user + token are present via `GET /api/v3/core/users/me/` → 200.

Cross-references: **#185** (the durable declarative token this guards); **#230** (once its Phase 2/3
flip lands and `ak-iac` drops `authentik Admins`, the post-upgrade superuser expectation changes to
the scoped `ak-iac IaC` role).

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
   kubectl rollout status deployment/authentik-server -n app-authentik
   kubectl rollout status deployment/authentik-worker -n app-authentik
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

The following groups are used for service access control. Create these in Authentik UI at **Directory → Groups**:

| Group Name | Slug | Purpose | Applications |
|------------|------|---------|--------------|
| `Servarr Users` | `servarr-users` | Access to all media services | Jellyseerr, Sonarr, Radarr, Prowlarr, Jellyfin, qBittorrent, etc. |
| `Grafana Admins` | `grafana-admins` | Grafana administrator access | Grafana (Admin role) |
| `Grafana Editors` | `grafana-editors` | Grafana editor access | Grafana (Editor role) |
| `Monitoring Users` | `monitoring-users` | Access to monitoring tools | Beszel, Grafana (Viewer) |

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
  - `sonarr.epaflix.com`, `sonarr2.epaflix.com`, `radarr.epaflix.com`, `prowlarr.epaflix.com`, `qbittorrent.epaflix.com`, `bazarr.epaflix.com` — UI gated, with a **priority-20 `/api` bypass** (no middleware) so API-key / inter-app traffic continues unchanged
  - `cleanuparr.epaflix.com`, `homarr.epaflix.com`, `lingarr.epaflix.com` — fully gated (no `/api` bypass)
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
  - membership in the built-in **`authentik Admins`** superuser group.
  - `ak-iac-token` — a **non-expiring** `intent: api` token whose `key` lives
    only in the SOPS Secret and (mirrored, for break-glass) in the git-ignored
    `.github/instructions/secrets.yml` as `authentik_iac_service_account_token`.

> **Reading the mirror: strip the quotes (#545).** The `secrets.yml` value is
> double-quoted. A naive `grep`/`awk -F': '` read carries the quotes into the
> `Authorization: Bearer` header and Authentik answers **403** - which looks
> exactly like a stale or revoked token, and that is how #293 concluded the
> mirror had drifted. It had not. Verified 2026-08-03: the quote-stripped mirror
> is **byte-identical** to the blueprint's `key:` and both return `200`.
> ```bash
> SEC=.github/instructions/secrets.yml
> TOK=$(sed -n 's/^authentik_iac_service_account_token:[[:space:]]*"\{0,1\}\([^"]*\)"\{0,1\}[[:space:]]*$/\1/p' "$SEC")
> echo "${#TOK}"   # expect 64 (66 means you captured the quotes) - print the length, never the value
> curl -s -o /dev/null -w '%{http_code}\n' -m 10 \
>   -H "Authorization: Bearer $TOK" https://auth.epaflix.com/api/v3/core/users/me/   # expect 200
> ```
> Same check straight from the cluster, no age key needed - this is the real
> break-glass path, since the token is only useful while the API is up anyway:
> ```bash
> BP=$(kubectl -n app-authentik get secret authentik-iac-blueprint -o jsonpath='{.data.*}' \
>      | base64 -d | awk '/identifiers:/,0' | awk -F': *' '/^[[:space:]]*key:/{print $2; exit}')
> ```

Because the token is declared in git-as-SOPS, it survives Authentik DB rebuilds
(re-applied from the blueprint) and never silently expires mid-deploy — which is
exactly the failure #185 fixed (the old personal token expired twice mid-run
during the Odysseus SSO bring-up #183).

> **On every chart MINOR/MAJOR bump**, re-validate this blueprint against the new
> goauthentik schema — see
> [Re-validate the IaC blueprint on each chart MINOR bump (#232)](#re-validate-the-iac-blueprint-on-each-chart-minor-bump-232).

##### Scoped RBAC role — `ak-iac IaC` (Phase 1 of #230, additive)

As of Phase 1 of #230, the same blueprint **also** provisions a least-privilege
path for `ak-iac`, so that the eventual flip off blanket superuser is a one-line
change rather than a re-design:

- `authentik_rbac.role` **`ak-iac`** — a role whose `attrs.permissions` lists only
  the **global** permissions the IaC blueprints actually exercise: view/add/change
  proxy + OAuth2 providers, view/change outposts, view/add/change applications,
  view/add/change groups, view/add policy bindings, view/add users, and view flows.
- `authentik_core.group` **`ak-iac IaC`** — a non-superuser group bound to that
  role via the group `roles` relation.
- The `ak-iac` user is now a member of **both** the new `ak-iac IaC` group **and**
  the existing built-in **`authentik Admins`** superuser group.

**This change is purely additive.** `ak-iac` is still a superuser this PR (it keeps
its `authentik Admins` membership); the scoped role does not *reduce* any capability.
Dropping `authentik Admins` (the actual privilege reduction) is deliberately
**deferred** to the verify-then-flip follow-up of #230 so it can be gated on proof
that the scoped permission set is sufficient.

> **The `authentik Admins` group itself is untouched** — its membership and
> `is_superuser: true` are unchanged. Nothing else depends on this blueprint's
> group set: **Grafana** authorizes off its own `Grafana Admins` / `Grafana Editors`
> groups, and **Jellyfin / servarr** authorize off their own groups (e.g.
> `Servarr users`). None of them reference `ak-iac` or `ak-iac IaC`.

**Phase 2/3 verification plan (before the flip):** mint a **scoped-ONLY** token on a
*temporary* service account that carries the `ak-iac IaC` role **but not**
`authentik Admins`, then exercise the full IaC op-list against the admin API as that
token:

- `POST` create a proxy/OAuth2 **provider**, an **application**, a **group**, and a
  **policy binding**;
- `GET` then `PATCH` an **outpost**'s `providers[]` array (the embedded outpost
  assignment the forward-auth blueprints rely on, per #185);
- `GET` **users** and **flows** (the `!Find`/`!KeyOf` lookups the blueprints resolve).

If any call returns **403**, widen `ak-iac` role `attrs.permissions` by exactly the
missing codename and re-verify; only once every op succeeds do we drop `ak-iac` from
`authentik Admins`. Delete the temporary SA + token immediately after the exercise.
Reverting the flip is trivial — re-add the `authentik Admins` `!Find` to the
membership entry.

**Use it** (read the value from `secrets.yml` / the SOPS Secret; never paste a
literal token into git):

```bash
# Verify the service-account token is live and superuser:
curl -s -H "Authorization: Bearer <AUTHENTIK_IAC_SERVICE_ACCOUNT_TOKEN>" \
  https://auth.epaflix.com/api/v3/core/users/me/ | jq '.user.username, .user.is_superuser'
# Expect: "ak-iac"  (and is_superuser true via authentik Admins)
```

##### Rotation

**Last rotated:** 2026-06-14 (issued #185) — bump this line every rotation.

**Cadence:** rotate **annually**, plus a **mandatory out-of-cycle rotation on
any suspected exposure** (age-key leak, `secrets.yml` leak, lost/stolen device,
or any reason to believe the token value escaped its at-rest stores). The
due-date is tracked via the **#169 periodic-review cadence** — there is no
CronJob or alert by design, because there is no in-cluster token-age signal to
fire one off.

**What "rotating" means here.** The token is **authoritative in the blueprint**:
the `key:` field of the `ak-iac-token` entry in
`authentik-iac-blueprint.enc.yaml`, which the Authentik worker **upserts** on the
stable `ak-iac-token` identifier. It is **mirrored** (for break-glass only) in the
git-ignored `secrets.yml` as `authentik_iac_service_account_token`. Rotating
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
openssl rand -hex 32

# 2. Edit the blueprint IN PLACE and replace ONLY the ak-iac-token `key:` value
#    (keep the `ak-iac-token` identifier so the worker UPSERTS rather than
#    creating a second token). Save re-encrypts via .sops.yaml:
sops 2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml

# 3. Update the secrets.yml mirror to the SAME value, then confirm no stale copy
#    of the OLD value lingers anywhere in the tree:
#    set authentik_iac_service_account_token = <new value>
git grep '<OLD_TOKEN_VALUE>'   # expect: no matches

# 4. Branch, rebase onto origin/main, push --force-with-lease, wait for the
#    `validate` check, then:
gh pr merge <n> --merge

# 5. ArgoCD: sync app-authentik (Synced/Healthy), then make the worker re-apply
#    the blueprint:
argocd app sync app-authentik
kubectl -n authentik rollout restart deploy/authentik-worker
#    Confirm the BlueprintInstance reconciled: status = successful.

# 6. Validate the swap against live Authentik:
#    NEW token -> 200, username "ak-iac":
curl -s -H "Authorization: Bearer <NEW_AUTHENTIK_IAC_SERVICE_ACCOUNT_TOKEN>" \
  https://auth.epaflix.com/api/v3/core/users/me/ | jq '.user.username'
#    OLD token -> 401/403 (mint/use/revoke mechanics per #227).
```

Cross-references: **#185** (issued the durable declarative token); **#230** (the
scoped-RBAC flip — once its Phase 2/3 flip lands and `ak-iac` drops
`authentik Admins`, the validate step's expectation changes from
`is_superuser: true` to the scoped `ak-iac IaC` role); **#339**; the in-place SOPS
recipe lives in
[sops.instructions.md](../../.github/instructions/sops.instructions.md).

#### 2. Personal superuser admin token — RETIRED (#175)

The standing, long-lived **personal** superuser admin API token (formerly the
`secrets.yml` key `authentik_admin_api_token`) has been **retired**. There is
**no** standing personal admin token kept at rest anywhere — not in
`secrets.yml`, not in any SOPS file, not in cluster Secrets. The live token
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
service-account token read from `secrets.yml`, key
`authentik_iac_service_account_token` — never paste a literal token into git;
read it with the quote-stripping form above, not a bare `grep`, or you get a
misleading `403`, see #545):

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
issue the token against a dedicated **non-superuser** service account scoped to
exactly the objects the task touches, instead of inheriting the `ak-iac`
superuser identity.

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
kubectl -n app-authentik get all

# Check pods
kubectl -n app-authentik get pods -o wide

# Check PVC binding
kubectl -n app-authentik get pvc

# Check IngressRoute
kubectl -n app-authentik get ingressroute
```

### Check Logs

```bash
# Server logs
kubectl -n app-authentik logs -l app.kubernetes.io/name=authentik,app.kubernetes.io/component=server -f

# Worker logs
kubectl -n app-authentik logs -l app.kubernetes.io/name=authentik,app.kubernetes.io/component=worker -f

# All Authentik logs
kubectl -n app-authentik logs -l app.kubernetes.io/name=authentik -f --max-log-requests=10
```

### Verify Certificate

```bash
# Check Traefik logs for ACME/Let's Encrypt
kubectl -n traefik-system logs -l app.kubernetes.io/name=traefik | grep -i acme

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
kubectl -n app-authentik scale deployment/authentik-server --replicas=3
```

### Scale Worker Replicas

```bash
# Edit helm-values.yaml and change worker.replicas
# Then apply:
helm upgrade authentik authentik/authentik \
  --namespace app-authentik \
  --values helm-values.yaml

# Or use kubectl (temporary):
kubectl -n app-authentik scale deployment/authentik-worker --replicas=2
```

**Note:** Media PVC uses `local-path` (ReadWriteOnce). Multi-replica access shares via the pod's mounted PVC.

## Troubleshooting

### Pods Not Starting

```bash
# Check pod events
kubectl -n app-authentik describe pods

# Check logs
kubectl -n app-authentik logs -l app.kubernetes.io/name=authentik --tail=100

# Common issues:
# - PVC not bound: Check local-path provisioner is running
# - Database connection: Verify CloudNativePG cluster is running at 192.168.10.105
# - Image pull: Verify ghcr.io is accessible
```

### Certificate Not Issued

```bash
# Check Traefik logs
kubectl -n traefik-system logs -l app.kubernetes.io/name=traefik | grep -i acme

# Common issues:
# - Cloudflare API token invalid
# - DNS not propagated
# - Rate limit hit (Let's Encrypt has rate limits)
```

### Database Connection Issues

```bash
# Test database connection from within cluster
kubectl run -it --rm psql-test --image=postgres:16 --restart=Never -- \
  psql "postgresql://authentik:<AUTHENTIK_DB_PASSWORD>@192.168.10.105:5432/authentik" -c "SELECT version();"

# Check CloudNativePG cluster status
kubectl -n postgres-system get cluster
kubectl -n postgres-system get pods
```

### Cannot Access https://auth.epaflix.com

```bash
# Check IngressRoute
kubectl -n app-authentik get ingressroute
kubectl -n app-authentik describe ingressroute authentik-https

# Check service
kubectl -n app-authentik get svc

# Check Traefik is running
kubectl -n traefik-system get pods,svc

# Check DNS (from local machine)
nslookup auth.epaflix.com

# Check router port forwarding: 80/443 → 192.168.10.101
```

### SMTP Issues

```bash
# Test SMTP from within a pod
kubectl -n app-authentik exec -it deployment/authentik-server -- ak test_email admin@example.com

# Check logs for SMTP errors
kubectl -n app-authentik logs -l app.kubernetes.io/name=authentik,app.kubernetes.io/component=server | grep -i smtp
```

## Uninstall

```bash
# Uninstall Helm release (keeps namespace, PVC, and PV)
helm uninstall authentik -n app-authentik

# Delete namespace and storage (optional)
kubectl delete -f storage/pv-pvc.yaml
kubectl delete -f namespace.yaml

# Clean media PVC (optional — will be recreated on next deploy)
kubectl delete pvc authentik-media-pvc -n app-authentik
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
the git-ignored `secrets.yml` under the `auth_email_*` keys. Env vars override
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

Rotation: the Secret is consumed as **env vars** via `global.envFrom`, so a
changed value does **not** roll the pods (#299). Reloader is not deployed in
`app-authentik` (only `servarr`, `traefik-system`, `odysseus` — see
[16.reloader](../16.reloader/kustomization.yaml)), so after any change:

```bash
kubectl rollout restart deploy/authentik-server deploy/authentik-worker -n app-authentik
```

This briefly interrupts SSO — including forward-auth for the 10 servarr UIs (#176).

Verify a real send (not just config):

```bash
kubectl exec deploy/authentik-worker -n app-authentik -- \
  sh -c 'ak test_email "$AUTHENTIK_EMAIL__USERNAME"'   # self-send, no address in argv
```

### Admin Credentials

```bash
Username: akadmin
Password: (Set during initial-setup)
```

## Additional Resources

- [Authentik Documentation](https://docs.goauthentik.io/)
- [Authentik Kubernetes Installation](https://docs.goauthentik.io/install-config/install/kubernetes/)
- [Authentik Helm Chart on ArtifactHub](https://artifacthub.io/packages/helm/goauthentik/authentik)
- [Authentik Configuration](https://docs.goauthentik.io/install-config/configuration/)
- [Traefik Documentation](https://doc.traefik.io/traefik/)
- [CloudNativePG Documentation](https://cloudnative-pg.io/documentation/)

## Support

For issues specific to this deployment:
1. Check logs: `kubectl -n app-authentik logs -l app.kubernetes.io/name=authentik`
2. Check pod status: `kubectl -n app-authentik get pods`
3. Review troubleshooting section above
4. Check Helm release: `helm status authentik -n app-authentik`
5. Check Authentik documentation for application-specific issues
