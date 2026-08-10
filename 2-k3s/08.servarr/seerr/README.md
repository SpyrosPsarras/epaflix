# Seerr — Media Request Management

This directory contains the single, canonical media-request deployment for the
servarr stack. It is named **seerr** and is reconciled by ArgoCD via
`2-k3s/08.servarr/kustomization.yaml`.

## What this is (and is NOT)

This is a **NAME consolidation**, not an image migration. There used to be two
identical media-request deployments — `jellyseerr` and `seerr` — running the
**same** image on the **same** PVC and the **same** ClusterIP port 5055. The
redundant `jellyseerr` Deployment/Service has been **retired**; `seerr` is the
surviving deployment.

It deliberately keeps running the upstream **fork** image, because OIDC support
is not in any stable seerr release:

| Property         | Value                                    |
|------------------|------------------------------------------|
| Image            | `fallenbagel/jellyseerr:preview-OIDC`    |
| Security context | UID/GID/fsGroup **568** (`PUID`/`PGID` 568) |
| Config storage   | PVC **`jellyseerr-config`** (legacy name, reused) |
| Database         | Postgres DB/user **`jellyseerr`** (legacy names) |
| DB credentials   | `servarr-postgres` Secret, keys `jellyseerr-host/-port/-user/-password/-database` |
| Health probes    | HTTP `GET /` on the `http` port (5055)   |
| Service          | ClusterIP `seerr:5055`                   |

> There is **no** upstream `ghcr.io/seerr-team/seerr` image in use, **no** init
> container, **no** UID-1000 user, and **no** `/api/v1/status` probe. Earlier
> revisions of this README described a migration to that upstream image — that
> migration never happened and the description was removed to avoid steering an
> operator into an unintended image swap + permission change against the live
> data PVC.

## Data-safety rule

**Never rename** the PVC `jellyseerr-config`, the Postgres DB/user `jellyseerr`,
or the `jellyseerr-*` keys in the `servarr-postgres` Secret. They are legacy
names retained on purpose; renaming any of them forces a data migration that is
out of scope. The `jellyseerr/backups/` directory and
`jellyseerr/backup-jellyseerr-db.sh` remain valid for rollback.

## Ingress / routing

Both hosts resolve to the `seerr` Service (see
`../_shared/ingress/public-routes.yaml`):

- `seerr.epaflix.com` — canonical
- `jellyseerr.epaflix.com` — legacy, kept so the OIDC redirect/callback URIs
  registered per-host keep resolving. There is intentionally **no** redirect
  from the legacy host to the canonical one.

## Files in this directory

- `seerr.yaml` — the Deployment + ClusterIP Service (canonical)
- `pdb.yaml` — PodDisruptionBudget
- `authentik-oidc-secret.yaml` — OIDC credentials template (imperative; see kustomization notes)
- `authentik-provider-config.md` — Authentik OIDC provider setup
- `AUTHENTIK-SECURITY-IMPLEMENTATION.md` — security model notes
- `README.md` — this file
- `QUICKSTART.md` — quick reference

## Operations

```bash
# Status / logs
kubectl --context epaflix get pods -n servarr -l app=seerr
kubectl --context epaflix logs -n servarr -l app=seerr -f

# Internal reachability
kubectl --context epaflix get svc seerr -n servarr
kubectl --context epaflix get endpoints seerr -n servarr
```

ArgoCD owns these manifests — do not `kubectl apply` or `kubectl edit` against
the live cluster for routine changes; commit to git and let ArgoCD reconcile.
selfHeal will revert manual edits to managed resources.

### Backups

```bash
cd ../jellyseerr
./backup-jellyseerr-db.sh   # dumps the jellyseerr DB + the running seerr pod's /app/config
```

This produces `jellyseerr/backups/jellyseerr-db-backup-<ts>.sql.gz` and
`jellyseerr/backups/jellyseerr-config-<ts>.tar.gz`.

### Rollback

The consolidation is a pure-GitOps change with **no data risk** (no
data-bearing resource was renamed or deleted). To re-introduce the redundant
`jellyseerr` Deployment, `git revert` the consolidation commit and let ArgoCD
re-sync. If you ever need to restore the database from a backup:

```bash
cd ../jellyseerr/backups
gunzip jellyseerr-db-backup-<timestamp>.sql.gz

DB_HOST=$(kubectl --context epaflix get secret -n servarr servarr-postgres -o jsonpath='{.data.jellyseerr-host}' | base64 -d)
DB_PORT=$(kubectl --context epaflix get secret -n servarr servarr-postgres -o jsonpath='{.data.jellyseerr-port}' | base64 -d)
DB_USER=$(kubectl --context epaflix get secret -n servarr servarr-postgres -o jsonpath='{.data.jellyseerr-user}' | base64 -d)
DB_PASS=$(kubectl --context epaflix get secret -n servarr servarr-postgres -o jsonpath='{.data.jellyseerr-password}' | base64 -d)
DB_NAME=$(kubectl --context epaflix get secret -n servarr servarr-postgres -o jsonpath='{.data.jellyseerr-database}' | base64 -d)

kubectl --context epaflix run jellyseerr-restore-pod \
  --namespace=servarr --image=postgres:16-alpine --restart=Never --rm --attach \
  --env="PGPASSWORD=${DB_PASS}" \
  --command -- psql -h "${DB_HOST}" -p "${DB_PORT}" -U "${DB_USER}" -d "${DB_NAME}" \
  < jellyseerr-db-backup-<timestamp>.sql
```

## Troubleshooting

### Pod stuck in CrashLoopBackOff

```bash
kubectl --context epaflix logs -n servarr -l app=seerr --tail=100
```

Common causes: permission issues on `/app/config` (the pod runs as UID 568 —
the `jellyseerr-config` PVC contents must be owned 568:568), or a database
connection failure (check the `servarr-postgres` Secret keys and connectivity).

### Cannot access via ingress

```bash
kubectl --context epaflix get ingressroute -n servarr | grep -E 'seerr'
kubectl --context epaflix get endpoints seerr -n servarr
```

All four IngressRoutes (`seerr-https`, `seerr-http`, `jellyseerr-https`,
`jellyseerr-http`) must back the `seerr` Service. If any backed the retired
`jellyseerr` Service, both hosts would 503.

## OIDC Authentication Setup with Authentik

The `fallenbagel/jellyseerr:preview-OIDC` image supports OIDC authentication.
This section describes how to integrate with Authentik for secure, group-based
access control.

### Why OIDC Authentication?

- **Centralized authentication**: Users sign in with Google OAuth through Authentik
- **Group-based authorization**: Only users in specific Authentik groups can access Seerr
- **Separation of concerns**: Authentication (who can sign in) is separate from authorization (who can access this app)
- **Security**: Prevents anyone with a Google account from automatically accessing your media server

### Configuration Files

- **[authentik-provider-config.md](authentik-provider-config.md)**: Complete guide for configuring Authentik as OIDC provider
- **[authentik-oidc-secret.yaml](authentik-oidc-secret.yaml)**: Kubernetes secret template for OIDC credentials

### Quick Setup Steps

1. **Configure Authentik** (see [authentik-provider-config.md](authentik-provider-config.md) for detailed instructions):
   - Create "Servarr Users" group in Authentik (covers all media services)
   - Create OAuth2/OIDC provider for Seerr
   - Create application with group-based access policy
   - Only users in "Servarr Users" group will be authorized

2. **Create Kubernetes secret** with OIDC credentials:
   ```bash
   # Get Client ID and Secret from Authentik provider
   kubectl --context epaflix create secret generic seerr-oidc-secret -n servarr \
     --from-literal=client-id='<AUTHENTIK_CLIENT_ID>' \
     --from-literal=client-secret='<AUTHENTIK_CLIENT_SECRET>'
   ```

3. **Configure OIDC** (via web UI at https://seerr.epaflix.com):
   - Navigate to Settings → Authentication (or Settings → Services → OIDC)
   - Enable OIDC authentication
   - Configure with Authentik endpoints:
     - **Issuer URL**: `https://auth.epaflix.com/application/o/jellyseerr/`
     - **Authorization URL**: `https://auth.epaflix.com/application/o/authorize/`
     - **Token URL**: `https://auth.epaflix.com/application/o/token/`
     - **UserInfo URL**: `https://auth.epaflix.com/application/o/userinfo/`
     - **Client ID**: From Kubernetes secret
     - **Client Secret**: From Kubernetes secret
     - **Button Label**: "Sign in with Authentik"

4. **Test authorization**:
   - Sign out
   - Visit https://seerr.epaflix.com
   - Click "Sign in with Authentik"
   - Sign in with Google (via Authentik)
   - If not in "Servarr Users" group → Access denied
   - Admin adds user to group in Authentik → Access granted to all media services

### User Management

**Adding Users:**
1. User signs in with Google → Account created in Authentik
2. User tries to access Seerr → Access denied (not in group)
3. Admin logs into Authentik (https://auth.epaflix.com)
4. Navigate to Directory → Users → select user
5. Go to Groups tab → Add to "Servarr Users" group
6. User can now access Seerr and all other Servarr/Jellyfin services

**Removing Users:**
1. Admin logs into Authentik
2. Navigate to Directory → Users → select user
3. Go to Groups tab → Remove from "Servarr Users" group
4. User loses access to all media services on next authentication

### Security Notes

- **Google OAuth does NOT grant automatic access**: Signing in with Google creates an account in Authentik but does not grant access to applications
- **Manual approval required**: Admins must explicitly add users to the "Servarr Users" group
- **Group-based authorization**: Application-level policies enforce that only group members can access
- **Audit regularly**: Review Directory → Users in Authentik to monitor new sign-ups

For complete configuration instructions, see [authentik-provider-config.md](authentik-provider-config.md).
