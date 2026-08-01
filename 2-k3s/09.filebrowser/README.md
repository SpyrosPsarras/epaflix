# FileBrowser Quantum for K3s

Web-based file manager with OIDC authentication via Authentik, accessing NFS media storage with UID/GID 568.

## Features

- **OIDC Authentication**: Single Sign-On via Authentik (password auth disabled)
- **Group-Based Access**: Admin rights via `filebrowser-admins` Authentik group
- **NFS Media Access**: Browse files served via NFS with correct permissions (568:568)
- **SQLite Database**: Lightweight internal database for user settings and metadata
- **Office File Support**: Edit documents directly in browser with OnlyOffice integration
- **Real-Time Search**: Content-aware file search across entire filesystem
- **Sharing**: Create expiring shares with granular permissions
- **WebDAV**: Full WebDAV server at `/dav/{source}/` — use with rclone for fast uploads

## Architecture

- **Domain**: `filebrowser.epaflix.com`
- **TLS**: Let's Encrypt via cert-manager
- **Namespace**: `filebrowser`
- **Replicas**: 1
- **Database**: SQLite (`/config/database.db`)
- **Storage**:
  - Config: local-path PVC (2Gi)
  - Media: hostPath to the unified NFS mount `/mnt/k3s-media` (UID 568), mounted once and re-exposed at `/srv/{animes,movies,tvshows,downloads}` via subPath
- **Image**: `gtstef/filebrowser:latest` (FileBrowser Quantum)
- **Version**: Latest from [FileBrowser Quantum](https://filebrowserquantum.com/)
- **GitOps**: Managed by ArgoCD Application `filebrowser` (see [2-k3s/11.argocd/apps/app-filebrowser.yaml](../11.argocd/apps/app-filebrowser.yaml)). The `filebrowser-oidc` Secret is created imperatively from `.github/instructions/secrets.yml` and is **not** tracked in git — same convention as Traefik's `cloudflare-api-token`.

## Lifecycle

- **First-time bootstrap** (fresh cluster): run `./deploy.sh` to create the namespace, storage, ConfigMap, and OIDC secret interactively, then apply the ArgoCD Application.
- **Day-2** (existing cluster): all changes to manifests under this directory flow through git → ArgoCD auto-sync. The OIDC Secret is the only resource not managed by ArgoCD; rotate it via `kubectl create secret ... --dry-run=client -o yaml | kubectl apply -f -` and then `kubectl rollout restart deployment/filebrowser -n filebrowser`.
- **OIDC creds substitution**: `configmap.yaml`'s `config.yaml` carries `${FILEBROWSER_OIDC_CLIENT_ID}` / `${FILEBROWSER_OIDC_CLIENT_SECRET}` placeholders. The `render-config` initContainer in `deployment.yaml` reads the values from the `filebrowser-oidc` Secret and writes a rendered file to an `emptyDir` that the main container reads — so real credentials never live in the ConfigMap.

## Prerequisites

1. **Traefik** deployed with cert-manager integration
2. **Authentik** running at `auth.epaflix.com`
3. **NFS server** with media accessible at `192.168.10.200`
4. **K3s nodes** with the unified NFS mount (already configured from Servarr setup, issue #195):
   - `/mnt/k3s-media` (uid=568, gid=568) — single export covering animes/movies/tvshows/downloads
5. **DNS**: `filebrowser.epaflix.com` pointing to Traefik LoadBalancer

## NFS Mount Verification

Your K3s nodes should already have the unified NFS mount from the Servarr deployment. Verify it exists:

```bash
# On any k3s node:
ls -la /mnt/k3s-media/
# Expected output:
# drwxrwxrwx+ 568 568 ... animes
# drwxrwxrwx+ 568 568 ... movies
# drwxrwxrwx+ 568 568 ... tvshows
# drwxrwxrwx+ 568 568 ... downloads
```

**Note**: If this mount is missing, refer to the Servarr setup documentation in `08.servarr/README.md` for NFS mount configuration.

## Authentik OIDC Provider Setup

1. **Create OAuth2/OpenID Provider**:
   - Go to: https://auth.epaflix.com/if/admin/#/core/providers
   - Click: **Create** → **OAuth2/OpenID Provider**
   - **Name**: FileBrowser
   - **Authorization flow**: default-provider-authorization-implicit-consent (authenticated users)
   - **Client type**: Confidential
   - **Client ID**: `filebrowser`
   - **Client Secret**: Generate and save (you'll need this)
   - **Redirect URIs**:
     ```
     https://filebrowser.epaflix.com/api/auth/oidc/callback
     ```
   - **Signing Key**: authentik Self-signed Certificate
   - **Scopes**: email, openid, profile, groups
   - Click **Finish**

2. **Create Application**:
   - Go to: https://auth.epaflix.com/if/admin/#/core/applications
   - Click: **Create**
   - **Name**: FileBrowser
   - **Slug**: `filebrowser`
   - **Provider**: Select the FileBrowser provider created above
   - **Launch URL**: `https://filebrowser.epaflix.com`
   - Click **Create**

3. **Create Admin Group** (optional):
   - Go to: https://auth.epaflix.com/if/admin/#/identity/groups
   - Click: **Create**
   - **Name**: `filebrowser-admins`
   - **Add members**: Add users who should have admin access
   - Click **Create**

## Deployment

### Quick Deploy

Run the deployment script:

```bash
cd /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/09.filebrowser
chmod +x deploy.sh
./deploy.sh
```

The script will:
1. Create namespace
2. Create storage resources
3. Create ConfigMap
4. Prompt for Authentik client secret
5. Deploy FileBrowser
6. Wait for pods to be ready

### Manual Deployment

```bash
# 1. Create namespace
kubectl apply -f namespace.yaml

# 2. Create storage
kubectl apply -f storage/

# 3. Create ConfigMap
kubectl apply -f configmap.yaml

# 4. Create OIDC secret (replace with actual secret from Authentik)
kubectl create secret generic filebrowser-oidc \
  -n filebrowser \
  --from-literal=client-id='filebrowser' \
  --from-literal=client-secret='YOUR_AUTHENTIK_CLIENT_SECRET'

# 5. Deploy application
kubectl apply -f deployment.yaml
kubectl apply -f service.yaml
kubectl apply -f ingress.yaml

# 6. Wait for deployment
kubectl wait --for=condition=available --timeout=300s deployment/filebrowser -n filebrowser
```

## Access

- **URL**: https://filebrowser.epaflix.com
- **Authentication**: Redirects to Authentik for OIDC login
- **Admin Access**: Users in `filebrowser-admins` Authentik group

## Configuration

### Config File

FileBrowser configuration is in [configmap.yaml](configmap.yaml). Key settings:

```yaml
auth:
  methods:
    password:
      enabled: false  # OIDC only
    oidc:
      enabled: true
      clientId: "filebrowser"
      issuerUrl: "https://auth.epaflix.com/application/o/filebrowser/"
      adminGroup: "filebrowser-admins"
      createUser: true  # Auto-create users on first login
```

### Access Rules

By default, `denyByDefault: true` requires explicit access rules. Configure in FileBrowser UI:

1. Login as admin
2. Go to **Settings** → **User Management**
3. Create access rules for groups or users
4. Example: Grant `filebrowser-admins` full access to `/srv`

### Environment Variables

Set in [deployment.yaml](deployment.yaml):

- `PUID=568` / `PGID=568`: Match NFS file ownership
- `TZ=Europe/Oslo`: Timezone
- `FILEBROWSER_CONFIG=/app-config/config.yaml`: Config file path (mounted from the emptyDir written by the `render-config` initContainer)

The `render-config` initContainer reads `client-id` / `client-secret` from the `filebrowser-oidc` Secret and substitutes them into the ConfigMap template before the main container starts.

## Storage Layout

```
FileBrowser Storage:
├── /config/                    # PVC: filebrowser-config (local-path, 2Gi)
│   ├── database.db            # SQLite database (users, settings)
│   └── cache/                 # Preview cache, temp files
└── /srv/                       # One PVC (filebrowser-media, hostPath → /mnt/k3s-media),
                                 # mounted 4x with subPath so the layout below is unchanged
    ├── animes/                # subPath: animes
    ├── movies/                # subPath: movies
    ├── tvshows/               # subPath: tvshows
    └── downloads/             # subPath: downloads
```

## Verification

```bash
# Check pods
kubectl get pods -n filebrowser

# Check logs
kubectl logs -f deployment/filebrowser -n filebrowser

# Check ingress and certificate
kubectl get ingress -n filebrowser
kubectl get certificate -n filebrowser

# Test NFS access from pod
kubectl exec -it deployment/filebrowser -n filebrowser -- ls -la /srv

# Check OIDC configuration
kubectl exec -it deployment/filebrowser -n filebrowser -- cat /app-config/config.yaml
```

## Troubleshooting

### OIDC Redirect Error

**Symptom**: "Invalid redirect URI" error after Authentik login

**Solution**: Verify redirect URI in Authentik provider matches exactly:
```
https://filebrowser.epaflix.com/api/auth/oidc/callback
```

### Permission Denied on NFS

**Symptom**: Cannot read/write files in `/srv/animes`, `/srv/movies`, etc.

**Solution**: Check NFS mount permissions on nodes:
```bash
# On k3s nodes:
ls -lan /mnt/k3s-media/
# Should show: drwxrwxrwx+ ... 568 568 ... animes
#              drwxrwxrwx+ ... 568 568 ... movies
#              drwxrwxrwx+ ... 568 568 ... tvshows
#              drwxrwxrwx+ ... 568 568 ... downloads

# Fix if needed (on TrueNAS):
ssh truenas_admin@192.168.10.200
sudo chown -R 568:568 /mnt/pool1/dataset01/{animes,movies,tvshows,downloads}
```

### Pod Won't Start

**Symptom**: Pod stuck in `Pending` or `CrashLoopBackOff`

**Solutions**:
```bash
# Check events
kubectl describe pod -n filebrowser <pod-name>

# Check PV/PVC binding
kubectl get pv,pvc -n filebrowser

# Verify NFS mount on node
kubectl get pods -n filebrowser -o wide  # Find which node
ssh <node-ip> "ls -la /mnt/ | grep k3s-"
```

### OIDC Secret Missing

**Symptom**: `render-config` initContainer fails with `No such file or directory` for `/secret/client-id` or `/secret/client-secret`, or the pod crash-loops with OIDC errors.

**Solution**: Recreate the imperative Secret:
```bash
kubectl create secret generic filebrowser-oidc \
  -n filebrowser \
  --from-literal=client-id='filebrowser' \
  --from-literal=client-secret='YOUR_SECRET' \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl rollout restart deployment/filebrowser -n filebrowser

# Verify the initContainer substituted creds:
kubectl -n filebrowser logs deploy/filebrowser -c render-config
kubectl -n filebrowser exec deploy/filebrowser -c filebrowser -- \
  grep -E 'clientId|clientSecret' /app-config/config.yaml
# (Should show real values, not ${FILEBROWSER_OIDC_CLIENT_ID})
```

## Backup

### Database Backup

```bash
# Backup SQLite database
kubectl exec -n filebrowser deployment/filebrowser -- \
  sqlite3 /config/database.db ".backup /config/database-backup.db"

# Copy backup locally
kubectl cp filebrowser/<pod-name>:/config/database-backup.db ./filebrowser-db-backup.db
```

### Configuration Backup

```bash
# Export ConfigMap
kubectl get configmap filebrowser-config -n filebrowser -o yaml > filebrowser-config-backup.yaml

# Export secret (WARNING: contains sensitive data)
kubectl get secret filebrowser-oidc -n filebrowser -o yaml > filebrowser-secret-backup.yaml
```

## Upgrade

```bash
# Update image tag in deployment.yaml, then:
kubectl apply -f deployment.yaml

# Or force pull latest:
kubectl rollout restart deployment/filebrowser -n filebrowser
```

## Uninstall

```bash
# 1. Remove ArgoCD Application first (otherwise it'll re-sync everything you delete)
kubectl delete -n argocd application filebrowser

# 2. Delete cluster resources
kubectl delete -f ingress.yaml
kubectl delete -f service.yaml
kubectl delete -f deployment.yaml
kubectl delete -f configmap.yaml
kubectl delete secret filebrowser-oidc -n filebrowser
kubectl delete -f storage/
kubectl delete -f namespace.yaml
```

## Fast Uploads via WebDAV + rclone

The browser upload UI uses sequential chunked HTTP uploads that top out at ~18 MB/s due to
JavaScript overhead. WebDAV with rclone bypasses this and streams directly to the server,
achieving the full single-stream HTTPS throughput (~18–25 MB/s) without chunk round-trip penalty.

### WebDAV endpoint

```
https://filebrowser.epaflix.com/dav/<source-name>/<path>
```

For the default "Media Files" source:
```
https://filebrowser.epaflix.com/dav/Media%20Files/
```

Authentication is HTTP Basic Auth where:
- **Username**: your FileBrowser username (OIDC email, e.g. `spypsarras@gmail.com`)
- **Password**: a FileBrowser API token (preferred — non-expiring) or a session JWT (expires in 2 h)

### Step 1 — Enable required permissions

WebDAV writes require the `Modify` permission in addition to `Create` and `Delete`.
API tokens require the `API` permission.

If your account is missing these (default OIDC-created accounts have `modify: false`, `api: false`):

1. **Become admin in FileBrowser** via Authentik:
   - Go to `https://auth.epaflix.com/if/admin/#/identity/groups`
   - Find the `filebrowser-admins` group and add your user
   - Log out of `filebrowser.epaflix.com` and log back in

2. **Enable Modify and API permissions**:
   - Go to `https://filebrowser.epaflix.com` → **Settings** → **Users** → click your user
   - Enable **Modify** and **API** checkboxes
   - Click **Save**

### Step 2 — Generate a non-expiring API token

Session JWTs expire in 2 hours and are not suitable for rclone. Generate an API token instead.

Using curl (replace `<SESSION_JWT>` with the cookie value from your browser's DevTools
after logging in — grab the `filebrowser_quantum_jwt` cookie value):

```bash
curl -s -X POST "https://filebrowser.epaflix.com/api/auth/token" \
  -H "Cookie: filebrowser_quantum_jwt=<SESSION_JWT>" \
  -H "Content-Type: application/json" \
  -d '{"name": "rclone", "expiresAt": ""}' | python3 -m json.tool
```

Copy the `token` value from the response. This token does not expire unless you delete it.

To list or delete existing tokens:
```bash
# List
curl -s "https://filebrowser.epaflix.com/api/auth/token/list" \
  -H "Cookie: filebrowser_quantum_jwt=<SESSION_JWT>"

# Delete by token name
curl -s -X DELETE "https://filebrowser.epaflix.com/api/auth/token?name=rclone" \
  -H "Cookie: filebrowser_quantum_jwt=<SESSION_JWT>"
```

### Step 3 — Install rclone

```bash
# Arch Linux
sudo pacman -S rclone

# Ubuntu/Debian
sudo apt install rclone

# Or install the latest version
curl https://rclone.org/install.sh | sudo bash
```

### Step 4 — Configure rclone

```bash
rclone config
```

Choose **n** (new remote), then:

| Prompt | Value |
|---|---|
| name | `filebrowser` |
| Storage type | `webdav` (type number for WebDAV) |
| URL | `https://filebrowser.epaflix.com/dav/Media Files` |
| WebDAV vendor | `other` |
| User | `spypsarras@gmail.com` |
| Password | *(enter your API token — rclone will obfuscate it)* |

Or create `~/.config/rclone/rclone.conf` directly:

```ini
[filebrowser]
type = webdav
url = https://filebrowser.epaflix.com/dav/Media Files
vendor = other
user = spypsarras@gmail.com
pass = <RCLONE_OBFUSCATED_TOKEN>
```

To obfuscate the token for the config file:
```bash
rclone obscure <YOUR_API_TOKEN>
```

### Step 5 — Upload files

```bash
# Copy a single file to downloads/
rclone copy "/path/to/video.mkv" "filebrowser:downloads/"

# Copy a folder recursively
rclone copy "/path/to/MasterChef/Season1/" "filebrowser:downloads/MasterChef/Season1/"

# Copy with progress display
rclone copy --progress "/path/to/video.mkv" "filebrowser:downloads/"

# Use multiple parallel transfers for many small files
rclone copy --transfers=4 --progress "/path/to/folder/" "filebrowser:downloads/folder/"

# Sync a local folder (deletes remote files not in local)
rclone sync --progress "/path/to/folder/" "filebrowser:downloads/folder/"
```

### Step 6 — Verify WebDAV is working

```bash
# List the downloads directory via WebDAV
rclone ls filebrowser:downloads/

# Check space usage
rclone about filebrowser:
```

### Performance notes

| Method | Observed speed | Bottleneck |
|---|---|---|
| Browser upload (old) | ~8 MB/s | JS progress overhead + small chunks |
| Browser upload (tuned: 150 MB chunks) | ~15–18 MB/s | Single HTTPS stream ceiling |
| rclone WebDAV (single stream) | ~18–25 MB/s | Single HTTPS stream ceiling |
| rclone WebDAV (multi-stream, large files) | Up to ~50 MB/s | Multiple concurrent connections |

The infrastructure is 10 GiB end-to-end (laptop WiFi 6 → switch → evanthoulaki Intel 82599ES →
TrueNAS Mellanox ConnectX). The HTTPS path through Traefik is the practical ceiling (~145 Mbps
per stream). Using `--transfers=N` in rclone opens multiple parallel streams, which helps when
uploading many files simultaneously.

### Troubleshooting WebDAV

**403 Forbidden on PROPFIND or PUT**
- Check that your user has `Modify: true` and `Download: true` in FileBrowser Settings → Users
- Ensure you are using an API token or a valid (non-expired) session JWT as the password

**401 Unauthorized**
- Verify the username is exactly your FileBrowser username (the OIDC `preferred_username`)
- Regenerate the API token

**Slow uploads (same as browser)**
- Ensure rclone is using `--transfers` > 1 for parallel uploads
- Check `rclone about filebrowser:` responds — confirms WebDAV is reachable

## Resources

- **FileBrowser Quantum**: https://filebrowserquantum.com/
- **GitHub**: https://github.com/gtsteffaniak/filebrowser
- **Documentation**: https://filebrowserquantum.com/en/docs/
- **OIDC Config**: https://filebrowserquantum.com/en/docs/configuration/authentication/oidc/
- **Authentik Docs**: https://goauthentik.io/docs/

## Notes

- FileBrowser Quantum uses SQLite internally - PostgreSQL is not needed
- Auto-creates users on first OIDC login (configurable via `createUser: true`)
- Access rules can be configured per user or group in the UI
- UID/GID 568 matches the TrueNAS/Servarr ecosystem standard
- Office file editing requires OnlyOffice integration (optional)
