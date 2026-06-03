# 11.argocd — ArgoCD + Image Updater

GitOps for k3s. ArgoCD reconciles Traefik, servarr, and authentik manifests
from this repo; Argo CD Image Updater bumps selected container image tags by
committing back to git.

## What this gives you

- **`argocd.epaflix.com`** — ArgoCD UI/API, Authentik SSO.
- **Application `traefik`** — watches `2-k3s/05.traefik-deployment/`
  whose `kustomization.yaml` inflates the upstream `traefik/traefik`
  Helm chart and reconciles middleware, dashboard routes, external
  service proxies, and TLS-related Traefik CRDs.
- **Application `servarr`** — watches `2-k3s/08.servarr/` and keeps the
  `servarr` namespace in sync with git.
- **Application `authentik`** — watches `2-k3s/07.authentik-deployment/`
  whose `kustomization.yaml` inflates the upstream `authentik/authentik`
  Helm chart (`helmCharts:`) with `helm-values.yaml`, into the
  `app-authentik` namespace.
- **Auto image bumps** — Image Updater polls registries every 2 minutes and
  commits new tags/digests to the `images:` block in each Application's
  `kustomization.yaml` (servarr → `2-k3s/08.servarr/`, authentik →
  `2-k3s/07.authentik-deployment/`); ArgoCD then auto-syncs the Deployments.

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
│   ├── app-servarr.yaml   # Argo Application for the servarr stack
│   ├── app-authentik.yaml # Argo Application for Authentik (Helm chart)
│   └── app-traefik.yaml   # Argo Application for Traefik (Helm chart)
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
              ArgoCD controller ── reconciles ──► traefik-system / servarr / app-authentik
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
| newtarr                | ghcr.io/elfhosted/newtarr                  | rolling     |
| cleanuparr             | ghcr.io/cleanuparr/cleanuparr              | latest      |
| flaresolverr           | ghcr.io/flaresolverr/flaresolverr          | latest      |
| jellyfin               | jellyfin/jellyfin                          | latest      |
| homarr                 | ghcr.io/ajnart/homarr                      | latest      |
| wizarr                 | ghcr.io/wizarrrr/wizarr                    | latest      |
| bazarr-autotranslate   | ghcr.io/zelak312/bazarr_autotranslate      | latest      |
| authentik              | ghcr.io/goauthentik/server                 | `^2026\.5\.\d+$` (semver, patch-only) |

*`sonarr2` shares the linuxserver/sonarr image with `sonarr`; both roll together.

†Authentik uses **semver + MINOR-pinned regex**, not `digest+latest`. The
chart and image versions are released in lockstep upstream; image-updater
only bumps the image, so allowing arbitrary tags would desync the chart's
template from the image's schema. MINOR-bump procedure: edit `helmCharts.version`
*and* `images.newTag` in `2-k3s/07.authentik-deployment/kustomization.yaml`
*and* the `allow-tags` regex in `apps/app-authentik.yaml`, all in the same commit.

Excluded deliberately:

| Deployment       | Image                              | Why excluded                          |
|------------------|------------------------------------|---------------------------------------|
| qbittorrent      | binhex/arch-qbittorrentvpn         | VPN-coupled, manual bumps             |
| seerr            | fallenbagel/jellyseerr:preview-OIDC| custom fork tag                       |
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

## Sync policy: app-of-apps drives auto-apply

As of 2026-05-29 (PR #97), the root **`app-of-apps`** Application has
`automated.selfHeal: true, prune: false`. It reconciles every child
`Application` definition in `apps/` directly from git.

**Merged `apps/` changes auto-apply — no more manual `app-of-apps`
sync.** Push a change to a child `Application` (or merge a PR touching
`2-k3s/11.argocd/apps/`) and ArgoCD propagates it on its own.

Before #97, `app-of-apps` was manual-sync, which silently left merged
child changes **dormant until someone ran a manual sync**. If a merged
change isn't live, first check whether the owning parent Application is
manual-sync.

Most child apps now run `automated.selfHeal: true, prune: false`.
Deliberate exceptions stay **manual** (no `automated` block) — do not
flip casually:

- **`argocd`** — self-manages the ArgoCD control plane; a bad self-sync
  can take it down. Upgrade in a maintenance window. See issues #96 / #46.
- **`system-upgrade-controller`** — manual.

`prune` is OFF everywhere on purpose (issue #21 tracks enabling it).
Note: an empty `syncPolicy: {}` in a manifest causes a permanent
*cosmetic* OutOfSync (never matches live `null`) — omit `syncPolicy`
entirely for a manual app instead.

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

## Traefik onboarding (safe adoption)

Traefik was Helmfile-installed before ArgoCD managed it, and it owns the
active ingress endpoint `192.168.10.101`. Adopt it manually first; do not
enable prune during initial adoption.

1. **Confirm runtime-only state exists**. These are not committed to git:
  ```
  kubectl -n traefik-system get secret cloudflare-api-token
  kubectl -n traefik-system get pvc
  kubectl -n traefik-system get svc traefik -o wide
  ```
  The Service must still show `192.168.10.101`, and the ACME PVC must be the
  same one currently mounted by Traefik.

2. **Render the git source before ArgoCD syncs it**:
  ```
  kubectl kustomize --enable-helm 2-k3s/05.traefik-deployment >/tmp/traefik-rendered.yaml
  ```
  Check that the rendered Traefik values still reference
  `cloudflare-api-token`, mount ACME storage at `/data/acme.json`, keep
  one replica, and keep the rendered Service `loadBalancerIP: 192.168.10.101`.

3. **Create the Application**:
  ```
  kubectl apply -f 2-k3s/11.argocd/apps/app-traefik.yaml
  ```

4. **Review the first diff, then sync manually only if safe**:
  ```
  argocd app diff traefik
  argocd app sync traefik
  ```

5. **Verify ingress after sync**:
  ```
  kubectl -n traefik-system rollout status deploy/traefik
  kubectl -n traefik-system get svc traefik -o wide
  curl -Ik https://traefik.epaflix.com/dashboard/
  ```

After a clean manual adoption window, switch the Application to automated
self-heal with `prune: false`. Leave prune disabled until deleting a manifest
from git should intentionally delete the live resource.

## Authentik onboarding (one-time)

Authentik was Helm-installed before ArgoCD existed. To put it under ArgoCD
without breaking sessions:

1. **Enable kustomize-with-helm in ArgoCD** (already in
   `2-k3s/11.argocd/helm-values.yaml` as `configs.cm.kustomize.buildOptions: --enable-helm`).
   If the cluster predates this change: `helm upgrade argocd argo/argo-cd
   --version 9.5.14 -n argocd -f helm-values.yaml --wait` then
   `kubectl -n argocd rollout restart deploy/argocd-repo-server`.

2. **Apply the runtime-secrets Secret** (substitute real values from
   `.github/instructions/secrets.yml` first — never commit the rendered file):
   ```
   kubectl apply -f 2-k3s/07.authentik-deployment/secret-app.yaml
   ```
   Keys: `AUTHENTIK_SECRET_KEY`, `AUTHENTIK_POSTGRESQL__PASSWORD`,
   `AUTHENTIK_EMAIL__PASSWORD`. `AUTHENTIK_SECRET_KEY` MUST match the value
   currently live in the chart-managed `authentik` Secret —
   `kubectl -n app-authentik get secret authentik -o jsonpath='{.data.AUTHENTIK_SECRET_KEY}' | base64 -d`.
   Mismatch invalidates every session/cookie/token.

3. **Re-render the chart locally** so the cluster matches the new
   git-tracked values file *before* ArgoCD adopts it:
   ```
   cd 2-k3s/07.authentik-deployment
   helm upgrade authentik authentik/authentik --version 2026.2.0 \
     -n app-authentik -f helm-values.yaml --wait
   ```
   Verify pods roll cleanly and `https://auth.epaflix.com` still works.

4. **Create the Application** (still manual sync):
   ```
   kubectl apply -f 2-k3s/11.argocd/apps/app-authentik.yaml
   ```
   In the UI: should be Healthy + (near-)Synced after the first refresh.
   Reconcile any drift, then flip `syncPolicy` to:
   ```yaml
   syncPolicy:
     automated: { selfHeal: true, prune: false }
     syncOptions: [ServerSideApply=true]
   ```

5. **Image-updater bumps**: appear in
   `kubectl -n argocd logs -l app.kubernetes.io/name=argocd-image-updater`.
   A new `2026.2.x` tag → commit on `main` editing `images.newTag` in
   `2-k3s/07.authentik-deployment/kustomization.yaml` → ArgoCD sync → rolling
   restart of server + worker.

## Observability onboarding (one-time)

The observability stack (kube-prometheus-stack 82.2.0, Loki 6.53.0,
Promtail 6.17.1) was Helm-installed before ArgoCD. Single Application with
three `helmCharts:` entries in one kustomization — Loki/Promtail
ServiceMonitors hard-depend on kube-prometheus-stack CRDs so splitting them
into separate Apps would force a sync-wave dance ArgoCD does not solve
cleanly.

1. **Apply the new imperative Secrets BEFORE touching Helm** (real values
   substituted from `.github/instructions/secrets.yml`; never commit the
   rendered Secrets):
   - `grafana-admin-secret` — keys `admin-user`, `admin-password`
   - `alertmanager-config-secret` — single key `alertmanager.yaml` carrying
     the full route/receivers/SMTP-globals block
   - Patch `grafana-oauth-secret` to add a `client_id` key alongside the
     existing `client_secret`:
     ```
     kubectl -n observability patch secret grafana-oauth-secret \
       --type=merge -p '{"stringData":{"client_id":"<id-from-secrets.yml>"}}'
     ```
   - Verify pre-existing `grafana-db-secret`, `grafana-oauth-secret`,
     `pve-exporter-secrets` are intact.

2. **Re-render each chart locally** so the cluster matches the new
   git-tracked values *before* ArgoCD adopts it. This rolls Alertmanager
   once (config → configSecret transition: ~30s/pod, ~3min total alert
   delivery gap with 3 replicas) and Grafana once (adminPassword →
   existingSecret: ~30s):
   ```
   cd 2-k3s/10.observability
   helm upgrade kube-prometheus-stack prometheus-community/kube-prometheus-stack \
     --version 82.2.0 -n observability -f prometheus-values.yaml --wait
   helm upgrade loki     grafana/loki     --version 6.53.0 -n observability -f loki-values.yaml     --wait
   helm upgrade promtail grafana/promtail --version 6.17.1 -n observability -f promtail-values.yaml --wait
   ```
   Verify rollouts and that `https://grafana.epaflix.com` still SSO-logs-in.

3. **Render-then-diff** against the live cluster (the step omitted on the
   filebrowser adoption that lost its OIDC Secret — non-negotiable):
   ```
   kubectl diff -f <(kubectl kustomize --enable-helm 2-k3s/10.observability)
   ```
   The diff MUST be empty modulo `ignoreDifferences`-covered fields. If
   anything else shows, fix git — do not proceed.

4. **Create the Application** (still manual sync):
   ```
   kubectl apply -f 2-k3s/11.argocd/apps/app-observability.yaml
   argocd app diff observability   # must be empty
   argocd app sync observability
   argocd app get observability    # Healthy + Synced
   ```

5. **Enable automated self-heal** after one clean sync — edit
   `app-observability.yaml` to:
   ```yaml
   syncPolicy:
     automated: { selfHeal: true, prune: false }
     syncOptions: [ServerSideApply=true]
   ```
   Commit, push, ArgoCD applies the new policy to itself. Hold `prune: false`
   for at least a week before considering `prune: true`.

6. **Image-updater not enabled.** kube-prometheus-stack chart bumps require
   coordinated CRD migrations; manual PRs editing `helmCharts[*].version`
   in `2-k3s/10.observability/kustomization.yaml`.

## SOPS age key bootstrap (run ONCE per fresh cluster)

Before ArgoCD can decrypt any `*.enc.yaml` Secret, the cluster needs the
age private key. This is the one Secret that cannot itself be GitOps-
managed (chicken-egg).

```bash
# From the maintainer workstation:
kubectl create secret generic sops-age \
  -n argocd \
  --from-file=keys.txt=$HOME/.config/sops/age/k3s-cluster.txt

# Verify the repo-server picked it up:
kubectl -n argocd rollout restart deploy/argocd-repo-server
kubectl -n argocd rollout status deploy/argocd-repo-server
kubectl -n argocd logs deploy/argocd-repo-server -c install-ksops | tail
# Expected: "Done."
```

Rotation, encrypt, and decrypt recipes:
`.github/instructions/sops.instructions.md`.

## See also

- `QUICKSTART.md` — ordered install steps with the exact commands.
- `2-k3s/05.traefik-deployment/examples/app-with-native-oidc-authentik.md`
  — Authentik OIDC provider setup (mirror those steps for `argocd`).
- `2-k3s/05.traefik-deployment/kustomization.yaml` — what ArgoCD watches
  for Traefik.
- `2-k3s/08.servarr/kustomization.yaml` — what ArgoCD watches for servarr.
- `2-k3s/07.authentik-deployment/kustomization.yaml` — what ArgoCD watches
  for authentik (image-updater write-back target = `images:` block).
- `2-k3s/10.observability/kustomization.yaml` — what ArgoCD watches for
  observability (three `helmCharts:` entries — no image-updater).
