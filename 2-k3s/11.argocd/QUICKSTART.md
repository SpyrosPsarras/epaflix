# ArgoCD Quickstart

Run from the repo root.

## 0. Prereqs

- k3s cluster reachable via `kubectl` (current context: `epaflix`).
- Traefik + cert-manager already deployed (`05.traefik-deployment/`,
  `02.cert-manager/`).
- Authentik already deployed (`07.authentik-deployment/`).
- Pi-hole admin access (192.168.10.30) to add a DNS record.

> Image bumps are delivered by Renovate, **not** ArgoCD Image Updater
> (retired in #192 / #265). There is nothing to install in this tier for
> image automation — see `2-k3s/12.renovate/` and the README's
> **Image bumps (Renovate)** section.

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

> No CLI? The `argocd` self-management app can be synced via
> `kubectl patch application argocd … operation …` — see the README's
> **Sync policy** section.

## 6. Create the Traefik Application

Traefik owns the active ingress endpoint (`192.168.10.101`) and ACME
certificate storage, so the first sync is manual.

```
kubectl apply -f 2-k3s/11.argocd/apps/app-traefik.yaml
argocd app diff traefik     # review before first sync
argocd app sync traefik     # apply only once the diff is safe
```

Leave prune disabled after adoption. Enable automated self-heal only after
the manual sync is confirmed clean.

## 7. Create the servarr Application

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

## 8. Verify image-bump delivery (Renovate)

Image bumps arrive as Renovate PRs, not Image Updater commits. To confirm the
pipeline is live:

1. Renovate runs on its schedule (`2-k3s/12.renovate/`). Check the
   **Dependency Dashboard** issue on `SpyrosPsarras/epaflix`, or list open
   Renovate PRs:
   ```
   gh pr list --repo SpyrosPsarras/epaflix --author 'app/renovate'
   ```
2. A servarr digest re-roll PR (`matchFileNames: 2-k3s/08.servarr/kustomization.yaml`,
   update type `digest`) auto-merges once the `validate` check is green.
3. After merge, ArgoCD syncs the servarr Application and rolls the Deployment:
   ```
   kubectl -n servarr rollout status deploy/<app>
   git log -- 2-k3s/08.servarr/kustomization.yaml   # audit trail
   ```

Floating mutable tags not in the digest-pinned `images:` block (e.g.
`newtarr:rolling`) are refreshed by the weekly CronJob
`2-k3s/maintenance/servarr-image-updater-cronjob.yaml` instead.
