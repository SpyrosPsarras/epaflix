# Grafana OAuth/Authentik Login Troubleshooting Guide

## Problem: Cannot login to Grafana with Authentik OAuth

### Symptoms
- User authenticates successfully in Authentik
- Redirect back to Grafana fails
- Error in logs: `"Failed to create user" error="user not found"`
- OAuth login doesn't create user account automatically

## Root Causes & Solutions

### 1. Missing `allow_sign_up` Configuration

**Problem**: Grafana was missing the `allow_sign_up: true` setting in OAuth configuration, preventing automatic user creation.

**Solution**: Added to `prometheus-values.yaml`:
```yaml
auth.generic_oauth:
  enabled: true
  name: Authentik
  client_id: <GRAFANA_OAUTH_CLIENT_ID>
  client_secret: $__file{/etc/secrets/grafana-oauth/client_secret}
  scopes: openid email profile
  auth_url: https://auth.epaflix.com/application/o/authorize/
  token_url: https://auth.epaflix.com/application/o/token/
  api_url: https://auth.epaflix.com/application/o/userinfo/
  role_attribute_path: contains(groups[*], 'Grafana Admins') && 'Admin' || contains(groups[*], 'Grafana Editors') && 'Editor' || 'Viewer'
  allow_sign_up: true      # ← CRITICAL: Allows auto-creation of users
  auto_login: false        # ← Keeps admin login available
  use_pkce: true          # ← Security enhancement
```

### 2. Grafana Using SQLite Instead of PostgreSQL

**Problem**: Even with correct OAuth config, Grafana was connecting to SQLite (in-memory) instead of PostgreSQL, causing user creation failures.

**Symptom in logs**:
```
logger=sqlstore level=info msg="Connecting to DB" dbtype=sqlite3
```

**Expected behavior**:
```
logger=sqlstore level=info msg="Connecting to DB" dbtype=postgres
```

**Cause**: Configuration changes in Helm values require pod restart to take effect. Simply running `helm upgrade` updates ConfigMaps but doesn't force pod restart if image/resources unchanged.

**Solution**: After Helm upgrade, manually restart Grafana pods:
```bash
# Apply Helm upgrade
helm upgrade kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n observability \
  -f prometheus-values.yaml \
  --wait --timeout=5m

# Force pod restart to reload configuration
kubectl delete pods -n observability -l app.kubernetes.io/name=grafana

# Wait for pods to be ready
kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=grafana -n observability --timeout=120s
```

### 3. Verify Database Connection

Check that Grafana is using PostgreSQL:
```bash
# Check logs for database type
kubectl logs -n observability -l app.kubernetes.io/name=grafana -c grafana | grep "Connecting to DB"

# Expected output:
# logger=sqlstore level=info msg="Connecting to DB" dbtype=postgres

# Check database secret is mounted
kubectl exec -n observability deployment/kube-prometheus-stack-grafana -c grafana -- cat /etc/secrets/grafana-db/password
# Should output: <POSTGRES_PASSWORD>

# Test PostgreSQL connectivity
kubectl exec -n observability deployment/kube-prometheus-stack-grafana -c grafana -- \
  nc -zv postgres-rw.postgres-system.svc.cluster.local 5432
# Expected: postgres-rw.postgres-system.svc.cluster.local (IP:5432) open
```

## Complete Fix Procedure

### Step 1: Update Helm Values

Ensure `prometheus-values.yaml` has correct OAuth configuration:
```yaml
grafana:
  grafana.ini:
    auth:
      disable_login_form: false
      oauth_auto_login: false

    auth.generic_oauth:
      enabled: true
      name: Authentik
      client_id: <GRAFANA_OAUTH_CLIENT_ID>
      client_secret: $__file{/etc/secrets/grafana-oauth/client_secret}
      scopes: openid email profile
      auth_url: https://auth.epaflix.com/application/o/authorize/
      token_url: https://auth.epaflix.com/application/o/token/
      api_url: https://auth.epaflix.com/application/o/userinfo/
      role_attribute_path: contains(groups[*], 'Grafana Admins') && 'Admin' || contains(groups[*], 'Grafana Editors') && 'Editor' || 'Viewer'
      allow_sign_up: true
      auto_login: false
      use_pkce: true
```

### Step 2: Apply Configuration

```bash
cd 2-k3s/10.observability

# Upgrade Helm release
helm upgrade kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n observability \
  -f prometheus-values.yaml \
  --wait --timeout=5m

# Force restart pods (REQUIRED for config to take effect)
kubectl delete pods -n observability -l app.kubernetes.io/name=grafana

# Wait for restart
kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=grafana -n observability --timeout=120s
```

### Step 3: Verify Configuration

```bash
# Check OAuth settings are applied
kubectl get configmap -n observability kube-prometheus-stack-grafana -o yaml | grep -A 15 "auth.generic_oauth"

# Verify allow_sign_up is true
# Expected:
# [auth.generic_oauth]
# allow_sign_up = true
# ...

# Check database type in logs
kubectl logs -n observability -l app.kubernetes.io/name=grafana -c grafana --tail=50 | grep "Connecting to DB"

# Should show: dbtype=postgres (NOT sqlite3)
```

### Step 4: Test Login

1. Go to https://grafana.epaflix.com
2. Click "Sign in with Authentik"
3. Authenticate with your Authentik credentials
4. **First time**: User should be automatically created
5. Role assigned based on group membership:
   - `Grafana Admins` group → Admin role
   - `Grafana Editors` group → Editor role
   - Any other user → Viewer role

## Authentik Configuration Checklist

### 1. OAuth Provider Setup

Navigate to **Applications → Providers** in Authentik (https://auth.epaflix.com):

- **Name**: Grafana Monitor
- **Client ID**: `<GRAFANA_OAUTH_CLIENT_ID>`
- **Client Secret**: (stored in k8s secret `grafana-oauth-secret`)
- **Redirect URIs**: `https://grafana\.epaflix\.com/login/generic_oauth`
- **Scopes**: `openid`, `email`, `profile`
- **Client Type**: Confidential
- **Authorization flow**: default-provider-authorization-implicit-consent

### 2. Application Setup

Navigate to **Applications → Applications**:

- **Name**: Grafana
- **Slug**: grafana
- **Provider**: (select Grafana Monitor provider)
- **Launch URL**: https://grafana.epaflix.com

### 3. Group-Based Access Control

Create groups in **Directory → Groups**:

- **Grafana Admins** - Full admin access
- **Grafana Editors** - Edit dashboards
- (Optional) **Grafana Viewers** - View-only access

### 4. Add Policy Binding

In the Grafana application:

1. Go to **Policy / Group / User Bindings** tab
2. Click **Create and bind Policy**
3. Configure:
   - **Name**: Grafana - Group Access Policy
   - **Policy type**: Group Membership Policy
   - **Group**: Select `Grafana Admins` (or appropriate group)
   - **Order**: 0
   - **Enabled**: ✅

### 5. Add Users to Groups

Navigate to **Directory → Users** → select user → **Groups** tab → **Add to existing group**

## Troubleshooting OAuth Errors

### Error: "Failed to create user" in logs

**Check**:
```bash
# Verify allow_sign_up is enabled
kubectl exec -n observability deployment/kube-prometheus-stack-grafana -c grafana -- \
  cat /etc/grafana/grafana.ini | grep -A 5 "auth.generic_oauth"
```

**Solution**: Ensure `allow_sign_up = true` and pods have been restarted.

### Error: "Invalid redirect URI"

**Check**: Authentik provider redirect URI must match exactly:
- Configured: `https://grafana\.epaflix\.com/login/generic_oauth` (regex with escaped dots)
- Actual callback: `https://grafana.epaflix.com/login/generic_oauth`

**Solution**: Update redirect URI in Authentik provider settings.

### Error: "Invalid client" or "Unauthorized"

**Check**:
```bash
# Verify client secret
kubectl get secret grafana-oauth-secret -n observability -o jsonpath='{.data.client_secret}' | base64 -d
```

**Solution**: Regenerate credentials in Authentik and update Kubernetes secret:
```bash
kubectl create secret generic grafana-oauth-secret -n observability \
  --from-literal=client_secret='NEW_CLIENT_SECRET_FROM_AUTHENTIK' \
  --dry-run=client -o yaml | kubectl apply -f -

# Restart pods to use new secret
kubectl delete pods -n observability -l app.kubernetes.io/name=grafana
```

### User has wrong role (Viewer instead of Admin)

**Check**: User group membership in Authentik:
```
Directory → Users → [select user] → Groups tab
```

**Solution**: Add user to appropriate group:
- For Admin access: Add to `Grafana Admins` group
- For Editor access: Add to `Grafana Editors` group

**Note**: Role changes require re-login to take effect.

### User can't access Grafana at all

**Check**: Application policy binding in Authentik:
```
Applications → Applications → Grafana → Policy / Group / User Bindings
```

**Solution**: 
1. Verify policy exists and is enabled
2. Ensure user is member of group specified in policy
3. Test with admin account first to rule out policy issues

## Monitoring OAuth Logins

### Watch login attempts in real-time

```bash
# Follow Grafana logs for OAuth events
kubectl logs -n observability -l app.kubernetes.io/name=grafana -c grafana -f | grep -i oauth
```

### Check successful logins

```bash
# Look for successful user creation
kubectl logs -n observability -l app.kubernetes.io/name=grafana -c grafana | grep "user.sync"
```

### Expected successful login flow in logs

```
logger=authn.service level=info msg="Successfully authenticated user" client=auth.client.generic_oauth id=...
logger=user.sync level=info msg="User synchronized" auth_module=oauth_generic_oauth auth_id=...
```

## Emergency Access

### Reset admin password (if locked out)

```bash
kubectl exec -n observability deployment/kube-prometheus-stack-grafana -c grafana -- \
  grafana cli admin reset-admin-password "<POSTGRES_PASSWORD>"
```

### Disable OAuth temporarily

```bash
# Edit Helm values
# Set: auth.generic_oauth.enabled: false
helm upgrade kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n observability \
  -f prometheus-values.yaml

# Restart pods
kubectl delete pods -n observability -l app.kubernetes.io/name=grafana
```

## Important Notes

1. **Always restart pods** after Helm configuration changes for settings to take effect
2. **PostgreSQL is required** - SQLite doesn't support multi-pod deployments and has permission issues
3. **First login creates user** - Subsequent logins update user info and role
4. **Role mapping is dynamic** - Changes to group membership require re-login
5. **OAuth and local login coexist** - `disable_login_form: false` keeps admin login available

## References

- Grafana OAuth2 Generic: https://grafana.com/docs/grafana/latest/setup-grafana/configure-security/configure-authentication/generic-oauth/
- Authentik OAuth2 Provider: https://goauthentik.io/docs/providers/oauth2/
- Helm Values: `2-k3s/10.observability/prometheus-values.yaml`
- OAuth Secret: `2-k3s/10.observability/grafana-config/oauth-secret.yaml`
