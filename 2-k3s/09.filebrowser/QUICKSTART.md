# FileBrowser Quantum - Quick Start

Get FileBrowser running with OIDC in 5 minutes.

## 1. Setup Authentik OAuth Provider

In Authentik admin UI (https://auth.epaflix.com/if/admin/):

**Create Provider:**
- Name: FileBrowser
- Type: OAuth2/OpenID Provider
- Client ID: `filebrowser`
- Redirect URI: `https://filebrowser.epaflix.com/api/auth/oidc/callback`
- Copy the **Client Secret** (you'll need it next)

**Create Application:**
- Name: FileBrowser
- Slug: `filebrowser`
- Provider: Select FileBrowser provider
- Launch URL: `https://filebrowser.epaflix.com`

**Create Group (optional):**
- Name: `filebrowser-admins`
- Add admin users

## 2. Verify NFS Mounts

Your K3s nodes should already have these NFS mounts (from Servarr setup):

```bash
# Verify mounts exist with correct permissions:
ls -la /mnt/
# Should show:
# drwxrwxrwx+ 568 568 ... k3s-animes
# drwxrwxrwx+ 568 568 ... k3s-movies
# drwxrwxrwx+ 568 568 ... k3s-tvshows
# drwxrwxrwx+ 568 568 ... k3s-downloads
```

If mounts are missing, they're likely already set up from your Servarr deployment. FileBrowser will use the same NFS shares.

## 3. Deploy FileBrowser

```bash
cd /home/spy/Documents/Epaflix/k3s-swarm-proxmox/2-k3s/09.filebrowser
./deploy.sh
```

When prompted, paste the Authentik Client Secret.

## 4. Access FileBrowser

Navigate to: **https://filebrowser.epaflix.com**

You'll be redirected to Authentik for login, then back to FileBrowser.

## 5. Configure Access Rules

Login as admin (user in `filebrowser-admins` group):

1. Go to **Settings** → **Users**
2. Set default permissions or create group-based rules
3. Grant access to `/srv` (media files)

## Done! 🎉

Your FileBrowser is now accessible with Authentik SSO and can browse NFS media files with proper permissions (UID/GID 568).

## Quick Commands

```bash
# Check status
kubectl get pods -n filebrowser

# View logs
kubectl logs -f deployment/filebrowser -n filebrowser

# Restart
kubectl rollout restart deployment/filebrowser -n filebrowser

# Check ingress/cert
kubectl get ingress,certificate -n filebrowser
```

## Troubleshooting

**Can't access files?**
- Check NFS mount: `kubectl exec -it deployment/filebrowser -n filebrowser -- ls -la /srv`
- Verify permissions: Should be `568:568`

**OIDC redirect error?**
- Verify Authentik redirect URI exactly matches: `https://filebrowser.epaflix.com/api/auth/oidc/callback`

**Pod won't start?**
- Check events: `kubectl describe pod -n filebrowser <pod-name>`
- Verify NFS mounts on nodes: `ssh <node-ip> "ls -la /mnt/ | grep k3s-"`

See [README.md](README.md) for full documentation.
