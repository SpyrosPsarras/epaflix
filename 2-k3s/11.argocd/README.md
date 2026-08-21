# 11.argocd — ArgoCD (GitOps control plane)

GitOps for k3s. ArgoCD reconciles every tier in this repo (Traefik, servarr,
authentik, observability, …) from git. Container-image bumps are **not** an
ArgoCD concern — they are delivered as git commits by **Renovate**
(`2-k3s/12.renovate/`, config `.github/renovate.json`), which ArgoCD then
syncs like any other change.

> **History:** image bumps used to be driven by Argo CD Image Updater (its own
> Helm release in this tier), which committed new tags back to `main`. That was
> retired in PR #192 / #265 because its git write-back pushed directly to `main`
> and the required `validate` branch-protection check rejected the pushes.
> Renovate now owns image delivery end-to-end through the normal PR + `validate`
> gate. Nothing in this tier installs Image Updater anymore.

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
- **Image bumps via Renovate** — Renovate opens PRs that re-pin the
  digest-locked `images:` blocks in each tier's `kustomization.yaml`
  (servarr → `2-k3s/08.servarr/`, authentik → `2-k3s/07.authentik-deployment/`);
  once merged, ArgoCD auto-syncs the Deployments. See **Image bumps (Renovate)**
  below.

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
├── kustomization.yaml     # inflates the argo-cd chart for self-management
└── apps/                  # app-of-apps child Applications (one file per app)
    ├── app-of-apps.yaml   # self-referential root (selfHeal: true)
    ├── app-argocd.yaml    # ArgoCD self-management (manual sync)
    ├── app-servarr.yaml   # servarr stack
    ├── app-authentik.yaml # Authentik (Helm chart)
    ├── app-traefik.yaml   # Traefik (Helm chart)
    └── …                  # one app-*.yaml per managed tier
```

## Architecture

```
GitHub repo  ─── ArgoCD repo-server (poll)
                    │
                    ▼
              ArgoCD controller ── reconciles ──► traefik-system / servarr / app-authentik / …
                    ▲
                    │ git commits (merged PRs)
                    │
                Renovate  ── opens PRs ──► GitHub
              (re-pins digests in the images: blocks;
               auto-merges digest/patch bumps through
               the validate gate — see 12.renovate)
```

## Versions

- argo-cd chart **9.5.20** (app v3.4.3)

Pinned in `kustomization.yaml` (`helmCharts[*].version`). Renovate opens PRs for
chart bumps; the `argocd` Application self-syncs **manually** in a maintenance
window (see **Sync policy** below). Bump deliberately.

## Image bumps (Renovate)

ArgoCD only reconciles git → cluster. Keeping the `images:` blocks fresh is
Renovate's job (`2-k3s/12.renovate/`, rules in `.github/renovate.json`):

- **servarr digests** — the `images:` block in
  `2-k3s/08.servarr/kustomization.yaml` is digest-pinned on moving tags
  (`:latest`, `:development`, …). A dedicated Renovate rule
  (`matchFileNames: 2-k3s/08.servarr/kustomization.yaml`,
  `matchUpdateTypes: [digest]`) **auto-merges** digest re-rolls through the
  normal PR + `validate` gate. File+datasource+digest scoped, so it cannot reach
  authentik or any minor/major/patch update.
- **repo-wide patches** — a separate rule auto-merges `patch` bumps repo-wide
  (including chart-only patches).
- **authentik** — chart version and container image move together upstream, so
  Renovate groups them in one PR (`groupName: authentik`). Patch auto-merges;
  **minor/major open a PR you review and merge manually** (release notes can
  carry DB migrations / breaking changes).
- **auto-rebase** — top-level `rebaseWhen: behind-base-branch` keeps every
  Renovate branch rebased onto `main` so the strict up-to-date branch
  protection never strands a stale branch.

Once a Renovate PR merges, ArgoCD observes the git change and rolls the
Deployment. Audit trail: `git log -- 2-k3s/08.servarr/kustomization.yaml` plus
the merged Renovate PRs.

**Floating tags not in an `images:` block** (e.g. `newtarr` is a hardcoded
`ghcr.io/elfhosted/newtarr:rolling` tag in `2-k3s/08.servarr/newtarr/newtarr.yaml`,
not in the digest-pinned block — known gap from #192) are refreshed by the
weekly **servarr image-refresh CronJob**
(`2-k3s/maintenance/servarr-image-updater-cronjob.yaml`), which rolling-restarts
the servarr Deployments so `imagePullPolicy: Always` re-pulls the mutable tag.

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
  can take it down. Upgrade in a maintenance window. There is no `argocd`
  CLI on the workstation — trigger a manual self-sync with:
  ```
  kubectl --context epaflix -n argocd patch application argocd --type merge \
    -p '{"operation":{"initiatedBy":{"username":"<you>-manual"},"sync":{"revision":"main","syncStrategy":{"apply":{}}}}}'
  # if it still reports OutOfSync afterwards (stale compare cache), hard-refresh:
  kubectl --context epaflix -n argocd annotate application argocd \
    argocd.argoproj.io/refresh=hard --overwrite
  ```
  See issues #96 / #46.
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

- Remove auto-sync from an app: edit its `app-*.yaml` to drop the
  `automated:` block (or `argocd app set servarr --sync-policy none` if you
  have the CLI), commit, push.
- Full ArgoCD uninstall:
  ```
  helm uninstall argocd -n argocd
  kubectl --context epaflix delete ns argocd
  ```
  Workloads stay running; each tier's `kustomization.yaml` is still in repo
  and applies with `kubectl --context epaflix apply -k 2-k3s/08.servarr/` if needed.

## Traefik onboarding (safe adoption)

Traefik was Helmfile-installed before ArgoCD managed it, and it owns the
active ingress endpoint `192.168.10.101`. Adopt it manually first; do not
enable prune during initial adoption.

1. **Confirm runtime-only state exists**. These are not committed to git:
  ```
  kubectl --context epaflix -n traefik-system get secret cloudflare-api-token
  kubectl --context epaflix -n traefik-system get pvc
  kubectl --context epaflix -n traefik-system get svc traefik -o wide
  ```
  The Service must still show `192.168.10.101`, and the ACME PVC must be the
  same one currently mounted by Traefik.

2. **Render the git source before ArgoCD syncs it**:
  ```
  kubectl --context epaflix kustomize --enable-helm 2-k3s/05.traefik-deployment >/tmp/traefik-rendered.yaml
  ```
  Check that the rendered Traefik values still reference
  `cloudflare-api-token`, mount ACME storage at `/data/acme.json`, keep
  one replica, and keep the rendered Service `loadBalancerIP: 192.168.10.101`.

3. **Create the Application**:
  ```
  kubectl --context epaflix apply -f 2-k3s/11.argocd/apps/app-traefik.yaml
  ```

4. **Review the first diff, then sync manually only if safe**:
  ```
  argocd app diff traefik
  argocd app sync traefik
  ```

5. **Verify ingress after sync**:
  ```
  kubectl --context epaflix -n traefik-system rollout status deploy/traefik
  kubectl --context epaflix -n traefik-system get svc traefik -o wide
  curl -Ik https://traefik.epaflix.com/dashboard/
  ```

After a clean manual adoption window, switch the Application to automated
self-heal with `prune: false`. Leave prune disabled until deleting a manifest
from git should intentionally delete the live resource.

## Break-glass: restoring local admin login

The local `admin` account is disabled (`configs.cm.admin.enabled: false` in
`helm-values.yaml`, #1039). Authentik SSO via the `ArgoCD Admins` group is the
only login path. If Authentik is down, its OIDC client secret is wrong, or the
group mapping breaks, **nobody can log in to the tool that deploys Authentik.**

This is recoverable and does not need a working ArgoCD UI, a working Authentik,
or this repo. It needs `kubectl` and the age key.

### Is this actually the problem?

Before reaching for break-glass, confirm SSO is the failure. A read-only session
is not the same as no session:

```bash
# What ArgoCD thinks the group mapping is
kubectl --context epaflix -n argocd get cm argocd-rbac-cm -o jsonpath='{.data.policy\.csv}'; echo

# Is Authentik answering at all?
curl -4 -s -o /dev/null -w '%{http_code}\n' --max-time 10 \
  --resolve auth.epaflix.com:443:192.168.10.101 \
  https://auth.epaflix.com/application/o/argocd/.well-known/openid-configuration
```

A `200` from that endpoint means Authentik is serving the provider. If you get a
`200` and still cannot log in, the fault is the group mapping or the client
secret, not availability, and re-enabling local admin is the fastest way in
while you fix it.

> Do **not** diagnose this from `scopes_supported`. It lists scope mappings, not
> deliverable claims. `groups` rides inside Authentik's default `profile`
> mapping and does not appear as its own scope. Misreading that produced a false
> alarm once already (#1040). The only reliable check is a real login.

### Re-enable the account

```bash
kubectl --context epaflix -n argocd patch cm argocd-cm \
  --type merge -p '{"data":{"admin.enabled":"true"}}'
kubectl --context epaflix -n argocd rollout restart deploy/argocd-server
kubectl --context epaflix -n argocd rollout status deploy/argocd-server --timeout=120s
```

This survives because the `argocd` Application is **manual-sync** - it has no
`syncPolicy`, so nothing reverts the patch. On an Application with
`selfHeal: true` this would be undone within minutes.

### Get the password

The chart generated `admin.password` as a bcrypt hash at install and preserves
it across renders through Helm `lookup`. It is **not** in git and the hash
itself is not reversible - but that does not mean the password is lost.

**The initial password is probably still live.** The chart wrote it to
`argocd-initial-admin-secret` at install and it stays valid until someone
rotates it. Check that first - it is a two-minute recovery instead of a
password reset under pressure. Compare the two timestamps; if they match,
nothing ever rotated the password and the one in that Secret is current:

```bash
kubectl --context epaflix -n argocd get secret argocd-initial-admin-secret \
  -o jsonpath='{.metadata.creationTimestamp}'; echo
kubectl --context epaflix -n argocd get secret argocd-secret \
  -o jsonpath='{.data.admin\.passwordMtime}' | base64 -d; echo
# Equal timestamps => never rotated. Read the password into a variable and
# do NOT print it (#602); paste it straight into the login form.
PW=$(kubectl --context epaflix -n argocd get secret argocd-initial-admin-secret \
       -o jsonpath='{.data.password}' | base64 -d); echo "len=${#PW}"
```

Measured 2026-08-21: both read `2026-05-17T08:09:11Z` and the Secret still
holds a 16-character password, so this path works today.

Only if the timestamps differ, or `argocd-initial-admin-secret` is gone, set a
new one:

```bash
# bcrypt a new password without it reaching the shell history or a transcript
read -rs NEWPW
HASH=$(kubectl --context epaflix -n argocd exec deploy/argocd-server -- \
         argocd account bcrypt --password "$NEWPW")
kubectl --context epaflix -n argocd patch secret argocd-secret \
  --type merge -p "{\"stringData\":{\"admin.password\":\"$HASH\",\"admin.passwordMtime\":\"$(date -u +%FT%TZ)\"}}"
unset NEWPW HASH
```

### After recovery

Re-disable the account once SSO works again, and do it in git rather than by a
reverse patch, so the repo stays the source of truth:

```bash
kubectl --context epaflix -n argocd patch cm argocd-cm \
  --type merge -p '{"data":{"admin.enabled":"false"}}'
kubectl --context epaflix -n argocd rollout restart deploy/argocd-server
```

### What not to touch

`admin.password`, `admin.passwordMtime` and `server.secretkey` in
`argocd-secret` are chart-generated and absent from a local render **by design**
(#779). Disabling the account is a ConfigMap change and nothing more. Do not
prune those keys to "clean up" - removing `server.secretkey` invalidates every
issued token and logs out every session, including yours.

## Authentik onboarding (one-time)

Authentik was Helm-installed before ArgoCD existed. To put it under ArgoCD
without breaking sessions:

1. **Enable kustomize-with-helm in ArgoCD** (already in
   `2-k3s/11.argocd/helm-values.yaml` as `configs.cm.kustomize.buildOptions: --enable-helm`).
   If the cluster predates this change: `helm upgrade argocd argo/argo-cd
   --version 9.5.20 -n argocd -f helm-values.yaml --wait` then
   `kubectl --context epaflix -n argocd rollout restart deploy/argocd-repo-server`.

2. **Apply the runtime-secrets Secret** (substitute real values from
   the credential store `.github/instructions/secrets.enc.yaml` first - never
   commit the rendered file):
   ```
   kubectl --context epaflix apply -f 2-k3s/07.authentik-deployment/secret-app.yaml
   ```
   Keys: `AUTHENTIK_SECRET_KEY`, `AUTHENTIK_POSTGRESQL__PASSWORD`,
   `AUTHENTIK_EMAIL__PASSWORD`. `AUTHENTIK_SECRET_KEY` MUST match the value
   currently live in the chart-managed `authentik` Secret —
   `kubectl --context epaflix -n app-authentik get secret authentik -o jsonpath='{.data.AUTHENTIK_SECRET_KEY}' | base64 -d`.
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
   kubectl --context epaflix apply -f 2-k3s/11.argocd/apps/app-authentik.yaml
   ```
   In the UI: should be Healthy + (near-)Synced after the first refresh.
   Reconcile any drift, then flip `syncPolicy` to:
   ```yaml
   syncPolicy:
     automated: { selfHeal: true, prune: false }
     syncOptions: [ServerSideApply=true]
   ```

5. **Image bumps**: Renovate opens a grouped `authentik` PR when a new chart +
   image release lands (`groupName: authentik` in `.github/renovate.json`).
   Patch bumps auto-merge; **minor/major you review and merge manually**. On
   merge, ArgoCD edits `images.newTag` + `helmCharts.version` in
   `2-k3s/07.authentik-deployment/kustomization.yaml` and rolling-restarts
   server + worker. (Chart and image move in lockstep upstream — keep them in
   one commit, which the grouped Renovate rule guarantees.)

## Observability onboarding (one-time)

The observability stack (kube-prometheus-stack 82.2.0, Loki 6.53.0,
Promtail 6.17.1) was Helm-installed before ArgoCD. Single Application with
three `helmCharts:` entries in one kustomization — Loki/Promtail
ServiceMonitors hard-depend on kube-prometheus-stack CRDs so splitting them
into separate Apps would force a sync-wave dance ArgoCD does not solve
cleanly.

1. **Apply the new imperative Secrets BEFORE touching Helm** (real values
   substituted from the credential store `.github/instructions/secrets.enc.yaml`;
   never commit the rendered Secrets):
   - `grafana-admin-secret` — keys `admin-user`, `admin-password`
   - `alertmanager-config-secret` — single key `alertmanager.yaml` carrying
     the full route/receivers/SMTP-globals block
   - Patch `grafana-oauth-secret` to add a `client_id` key alongside the
     existing `client_secret`:
     ```
     kubectl --context epaflix -n observability patch secret grafana-oauth-secret \
       --type=merge -p '{"stringData":{"client_id":"<client-id-from-credential-store>"}}'
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
   kubectl --context epaflix diff -f <(kubectl --context epaflix kustomize --enable-helm 2-k3s/10.observability)
   ```
   The diff MUST be empty modulo `ignoreDifferences`-covered fields. If
   anything else shows, fix git — do not proceed.

4. **Create the Application** (still manual sync):
   ```
   kubectl --context epaflix apply -f 2-k3s/11.argocd/apps/app-observability.yaml
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

6. **Chart bumps are manual.** kube-prometheus-stack bumps require coordinated
   CRD migrations; Renovate opens minor/major PRs editing `helmCharts[*].version`
   in `2-k3s/10.observability/kustomization.yaml` for you to review and merge.

## Stale field-manager keys after Helm adoption (#779)

Every tier in this repo was `helm install`ed before ArgoCD adopted it, so the
original `helm` field manager still co-owns the chart-rendered Secrets and
ConfigMaps. Under `ServerSideApply=true`, dropping a key from git only releases
**ArgoCD's** claim. The `helm` claim keeps the key on the live object, and
ArgoCD diffs against the predicted merge result, so live equals predicted and
the App reports **Synced**. Git says the key is gone, the cluster still serves
it, and nothing goes red. Surfaced in #461 / PR #777.

### 1. The sweep needs `--show-managed-fields` or it lies

Read this before anything else. `kubectl --context epaflix get ... -o json` **strips
`managedFields` by default** (kubectl >= 1.21). A sweep without
`--show-managed-fields` returns **zero** hits and looks like a clean bill of
health. That is a false negative, not a clean cluster.

The corrected sweep - reads `.metadata.managedFields[].manager` only, never
`.data`:

```bash
kubectl --context epaflix get secret,configmap -A --show-managed-fields -o json | jq -r '
.items[] | select(((.metadata.managedFields//[])|map(.manager)|index("helm")) and ((.metadata.managedFields//[])|map(.manager)|index("argocd-controller")))
| .kind + "|" + .metadata.namespace + "|" + .metadata.name + "|" + (((.metadata.managedFields//[])|map(.manager+":"+.operation)|unique)|join(","))' | sort
```

Measured 2026-08-10: 191 Secret+ConfigMap objects cluster-wide, 100 carry
`argocd-controller`, 45 carry `helm`, and all 45 of those also carry
`argocd-controller` (helm-only: 0). Blast radius is 3 Apps only: `authentik`,
`observability`, `argocd`. `traefik`, `postgres` and `cert-manager` do **not**
carry it.

Capital-`Helm` is a different manager and needs its own pass. It is on 29
objects, all of them `sh.helm.release.v1.*` release-state Secrets, and **none**
is co-owned with `argocd-controller`. Nothing to act on there - but re-run the
pass rather than assume, swapping `"helm"` for `"Helm"` in the `jq` above.

### 2. Co-ownership on its own is NOT a defect

Do not act on a `helm` managedFields entry just because it exists. Of the 45
co-owned objects, **44 have zero dead keys** - the entry is leftover
co-ownership, not drift. Mass-stripping field managers or re-applying the lot
with `--force-conflicts` would rewrite ownership on every Grafana dashboard
ConfigMap plus the Alertmanager and promtail Secrets to fix nothing.

Confirm a real dead key per object before touching it. Live key **names** only,
never values:

```bash
# live key names, one object at a time
kubectl --context epaflix -n <ns> get <kind> <name> --template \
  '{{range $k,$v := .data}}{{$k}}{{"\n"}}{{end}}{{range $k,$v := .binaryData}}{{$k}}{{"\n"}}{{end}}'

# what git actually renders
kustomize build --enable-helm --enable-alpha-plugins --enable-exec 2-k3s/<tier>
```

A key is dead only if it is live **and** absent from the render. Never use
`-o yaml` / `-o json` on a Secret to do this - that prints every value.

### 3. Removing a confirmed dead key

Targeted, one key at a time. This is what #461 did and the App stayed
Synced/Healthy:

```bash
kubectl --context epaflix -n <ns> patch <kind> <name> --type=json \
  -p '[{"op":"remove","path":"/data/<key>"}]'
```

Prove it with a key **count** before and after, plus the App still
Synced/Healthy. Do not delete the object, and do not rotate anything as part of
this - those are separate decisions.

### 4. `argocd/argocd-secret` is EXCLUDED from field-manager surgery

Never strip a field manager and never force-apply on `argocd/argocd-secret`.
It carries 6 live-only keys and the chart renders the Secret with **zero** data
keys, so a render diff flags all 6 - and 5 of them are live-only by design:

- `admin.password`, `admin.passwordMtime`, `server.secretkey` - generated by the
  argo-cd chart at install and preserved through a Helm `lookup`. `helm
  template` / `kustomize build` has no cluster access, so `lookup` returns empty
  and these can never appear in a local render.
- `oidc.authentik.clientId`, `oidc.authentik.clientSecret` - merge-patched in
  out-of-git by `install.sh` step 4, documented in `kustomization.yaml` and
  tracked in #29.

Ownership, read with names only (`-o jsonpath` scoped to `.metadata.managedFields`
carries key names, never values):

```bash
kubectl --context epaflix -n argocd get secret argocd-secret \
  -o jsonpath='{.metadata.managedFields}' | jq -r '
  .[] | .manager + " / " + .operation + "  data-keys: " +
  ((.fieldsV1["f:data"] // {} | keys | map(sub("^f:";"")) | join(", ")) // "none")'
```

Measured 2026-08-10: `argocd-server:Update` owns `accounts.image-updater.tokens`,
`admin.password`, `admin.passwordMtime`, `server.secretkey`;
`kubectl-patch:Update` owns the two `oidc.*` keys; `helm:Update`,
`argocd-controller:Apply` and `argocd-controller:Update` own **zero** data keys
here. So the `helm` entry is not even holding a key alive on this object.

Force-applying or stripping ownership here risks pruning `server.secretkey`,
which logs every ArgoCD session out. The only supported change on this Secret is
a targeted single-key `kubectl patch` on a key proven dead.

### 5. Adoption rule for the next `helm install`ed release

Before adopting a `helm install`ed release into ArgoCD with
`ServerSideApply=true`:

1. Record the live `data` key **names** of every chart Secret and ConfigMap the
   App will own (the `--template` command in step 2).
2. Run the first sync.
3. Diff those names against the first render.

Co-ownership makes key **deletion** silent - ArgoCD reports Synced either way -
so deletion has to be verified out of band, once, at adoption time. Additions
show up normally; it is only removals that disappear.

### 6. Never write `syncPolicy: {}` in an Application (#1033)

Same lesson, different field. An empty map is not a stable way to say "manual
 sync": whether it survives depends on whether a field manager takes ownership
of `spec.syncPolicy` during apply. When one does, live stores `{}` and the
child is Synced. When none does, live stays `null`, git says `{}`, and the
child is `OutOfSync` forever over a purely cosmetic difference.

The ownership is an accident, not a property of the file. `app-reloader.yaml`
and `app-remote-pi.yaml` carried the identical `syncPolicy: {}` line and were
measured on 2026-08-20 with *opposite* ownership - `reloader` owned by
`argocd-controller` and Synced, `remote-pi` unowned and OutOfSync. Re-measured
on 2026-08-21 with no manifest change in between, both were unowned and both
were OutOfSync. A re-apply can flip it either way.

It is invisible without `--show-managed-fields`, exactly as in section 1:

```bash
for a in reloader remote-pi; do
  kubectl --context epaflix -n argocd get application "$a" --show-managed-fields -o json \
    | jq -r --arg a "$a" '.metadata.managedFields[]?
        | "\($a) manager=\(.manager) syncPolicy=\(.fieldsV1["f:spec"]["f:syncPolicy"] // "not-owned")"'
done
```

**Rule:** to mean manual sync, *omit* the key and leave a comment saying so, as
`app-argocd.yaml` does. Write a `syncPolicy:` block only when it has real
content (`automated`, `syncOptions`). Never `{}`.

## SOPS age key bootstrap (run ONCE per fresh cluster)

Before ArgoCD can decrypt any `*.enc.yaml` Secret, the cluster needs the
age private key. This is the one Secret that cannot itself be GitOps-
managed (chicken-egg).

```bash
# From the maintainer workstation:
kubectl --context epaflix create secret generic sops-age \
  -n argocd \
  --from-file=keys.txt=$HOME/.config/sops/age/k3s-cluster.txt

# Verify the repo-server picked it up:
kubectl --context epaflix -n argocd rollout restart deploy/argocd-repo-server
kubectl --context epaflix -n argocd rollout status deploy/argocd-repo-server
kubectl --context epaflix -n argocd logs deploy/argocd-repo-server -c install-ksops | tail
# Expected: "Done."
```

Rotation, encrypt, and decrypt recipes:
`.github/instructions/sops.instructions.md`.

## See also

- `QUICKSTART.md` — ordered install steps with the exact commands.
- `2-k3s/12.renovate/` + `.github/renovate.json` — image/chart bump delivery.
- `2-k3s/05.traefik-deployment/examples/app-with-native-oidc-authentik.md`
  — Authentik OIDC provider setup (mirror those steps for `argocd`).
- `2-k3s/05.traefik-deployment/kustomization.yaml` — what ArgoCD watches
  for Traefik.
- `2-k3s/08.servarr/kustomization.yaml` — what ArgoCD watches for servarr
  (Renovate digest-bump target = `images:` block).
- `2-k3s/07.authentik-deployment/kustomization.yaml` — what ArgoCD watches
  for authentik (Renovate grouped chart+image target = `images:` block).
- `2-k3s/10.observability/kustomization.yaml` — what ArgoCD watches for
  observability (three `helmCharts:` entries).
