# 13.odysseus — Odysseus AI workspace (k3s GitOps tier)

Backend-only migration of the **Odysseus** AI workspace
(`pewdiepie-archdaemon/odysseus`) from a 4-container TrueNAS Custom App
(host port `30070`) into this GitOps tier. Resolves **#184**.

## What changed vs. the TrueNAS deployment

- **GPU dropped.** k3s workers have no GPU. Chat/embedding models are served
  by the **existing remote Ollama** on TrueNAS
  (`http://192.168.10.200:30068/v1`); `fastembed` runs on CPU. No `nvidia.com/gpu`
  request/limit anywhere.
- **Ollama is remote**, not co-located — `OLLAMA_BASE_URL` / `LLM_HOST` point
  at the TrueNAS Ollama (reachable from the pods, verified in the test plan).
- **SearXNG JSON-403 search bug fixed structurally.** `settings.yml` is shipped
  by the `searxng-settings` ConfigMap mounted at `/etc/searxng/settings.yml`
  (`use_default_settings: true`, `server.limiter: false`,
  `search.formats: [html, json]`). `server.secret_key` is injected from the
  `SEARXNG_SECRET` env, so the committed ConfigMap stays secret-free.
- **Sidecars** (`searxng`, `chromadb`, `ntfy`) run in the same `odysseus`
  namespace and are reached by intra-namespace DNS: `searxng:8080`,
  `chromadb:8000`, `ntfy:8091`.
- **Authentik UNCHANGED.** Provider `pk83`, app slug `odysseus`, group
  "Odysseus users", `external_host https://odysseus.epaflix.com`, embedded
  outpost — all untouched. Odysseus keeps `AUTH_ENABLED=true`, so the
  double-login-by-design (Authentik perimeter + Odysseus built-in login) is
  preserved.
- **No `/app/.ssh` mount** — deliberately omitted (RCE hardening carried from
  the compose; the dir never existed in the running container).

## Files

| File | Purpose |
|---|---|
| `namespace.yaml` | `odysseus` namespace |
| `odysseus.yaml` | App Deployment + ClusterIP Service `:7000` + local-path data PVC + non-clobber `seed-data` initContainer |
| `searxng.yaml` | SearXNG Deployment + Service `:8080` (settings via ConfigMap) |
| `chromadb.yaml` | ChromaDB Deployment + Service `:8000` + local-path PVC |
| `ntfy.yaml` | ntfy Deployment + Service `:8091` |
| `configmap.yaml` | `odysseus-config` (non-secret env) + `searxng-settings` (settings.yml) |
| `odysseus-secrets.enc.yaml` | SOPS Secret: `ODYSSEUS_ADMIN_PASSWORD`, `SEARXNG_SECRET`, `OPENAI_API_KEY`, `HF_TOKEN` |
| `odysseus-data-seed.enc.yaml` | SOPS Secret seeding small irreplaceable state into the data PVC |
| `ksops-generator.yaml` | ksops generator listing the two `*.enc.yaml` files |
| `pdb.yaml` | PodDisruptionBudget |
| `kustomization.yaml` | Tier kustomization |

## Data migration order (CRITICAL)

The owner's login and tool state live in small files on TrueNAS
(`/mnt/pool1/odysseus/data`): `auth.json` (bcrypt admin record),
`settings.json` (SearXNG-json / `disabled_tools` state), `app.db` (~561 KB
SQLite), plus the `skills/`, `personal_docs/`, `rag/` trees.

The migration MUST land **before Odysseus serves**, so the app never
auto-creates a fresh admin that clobbers the migrated `auth.json`. Two
non-clobber mechanisms cooperate (pick ONE per file to avoid double-seed):

1. **`seed-data` initContainer (in `odysseus.yaml`).** Copies `/seed/*` (from
   the `odysseus-data-seed` SOPS Secret) into `/app/data` **only when the
   destination does not already exist**, then `chown -R 1000:1000 /app/data`.
   Pattern copied verbatim from `08.servarr/newtarr/newtarr.yaml`. Keys with
   `/` (the dir trees) cannot be Secret keys, so the Secret carries only tiny
   JSONs; today it ships a single inert `.seed-managed-by` marker
   (dot-prefixed, so it is NOT matched by `/seed/*`) — to codify the real
   `auth.json`/`settings.json` in git, add them via
   `sops odysseus-data-seed.enc.yaml`.
2. **tar-staging (preferred for `app.db` + dir trees).** On TrueNAS, tar ONLY
   the irreplaceable small state (NOT the ~4.8 G caches):

   ```bash
   ssh truenas_admin@192.168.10.200
   sudo tar -C /mnt/pool1/odysseus/data -czf /tmp/odysseus-state.tgz \
     app.db auth.json settings.json cookbook_state.json memory.json \
     presets.json sessions.json user_prefs.json scheduled_emails.db \
     skills personal_docs rag memory_vectors
   ```

   Stage it onto an in-cluster-reachable path (NFS, or `kubectl cp` into a
   one-shot seed Job pod) and extract NON-CLOBBERINGLY into the
   `odysseus-data` PVC, then `chown -R 1000:1000 /app/data`. Verify the tarred
   `settings.json` is the post-fix (SearXNG-json-friendly) version.

`ODYSSEUS_ADMIN_PASSWORD` in `odysseus-secrets` is the **bootstrap/reset**
password and is distinct from the stored hash; restoring `auth.json` verbatim
means the owner logs in with their EXISTING password (no reset).

**NOT migrated** (re-buildable): `data/huggingface`, `data/local`,
`fastembed_cache` (~4.8 G HF/fastembed model caches — re-download on first
run, CPU), ChromaDB vectors (cold start; RAG re-embeds on demand), `ntfy-cache`
(non-critical, emptyDir), `searxng-data` (replaced by the ConfigMap). Optional
ChromaDB vector carry-over: `docker run --rm -v chromadb-data:/from -v /tmp:/to
busybox tar -C /from -czf /to/chroma.tgz .` on TrueNAS, then seed into the
`chromadb-data` PVC the same way.

## Deploy

Prerequisites (owner-side, NOT part of the manifest PR):

1. Provision a `write:packages` credential and push the image from the TrueNAS
   docker engine to `ghcr.io/spyrospsarras/odysseus:73673258` (lowercase),
   then make the package **public** (k3s has no GHCR imagePullSecret). Verify
   `sudo k3s ctr images pull ghcr.io/spyrospsarras/odysseus:73673258` on a
   worker.
2. Re-verify `pk83` / slug `odysseus` / `external_host` — expect zero edits.
   The personal Authentik admin API token was **retired by #175**, so there is
   nothing to regenerate; do this check in the UI, or if scripting use the
   durable `ak-iac` service-account token (#185) — or mint a short-lived scoped
   token on demand and delete it afterward — per the **Admin / Automation API
   tokens** runbook in
   [../07.authentik-deployment/README.md](../07.authentik-deployment/README.md).

Then (per the Epaflix adoption-order rule — git aligned BEFORE the Application
exists):

```bash
# After merge, app-of-apps (selfHeal:true) auto-CREATES the manual-sync
# odysseus Application. Creation alone does NOT deploy — trigger the first sync:
argocd app sync odysseus
# Pods schedule (odysseus, searxng, chromadb, ntfy); seed-data populates the PVC.
# The public route still points at the in-cluster Service after the route PR
# below merges — TrueNAS stays LIVE as fallback until then.
```

Verify in-cluster WITHOUT touching the public route (port-forward
`svc/odysseus 7000:7000`): no GPU scheduled, image is the GHCR digest, Ollama
reachable, `searxng /search?...&format=json` → 200 (was 403), UI loads, login
with the existing password, `app.db`/`auth.json` present and owned `1000:1000`.

## Route cutover

`2-k3s/05.traefik-deployment/ingress/odysseus-proxy.yaml` was repointed from
the out-of-band Endpoints/headless Service (→ `192.168.10.200:30070`) to the
in-cluster `odysseus` ClusterIP Service cross-namespace
(`services[].namespace: odysseus`, `port: 7000`, `scheme: http`). Cross-namespace
refs work because Traefik runs with `--providers.kubernetescrd.allowCrossNamespace=true`.
The priority:10 app route (`authentik-forwardauth`), priority:15 outpost route
(`authentik-server`), and the `web`→`redirect-https` redirect are all preserved.

## Rollback

- **Manifests:** `git revert` the PR. app-of-apps selfHeal removes/repairs the
  `odysseus` Application. With `syncPolicy: {}` (manual) + `prune: false`, an
  erroneous manifest never auto-deletes live resources.
- **Route:** one-file change — restore the Endpoints + headless Service block
  and point the IngressRoutes back at `192.168.10.200:30070`. TrueNAS stays
  LIVE through the whole verify window, so reverting the route file instantly
  restores production with zero data movement.
- **Data:** the seed is non-clobbering and the TrueNAS
  `/mnt/pool1/odysseus/data` is never mutated (read-only tar). Keep
  `/mnt/pool1/odysseus` as a cold backup even after decommission.
- **Decommission of the TrueNAS Custom App is a SEPARATE post-soak gate** —
  do not stop `ix-odysseus` until the k3s deployment has soaked clean.

## Image refresh / rebuild

The container is a **locally-built image** (`ghcr.io/spyrospsarras/odysseus`),
not an upstream-published tag — there is no semver/`:latest` stream to track, so
the refresh is a deliberate manual flow. Resolves **#211**.

### Source + reproducible build

- **Upstream:** `pewdiepie-archdaemon/odysseus`, pinned at commit
  `73673258199b353f9b3e04da9b37ae95077e2c8b` (= the short image tag `73673258`).
- **Build:** `docker build --build-arg INSTALL_OPTIONAL=false …` — MIT-clean
  (`INSTALL_OPTIONAL=true` pulls PyMuPDF AGPL-3.0 + markitdown; deliberately
  omitted). The detailed, step-by-step recipe (clone → `git checkout <sha>` →
  `git rev-parse HEAD` guard → build) lives in the provenance runbook at
  [../../0-truenas/custom-apps/odysseus/README.md](../../0-truenas/custom-apps/odysseus/README.md).
- **Config is NOT baked into the image.** Two pieces of runtime behavior live
  outside the image and therefore survive a rebuild unchanged — do **not**
  re-apply them when refreshing:
  - the **SearXNG JSON search fix** lives in the `searxng-settings` ConfigMap
    (`configmap.yaml`), mounted at `/etc/searxng/settings.yml`;
  - `disabled_tools` lives in the `odysseus-data-seed` SOPS Secret
    (`settings.json`, applied by the `seed-data` initContainer into the data
    PVC).

### Manual rebuild → deploy flow

1. **Build** the new image at the chosen upstream commit, tagged with its short
   SHA: `docker build --build-arg INSTALL_OPTIONAL=false -t odysseus:<newshortsha> .`
   (`git rev-parse HEAD` must match the full SHA before building).
2. **Push to GHCR** (lowercase namespace):

   ```bash
   docker tag odysseus:<newshortsha> ghcr.io/spyrospsarras/odysseus:<newshortsha>
   docker push ghcr.io/spyrospsarras/odysseus:<newshortsha>
   ```

3. **Make the package PUBLIC** in the GitHub web UI (k3s has no GHCR
   imagePullSecret, so containerd pulls anonymously). This is a **manual web-UI
   step** — see the PAT caveat below.
4. **Verify the pull** on a k3s worker before bumping the manifest:

   ```bash
   sudo k3s ctr images pull ghcr.io/spyrospsarras/odysseus:<newshortsha>
   ```

   (or let the kubelet pull once the manifest lands).
5. **Bump the manifest.** Edit the **literal** image tag in `odysseus.yaml`
   (`image: ghcr.io/spyrospsarras/odysseus:<newshortsha>`, ~line 139) — it is a
   plain literal, **not** a kustomize `images:`/`newTag` override. Open a PR.
6. **After merge, confirm the sync.** The `odysseus` ArgoCD app reconciles the
   new tag automatically (selfHeal is **on** since **#210**) — confirm
   Synced/Healthy and the new image is running.

### Refresh cadence / trigger

- **Quarterly review** of `pewdiepie-archdaemon/odysseus` commits since the
  pinned SHA, **plus** an on-demand rebuild on any upstream **security advisory**
  or needed fix. **Watch** the upstream repo for the signal.
- **Renovate stays DISABLED** for this image (no semver stream to track — see
  the `ghcr.io/spyrospsarras/odysseus` rule in `.github/renovate.json`).
  Reconsider a constrained image-updater alias only if a stable published tag
  scheme emerges upstream.

### GHCR PAT caveat

`ghcr_write_packages_pat` (in `.github/instructions/secrets.yml`) has only the
`write:packages` scope, **not** `admin:packages` — so package **visibility**
toggles (public/private) must be done in the **GitHub web UI**, not via the API
or CLI. The token is sufficient to `docker push` the new tag, but not to flip
visibility.

## Follow-ups

selfHeal flip, prune flip, TrueNAS decommission, the `write:packages` PAT
prerequisite, Authentik token regeneration — tracked as separate `gh` issues
(#184 and children).
