# SOPS + age secret automation for ArgoCD-managed apps

> **Partly superseded 2026-08-21:** the workstation key file this design places
> at `~/.config/sops/age/keys.txt` was shredded. The key now lives in KeePassXC
> (entry `sops-age-k3s-cluster`), read by a `sops` wrapper on PATH. The
> in-cluster half of the design, `argocd/sops-age` mounted at
> `/var/sops/keys.txt`, is unchanged. Current procedures:
> `.github/instructions/sops.instructions.md`.

- **Issue:** SpyrosPsarras/epaflix#29
- **Date:** 2026-05-25
- **Status:** Approved, awaiting implementation plan
- **Author:** Spyros Psarras (with Claude assist)

## Background

Every ArgoCD Application in this repo (`authentik`, `traefik`, `servarr`,
`observability`, `filebrowser`, `postgres`, `cert-manager`, ...) currently
creates its required Secrets **imperatively** from the maintainer's plaintext
credential file, since decommissioned in favour of the credential store
`.github/instructions/secrets.enc.yaml`. The Secret resources are deliberately
excluded from each App's kustomization so that ArgoCD does not overwrite
live credentials with the placeholder YAML committed to git.

Drawbacks of the imperative pattern:

- No GitOps source-of-truth for Secrets — a freshly rebuilt cluster requires
  hand-running the imperative steps in each app's `README.md`.
- That plaintext credential file was git-ignored and lived only on the
  maintainer's workstation - single point of loss.
- Per-app exclusions are easy to forget when a new Secret is introduced
  (see PRs #24, #27).
- Rotation is fully manual and undocumented per-app.

This design adopts **SOPS + age** with the **ksops** Config Management
Plugin running as a sidecar on `argocd-repo-server`. Encrypted Secrets live
next to their kustomization; the master age key lives on the maintainer's
workstation (mirrored to a TrueNAS encrypted dataset) and is seeded into
the cluster as one bootstrap Secret.

## Goals

1. Every Secret currently created imperatively becomes a GitOps artifact
   under ArgoCD.
2. Each Application's kustomization stops excluding its Secret(s).
3. Survive a full cluster rebuild from git + one restored age key.
4. Add **zero** in-cluster operators beyond a sidecar on
   `argocd-repo-server`.
5. Document rotation, encrypt/decrypt, and bootstrap recipes.

## Non-goals

- Migrating every App in the first PR. The first PR is **infra + one
  canary App (`filebrowser`)**; remaining Apps each get a follow-up PR
  filed as gh issues against `SpyrosPsarras/epaflix`.
- Per-app age recipients. One cluster-wide recipient is enough.
- Removing the plaintext credential file. As of this design it stays as the
  source-of-truth that the developer encrypts FROM, plus a workstation
  backup, and decommissioning it is out of scope. **Since done:** the
  plaintext file is gone and the credential store is the committed
  `.github/instructions/secrets.enc.yaml`.
- Securing ArgoCD bootstrap Secrets (admin password, OIDC client). The
  age key Secret IS the chicken-egg; remaining ArgoCD bootstrap Secrets
  continue to be created imperatively for now.
- CNPG webhook TLS — operator-managed, not user-managed, untouched.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ workstation (laptop)                                        │
│   ~/.config/sops/age/keys.txt   ← age private key (master) │
│   `sops -e -i path/secret.enc.yaml`                         │
└──────────┬──────────────────────────────────────────────────┘
           │ git push (only ENCRYPTED yaml in repo)
           ▼
┌─────────────────────────────────────────────────────────────┐
│ GitHub: SpyrosPsarras/k3s-swarm-proxmox                     │
│   2-k3s/<app>/<app>-secret.enc.yaml   ← ciphertext          │
│   .sops.yaml                          ← encryption rules    │
└──────────┬──────────────────────────────────────────────────┘
           │ ArgoCD repo-server clone
           ▼
┌─────────────────────────────────────────────────────────────┐
│ argocd-repo-server pod                                      │
│   sidecar: ksops CMP                                        │
│     mounts Secret `argocd/sops-age` → SOPS_AGE_KEY_FILE     │
│     kustomize build --enable-alpha-plugins                  │
│     decrypts at render-time → emits plaintext Secret        │
└──────────┬──────────────────────────────────────────────────┘
           │ rendered manifests → app controller → cluster
           ▼
┌─────────────────────────────────────────────────────────────┐
│ k8s API: native Secret  (decrypted, runtime)                │
└─────────────────────────────────────────────────────────────┘

OUT-OF-BAND (one-shot, manual):
   workstation → kubectl create secret generic sops-age \
                   -n argocd --from-file=keys.txt=~/.config/sops/age/keys.txt
```

Trust boundary: ciphertext in git; plaintext only on the workstation
(`~/.config/sops/age/`), the TrueNAS encrypted backup, and at runtime
inside `argocd-repo-server` + the k8s Secret API.

## Components

### Repo additions

```
k3s-swarm-proxmox/
├── .sops.yaml                          # creation rule: *.enc.yaml → age recipient
├── 2-k3s/
│   ├── 11.argocd/
│   │   ├── values.yaml                 # repo-server.extraContainers (ksops sidecar)
│   │   │                               # repo-server.volumes / volumeMounts for sops-age
│   │   ├── kustomization.yaml          # excludes Secret sops-age (imperative)
│   │   └── README.md                   # bootstrap: kubectl create secret sops-age ...
│   └── 09.filebrowser/                 # CANARY
│       ├── kustomization.yaml          # adds ksops generator; drops Secret exclusion
│       ├── ksops-generator.yaml        # generator: [filebrowser-oidc.enc.yaml]
│       └── filebrowser-oidc.enc.yaml   # ciphertext (committed)
└── .github/instructions/
    ├── secrets.enc.yaml                # Credential store (SOPS+age, committed)
    └── sops.instructions.md            # NEW: encrypt/rotate/decrypt recipes
```

### ksops

- Image: `viaductoss/ksops:vX.Y.Z` (latest stable at implementation time;
  pin via Renovate / image-updater).
- Installed via the canonical pattern: an **initContainer** on
  `argocd-repo-server` copies the `ksops` and `kustomize` binaries into
  an emptyDir shared with the main repo-server container at
  `/custom-tools/`.
- The main repo-server container mounts the age key Secret at
  `/var/sops/keys.txt` and exports `SOPS_AGE_KEY_FILE` so that `ksops`,
  invoked by `kustomize build`, can decrypt at render time.
- Requires `kustomize.buildOptions: "--enable-alpha-plugins --enable-exec"`
  in the `argocd-cm` ConfigMap (set via Helm values
  `configs.cm.kustomize.buildOptions`).

### `.sops.yaml`

```yaml
creation_rules:
  - path_regex: '\.enc\.yaml$'
    encrypted_regex: '^(data|stringData)$'
    age: age1XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
```

`encrypted_regex` keeps `apiVersion`/`kind`/`metadata` cleartext so diffs
remain meaningful in PR review.

### Age key Secret

- Namespace: `argocd`
- Name: `sops-age`
- Key: `keys.txt`
- Created imperatively, **once**, by the maintainer:

```bash
kubectl --context epaflix create secret generic sops-age \
  -n argocd \
  --from-file=keys.txt=$HOME/.config/sops/age/keys.txt
```

- Listed under `# excluded by design` in `2-k3s/11.argocd/kustomization.yaml`
  with a comment explaining the chicken-egg.

### `ksops-generator.yaml` (per-app)

```yaml
apiVersion: viaduct.ai/v1
kind: ksops
metadata:
  name: filebrowser-oidc
files:
  - filebrowser-oidc.enc.yaml
```

Referenced from `kustomization.yaml` under `generators:` (NOT `resources:`).

## Data flow

### Encrypt (developer workflow)

```
1. Edit plaintext locally:    nano filebrowser-oidc.yaml
2. Encrypt in place:           sops -e -i filebrowser-oidc.yaml
3. Rename:                     mv filebrowser-oidc.yaml filebrowser-oidc.enc.yaml
4. git add filebrowser-oidc.enc.yaml ; git commit ; git push
```

Safeguards:

- `.gitignore` adds `*-plaintext.yaml` and explicitly NOT `*.enc.yaml`.
- Pre-commit hook implemented as plain shell at
  `.github/hooks/check-sops-encrypted.sh`, symlinked into
  `.git/hooks/pre-commit` by a one-line `install-hooks.sh`. Rejects any
  staged `*.yaml` matching `kind: Secret` that lacks an encrypted
  `sops:` block.

### Decrypt (ArgoCD sync)

```
ArgoCD repo-server clones repo
  → kustomize build --enable-alpha-plugins
    → ksops generator runs (sidecar via shared binary)
      → sops decrypt with SOPS_AGE_KEY_FILE=/var/sops/keys.txt
        → emits plaintext Secret manifest to stdout
          → ArgoCD applies to cluster → native Secret created
```

### Rotate (age key)

```
1. age-keygen -o new-keys.txt           # workstation
2. Add new public key to .sops.yaml as additional recipient (do not remove old yet)
3. sops updatekeys path/**/*.enc.yaml   # re-wraps DEK to both recipients
4. git commit re-wrapped files ; push ; ArgoCD sync (still decryptable with old)
5. kubectl --context epaflix create secret generic sops-age \
     -n argocd \
     --from-file=keys.txt=new-keys.txt \
     --dry-run=client -o yaml | kubectl --context epaflix apply -f -
6. kubectl --context epaflix rollout restart deploy/argocd-repo-server -n argocd
7. Drop old recipient from .sops.yaml ; sops updatekeys again ; commit
8. Securely destroy old keys.txt on TrueNAS backup
```

## Error handling

| Failure | Symptom | Response |
|---------|---------|----------|
| age Secret missing in argocd ns | repo-server CrashLoop or `sops: cannot find age private key` in render | Run bootstrap `kubectl --context epaflix create secret`; documented in 11.argocd/README |
| Wrong age key (rotation mismatch) | `kustomize build` errors: `no key could decrypt the data` | Re-run `sops updatekeys`, redeploy Secret |
| Plaintext leaked to git | pre-commit grep catches placeholder pattern `<[A-Z_]+>` or missing `sops:` block | Pre-commit hook rejects commit; rotate any leaked credential |
| ksops sidecar image unavailable | repo-server pod NotReady | image-updater pin to known-good digest; Renovate alerts on new tag |
| `*.enc.yaml` applied as raw Secret | placeholder ciphertext applied as actual Secret | Generators emit via `generators:` block; encrypted files NEVER appear in `resources:` |

## Testing strategy

### Pre-merge

```
T1. sops round-trip
    sops -d 2-k3s/09.filebrowser/filebrowser-oidc.enc.yaml | head -20
    → emits valid Secret YAML with real OIDC client_secret

T2. kustomize render with ksops locally
    cd 2-k3s/09.filebrowser
    KSOPS_BIN=$(which ksops) kustomize build --enable-alpha-plugins --enable-exec .
    → output contains decrypted Secret filebrowser-oidc

T3. ArgoCD diff pre-sync
    argocd app diff filebrowser
    → no destructive diff; Secret filebrowser-oidc identical to current live

T4. Repo-server pod healthy after Helm/values bump
    kubectl --context epaflix -n argocd get pods -l app.kubernetes.io/component=repo-server
    → 1/1 Ready; describe shows initContainer "install-ksops" Completed

T5. Pre-commit safety
    Stage plaintext filebrowser-oidc.yaml ; git commit
    → rejected by hook (no SOPS metadata header)
```

### Post-merge

```
T6. ArgoCD sync filebrowser App (manual first)
    → Synced + Healthy

T7. Secret unchanged in-cluster
    kubectl --context epaflix -n filebrowser get secret filebrowser-oidc -o yaml | yq '.data'
    → matches pre-migration values

T8. Filebrowser pod still serves OIDC login
    curl -sI https://filebrowser.epaflix.com/ → 302 → /api/auth/oidc
    Browser login round-trip via Authentik → success

T9. Drop secret exclusion + re-sync
    Remove `# excluded by design` from 09.filebrowser/kustomization.yaml
    → ArgoCD reports OutOfSync? No — generator-emitted Secret IS the live one

T10. Soak 48h with selfHeal ON
    No drift, no recreated Secret, no pod restarts
```

### Rollback

```
R1. Worst case: ksops broken → revert PR; ArgoCD reverts repo-server to
    pre-sidecar state; Secret stays in cluster (already created, not
    pruned); filebrowser unaffected.

R2. If Secret somehow deleted: re-create imperatively from the credential
    store .github/instructions/secrets.enc.yaml (the old recipe stays in
    README backup until canary soaked).
```

## Dependencies / sequencing

1. Issue #29 design approved (this doc).
2. PR — `11.argocd/values.yaml`: repo-server ksops sidecar +
   `sops-age` volume mount; kustomization exclusion comment;
   `.sops.yaml` at repo root; `.github/instructions/sops.instructions.md`;
   pre-commit hook; canary on `09.filebrowser` (`filebrowser-oidc.enc.yaml`
   committed; exclusion dropped from kustomization). Manual sync first.
3. 48 h soak with selfHeal ON.
4. Follow-up gh issues filed per remaining App:
   `authentik`, `traefik`, `servarr`, `observability`, `postgres`,
   `cert-manager`. Each gets its own PR.
5. Decommission the plaintext credential file as last step (separate issue).
   **Done:** it is gone, and the credential store is the committed
   `.github/instructions/secrets.enc.yaml`.

## Coordination notes

- Coordinates with #14 (postgres), #15 (cert-manager), #19 (self-managed
  ArgoCD). Those Apps continue using placeholder-Secret exclusion until
  their follow-up migration PR.
- Canary App is `filebrowser` because it has the smallest Secret surface
  (one `filebrowser-oidc` Secret) and a trivial OIDC verification path.
- ksops sidecar will reuse Renovate / image-updater conventions
  (`feedback_image_pin_safety.md`).
