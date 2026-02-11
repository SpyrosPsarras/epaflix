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
- **Version**: 2025.12.1 (configurable in [helm-values.yaml](helm-values.yaml))

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
  --version 2025.12.1
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

```bash
Host: mail.epaflix.com
Port: 587
From: noreply@epaflix.com
Username: truenas_admin
Password: <SMTP_PASSWORD>
TLS: Enabled
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
