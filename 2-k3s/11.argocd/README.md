# 11.argocd — ArgoCD + Image Updater

GitOps for k3s. ArgoCD reconciles servarr manifests from this repo;
Argo CD Image Updater bumps container image tags by committing back to git.

## What this gives you

- **`argocd.epaflix.com`** — ArgoCD UI/API, Authentik SSO.
- **Application `servarr`** — watches `2-k3s/08.servarr/` and keeps the
  `servarr` namespace in sync with git.
- **Auto image bumps** — Image Updater polls registries every 2 minutes and
  commits new digests to `2-k3s/08.servarr/kustomization.yaml`'s
  `images:` block; ArgoCD then auto-syncs the Deployment.

## Layout

```
11.argocd/
├── README.md              # this file
├── QUICKSTART.md          # ordered install steps
├── namespace.yaml         # argocd ns
├── helm-values.yaml       # argo-cd Helm chart values
├── install.sh             # helm upgrade + ingress
├── ingress.yaml           # Traefik IngressRoute for argocd.epaflix.com
├── oidc-secret.yaml       # template (DO NOT apply as-is — see below)
├── apps/
│   └── app-servarr.yaml   # Argo Application for the servarr stack
└── image-updater/
    ├── install.sh
    ├── values.yaml
    └── git-creds-secret.yaml   # template
```

## Architecture

```
GitHub repo  ─── ArgoCD repo-server (poll)
                    │
                    ▼
              ArgoCD controller ── reconciles ──► servarr ns
                    ▲
                    │ Application API
                    │
        argocd-image-updater  ── git push ──► GitHub
              (polls registries every 2m, runs
               `kustomize edit set image` on
               2-k3s/08.servarr/kustomization.yaml)
```

## Versions

- argo-cd chart **9.5.14** (app v3.4.2)
- argocd-image-updater chart **0.14.0** (app v0.17.0)

Pinned in `install.sh` (`CHART_VERSION=…`). Bump deliberately.

**Important — pinned to v0.17 on purpose**: chart 1.x ships
argocd-image-updater v1.x, which switched to a CRD-driven `ImageUpdater`
operator model AND introduced a registry-prefix normalization bug that
silently drops `lscr.io/`-prefixed images during the "live image" match.
The v0.x line still reads the classic
`argocd-image-updater.argoproj.io/*` annotations on the Application
itself (see `apps/app-servarr.yaml`) and works end-to-end for this stack.

## Image Updater scope

Tracked (digest strategy on the moving tag):

| Alias                  | Image                                      | Tag         |
|------------------------|--------------------------------------------|-------------|
| sonarr / sonarr2*      | lscr.io/linuxserver/sonarr                 | latest      |
| radarr                 | lscr.io/linuxserver/radarr                 | latest      |
| prowlarr               | lscr.io/linuxserver/prowlarr               | latest      |
| bazarr                 | lscr.io/linuxserver/bazarr                 | development |
| huntarr                | huntarr/huntarr                            | latest      |
| cleanuparr             | ghcr.io/cleanuparr/cleanuparr              | latest      |
| flaresolverr           | ghcr.io/flaresolverr/flaresolverr          | latest      |
| jellyfin               | jellyfin/jellyfin                          | latest      |
| homarr                 | ghcr.io/ajnart/homarr                      | latest      |
| wizarr                 | ghcr.io/wizarrrr/wizarr                    | latest      |
| bazarr-autotranslate   | ghcr.io/zelak312/bazarr_autotranslate      | latest      |

*`sonarr2` shares the linuxserver/sonarr image with `sonarr`; both roll together.

Excluded deliberately:

| Deployment       | Image                              | Why excluded                          |
|------------------|------------------------------------|---------------------------------------|
| qbittorrent      | binhex/arch-qbittorrentvpn         | VPN-coupled, manual bumps             |
| jellyseerr/seerr | fallenbagel/jellyseerr:preview-OIDC| custom fork tag                       |
| lingarr          | ghcr.io/spyrospsarras/lingarr:…    | pinned to a private branch tag        |

Opt-in later by adding the alias to the `image-list` annotation on
`apps/app-servarr.yaml`.

## How updates flow

1. Image Updater wakes (2-minute interval), compares each tracked image's
   live digest with the digest currently pinned in
   `08.servarr/kustomization.yaml`'s `images:` block.
2. On change: `kustomize edit set image <name>=<repo>:<tag>@<digest>` →
   `git commit -m "[image-updater] bump …"` → `git push origin main`.
3. ArgoCD controller observes the git change, syncs the Application,
   Deployment rolls.
4. Audit trail: `git log -- 2-k3s/08.servarr/kustomization.yaml`.

## First-install gotcha: adoption drift

The cluster has imperative `kubectl edit` changes that diverge from the
manifests in git (bazarr `imagePullPolicy`, jellyfin resources,
bazarr-autotranslate `BAZARR_BASE_URL`, ...). Snapshot in
`.history/2026-05-17-argocd-adoption-drift.diff`.

The `Application` ships with `syncPolicy: {}` (manual). Until you
reconcile the drift either way and explicitly sync, ArgoCD will only
**observe**, not overwrite. After reconciling, switch to:

```yaml
spec:
  syncPolicy:
    automated:
      selfHeal: true
      prune: false   # flip to true after a clean week
```

## Rollback

- Disable Image Updater: `kubectl -n argocd scale deploy/argocd-image-updater --replicas=0`
- Remove auto-sync: `argocd app set servarr --sync-policy none`
- Full uninstall:
  ```
  helm uninstall argocd-image-updater argocd -n argocd
  kubectl delete ns argocd
  ```
  servarr Deployments stay running; `kustomization.yaml` is still in repo
  and applies with `kubectl apply -k 2-k3s/08.servarr/` if needed.

## See also

- `QUICKSTART.md` — ordered install steps with the exact commands.
- `2-k3s/05.traefik-deployment/examples/app-with-native-oidc-authentik.md`
  — Authentik OIDC provider setup (mirror those steps for `argocd`).
- `2-k3s/08.servarr/kustomization.yaml` — what ArgoCD watches.
