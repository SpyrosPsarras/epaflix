# SearXNG web search for Pi — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the `pi` coding agent a `web_search` tool backed by a self-hosted SearXNG running on the Epaflix K3s cluster, so Pi (using local Ollama models) can search the web with nothing but outbound engine calls leaving the LAN.

**Architecture:** SearXNG is deployed as a new ArgoCD-managed app `2-k3s/14.searxng/` (kustomize + ksops + Traefik IngressRoute), mirroring `2-k3s/09.filebrowser/`. Pi gets a global auto-discovered TypeScript extension at `~/.pi/agent/extensions/searxng-web-search/` that calls SearXNG's JSON API over the internal Traefik path.

**Tech Stack:** K3s, ArgoCD (app-of-apps), Kustomize, KSOPS (SOPS+age), Traefik IngressRoute + cert-manager wildcard cert (Let's Encrypt via Cloudflare DNS-01), SearXNG container, Pi extension (TypeScript via jiti, typebox).

## Global Constraints

- Secrets live ONLY as SOPS `*.enc.yaml`; never commit a plaintext `kind: Secret` (pre-commit hook `check-sops-encrypted.sh` is a hard error). SOPS recipient: `age1586thf5vkcdf5lcn3zwjpu8ltkwyq8efrhj8lr0vdrrt9k5f3qgsxeg7gx`; `encrypted_regex: ^(data|stringData)$` (set by repo `.sops.yaml`, applied automatically by path `*.enc.yaml`).
- Decrypt key file: `~/.config/sops/age/k3s-cluster.txt` (set `SOPS_AGE_KEY_FILE` to it for decrypt/verify).
- Image is PINNED to a release tag; Renovate (`12.renovate`) manages bumps. No `:latest`.
- DNS: edit `/etc/dnsmasq.d/10-epaflix.conf` on Pi-hole `192.168.10.30` only — never the Pi-hole UI. The `*.epaflix.com` zone is per-host A records (NOT a wildcard).
- ArgoCD adoption order: push aligned git to `main` FIRST, then the app-of-apps creates the Application; do not create the Application before manifests are on `main`.
- Internal path only from Pi: `searxng.epaflix.com` A → `192.168.10.101` (Traefik LB). IPv6/AAAA points at Cloudflare (no tunnel) → the extension forces IPv4.
- Merge policy: branch → PR → rebase onto `origin/main` → merge (`gh pr merge --merge`). NEVER auto-merge. Show the PR body to Spyros before posting (external text rule).
- HARD GATES — STOP and get explicit OK before: (a) opening the PR (show body first), (b) the Pi-hole edit, (c) merging the PR, (d) any cluster apply. These are marked **[GATE]** below.
- Repo working path on disk: `~/Documents/Epaflix/k3s-proxmox/epaflix`. (Repo remote: `github.com/SpyrosPsarras/epaflix`.)

---

### Task 1: Feature branch + core manifests (namespace, configmap, service)

**Files:**
- Create: `2-k3s/14.searxng/namespace.yaml`
- Create: `2-k3s/14.searxng/configmap.yaml`
- Create: `2-k3s/14.searxng/service.yaml`

**Interfaces:**
- Produces: namespace `searxng`; ConfigMap `searxng-settings` (key `settings.yml`, contains literal token `${SEARXNG_SECRET}` to be rendered at pod start); Service `searxng` (ClusterIP, port `8080` name `http`, selector `app: searxng`).

- [ ] **Step 1: Create the feature branch**

```bash
cd ~/Documents/Epaflix/k3s-proxmox/epaflix
git fetch origin
git switch -c feat/searxng-web-search origin/main
```

- [ ] **Step 2: Write `namespace.yaml`**

```yaml
---
apiVersion: v1
kind: Namespace
metadata:
  name: searxng
  labels:
    app: searxng
```

- [ ] **Step 3: Write `configmap.yaml`** (settings.yml template; `${SEARXNG_SECRET}` is substituted by the initContainer in Task 2)

```yaml
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: searxng-settings
  namespace: searxng
  labels:
    app: searxng
data:
  settings.yml: |
    use_default_settings: true
    general:
      instance_name: "epaflix-searxng"
      debug: false
    search:
      safe_search: 1
      default_lang: "en"
      formats:
        - html
        - json
    server:
      bind_address: "0.0.0.0"
      port: 8080
      base_url: "https://searxng.epaflix.com/"
      secret_key: "${SEARXNG_SECRET}"
      limiter: false
      public_instance: false
      image_proxy: false
      method: "GET"
    ui:
      static_use_hash: true
```

- [ ] **Step 4: Write `service.yaml`**

```yaml
---
apiVersion: v1
kind: Service
metadata:
  name: searxng
  namespace: searxng
  labels:
    app: searxng
spec:
  type: ClusterIP
  selector:
    app: searxng
  ports:
    - name: http
      port: 8080
      targetPort: http
```

- [ ] **Step 5: Validate YAML syntax**

Run:
```bash
kubectl --context epaflix create --dry-run=client -o name -f 2-k3s/14.searxng/namespace.yaml -f 2-k3s/14.searxng/configmap.yaml -f 2-k3s/14.searxng/service.yaml
```
Expected: prints `namespace/searxng`, `configmap/searxng-settings`, `service/searxng` with no error.

- [ ] **Step 6: Commit**

```bash
git add 2-k3s/14.searxng/namespace.yaml 2-k3s/14.searxng/configmap.yaml 2-k3s/14.searxng/service.yaml
git commit -m "feat(searxng): namespace, settings configmap, service"
```

---

### Task 2: Deployment (with secret-render initContainer) + Traefik ingress

**Files:**
- Create: `2-k3s/14.searxng/deployment.yaml`
- Create: `2-k3s/14.searxng/ingress.yaml`

**Interfaces:**
- Consumes: ConfigMap `searxng-settings`, Service `searxng:8080` (Task 1); Secret `searxng-secret` with key `secret_key` (created in Task 3 — referenced here by name).
- Produces: Deployment `searxng` serving SearXNG on container port `8080` (name `http`), reading rendered `/etc/searxng/settings.yml`; IngressRoutes for `searxng.epaflix.com`.

- [ ] **Step 1: Write `deployment.yaml`** (image tag is the literal token `SEARXNG_TAG`, pinned in Task 4)

```yaml
---
# SearXNG deployment. The render-config initContainer substitutes the
# secret_key from the searxng-secret Secret into the ConfigMap-templated
# settings.yml, writing the result to an emptyDir the main container reads.
# Keeps the real secret_key out of the ArgoCD-managed ConfigMap.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: searxng
  namespace: searxng
  labels:
    app: searxng
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: searxng
  template:
    metadata:
      labels:
        app: searxng
    spec:
      securityContext:
        runAsUser: 977
        runAsGroup: 977
        fsGroup: 977
      initContainers:
        - name: render-config
          image: bash:5.2-alpine3.19
          command:
            - bash
            - -c
            - |
              set -euo pipefail
              SEC="$(< /secret/secret_key)"
              tpl="$(< /template/settings.yml)"
              tpl="${tpl//'${SEARXNG_SECRET}'/$SEC}"
              printf '%s' "$tpl" > /rendered/settings.yml
              echo "render-config: wrote $(wc -c < /rendered/settings.yml) bytes"
          volumeMounts:
            - name: settings-template
              mountPath: /template
              readOnly: true
            - name: searxng-secret
              mountPath: /secret
              readOnly: true
            - name: searxng-config
              mountPath: /rendered
      containers:
        - name: searxng
          image: searxng/searxng:SEARXNG_TAG
          env:
            - name: SEARXNG_SETTINGS_PATH
              value: /etc/searxng/settings.yml
            - name: TZ
              value: "Europe/Oslo"
          ports:
            - containerPort: 8080
              name: http
          volumeMounts:
            - name: searxng-config
              mountPath: /etc/searxng
          livenessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 20
            periodSeconds: 30
            timeoutSeconds: 10
          readinessProbe:
            httpGet:
              path: /healthz
              port: http
            initialDelaySeconds: 10
            periodSeconds: 10
            timeoutSeconds: 5
          resources:
            requests:
              memory: "256Mi"
              cpu: "100m"
      terminationGracePeriodSeconds: 30
      volumes:
        - name: settings-template
          configMap:
            name: searxng-settings
        - name: searxng-secret
          secret:
            secretName: searxng-secret
        - name: searxng-config
          emptyDir: {}
```

- [ ] **Step 2: Write `ingress.yaml`** (mirrors filebrowser exactly — websecure + http→https redirect)

```yaml
---
# SearXNG - HTTPS IngressRoute with Let's Encrypt TLS via Cloudflare
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: searxng-https
  namespace: searxng
spec:
  entryPoints:
    - websecure
  routes:
    - match: Host(`searxng.epaflix.com`)
      kind: Rule
      services:
        - name: searxng
          port: 8080
  tls:
    certResolver: cloudflare
    domains:
      - main: epaflix.com
        sans:
          - "*.epaflix.com"
---
# HTTP to HTTPS redirect for SearXNG
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: searxng-http
  namespace: searxng
spec:
  entryPoints:
    - web
  routes:
    - match: Host(`searxng.epaflix.com`)
      kind: Rule
      middlewares:
        - name: redirect-https
          namespace: traefik-system
      services:
        - name: searxng
          port: 8080
```

- [ ] **Step 3: Validate YAML syntax**

Run (server dry-run validates the Traefik CRD; requires the epaflix kubeconfig context):
```bash
kubectl config current-context   # confirm it targets the epaflix cluster
kubectl --context epaflix apply --dry-run=server -f 2-k3s/14.searxng/deployment.yaml -f 2-k3s/14.searxng/ingress.yaml
```
Expected: `deployment.apps/searxng created (server dry run)` and both IngressRoutes `created (server dry run)`, no schema errors. (The Deployment will reference Secret `searxng-secret`, not yet present — dry-run does not check that.)
Fallback if no cluster context: `kubectl --context epaflix create --dry-run=client -o name -f 2-k3s/14.searxng/deployment.yaml` for the Deployment, and lint the IngressRoute YAML with `python3 -c "import yaml,sys; list(yaml.safe_load_all(open('2-k3s/14.searxng/ingress.yaml')))"` (expected: no output = valid YAML).

- [ ] **Step 4: Commit**

```bash
git add 2-k3s/14.searxng/deployment.yaml 2-k3s/14.searxng/ingress.yaml
git commit -m "feat(searxng): deployment with secret-render initContainer and Traefik ingress"
```

---

### Task 3: SOPS secret + ksops generator + kustomization

**Files:**
- Create: `2-k3s/14.searxng/searxng-secret.enc.yaml` (written plaintext, then encrypted in place)
- Create: `2-k3s/14.searxng/ksops-generator.yaml`
- Create: `2-k3s/14.searxng/kustomization.yaml`

**Interfaces:**
- Produces: Secret `searxng-secret` (namespace `searxng`, `stringData.secret_key`) consumed by the Task 2 initContainer; a kustomization that aggregates all resources + the ksops generator.

- [ ] **Step 1: Write the plaintext secret to `searxng-secret.enc.yaml`** (generate a random key; do NOT commit yet)

```bash
cd ~/Documents/Epaflix/k3s-proxmox/epaflix
SECRET_KEY="$(openssl rand -hex 32)"
cat > 2-k3s/14.searxng/searxng-secret.enc.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: searxng-secret
  namespace: searxng
type: Opaque
stringData:
  secret_key: "${SECRET_KEY}"
EOF
unset SECRET_KEY
```

- [ ] **Step 2: Encrypt in place with SOPS** (uses `.sops.yaml` rule automatically via the `.enc.yaml` path)

```bash
sops --encrypt --in-place 2-k3s/14.searxng/searxng-secret.enc.yaml
```

- [ ] **Step 3: Verify it is encrypted and round-trips**

Run:
```bash
grep -q 'secret_key: ENC\[' 2-k3s/14.searxng/searxng-secret.enc.yaml && echo "ENCRYPTED-OK"
grep -q 'kind: Secret' 2-k3s/14.searxng/searxng-secret.enc.yaml && echo "METADATA-CLEARTEXT-OK"
SOPS_AGE_KEY_FILE=~/.config/sops/age/k3s-cluster.txt sops -d 2-k3s/14.searxng/searxng-secret.enc.yaml | grep -q 'secret_key:' && echo "DECRYPT-OK"
```
Expected: `ENCRYPTED-OK`, `METADATA-CLEARTEXT-OK`, `DECRYPT-OK`. If any fails, do NOT proceed (a plaintext secret must never be committed).

- [ ] **Step 4: Write `ksops-generator.yaml`**

```yaml
# ksops generator — decrypts searxng-secret.enc.yaml at render time inside
# argocd-repo-server (ksops binary from the install-ksops initContainer; see
# 2-k3s/11.argocd/helm-values.yaml). Local build:
#   SOPS_AGE_KEY_FILE=~/.config/sops/age/k3s-cluster.txt \
#   KSOPS_BIN=$(which ksops) kustomize build --enable-alpha-plugins --enable-exec .
apiVersion: viaduct.ai/v1
kind: ksops
metadata:
  name: searxng-secrets
  annotations:
    config.kubernetes.io/function: |
      exec:
        path: ksops
files:
  - searxng-secret.enc.yaml
```

- [ ] **Step 5: Write `kustomization.yaml`**

```yaml
# SearXNG — kustomization for the ArgoCD-managed App. The ksops generator
# inflates the searxng-secret Secret from searxng-secret.enc.yaml at render
# time (SOPS+age, Issue #29 pattern).
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - namespace.yaml
  - configmap.yaml
  - deployment.yaml
  - service.yaml
  - ingress.yaml

generators:
  - ksops-generator.yaml
```

- [ ] **Step 6: (Optional, recommended) Full local render with ksops**

Only if `kustomize` and `ksops` are installed locally (both currently MISSING — install: `kustomize` via `pacman -S kustomize` or the official release; `ksops` from `https://github.com/viaduct-ai/kustomize-sops/releases`). Then:
```bash
SOPS_AGE_KEY_FILE=~/.config/sops/age/k3s-cluster.txt \
KSOPS_BIN=$(which ksops) kustomize build --enable-alpha-plugins --enable-exec 2-k3s/14.searxng | kubectl --context epaflix apply --dry-run=client -f -
```
Expected: all resources incl. `secret/searxng-secret` print as `(dry run)`. If tools are not installed, skip — the authoritative render happens in-cluster via ArgoCD at Task 9.

- [ ] **Step 7: Commit**

```bash
git add 2-k3s/14.searxng/searxng-secret.enc.yaml 2-k3s/14.searxng/ksops-generator.yaml 2-k3s/14.searxng/kustomization.yaml
git commit -m "feat(searxng): SOPS secret_key + ksops generator + kustomization"
```

---

### Task 4: Pin the SearXNG image tag

**Files:**
- Modify: `2-k3s/14.searxng/deployment.yaml` (replace `SEARXNG_TAG`)

- [ ] **Step 1: Resolve the latest release tag**

Run:
```bash
curl -s "https://hub.docker.com/v2/repositories/searxng/searxng/tags?page_size=25&ordering=last_updated" \
  | jq -r '.results[].name' | grep -E '^[0-9]{4}\.[0-9]' | sort -r | head -5
```
Expected: a list of date-style tags (e.g. `2026.6.15-abc1234`). Pick the newest stable one (no `-rc`, no `latest`). Record it as the value for the next step.

- [ ] **Step 2: Substitute the tag into `deployment.yaml`**

```bash
# Replace RESOLVED_TAG with the tag chosen in Step 1
sed -i 's#searxng/searxng:SEARXNG_TAG#searxng/searxng:RESOLVED_TAG#' 2-k3s/14.searxng/deployment.yaml
grep 'image: searxng/searxng:' 2-k3s/14.searxng/deployment.yaml
```
Expected: the `image:` line shows the resolved tag and no longer contains `SEARXNG_TAG`.

- [ ] **Step 3: Commit**

```bash
git add 2-k3s/14.searxng/deployment.yaml
git commit -m "feat(searxng): pin searxng/searxng image tag"
```

Note: Renovate's kubernetes manager (`12.renovate`) picks up the `image:` line and will open bump PRs; no extra annotation needed.

---

### Task 5: ArgoCD Application + register in app-of-apps

**Files:**
- Create: `2-k3s/11.argocd/apps/app-searxng.yaml`
- Modify: `2-k3s/11.argocd/apps/kustomization.yaml` (add `- app-searxng.yaml`, keep alphabetical)

**Interfaces:**
- Consumes: the `2-k3s/14.searxng` kustomization (Task 1-4).
- Produces: ArgoCD Application `searxng` in the app-of-apps set.

- [ ] **Step 1: Write `app-searxng.yaml`** (adopt with `prune: false`; flip after soak per follow-up issue)

```yaml
---
# ArgoCD Application: SearXNG (self-hosted metasearch, JSON API for Pi).
#
# Source:       this repo, path 2-k3s/14.searxng, kustomize. The
#               kustomization inflates the ksops generator which decrypts
#               searxng-secret.enc.yaml at render time (Issue #29).
# Destination:  in-cluster `searxng` namespace.
# Sync:         selfHeal on, prune OFF for first adoption. Flip prune true
#               after the soak window (follow-up issue).
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: searxng
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/SpyrosPsarras/epaflix.git
    targetRevision: main
    path: 2-k3s/14.searxng
  destination:
    server: https://kubernetes.default.svc
    namespace: searxng
  syncPolicy:
    automated:
      selfHeal: true
      prune: false
  ignoreDifferences:
    - group: ""
      kind: Service
      jsonPointers:
        - /spec/clusterIP
        - /spec/clusterIPs
        - /status
```

- [ ] **Step 2: Add to `apps/kustomization.yaml`**

Modify the `resources:` list to include `app-searxng.yaml` in alphabetical position (between `app-renovate.yaml` and `app-servarr.yaml`):

```yaml
  - app-renovate.yaml
  - app-searxng.yaml
  - app-servarr.yaml
```

- [ ] **Step 3: Validate the apps kustomization still builds**

Run:
```bash
kubectl --context epaflix kustomize 2-k3s/11.argocd/apps >/dev/null && echo "APPS-KUSTOMIZE-OK"
```
Expected: `APPS-KUSTOMIZE-OK` (this set has no ksops generator, so plain `kubectl kustomize` works). Confirm the new Application appears:
```bash
kubectl --context epaflix kustomize 2-k3s/11.argocd/apps | grep -A1 'name: searxng'
```
Expected: shows the `searxng` Application metadata.

- [ ] **Step 4: Commit**

```bash
git add 2-k3s/11.argocd/apps/app-searxng.yaml 2-k3s/11.argocd/apps/kustomization.yaml
git commit -m "feat(searxng): ArgoCD Application + register in app-of-apps"
```

---

### Task 6: App docs (README + QUICKSTART)

**Files:**
- Create: `2-k3s/14.searxng/README.md`
- Create: `2-k3s/14.searxng/QUICKSTART.md`

- [ ] **Step 1: Write `README.md`**

```markdown
# SearXNG

Self-hosted metasearch engine. Sole consumer is the `pi` coding agent on the
maintainer workstation, which calls the JSON API for its `web_search` tool.

## Design
- ArgoCD-managed (app-of-apps `app-searxng`), kustomize + ksops.
- `secret_key` injected from SOPS Secret `searxng-secret` via a render
  initContainer into `/etc/searxng/settings.yml` (filebrowser pattern).
- `limiter: false`, `public_instance: false` → no Valkey/Redis, stateless,
  single replica. JSON API enabled via `search.formats: [html, json]`.
- Exposed at `https://searxng.epaflix.com` (Traefik websecure, wildcard
  `*.epaflix.com` Let's Encrypt cert). Internal-only: Pi-hole A record
  `searxng.epaflix.com → 192.168.10.101`; no Cloudflare tunnel, not public.

## Verify
    curl --resolve searxng.epaflix.com:443:192.168.10.101 \
      'https://searxng.epaflix.com/search?q=test&format=json' | jq '.results | length'

## Roll back
Revert the `14.searxng` manifests on `main`; ArgoCD prunes after the
soak-window prune flip, or delete the Application + namespace manually.
```

- [ ] **Step 2: Write `QUICKSTART.md`**

```markdown
# SearXNG — quickstart

## Deploy (GitOps)
1. Merge the `14.searxng` manifests + `app-searxng.yaml` to `main`.
2. app-of-apps reconciles → ArgoCD creates the `searxng` Application.
3. Add Pi-hole record `searxng.epaflix.com → 192.168.10.101` in
   `/etc/dnsmasq.d/10-epaflix.conf`, reload FTL.

## Check
    kubectl --context epaflix -n argocd get application searxng
    kubectl --context epaflix -n searxng get pods
    curl --resolve searxng.epaflix.com:443:192.168.10.101 \
      'https://searxng.epaflix.com/search?q=test&format=json' | jq '.results | length'

## Pi tool
Extension at `~/.pi/agent/extensions/searxng-web-search/` (auto-loaded).
Test: `pi -p "search the web for the latest k3s release"`.
```

- [ ] **Step 3: Commit**

```bash
git add 2-k3s/14.searxng/README.md 2-k3s/14.searxng/QUICKSTART.md
git commit -m "docs(searxng): README and QUICKSTART"
```

---

### Task 7: **[GATE]** Push branch + open PR (show body first)

**Files:** none (git/GitHub only)

- [ ] **Step 1: Push the branch**

```bash
git push -u origin feat/searxng-web-search
```

- [ ] **Step 2: Draft the PR body and SHOW IT TO SPYROS** — do not run `gh pr create` until he approves the wording (external-text rule). Follow the `create-pull-request` skill for format + the mandatory `/security-review`. Draft body covers: what (SearXNG app + Pi extension follows separately), why, the JSON-API/limiter-off decision, the internal-only exposure, the adopt-with-prune-off choice, and a Test plan checklist (ArgoCD Synced/Healthy, curl JSON, DNS resolves, pi search works).

- [ ] **Step 3: After Spyros approves the body, create the PR**

```bash
gh pr create --repo SpyrosPsarras/epaflix --base main --head feat/searxng-web-search \
  --title "feat(searxng): self-hosted web search for the pi agent" \
  --body-file <approved-body-file>
```

- [ ] **Step 4: STOP.** Do not merge. Report the PR URL. Merge happens in Task 9 after rebase + Spyros's OK.

---

### Task 8: **[GATE]** Pi-hole DNS record

**Files:** `/etc/dnsmasq.d/10-epaflix.conf` on Pi-hole `192.168.10.30` (not in this repo)

- [ ] **Step 1: Confirm with Spyros before editing Pi-hole.** This is an infra change.

- [ ] **Step 2: Inspect the existing per-host record format**

```bash
ssh ubuntu@192.168.10.30 "grep -n 'filebrowser.epaflix.com' /etc/dnsmasq.d/10-epaflix.conf"
```
Expected: shows the exact directive style used (e.g. `address=/filebrowser.epaflix.com/192.168.10.101` or `host-record=...`). Match it exactly.

- [ ] **Step 3: Add the SearXNG record in the same style** (example assumes `address=` style; use whatever Step 2 revealed)

```bash
ssh ubuntu@192.168.10.30 \
  "echo 'address=/searxng.epaflix.com/192.168.10.101' | sudo tee -a /etc/dnsmasq.d/10-epaflix.conf"
ssh ubuntu@192.168.10.30 "sudo systemctl restart pihole-FTL"
```

- [ ] **Step 4: Verify resolution from the workstation**

```bash
getent ahostsv4 searxng.epaflix.com | head -1
```
Expected: `192.168.10.101  STREAM searxng.epaflix.com`. (If it still shows Cloudflare `104.21.x`, the record or reload failed.)

---

### Task 9: **[GATE]** Merge + ArgoCD sync + verify live

**Files:** none (GitHub + cluster)

- [ ] **Step 1: Rebase the branch onto latest main** (required before merge)

```bash
cd ~/Documents/Epaflix/k3s-proxmox/epaflix
git fetch origin
git rebase origin/main
git push --force-with-lease
```
(Force-push is gated — Spyros's OK already covered by the merge gate; if he wants, he runs it.)

- [ ] **Step 2: Spyros merges the PR** (or explicitly authorizes it). Never auto-merge.

```bash
gh pr merge <N> --repo SpyrosPsarras/epaflix --merge
```

- [ ] **Step 3: Watch ArgoCD create + sync the app**

```bash
kubectl --context epaflix -n argocd get application searxng -w
```
Expected: `SYNC STATUS = Synced`, `HEALTH STATUS = Healthy`. (Ctrl-C when reached.)

- [ ] **Step 4: Verify the pod and JSON API**

```bash
kubectl --context epaflix -n searxng get pods
curl --resolve searxng.epaflix.com:443:192.168.10.101 \
  'https://searxng.epaflix.com/search?q=test&format=json' | jq '.results | length'
```
Expected: pod `Running` 1/1; the curl returns a JSON number > 0. If `format=json` returns an HTML error, re-check `search.formats` in the ConfigMap.

- [ ] **Step 5: Verify via the real DNS name (after Task 8)**

```bash
curl -s 'https://searxng.epaflix.com/search?q=test&format=json' | jq '.results | length'
```
Expected: a number > 0, served over the valid Let's Encrypt cert with no `--resolve`.

---

### Task 10: Pi `web_search` extension (workstation)

**Files:**
- Create: `~/.pi/agent/extensions/searxng-web-search/format.mjs`
- Create: `~/.pi/agent/extensions/searxng-web-search/index.ts`
- Create: `~/.pi/agent/extensions/searxng-web-search/test/format.test.mjs`

**Interfaces:**
- `buildSearchUrl(baseUrl: string, query: string, count: number) => string` — builds the SearXNG JSON search URL.
- `formatResults(json: object, count: number) => string` — renders top-`count` results as text; returns a clear "no results" string when empty.
- `index.ts` registers tool `web_search` ({ query: string, count?: number }) using the above; forces IPv4 via `dns.setDefaultResultOrder("ipv4first")`.

- [ ] **Step 1: Write the failing test** `test/format.test.mjs`

```javascript
import { test } from "node:test";
import assert from "node:assert/strict";
import { buildSearchUrl, formatResults } from "../format.mjs";

test("buildSearchUrl adds format=json and encodes the query", () => {
  const url = buildSearchUrl("https://searxng.epaflix.com", "k3s release", 5);
  assert.match(url, /\/search\?/);
  assert.match(url, /format=json/);
  assert.match(url, /q=k3s\+release|q=k3s%20release/);
  assert.match(url, /language=en/);
});

test("buildSearchUrl trims a trailing slash on baseUrl", () => {
  const url = buildSearchUrl("https://searxng.epaflix.com/", "x", 3);
  assert.ok(!url.includes("com//search"));
});

test("formatResults renders top N as title/url/snippet", () => {
  const json = { results: [
    { title: "A", url: "https://a.test", content: "snippet a" },
    { title: "B", url: "https://b.test", content: "snippet b" },
    { title: "C", url: "https://c.test", content: "snippet c" },
  ] };
  const out = formatResults(json, 2);
  assert.match(out, /A/);
  assert.match(out, /https:\/\/a\.test/);
  assert.match(out, /snippet a/);
  assert.ok(!out.includes("C"), "should cap at count=2");
});

test("formatResults handles empty results", () => {
  assert.match(formatResults({ results: [] }, 5), /no results/i);
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run:
```bash
node --test ~/.pi/agent/extensions/searxng-web-search/test/format.test.mjs
```
Expected: FAIL — cannot find module `../format.mjs`.

- [ ] **Step 3: Write `format.mjs`**

```javascript
// Pure helpers for the SearXNG web_search tool — no I/O, unit-tested.
export function buildSearchUrl(baseUrl, query, count) {
  const base = baseUrl.replace(/\/+$/, "");
  const params = new URLSearchParams({
    q: query,
    format: "json",
    language: "en",
    safesearch: "1",
  });
  return `${base}/search?${params.toString()}`;
}

export function formatResults(json, count) {
  const results = Array.isArray(json?.results) ? json.results : [];
  if (results.length === 0) {
    return "No results found.";
  }
  return results
    .slice(0, count)
    .map((r, i) => {
      const title = (r.title || "(untitled)").trim();
      const url = r.url || "";
      const snippet = (r.content || "").trim();
      return `${i + 1}. ${title}\n   ${url}${snippet ? `\n   ${snippet}` : ""}`;
    })
    .join("\n\n");
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run:
```bash
node --test ~/.pi/agent/extensions/searxng-web-search/test/format.test.mjs
```
Expected: PASS — 4 tests pass.

- [ ] **Step 5: Write `index.ts`** (the Pi extension)

```typescript
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import dns from "node:dns";
import { buildSearchUrl, formatResults } from "./format.mjs";

// searxng.epaflix.com has an internal A record (192.168.10.101) but its
// AAAA points at Cloudflare (no tunnel). Prefer IPv4 so we hit Traefik.
dns.setDefaultResultOrder("ipv4first");

const SEARXNG_URL = process.env.SEARXNG_URL || "https://searxng.epaflix.com";

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "web_search",
    label: "Web Search",
    description:
      "Search the web via the self-hosted SearXNG instance. Returns the top results as title, URL, and snippet. Use the URLs with bash+curl to read a page in full.",
    parameters: Type.Object({
      query: Type.String({ description: "The search query" }),
      count: Type.Optional(
        Type.Number({ description: "How many results to return (default 5)" }),
      ),
    }),
    promptSnippet: "web_search(query, count?) — search the web via SearXNG",
    promptGuidelines: [
      "Use web_search when the user asks about current events, library/tool versions, or facts not present in the codebase.",
      "After web_search, use bash+curl to fetch a result URL when you need the full page content.",
    ],
    async execute(_toolCallId, params, signal) {
      const count =
        typeof params.count === "number" && params.count > 0 ? params.count : 5;
      const url = buildSearchUrl(SEARXNG_URL, params.query, count);
      let res: Response;
      try {
        res = await fetch(url, {
          signal,
          headers: { Accept: "application/json" },
        });
      } catch (e) {
        return {
          content: [
            { type: "text", text: `web_search failed to reach SearXNG at ${SEARXNG_URL}: ${(e as Error).message}` },
          ],
          details: {},
        };
      }
      if (!res.ok) {
        return {
          content: [
            { type: "text", text: `web_search got HTTP ${res.status} from SearXNG.` },
          ],
          details: {},
        };
      }
      const json = await res.json();
      return {
        content: [{ type: "text", text: formatResults(json, count) }],
        details: {},
      };
    },
  });
}
```

- [ ] **Step 6: Confirm Pi loads the extension and registers the tool**

Run:
```bash
pi -e ~/.pi/agent/extensions/searxng-web-search/index.ts -p "list your available tools" 2>&1 | grep -i web_search
```
Expected: output mentions `web_search` (the extension loaded with no TypeScript/import error). If it errors on `./format.mjs`, confirm the file is beside `index.ts`.

- [ ] **Step 7: Commit is N/A** — `~/.pi/agent/extensions/` is not in the epaflix repo. Note the extension location in the PR description / a follow-up note instead. (No separate VCS for `~/.pi` unless Spyros wants one — ask.)

---

### Task 11: **[GATE]** End-to-end verification through Pi

**Files:** none

- [ ] **Step 1: Confirm the extension is auto-discovered globally** (it lives in `~/.pi/agent/extensions/searxng-web-search/index.ts`, an auto-load location — no `settings.json` edit needed; the other Claude session concern does not apply)

```bash
pi -p "use web_search to find the latest k3s release version, then tell me the version number" 2>&1 | tail -20
```
Expected: Pi calls `web_search`, gets SearXNG results, and answers with a version. This requires Task 9 (SearXNG live) and Task 8 (DNS) done.

- [ ] **Step 2: Negative check — confirm IPv4 path is used (not Cloudflare)**

```bash
SEARXNG_URL=https://searxng.epaflix.com node -e "import('node:dns').then(d=>{d.default.setDefaultResultOrder('ipv4first');d.default.lookup('searxng.epaflix.com',{family:4},(e,a)=>console.log(a))})"
```
Expected: prints `192.168.10.101`.

- [ ] **Step 3: Report** results to Spyros (PR URL, ArgoCD status, sample search output).

---

### Task 12: Follow-up issues + report

**Files:** none

- [ ] **Step 1: Open follow-up `gh issue`s** on `SpyrosPsarras/epaflix` (repo rule — every follow-up gets an issue), using the repo's `## Finding / ## Current state / ## Desired outcome / ## Notes` shape:
  - Soak → flip `app-searxng` `syncPolicy.automated.prune: true` after the soak window.
  - Optional: add a `fetch_url` tool to the Pi extension if the models struggle to read pages with bash+curl.
  - Optional: decide whether `~/.pi/agent/extensions/searxng-web-search/` should be version-controlled.

- [ ] **Step 2: Final report** — summarize what shipped, the PR, live verification evidence, and the open follow-up issues.

---

## Self-Review

**Spec coverage:**
- SearXNG k3s app (namespace/configmap/deployment/service/ingress/secret/ksops/kustomization) → Tasks 1-4, 6. ✓
- JSON API + limiter off, no Redis → configmap in Task 1. ✓
- SOPS secret_key → Task 3. ✓
- ArgoCD app + app-of-apps registration, adopt prune:false → Task 5. ✓
- Pi-hole A record → Task 8. ✓
- Adoption order (git first) → enforced by ordering Tasks 7/9 before the Application exists on main; Application only reconciles post-merge. ✓
- Pi extension web_search, IPv4-forced, SEARXNG_URL default → Task 10. ✓
- Gates (PR body shown, Pi-hole, merge, apply) → Tasks 7, 8, 9. ✓
- Verification checklist from spec → Tasks 9, 11. ✓
- Out-of-scope items (SSO, Redis, HA, fetch_url) → not built; fetch_url tracked as follow-up Task 12. ✓

**Placeholder scan:** `SEARXNG_TAG`/`RESOLVED_TAG` are deliberate resolve-at-apply tokens handled by Task 4 (with the resolving command), not vague TODOs. `<N>`/`<approved-body-file>` are runtime values (PR number, the file holding the Spyros-approved body). No "TBD"/"add error handling"/"similar to Task N" left.

**Type consistency:** `buildSearchUrl(baseUrl, query, count)` and `formatResults(json, count)` are defined in `format.mjs` (Task 10 Step 3), tested with the same signatures (Step 1), and imported with the same names in `index.ts` (Step 5). Secret name `searxng-secret` and key `secret_key` are consistent across deployment (Task 2), secret (Task 3), and initContainer mount path `/secret/secret_key`. Service port `8080`/name `http` consistent across service, deployment, ingress.
