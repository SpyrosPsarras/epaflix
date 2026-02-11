# Jellyseerr to Seerr Migration Guide

This directory contains the deployment manifests and instructions for migrating from Jellyseerr to Seerr.

## What is Seerr?

Seerr is the official successor to Jellyseerr, providing a unified media request management solution. This migration is necessary as Jellyseerr has been discontinued in favor of Seerr.

For more information, see:
- [Seerr Release Announcement](https://docs.seerr.dev/blog/seerr-release)
- [Official Migration Guide](https://docs.seerr.dev/migration-guide)

## Migration Overview

The migration process involves:
1. **Backing up** the existing Jellyseerr database and configuration
2. **Scaling down** the Jellyseerr deployment
3. **Deploying** Seerr with updated configuration
4. **Automatic migration** - Seerr will automatically migrate your data on first startup
5. **Updating** ingress and DNS (optional, to use seerr.epaflix.com)

## Key Changes

### Image
- **Old**: `fallenbagel/jellyseerr:preview-OIDC`
- **New**: `ghcr.io/seerr-team/seerr:latest`

### Security Context
- **Old**: UID 568 (custom user)
- **New**: UID 1000 (node user)
- An init container fixes permissions on the config directory

### Environment Variables
- Removed: `PUID`, `PGID` (Seerr manages user internally)
- Added: `LOG_LEVEL`, `PORT`
- Database credentials: Unchanged (still using same PostgreSQL database)

### Health Checks
- Updated to use `/api/v1/status` endpoint (recommended by Seerr)

## Files in this Directory

- `seerr.yaml` - Main deployment and service manifest
- `ingress.yaml` - Ingress configuration for seerr.epaflix.com
- `README.md` - This file

## Prerequisites

Before starting the migration:
1. Ensure you have kubectl access to the cluster
2. Verify the jellyseerr deployment is running
3. Have access to the servarr namespace

## Migration Steps

### 1. Backup Jellyseerr Data

**CRITICAL**: Always backup before migration!

Run the backup script in the jellyseerr directory:

```bash
cd ../jellyseerr
chmod +x backup-jellyseerr-db.sh
./backup-jellyseerr-db.sh
```

This will create:
- `backups/jellyseerr-db-backup-<timestamp>.sql.gz` - PostgreSQL database dump
- `backups/jellyseerr-config-<timestamp>.tar.gz` - Config directory backup

**Keep these backups safe!** You'll need them if you need to rollback.

### 2. Scale Down Jellyseerr

Stop the jellyseerr deployment:

```bash
kubectl scale deployment jellyseerr -n servarr --replicas=0
```

Verify it's stopped:

```bash
kubectl get pods -n servarr -l app=jellyseerr
# Should show no pods or STATUS=Terminating
```

### 3. Fix Config Directory Permissions (Optional but Recommended)

Since Seerr runs as UID 1000 instead of UID 568, we need to fix permissions. The seerr.yaml includes an init container that does this automatically, but you can also do it manually:

```bash
# Get the PV name
kubectl get pvc jellyseerr-config -n servarr

# The init container in seerr.yaml will handle this automatically
```

### 4. Deploy Seerr

Apply the Seerr manifests:

```bash
cd ../seerr
kubectl apply -f seerr.yaml
```

### 5. Monitor the Migration

Watch the Seerr pod startup and automatic migration:

```bash
# Watch pod status
kubectl get pods -n servarr -l app=seerr -w

# View logs to see migration progress
kubectl logs -n servarr -l app=seerr -f
```

You should see logs indicating:
- Database connection established
- Automatic migration running (if coming from Jellyseerr)
- Application startup

### 6. Verify Seerr is Working

Check the service:

```bash
kubectl get svc seerr -n servarr
```

Test internal access:

```bash
kubectl run -it --rm test-seerr --image=busybox --restart=Never -- wget -O- http://seerr:5055/api/v1/status
```

You should see a JSON response with status information.

### 7. Update Ingress (Optional)

If you want to use the new domain `seerr.epaflix.com`:

```bash
kubectl apply -f ingress.yaml
```

This will:
- Create a new ingress for seerr.epaflix.com
- Request a new TLS certificate from Let's Encrypt
- The old jellyseerr.epaflix.com ingress will remain active

**Option A**: Keep both domains pointing to Seerr
```bash
# Point jellyseerr ingress to seerr service
kubectl patch ingress jellyseerr -n servarr --type='json' -p='[{"op": "replace", "path": "/spec/rules/0/http/paths/0/backend/service/name", "value":"seerr"}]'
```

**Option B**: Use only new domain
```bash
# Delete old ingress
kubectl delete ingress jellyseerr -n servarr
```

### 8. Update DNS (if using new domain)

Add a DNS record pointing `seerr.epaflix.com` to your ingress IP or update your existing jellyseerr DNS.

### 9. Clean Up Old Jellyseerr Deployment (After Verification)

Once you've verified everything works for at least 24-48 hours:

```bash
# Delete the jellyseerr deployment (keeps PVC)
kubectl delete deployment jellyseerr -n servarr
kubectl delete service jellyseerr -n servarr

# Optionally delete the old ingress if you created a new one
kubectl delete ingress jellyseerr -n servarr
```

**DO NOT delete the PVC** - Seerr is using the same config storage!

## Rollback Procedure

If something goes wrong:

### Quick Rollback (if Seerr pod is failing)

```bash
# Scale down Seerr
kubectl scale deployment seerr -n servarr --replicas=0

# Scale up Jellyseerr
kubectl scale deployment jellyseerr -n servarr --replicas=1
```

### Full Rollback (if database was migrated and broken)

```bash
# Scale down Seerr
kubectl delete deployment seerr -n servarr

# Restore database backup
cd ../jellyseerr/backups
gunzip jellyseerr-db-backup-<timestamp>.sql.gz

# Get DB credentials
DB_HOST=$(kubectl get secret -n servarr servarr-postgres -o jsonpath='{.data.jellyseerr-host}' | base64 -d)
DB_PORT=$(kubectl get secret -n servarr servarr-postgres -o jsonpath='{.data.jellyseerr-port}' | base64 -d)
DB_USER=$(kubectl get secret -n servarr servarr-postgres -o jsonpath='{.data.jellyseerr-user}' | base64 -d)
DB_PASS=$(kubectl get secret -n servarr servarr-postgres -o jsonpath='{.data.jellyseerr-password}' | base64 -d)
DB_NAME=$(kubectl get secret -n servarr servarr-postgres -o jsonpath='{.data.jellyseerr-database}' | base64 -d)

# Restore database
kubectl run jellyseerr-restore-pod \
  --namespace=servarr \
  --image=postgres:15-alpine \
  --restart=Never \
  --rm \
  --attach \
  --env="PGPASSWORD=${DB_PASS}" \
  --command -- psql \
  -h "${DB_HOST}" \
  -p "${DB_PORT}" \
  -U "${DB_USER}" \
  -d "${DB_NAME}" \
  < jellyseerr-db-backup-<timestamp>.sql

# Scale up Jellyseerr
kubectl scale deployment jellyseerr -n servarr --replicas=1
```

## Troubleshooting

### Seerr pod stuck in CrashLoopBackOff

Check logs:
```bash
kubectl logs -n servarr -l app=seerr --tail=100
```

Common issues:
- **Permission denied on /app/config**: The init container should fix this, but verify PVC permissions
- **Database connection failed**: Check database credentials and connectivity
- **Migration failed**: Check logs for specific migration errors

### Cannot access Seerr via ingress

```bash
# Check ingress status
kubectl get ingress seerr -n servarr
kubectl describe ingress seerr -n servarr

# Check certificate status
kubectl get certificate seerr-tls -n servarr
kubectl describe certificate seerr-tls -n servarr

# Check service endpoints
kubectl get endpoints seerr -n servarr
```

### Data is missing after migration

Seerr performs an automatic migration. If data is missing:
1. Check the logs during first startup for migration messages
2. Verify you're using the same database (check DB_NAME, DB_HOST)
3. Consider restoring from backup and trying again

## Post-Migration Tasks

After successful migration:

1. **Test all functionality**:
   - Login with your existing credentials
   - Verify media requests are visible
   - Test creating new requests
   - Check integrations (Sonarr, Radarr, etc.)

2. **Update bookmarks** and links to use the new domain (if changed)

3. **Update any automation** or scripts that reference the old service

4. **Monitor for a few days** before deleting the old Jellyseerr deployment

## OIDC Authentication Setup with Authentik

Jellyseerr/Seerr supports OIDC authentication via the `fallenbagel/jellyseerr:preview-OIDC` image. This section describes how to integrate with Authentik for secure, group-based access control.

### Why OIDC Authentication?

- **Centralized authentication**: Users sign in with Google OAuth through Authentik
- **Group-based authorization**: Only users in specific Authentik groups can access Jellyseerr
- **Separation of concerns**: Authentication (who can sign in) is separate from authorization (who can access this app)
- **Security**: Prevents anyone with a Google account from automatically accessing your media server

### Configuration Files

- **[authentik-provider-config.md](authentik-provider-config.md)**: Complete guide for configuring Authentik as OIDC provider
- **[authentik-oidc-secret.yaml](authentik-oidc-secret.yaml)**: Kubernetes secret template for OIDC credentials

### Quick Setup Steps

1. **Configure Authentik** (see [authentik-provider-config.md](authentik-provider-config.md) for detailed instructions):
   - Create "Servarr Users" group in Authentik (covers all media services)
   - Create OAuth2/OIDC provider for Jellyseerr
   - Create application with group-based access policy
   - Only users in "Servarr Users" group will be authorized

2. **Create Kubernetes secret** with OIDC credentials:
   ```bash
   # Get Client ID and Secret from Authentik provider
   kubectl create secret generic seerr-oidc-secret -n servarr \
     --from-literal=client-id='<AUTHENTIK_CLIENT_ID>' \
     --from-literal=client-secret='<AUTHENTIK_CLIENT_SECRET>'
   ```

3. **Configure Jellyseerr OIDC** (via web UI at https://seerr.epaflix.com):
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
   - Sign out of Jellyseerr
   - Visit https://seerr.epaflix.com
   - Click "Sign in with Authentik"
   - Sign in with Google (via Authentik)
   - If not in "Servarr Users" group → Access denied
   - Admin adds user to group in Authentik → Access granted to all media services

### User Management

**Adding Users:**
1. User signs in with Google → Account created in Authentik
2. User tries to access Jellyseerr → Access denied (not in group)
3. Admin logs into Authentik (https://auth.epaflix.com)
4. Navigate to Directory → Users → select user
5. Go to Groups tab → Add to "Servarr Users" group
6. User can now access Jellyseerr and all other Servarr/Jellyfin services

**Removing Users:**
1. Admin logs into Authentik
2. Navigate to Directory → Users → select user
3. Go to Groups tab → Remove from "Servarr Users" group
4. User loses access to all media services on next authentication

### Security Notes

- **Google OAuth does NOT grant automatic access**: Signing in with Google creates an account in Authentik but does not grant access to applications
- **Manual approval required**: Admins must explicitly add users to the "Jellyseerr Users" group
- **Group-based authorization**: Application-level policies enforce that only group members can access
- **Audit regularly**: Review Directory → Users in Authentik to monitor new sign-ups

For complete configuration instructions, see [authentik-provider-config.md](authentik-provider-config.md).

## Support

- [Seerr Documentation](https://docs.seerr.dev/)
- [Seerr Discord](https://discord.gg/seerr)
- [Seerr GitHub Issues](https://github.com/seerr-team/seerr/issues)
