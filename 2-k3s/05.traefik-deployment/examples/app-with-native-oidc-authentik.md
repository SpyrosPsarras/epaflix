# Application Integration with Authentik OIDC/OAuth2

This template provides a step-by-step guide for integrating applications with Authentik using native OIDC/OAuth2 support.

## Overview

Use this pattern when:
- ✅ Your application has built-in OIDC/OAuth2 support
- ✅ You want users to see a "Sign in with Authentik" button in the application
- ✅ The application can handle OAuth callbacks and token validation

**Alternative**: If the application doesn't support OIDC, use [Forward Auth pattern](protected-app-with-sso.yaml) instead.

## Architecture

```
User Browser
    ↓
Application (with OIDC button)
    ↓
Authentik (OAuth2 Provider)
    ↓
Google OAuth / LDAP / Local Auth
    ↓
Group Membership Check
    ↓
Access Granted/Denied
```

## Prerequisites

- Authentik deployed and accessible at `https://auth.epaflix.com`
- Application deployed in Kubernetes
- Application has OIDC/OAuth2 configuration settings
- DNS and Ingress configured for application

## Configuration Steps

### Step 1: Create Authorization Group

1. Navigate to **Directory → Groups** in Authentik UI at https://auth.epaflix.com
2. Click **Create**
3. Configure:
   - **Name**: `<AppName> Users` (e.g., "Jellyseerr Users")
   - **Slug**: Auto-generated (e.g., `jellyseerr-users`)
4. Click **Create**

### Step 2: Create OAuth2/OIDC Provider

1. Navigate to **Applications → Providers** in Authentik UI
2. Click **Create** → **OAuth2/OpenID Provider**
3. Configure:

   **Basic Settings:**
   - **Name**: `<AppName>` (e.g., "Jellyseerr")
   - **Authorization flow**: `default-provider-authorization-implicit-consent`
   - **Client type**: `Confidential`
   - **Client ID**: (auto-generated - **COPY THIS**)
   - **Client Secret**: (auto-generated - **COPY THIS**)

   **Redirect URIs:**
   - **Redirect URIs/Origins (RegEx)**:
     ```
     https://<app-domain>\.<your-domain>\.com/api/auth/callback/authentik
     ```
     Replace:
     - `<app-domain>`: Your app subdomain (e.g., `seerr`, `app`)
     - `<your-domain>`: Your domain (e.g., `epaflix`)
     - Escape dots with `\.` for regex matching
     - Check your app's documentation for exact callback path

   **Scopes:**
   - ✅ `openid` (required)
   - ✅ `email` (recommended)
   - ✅ `profile` (recommended)
   - Add others if needed by your application

   **Advanced:**
   - **Subject mode**: `Based on the User's UUID` (recommended)
   - **Include claims in id_token**: ✅ Enabled

4. Click **Finish**
5. **IMPORTANT**: Save the Client ID and Client Secret for Step 4

### Step 3: Create Application with Group Policy

1. Navigate to **Applications → Applications** in Authentik UI
2. Click **Create**
3. Configure:
   - **Name**: `<AppName>` (e.g., "Jellyseerr")
   - **Slug**: Auto-generated (e.g., `jellyseerr`)
   - **Provider**: Select the provider from Step 2
   - **Launch URL**: `https://<app-domain>.<your-domain>.com`
4. Click **Create**

5. **Add Group Policy**:
   - Click on the application in the list
   - Go to **Policy / Group / User Bindings** tab
   - Click **Create and bind Policy**
   - Configure the policy:
     - **Name**: `<AppName> - Group Access Policy`
     - **Policy type**: `Group Membership Policy`
     - **Group**: Select `<AppName> Users` group from Step 1
     - **Order**: 0 (default)
     - **Enabled**: ✅
     - **Timeout**: 30 seconds (default)
   - Click **Create**

**Result**: Only users in the `<AppName> Users` group can access this application.

### Step 4: Create Kubernetes Secret

Create a Kubernetes secret with the OIDC credentials from Step 2:

```bash
# Replace placeholders with actual values
kubectl create secret generic <app-name>-oidc-secret -n <namespace> \
  --from-literal=client-id='<CLIENT_ID_FROM_STEP2>' \
  --from-literal=client-secret='<CLIENT_SECRET_FROM_STEP2>'
```

**Example:**
```bash
kubectl create secret generic jellyseerr-oidc-secret -n servarr \
  --from-literal=client-id='<GRAFANA_OAUTH_CLIENT_ID>' \
  --from-literal=client-secret='eJw0j45GGpRa...long-secret...xyz=='
```

**Verify:**
```bash
kubectl get secret <app-name>-oidc-secret -n <namespace>
```

### Step 5: Configure Application OIDC

Configure your application's OIDC settings (usually in web UI or environment variables):

**OIDC Endpoints:**
- **Issuer URL**: `https://auth.epaflix.com/application/o/<app-slug>/`
- **Authorization URL**: `https://auth.epaflix.com/application/o/authorize/`
- **Token URL**: `https://auth.epaflix.com/application/o/token/`
- **UserInfo URL**: `https://auth.epaflix.com/application/o/userinfo/`
- **Logout URL**: `https://auth.epaflix.com/application/o/<app-slug>/end-session/`
- **JWKS URL**: `https://auth.epaflix.com/application/o/<app-slug>/jwks/`

**Client Credentials:**
- **Client ID**: From Kubernetes secret or Step 2
- **Client Secret**: From Kubernetes secret or Step 2

**Scopes:**
- Request: `openid email profile`

**Other Settings:**
- **Button Label**: "Sign in with Authentik" (or "Sign in with SSO")
- **Auto-discovery**: Some apps support discovery URL: `https://auth.epaflix.com/application/o/<app-slug>/.well-known/openid-configuration`

**Configuration Methods** (application-specific):
- **Web UI**: Settings → Authentication → OIDC/OAuth
- **Environment Variables**: Set in deployment manifest
- **Config File**: Mount ConfigMap with OIDC settings

### Step 6: Test Authorization

1. **Verify access denied for non-members**:
   - Log out of application and Authentik
   - Sign in with a test account (Google OAuth or local)
   - Click "Sign in with Authentik" in application
   - **Expected**: Access denied or redirect back to login
   - **Verify in Authentik**: User exists but NOT in `<AppName> Users` group

2. **Grant access**:
   - In Authentik UI: **Directory → Users** → select test user
   - Go to **Groups** tab → **Add to existing group**
   - Select `<AppName> Users` group → **Add**

3. **Verify access granted**:
   - Refresh application or sign in again
   - **Expected**: Access granted, user can use application

4. **Test logout** (optional):
   - Sign out from application
   - Verify Authentik session is also terminated (if logout URL configured)

## Environment Variables Pattern

If your application uses environment variables for OIDC configuration:

```yaml
env:
  - name: OIDC_ISSUER
    value: "https://auth.epaflix.com/application/o/<app-slug>/"
  - name: OIDC_CLIENT_ID
    valueFrom:
      secretKeyRef:
        name: <app-name>-oidc-secret
        key: client-id
  - name: OIDC_CLIENT_SECRET
    valueFrom:
      secretKeyRef:
        name: <app-name>-oidc-secret
        key: client-secret
  - name: OIDC_AUTH_URL
    value: "https://auth.epaflix.com/application/o/authorize/"
  - name: OIDC_TOKEN_URL
    value: "https://auth.epaflix.com/application/o/token/"
  - name: OIDC_USERINFO_URL
    value: "https://auth.epaflix.com/application/o/userinfo/"
  - name: OIDC_SCOPES
    value: "openid email profile"
```

## User Management Workflow

### Adding Users

1. User visits application and clicks "Sign in with Authentik"
2. User authenticates (Google OAuth, local, etc.)
3. Account created in Authentik (if first time)
4. Access denied (not in group)
5. Admin adds user to `<AppName> Users` group in Authentik UI
6. User refreshes and gains access

### Removing Users

1. Admin removes user from `<AppName> Users` group in Authentik UI
2. User loses access on next authentication

### Bulk Management

Add multiple users at once:
1. **Directory → Groups** → select `<AppName> Users` group
2. **Users** tab → **Add existing user**
3. Select multiple users → **Add**

## Troubleshooting

### "Access Denied" after signing in

**Cause**: User authenticated but not in required group.

**Solution**: Add user to `<AppName> Users` group in Authentik UI.

### "Invalid redirect URI" error

**Cause**: Redirect URI mismatch between application and Authentik provider.

**Solution**:
- Check application logs for actual callback URI being sent
- Update provider's "Redirect URIs" in Authentik to match
- Ensure regex escaping is correct (`\.` for dots)

### "Invalid client" or "Unauthorized client"

**Cause**: Incorrect Client ID or Client Secret.

**Solution**:
- Verify Kubernetes secret: `kubectl get secret <app-name>-oidc-secret -n <namespace> -o yaml`
- Check application configuration has matching Client ID
- Regenerate credentials in Authentik if needed

### Claims missing in application

**Cause**: Scopes not requested or claims not included in token.

**Solution**:
- Verify `openid email profile` scopes selected in Authentik provider
- Enable "Include claims in id_token" in provider settings
- Check application is requesting correct scopes

### Session/logout issues

**Cause**: Application doesn't support proper logout or session management.

**Solution**:
- Configure logout URL in application: `https://auth.epaflix.com/application/o/<app-slug>/end-session/`
- Some applications require end_session_endpoint configuration
- Test by clearing browser cookies

## Security Checklist

- ✅ Group policy bound to application (no open access)
- ✅ Redirect URIs limited to application domain only
- ✅ Client Secret stored in Kubernetes Secret (not in code)
- ✅ Scopes limited to what application needs
- ✅ HTTPS enforced for all endpoints
- ✅ Regular audit of group memberships
- ✅ MFA enabled for admin accounts
- ✅ Event logging enabled in Authentik

## Application-Specific Examples

### Jellyseerr/Seerr

See detailed guide: [08.servarr/seerr/authentik-provider-config.md](../../08.servarr/seerr/authentik-provider-config.md)

**Callback URI**: `https://seerr.epaflix.com/api/auth/callback/authentik`

### Grafana

See configuration: [10.observability/grafana-config/](../../10.observability/grafana-config/)

**Callback URI**: `https://grafana.epaflix.com/login/generic_oauth`
**Special**: Supports role mapping via `role_attribute_path`

### Generic Application

**Common callback paths**:
- `/oauth/callback`
- `/auth/callback`
- `/api/auth/callback/<provider>`
- `/login/oauth2/code/<provider>`

Check your application's documentation for the exact path.

## Reference

- **Authentik OIDC Docs**: https://goauthentik.io/docs/providers/oauth2/
- **OAuth 2.0 RFC**: https://tools.ietf.org/html/rfc6749
- **OpenID Connect Core**: https://openid.net/specs/openid-connect-core-1_0.html
- **Authentik Deployment**: [07.authentik-deployment/README.md](../../07.authentik-deployment/README.md)
- **Forward Auth Alternative**: [protected-app-with-sso.yaml](protected-app-with-sso.yaml)
