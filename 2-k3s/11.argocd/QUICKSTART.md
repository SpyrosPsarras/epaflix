# ArgoCD Quickstart

Run from the repo root.

## 0. Prereqs

- k3s cluster reachable via `kubectl` (current context: `epaflix`).
- Traefik + cert-manager already deployed (`05.traefik-deployment/`,
  `02.cert-manager/`).
- Authentik already deployed (`07.authentik-deployment/`).
- GitHub PAT with `contents: write` on `SpyrosPsarras/epaflix`.
- Pi-hole admin access (192.168.10.30) to add a DNS record.

## 1. DNS record

On the Pi-hole VM, add to `/etc/dnsmasq.d/10-epaflix.conf`:

```
address=/argocd.epaflix.com/192.168.10.101
```

Reload:
```
ssh root@192.168.10.30 'sudo systemctl restart pihole-FTL'
```

Verify:
```
dig +short argocd.epaflix.com @192.168.10.30
# expect 192.168.10.101
```

## 2. Install ArgoCD

```
./2-k3s/11.argocd/install.sh
```

Wait for pods:
```
kubectl -n argocd get pods -w
```

Initial admin password (only until OIDC is wired; use SSO afterwards):
```
kubectl -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' | base64 -d ; echo
```

## 3. Authentik OIDC provider

Follow `2-k3s/05.traefik-deployment/examples/app-with-native-oidc-authentik.md` step-by-step:

- **Group**: `ArgoCD Admins`
- **Provider** (OAuth2/OIDC):
  - Name: `ArgoCD`
  - App slug: `argocd` (matches issuer URL in `helm-values.yaml`)
  - Client type: Confidential
  - Redirect URIs (regex): `https://argocd\.epaflix\.com/auth/callback`
  - Scopes: `openid`, `profile`, `email`, `groups`
  - Include claims in id_token: ✅
- **Application**: `ArgoCD`, bound to the `ArgoCD Admins` group via Group
  Membership Policy.

Copy the resulting **client-id** and **client-secret** into
`.github/instructions/secrets.yml` (NOT into git-tracked files) under:

```yaml
argocd:
  oidc:
    client_id: <fill-me-in>
    client_secret: <fill-me-in>
  image_updater:
    github_pat: <fill-me-in>
```

## 4. Wire OIDC into ArgoCD

Do **not** apply `oidc-secret.yaml` — the chart manages `argocd-secret`.
Merge-patch the OIDC keys instead:

```
CID=$(yq '.argocd.oidc.client_id'     .github/instructions/secrets.yml)
CSEC=$(yq '.argocd.oidc.client_secret' .github/instructions/secrets.yml)
kubectl -n argocd patch secret argocd-secret --type=merge \
  -p "{\"stringData\":{\"oidc.authentik.clientId\":\"$CID\",\"oidc.authentik.clientSecret\":\"$CSEC\"}}"
kubectl -n argocd rollout restart deploy/argocd-server
```

Verify:
- Browse https://argocd.epaflix.com → click "Log in via authentik".
- Land back logged in. If the user is in `ArgoCD Admins`, the UI shows
  admin controls.

## 5. Install ArgoCD CLI (optional but recommended)

```
brew install argocd                       # macOS
sudo snap install argocd --classic        # ubuntu
argocd login argocd.epaflix.com --sso
```

## 6. Image Updater git creds

```
PAT=$(yq '.argocd.image_updater.github_pat' .github/instructions/secrets.yml)
kubectl -n argocd create secret generic git-creds \
  --from-literal=username=git \
  --from-literal=password="$PAT" \
  --dry-run=client -o yaml | kubectl apply -f -
```

## 7. Install Image Updater

```
./2-k3s/11.argocd/image-updater/install.sh
```

## 8. Create the servarr Application

```
kubectl apply -f 2-k3s/11.argocd/apps/app-servarr.yaml
```

The Application starts in **manual sync** (`syncPolicy: {}`). ArgoCD will
show it as `OutOfSync` against live drift documented in
`.history/2026-05-17-argocd-adoption-drift.diff`. Reconcile that drift
(import live into git OR accept overwrite) before doing the first sync:

```
argocd app diff servarr     # review what would change
argocd app sync servarr     # apply once you're sure
```

Then enable auto-sync:

```
argocd app set servarr --sync-policy automated --self-heal
```

(Leave `--auto-prune` off for a week.)

## 9. End-to-end image bump test

Make sure Image Updater is healthy and the test is observable:

```
kubectl -n argocd logs -f deploy/argocd-image-updater
```

To force a real bump, simulate stale digest (one of the tracked images):

```
# inside the repo
cd 2-k3s/08.servarr
kustomize edit set image lscr.io/linuxserver/bazarr=lscr.io/linuxserver/bazarr:development@sha256:0000000000000000000000000000000000000000000000000000000000000000
git commit -am "[test] stale bazarr digest"
git push
```

Within ~2 minutes Image Updater should commit a corrected digest. Argo
auto-syncs, `kubectl rollout status deploy/bazarr -n servarr` reports
fresh.
