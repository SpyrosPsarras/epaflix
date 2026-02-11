# Authentik OIDC Provider Configuration for Jellyseerr/Seerr

This document describes how to configure Authentik as an OIDC provider for Jellyseerr/Seerr with group-based access control.

## Overview

This setup separates **authentication** (who can sign in) from **authorization** (who can access this app):

- **Authentication**: Users can sign in with Google OAuth through Authentik
- **Authorization**: Only users in the "Jellyseerr Users" group can access the application
- **Result**: Signing in with Google creates an account in Authentik but does NOT grant access to Jellyseerr automatically

## Prerequisites

- Authentik deployed and accessible at `https://auth.epaflix.com`
- Admin access to Authentik UI
- Jellyseerr/Seerr deployed in `servarr` namespace
- Google OAuth source configured in Authentik (see main Authentik README)

## Configuration Steps

### Step 1: Create User Group

1. Navigate to **Directory → Groups** in Authentik UI
2. Click **Create**
3. Configure the group:
   - **Name**: `Servarr Users`
   - **Slug**: `servarr-users` (auto-generated from name)
   - **Parent**: (leave empty)
   - **Attributes**: (optional, leave empty)
4. Click **Create**

**Purpose**: This group controls who can access Servarr services (Jellyseerr, Sonarr, Radarr, Prowlarr, Jellyfin, etc.). Only members of this group will be authorized.

### Step 2: Create OAuth2/OIDC Provider

1. Navigate to **Applications → Providers** in Authentik UI
2. Click **Create**
3. Select **OAuth2/OpenID Provider**
4. Configure the provider:

   **Basic Settings:**
   - **Name**: `Jellyseerr`
   - **Authorization flow**: `default-provider-authorization-implicit-consent`
   - **Client type**: `Confidential`
   - **Client ID**: (auto-generated - **SAVE THIS**)
   - **Client Secret**: (auto-generated - **SAVE THIS**)
   - **Access code validity**: 1 minutes (default)
   - **Access token validity**: 5 minutes (default)
   - **Refresh token validity**: 30 days (default)

   **URLs:**
   - **Redirect URIs/Origins (RegEx)**:
     ```
     https://seerr\.epaflix\.com/api/auth/callback/authentik
     ```
     Note: Use `\.` to escape dots in regex or disable regex matching

   **Scopes:**
   Select the following scopes:
   - ✅ `openid` (OpenID Connect)
   - ✅ `email` (User email address)
   - ✅ `profile` (User profile information)

   **Advanced Settings:**
   - **Subject mode**: `Based on the User's UUID` (recommended for consistency)
   - **Include claims in id_token**: ✅ Enabled (recommended)
   - **Issuer mode**: `Per Provider` (default)

5. Click **Finish**

**IMPORTANT**: Copy the **Client ID** and **Client Secret** - you'll need these for the Kubernetes secret.

### Step 3: Create Application

1. Navigate to **Applications → Applications** in Authentik UI
2. Click **Create**
3. Configure the application:
   - **Name**: `Jellyseerr`
   - **Slug**: `jellyseerr` (auto-generated)
   - **Group**: (optional, leave empty)
   - **Provider**: Select `Jellyseerr` (the provider created in Step 2)
   - **Launch URL**: `https://seerr.epaflix.com`
   - **Open in new tab**: ✅ (optional, user preference)
4. Click **Create**

### Step 4: Add Group-Based Access Policy

1. In the Applications list, click on the **Jellyseerr** application
2. Go to the **Policy / Group / User Bindings** tab
3. Click **Create and bind Policy**
4. Configure the policy:

   **Policy Configuration:**
   - **Name**: `Jellyseerr - Group Access Policy`
   - **Policy type**: Select `Group Membership Policy`
   - **Group**: Select `Servarr Users`
   - **Order**: 0 (default)
   - **Enabled**: ✅
   - **Timeout**: 30 seconds (default)

5. Click **Create**

**Result**: The policy is created and bound to the application in one step. Only users who are members of the "Servarr Users" group can now access this application through OIDC.

## OIDC Endpoints

Use these endpoints when configuring Jellyseerr's OIDC settings:

- **Issuer URL**: `https://auth.epaflix.com/application/o/jellyseerr/`
- **Authorization URL**: `https://auth.epaflix.com/application/o/authorize/`
- **Token URL**: `https://auth.epaflix.com/application/o/token/`
- **UserInfo URL**: `https://auth.epaflix.com/application/o/userinfo/`
- **Logout URL**: `https://auth.epaflix.com/application/o/jellyseerr/end-session/`
- **JWKS URL**: `https://auth.epaflix.com/application/o/jellyseerr/jwks/`

**Client Credentials:**
- **Client ID**: (from Step 2, stored in Kubernetes secret)
- **Client Secret**: (from Step 2, stored in Kubernetes secret)

## Kubernetes Secret

Create a Kubernetes secret with the OIDC credentials:

```bash
# Replace <CLIENT_ID> and <CLIENT_SECRET> with values from Step 2
kubectl create secret generic seerr-oidc-secret -n servarr \
  --from-literal=client-id='<CLIENT_ID>' \
  --from-literal=client-secret='<CLIENT_SECRET>'
```

Or use the template in `authentik-oidc-secret.yaml` (see separate file).

## Configuring Jellyseerr OIDC

Once the Authentik provider is configured:

1. Access Jellyseerr at `https://seerr.epaflix.com`
2. Log in with local admin account (if first time)
3. Navigate to **Settings → Authentication** (or **Settings → Services → OIDC**)
4. Enable OIDC authentication
5. Configure with values from "OIDC Endpoints" section above
6. Test by clicking "Sign in with Authentik" button

**Note**: Exact configuration steps may vary depending on Jellyseerr OIDC preview image interface.

## User Management Workflow

### Adding New Users

1. **User signs in**: User visits `https://seerr.epaflix.com` and clicks "Sign in with Authentik"
2. **Authentik authentication**: User is redirected to Authentik and signs in with Google OAuth
3. **Account created**: User account is created in Authentik (if first time)
4. **Access denied**: Jellyseerr shows "Access Denied" or returns to login (user not in group)
5. **Admin grants access**:
   - Admin logs into Authentik at `https://auth.epaflix.com`
   - Navigate to **Directory → Users**
   - Find the new user (search by email or username)
   - Click on the user
   - Go to **Groups** tab
   - Click **Add to existing group**
   - Select `Jellyseerr Users`
   - Click **Add**
6. **User gains access**: User refreshes Jellyseerr or signs in again → access granted

### Removing User Access

1. Admin logs into Authentik
2. Navigate to **Directory → Users** → select user
3. Go to **Groups** tab
4. Find `Jellyseerr Users` group
5. Click **Remove** (trash icon)
6. User will lose access on next authentication

### Bulk User Management

To add multiple users to the group:

1. Navigate to **Directory → Groups**
2. Click on `Jellyseerr Users` group
3. Go to **Users** tab
4. Click **Add existing user**
5. Select multiple users
6. Click **Add**

## Testing Authorization

### Test 1: Verify Access Denied (Not in Group)

1. Create a test Google account (or use non-admin account)
2. Log out of Jellyseerr and Authentik
3. Visit `https://seerr.epaflix.com`
4. Click "Sign in with Authentik"
5. Authenticate with test Google account
6. **Expected**: Access denied or redirected back to login
7. **Verify in Authentik**: User exists in Directory → Users but NOT in "Jellyseerr Users" group

### Test 2: Verify Access Granted (In Group)

1. In Authentik UI, add test user to "Jellyseerr Users" group
2. Return to Jellyseerr and refresh or sign in again
3. **Expected**: Access granted, user can see Jellyseerr dashboard
4. **Verify**: User can create media requests and use all features

### Test 3: Verify Access Removed

1. In Authentik UI, remove test user from "Jellyseerr Users" group
2. Have user refresh Jellyseerr or sign out and back in
3. **Expected**: Access denied

## Troubleshooting

### User gets "Access Denied" after signing in

**Cause**: User is authenticated in Authentik but not in the "Servarr Users" group.

**Solution**: Add user to the group (see "User Management Workflow" above).

### OIDC login fails with "Invalid redirect URI"

**Cause**: Redirect URI in Authentik provider doesn't match the callback URL Jellyseerr is using.

**Solution**:
- Check Jellyseerr logs for actual redirect URI being used
- Update provider's "Redirect URIs/Origins" in Authentik
- Common URI: `https://seerr.epaflix.com/api/auth/callback/authentik`

### User gets "Invalid client" error

**Cause**: Client ID or Client Secret is incorrect.

**Solution**:
- Verify Kubernetes secret has correct values: `kubectl get secret seerr-oidc-secret -n servarr -o yaml`
- Check Jellyseerr OIDC configuration has correct client ID
- Regenerate client secret in Authentik if needed

### Admin can't access after enabling OIDC

**Cause**: Admin account may not be in "Servarr Users" group.
- Add admin user to the group in Authentik UI
- Or disable OIDC temporarily and use local login to reconfigure

### Claims not appearing in Jellyseerr

**Cause**: Scopes not requested or provider not configured to include claims.

**Solution**:
- Verify `openid`, `email`, `profile` scopes are selected in provider
- Enable "Include claims in id_token" in provider settings
- Check Jellyseerr is requesting correct scopes

## Security Best Practices

1. **Group membership required**: Always bind a group policy to applications
2. **Monitor new users**: Regularly check Directory → Users for new sign-ups
3. **Rotate secrets**: Periodically regenerate client secrets (in provider settings)
4. **Audit logs**: Review Authentik event logs for suspicious activity
5. **Principle of least privilege**: Only grant access to users who need it
6. **Disable local auth**: Consider disabling Jellyseerr local authentication once OIDC is working

## Migration from Open Registration

If you previously had auto-registration enabled:

1. **Audit existing users**:
   - Navigate to Directory → Users in Authentik
   - Review all users who have signed up via Google OAuth
2. **Clean up unauthorized accounts** (optional):
   - Delete or disable accounts that shouldn't have access
3. **Apply group policy**: Follow steps above to create group and policy
4. **Add authorized users**: Manually add legitimate users to "Servarr Users" group
5. **Test**: Verify unauthorized users can't access any Servarr services

## Reference

- Pattern based on Grafana OAuth configuration: [10.observability/grafana-config/](../../10.observability/grafana-config/)
- Authentik OIDC documentation: https://goauthentik.io/docs/providers/oauth2/
- Jellyseerr OIDC image: `fallenbagel/jellyseerr:preview-OIDC`
