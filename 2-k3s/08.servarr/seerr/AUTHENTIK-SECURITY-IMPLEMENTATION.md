# Securing Jellyseerr/Seerr with Authentik - Implementation Checklist

This checklist provides the immediate steps to secure your Jellyseerr/Seerr deployment with group-based access control.

## Current Situation

❌ **Problem**: Google OAuth in Authentik allows anyone with a Gmail account to access Jellyseerr/Seerr and other media services automatically

✅ **Solution**: Separate authentication (Google sign-in) from authorization (app access via groups). Use a single group for all Servarr/Jellyfin ecosystem services.

## Implementation Steps

### Phase 1: Configure Authentik (via Web UI)

Access Authentik at: **https://auth.epaflix.com**

#### 1. Create Authorization Group (5 minutes)

- [ ] Navigate to **Directory → Groups**
- [ ] Click **Create**
- [ ] **Name**: `Servarr Users`
- [ ] **Slug**: `servarr-users` (auto-generated)
- [ ] Click **Create**
- [ ] **Note**: This group will be used for all media services (Jellyseerr, Sonarr, Radarr, Prowlarr, Jellyfin, qBittorrent, etc.)
- [ ] **Add yourself to the group** (for testing):
  - Click on the group
  - Go to **Users** tab
  - Click **Add existing user**
  - Select your admin account
  - Click **Add**

#### 2. Create OAuth2 Provider (10 minutes)

- [ ] Navigate to **Applications → Providers**
- [ ] Click **Create** → **OAuth2/OpenID Provider**
- [ ] Configure:
  - **Name**: `Jellyseerr`
  - **Authorization flow**: `default-provider-authorization-implicit-consent`
  - **Client type**: `Confidential`
  - **Client ID**: (auto-generated - **COPY TO NOTEPAD**)
  - **Client Secret**: (auto-generated - **COPY TO NOTEPAD**)
  - **Redirect URIs/Origins (RegEx)**: `https://seerr\.epaflix\.com/api/auth/callback/authentik`
  - **Scopes**: Select `openid`, `email`, `profile`
  - **Subject mode**: `Based on the User's UUID`
  - **Include claims in id_token**: ✅ Enabled
- [ ] Click **Finish**
- [ ] **Save Client ID and Secret** somewhere safe (you'll need them in Phase 2)

#### 3. Create Application (5 minutes)

- [ ] Navigate to **Applications → Applications**
- [ ] Click **Create**
- [ ] Configure:
  - **Name**: `Jellyseerr`
  - **Slug**: `jellyseerr` (auto-generated)
  - **Provider**: Select `Jellyseerr` (from step 2)
  - **Launch URL**: `https://seerr.epaflix.com`
- [ ] Click **Create**

#### 4. Add Group Policy (5 minutes)

- [ ] Click on the **Jellyseerr** application in the list
- [ ] Go to **Policy / Group / User Bindings** tab
- [ ] Click **Create and bind Policy**
- [ ] Configure the policy:
  - **Name**: `Jellyseerr - Group Access Policy`
  - **Policy type**: Select `Group Membership Policy`
  - **Group**: Select `Servarr Users`
  - **Order**: 0 (default)
  - **Enabled**: ✅
  - **Timeout**: 30 seconds (default)
- [ ] Click **Create**

**Note**: The policy is created and bound to the application in one step. Users must now be in the "Servarr Users" group to access Jellyseerr.

**✅ Authentik Configuration Complete!**

### Phase 2: Configure Kubernetes Secret (2 minutes)

From your terminal:

```bash
# Navigate to Seerr directory
cd /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/08.servarr/seerr

# Create secret with values from Phase 1, Step 2
kubectl create secret generic seerr-oidc-secret -n servarr \
  --from-literal=client-id='<PASTE_CLIENT_ID_HERE>' \
  --from-literal=client-secret='<PASTE_CLIENT_SECRET_HERE>'

# Verify secret was created
kubectl get secret seerr-oidc-secret -n servarr
```

**✅ Kubernetes Secret Created!**

### Phase 3: Configure Jellyseerr OIDC (10 minutes)

#### Access Jellyseerr

- [ ] Open browser to: **https://seerr.epaflix.com**
- [ ] Sign in with your current admin account (local authentication)

#### Enable OIDC

- [ ] Navigate to **Settings** (gear icon)
- [ ] Find **Authentication** or **OIDC** or **Services** section (exact location depends on Jellyseerr version)
- [ ] Enable OIDC authentication
- [ ] Configure OIDC settings:

**Required Settings:**

| Field | Value |
|-------|-------|
| **Issuer URL** | `https://auth.epaflix.com/application/o/jellyseerr/` |
| **Authorization URL** | `https://auth.epaflix.com/application/o/authorize/` |
| **Token URL** | `https://auth.epaflix.com/application/o/token/` |
| **UserInfo URL** | `https://auth.epaflix.com/application/o/userinfo/` |
| **Client ID** | (From Kubernetes secret or Phase 1, Step 2) |
| **Client Secret** | (From Kubernetes secret or Phase 1, Step 2) |
| **Scopes** | `openid email profile` |
| **Button Label** | `Sign in with Authentik` |

- [ ] **Save** configuration
- [ ] Verify "Sign in with Authentik" button appears on login page (may need to sign out first)

**Note**: If Jellyseerr doesn't show OIDC settings, verify you're using the `fallenbagel/jellyseerr:preview-OIDC` image in `seerr.yaml`. If not, update the image and redeploy.

**✅ Jellyseerr OIDC Configured!**

### Phase 4: Test Authorization (10 minutes)

#### Test 1: Verify Group Requirement Works

- [ ] **Sign out** of Jellyseerr completely
- [ ] Sign out of Authentik (visit `https://auth.epaflix.com/if/user/` and sign out)
- [ ] Open a private/incognito browser window
- [ ] Visit `https://seerr.epaflix.com`
- [ ] Click "Sign in with Authentik" button
- [ ] Use a **test Google account** (NOT your admin account)
- [ ] **Expected Result**:
  - ✅ Redirected to Authentik
  - ✅ Signed in with Google successfully
  - ❌ Access Denied by Jellyseerr (or returned to login)
- [ ] **Verify in Authentik**:
  - Log into Authentik as admin
  - Navigate to **Directory → Users**
  - Test user account should exist
  - Go to **Groups** tab for that user
  - Confirm user is **NOT** in "Servarr Users" group

#### Test 2: Grant Access

- [ ] In Authentik UI: **Directory → Users** → select test user
- [ ] Go to **Groups** tab
- [ ] Click **Add to existing group**
- [ ] Select `Servarr Users`
- [ ] Click **Add**

#### Test 3: Verify Access Granted

- [ ] Return to test user's browser
- [ ] Refresh Jellyseerr or sign in again with Authentik
- [ ] **Expected Result**:
  - ✅ Access granted
  - ✅ User can see Jellyseerr dashboard
  - ✅ User can create media requests
- [ ] **Bonus**: Test access to other services (Sonarr, Radarr, Jellyfin) if configured with same group

#### Test 4: Verify Your Admin Account Still Works

- [ ] Sign in with your admin account via Authentik
- [ ] **Expected Result**:
  - ✅ Access granted (you added yourself to group in Phase 1, Step 1)
  - ✅ Full admin access to Jellyseerr

**✅ Authorization Working Correctly!**

### Phase 5: Review and Clean Up (Optional)

#### Audit Existing Users

If Google OAuth was previously allowing auto-access:

- [ ] Navigate to **Directory → Users** in Authentik
- [ ] Review all existing user accounts
- [ ] Identify users who should NOT have access
- [ ] Options:
  - **Remove from group** (keeps account but denies access)
  - **Deactivate account** (prevents login entirely)
  - **Delete account** (permanent removal)

#### Add Authorized Users to Group

- [ ] Navigate to **Directory → Groups** → `Servarr Users`
- [ ] Go to **Users** tab
- [ ] Click **Add existing user**
- [ ] Select all users who should have access to media services
- [ ] Click **Add**

#### Verify Google OAuth Source Configuration (Important)

- [ ] Navigate to **Directory → Federation & Social login**
- [ ] Click on your **Google** source
- [ ] Verify **Enrollment flow** setting
- [ ] **Important**: The enrollment flow creates user accounts but should NOT auto-add users to any service groups
- [ ] If you have a custom enrollment flow with auto-group-assignment, remove those stages

**✅ Clean Up Complete!**

## Verification Commands

Check deployment status:

```bash
# Verify secret exists
kubectl get secret seerr-oidc-secret -n servarr

# Check Seerr is running with OIDC image
kubectl describe deployment seerr -n servarr | grep -i image

# View Seerr logs (look for OIDC initialization)
kubectl logs -n servarr -l app=seerr --tail=50
```

## What This Achieves

✅ **Before**: Anyone with a Google account → Automatic access to Jellyseerr and media services
✅ **After**: Anyone with a Google account → Authentik account created → Manual approval required by admin → Access granted only after added to "Servarr Users" group

**Bonus**: Same group membership grants access to all media services (Jellyseerr, Sonarr, Radarr, Prowlarr, Jellyfin, qBittorrent, etc.)

## User Management Going Forward

### When a New User Requests Access:

1. **User signs in** with Google via Authentik
2. **Account is created** in Authentik automatically
3. **Access is denied** to all media services (not in group)
4. **User contacts admin** requesting access
5. **Admin reviews request** and decides to approve/deny
6. **Admin adds to group** (if approved):
   - Authentik UI → **Directory → Users** → select user
   - **Groups** tab → **Add to existing group** → `Servarr Users`
7. **User gains access** to all media services on next sign-in

### When Access Should Be Revoked:

1. **Admin removes from group**:
   - Authentik UI → **Directory → Users** → select user
   - **Groups** tab → Find `Servarr Users` → **Remove**
2. **User loses access** to all media services immediately (on next authentication)

## Documentation References

For detailed information, see:

- **Full Authentik OIDC Setup**: [08.servarr/seerr/authentik-provider-config.md](authentik-provider-config.md)
- **Seerr README with OIDC**: [08.servarr/seerr/README.md](README.md#oidc-authentication-setup-with-authentik)
- **Authentik Authorization Model**: [07.authentik-deployment/README.md](../../07.authentik-deployment/README.md#authorization--application-integration)
- **OIDC Template for Other Apps**: [05.traefik-deployment/examples/app-with-native-oidc-authentik.md](../../05.traefik-deployment/examples/app-with-native-oidc-authentik.md)

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Can't find OIDC settings in Jellyseerr UI | Verify image is `fallenbagel/jellyseerr:preview-OIDC` |
| "Invalid redirect URI" error | Check callback URI matches exactly in Authentik provider |
| "Invalid client" error | Verify Client ID/Secret in Kubernetes secret matches Authentik provider |
| Admin can't access after enabling OIDC | Add admin account to "Servarr Users" group in Authentik |
| User authenticated but "Access Denied" | Expected behavior - add user to "Servarr Users" group in Authentik |

## Need Help?

- Check logs: `kubectl logs -n servarr -l app=seerr --tail=100`
- Check Authentik events: Authentik UI → **Events → Logs**
- Review detailed guides in documentation references above
