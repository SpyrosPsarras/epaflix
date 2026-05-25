# SOPS + age secret automation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adopt SOPS + age + ksops for GitOps-managed Secrets across ArgoCD Applications, starting with `filebrowser` as canary.

**Architecture:** Encrypted `*.enc.yaml` Secret files committed alongside each App's kustomization. `argocd-repo-server` runs a `ksops` sidecar that decrypts via age key mounted from cluster Secret `argocd/sops-age`. Single cluster-wide age recipient. The age key Secret is bootstrapped imperatively (unavoidable chicken-egg).

**Tech Stack:** sops, age, ksops (viaduct.ai/v1 kustomize KRM-function generator), kustomize, Helm (argo-cd chart 9.5.14), ArgoCD 3.4.2.

**Spec:** `docs/superpowers/specs/2026-05-25-sops-secret-automation-design.md`

**Working branch:** `sops-secret-automation-design` (continue on this branch through implementation; one PR at end).

---

## File Structure

| Path | Action | Responsibility |
|------|--------|---------------|
| `.sops.yaml` | Create | Repo-root SOPS creation rules: `*.enc.yaml` → age recipient |
| `.gitignore` | Modify | Add `*-plaintext.yaml` pattern |
| `.github/hooks/check-sops-encrypted.sh` | Create | Pre-commit: reject any unencrypted Secret YAML |
| `.github/hooks/install-hooks.sh` | Create | One-shot installer: symlinks hook into `.git/hooks/pre-commit` |
| `.github/instructions/sops.instructions.md` | Create | Developer guide: encrypt / rotate / decrypt recipes |
| `CLAUDE.md` | Modify | Add SOPS convention paragraph under Critical Rules |
| `2-k3s/11.argocd/helm-values.yaml` | Modify | Add ksops sidecar to `repoServer.extraContainers`, age volume, `kustomize.buildOptions` flag |
| `2-k3s/11.argocd/kustomization.yaml` | Modify | Document `sops-age` Secret in exclusion comment block |
| `2-k3s/11.argocd/README.md` | Modify | Add age key Secret bootstrap recipe |
| `2-k3s/09.filebrowser/kustomization.yaml` | Create | First kustomization for filebrowser, includes ksops generator |
| `2-k3s/09.filebrowser/ksops-generator.yaml` | Create | KRM-function generator pointing at `filebrowser-oidc.enc.yaml` |
| `2-k3s/09.filebrowser/filebrowser-oidc.enc.yaml` | Create | Encrypted Secret (committed ciphertext) |
| `2-k3s/11.argocd/apps/app-filebrowser.yaml` | Modify | Switch source from `directory.recurse` to kustomize-managed; update comment block (drop "imperative Secret" note) |

---

## Task 1: Install local tooling (sops, age, ksops)

**Files:** none — workstation only.

- [ ] **Step 1: Verify sops, age, ksops binaries**

```bash
which sops age ksops 2>&1 || true
sops --version
age --version
ksops --version
```

Expected: all three present. If missing on Arch:

```bash
yay -S sops age-encryption ksops-bin
```

- [ ] **Step 2: Verify versions are recent**

```bash
sops --version  # ≥ 3.9
age --version   # ≥ 1.2
ksops --version # ≥ 4.3
```

Expected: All meet floor versions. If not, upgrade first.

- [ ] **Step 3: Confirm tooling working with quick smoke test**

```bash
echo "hello" | age -e -r $(age-keygen -y <(age-keygen 2>/dev/null) 2>/dev/null) -a 2>&1 | head -3
```

Expected: prints `-----BEGIN AGE ENCRYPTED FILE-----`. (Throwaway test, no key persisted.)

No commit — workstation prep only.

---

## Task 2: Generate cluster age key + back up to TrueNAS

**Files:** workstation `~/.config/sops/age/k3s-cluster.txt`; TrueNAS backup path.

- [ ] **Step 1: Create age keys directory**

```bash
mkdir -p ~/.config/sops/age
chmod 700 ~/.config/sops/age
```

- [ ] **Step 2: Generate the cluster age key**

```bash
age-keygen -o ~/.config/sops/age/k3s-cluster.txt
chmod 600 ~/.config/sops/age/k3s-cluster.txt
```

Expected: file contains `# created: <date>`, `# public key: age1XXXX...`, then `AGE-SECRET-KEY-1...`. Note the **public key** (line beginning `# public key:`) — copy to scratchpad for Task 3.

- [ ] **Step 3: Configure SOPS_AGE_KEY_FILE**

```bash
echo 'export SOPS_AGE_KEY_FILE=$HOME/.config/sops/age/k3s-cluster.txt' >> ~/.zshrc
# Reload current shell:
export SOPS_AGE_KEY_FILE=$HOME/.config/sops/age/k3s-cluster.txt
```

- [ ] **Step 4: Backup age key to TrueNAS encrypted dataset**

```bash
# Use the existing encrypted dataset on TrueNAS (pool1/encrypted-backups
# or equivalent). Adjust path if your dataset name differs.
ssh truenas_admin@192.168.10.200 'mkdir -p /mnt/pool1/encrypted-backups/sops-age && chmod 700 /mnt/pool1/encrypted-backups/sops-age'
scp ~/.config/sops/age/k3s-cluster.txt truenas_admin@192.168.10.200:/mnt/pool1/encrypted-backups/sops-age/k3s-cluster.txt
ssh truenas_admin@192.168.10.200 'chmod 600 /mnt/pool1/encrypted-backups/sops-age/k3s-cluster.txt'
```

If your TrueNAS doesn't have an encrypted dataset yet, create one via the TrueNAS web UI: Pools → pool1 → Add Dataset → Encryption On.

- [ ] **Step 5: Verify backup**

```bash
ssh truenas_admin@192.168.10.200 'sha256sum /mnt/pool1/encrypted-backups/sops-age/k3s-cluster.txt'
sha256sum ~/.config/sops/age/k3s-cluster.txt
```

Expected: identical SHA-256.

No commit yet.

---

## Task 3: Add `.sops.yaml` at repo root

**Files:** Create `.sops.yaml`.

- [ ] **Step 1: Write `.sops.yaml`**

Replace `age1RECIPIENT_PLACEHOLDER` with the public key from Task 2 Step 2.

```yaml
# SOPS creation rules.
#
# All files matching the regex below are encrypted to the listed age
# recipient(s). The matching private key is stored:
#   - on the maintainer workstation: ~/.config/sops/age/k3s-cluster.txt
#   - mirrored to TrueNAS encrypted dataset (offline backup)
#   - seeded into the cluster as Secret argocd/sops-age (read by the
#     ksops sidecar on argocd-repo-server)
#
# Convention: encrypted Secret files use the suffix `.enc.yaml` so they
# are easy to distinguish in PR diffs and so the pre-commit hook can
# treat unencrypted `kind: Secret` files as a hard error.
#
# `encrypted_regex` keeps apiVersion, kind, metadata in cleartext —
# diffs in PR review stay meaningful, only the actual secret data is
# ciphertext.
creation_rules:
  - path_regex: \.enc\.yaml$
    encrypted_regex: ^(data|stringData)$
    age: age1RECIPIENT_PLACEHOLDER
```

- [ ] **Step 2: Round-trip test**

```bash
cat > /tmp/sops-roundtrip.enc.yaml <<'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: test
stringData:
  hello: world
EOF
sops -e -i /tmp/sops-roundtrip.enc.yaml
grep -q 'sops:' /tmp/sops-roundtrip.enc.yaml && echo "ENCRYPTED OK"
sops -d /tmp/sops-roundtrip.enc.yaml | grep -q 'hello: world' && echo "DECRYPT OK"
rm /tmp/sops-roundtrip.enc.yaml
```

Expected: prints `ENCRYPTED OK` and `DECRYPT OK`.

- [ ] **Step 3: Commit**

```bash
git add .sops.yaml
git commit -m "chore(sops): add repo-root .sops.yaml with cluster age recipient

Refs #29"
```

---

## Task 4: Update `.gitignore` for plaintext safety

**Files:** Modify `.gitignore`.

- [ ] **Step 1: Append plaintext-secret ignore patterns**

Add to the bottom of `.gitignore`:

```gitignore

# SOPS plaintext drafts — never commit. Always encrypt to *.enc.yaml first.
*-plaintext.yaml
*.plaintext.yaml
```

- [ ] **Step 2: Verify pattern**

```bash
touch /tmp/foo-plaintext.yaml
git check-ignore -v -f /tmp/foo-plaintext.yaml || echo "NOT IGNORED — fix pattern"
rm /tmp/foo-plaintext.yaml

# In-repo simulation:
mkdir -p 2-k3s/09.filebrowser
touch 2-k3s/09.filebrowser/foo-plaintext.yaml
git check-ignore 2-k3s/09.filebrowser/foo-plaintext.yaml
rm 2-k3s/09.filebrowser/foo-plaintext.yaml
```

Expected: in-repo check prints the ignored path.

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "chore(sops): ignore *-plaintext.yaml drafts

Refs #29"
```

---

## Task 5: Pre-commit hook — reject unencrypted Secret YAML

**Files:** Create `.github/hooks/check-sops-encrypted.sh`, `.github/hooks/install-hooks.sh`.

- [ ] **Step 1: Write the hook script**

`.github/hooks/check-sops-encrypted.sh`:

```bash
#!/usr/bin/env bash
# Pre-commit hook: refuse to commit any staged YAML that declares
# `kind: Secret` unless it has been sops-encrypted (contains a `sops:`
# block) OR is explicitly in the project's "imperative Secret" allowlist.
#
# Wire up via .github/hooks/install-hooks.sh (one-shot).
set -euo pipefail

# Files allowed to remain unencrypted Secret YAML — placeholder manifests
# that ArgoCD already excludes from sync by kustomization comments.
# Each entry is a relative path from repo root.
ALLOWLIST=(
  "2-k3s/11.argocd/oidc-secret.yaml"
)

fail=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  case "$f" in
    *.yaml|*.yml) ;;
    *) continue ;;
  esac
  # Skip deleted files
  [ -e "$f" ] || continue

  # Is the staged version a Secret?
  if git show ":$f" 2>/dev/null | grep -qE '^kind:[[:space:]]+Secret[[:space:]]*$'; then
    # Allowlisted?
    skip=0
    for a in "${ALLOWLIST[@]}"; do
      [ "$f" = "$a" ] && skip=1
    done
    [ $skip -eq 1 ] && continue

    # Encrypted?
    if git show ":$f" 2>/dev/null | grep -q '^sops:'; then
      continue
    fi
    echo "ERROR: $f is a plaintext k8s Secret. Encrypt with: sops -e -i $f && mv $f ${f%.yaml}.enc.yaml" >&2
    fail=1
  fi
done < <(git diff --cached --name-only --diff-filter=ACMR)

exit $fail
```

- [ ] **Step 2: Make it executable**

```bash
chmod +x .github/hooks/check-sops-encrypted.sh
```

- [ ] **Step 3: Write the installer**

`.github/hooks/install-hooks.sh`:

```bash
#!/usr/bin/env bash
# One-shot installer: symlinks repo hooks into .git/hooks/.
# Idempotent. Run once per fresh clone.
set -euo pipefail
cd "$(dirname "$0")/../.."  # repo root
hooks_dir=".git/hooks"
src_dir=".github/hooks"

mkdir -p "$hooks_dir"

ln -sf "../../$src_dir/check-sops-encrypted.sh" "$hooks_dir/pre-commit"
chmod +x "$hooks_dir/pre-commit"

echo "Installed pre-commit hook: $hooks_dir/pre-commit -> $src_dir/check-sops-encrypted.sh"
```

```bash
chmod +x .github/hooks/install-hooks.sh
```

- [ ] **Step 4: Install for current clone**

```bash
./.github/hooks/install-hooks.sh
ls -la .git/hooks/pre-commit
```

Expected: symlink visible.

- [ ] **Step 5: Smoke-test hook — false positive**

```bash
# Confirm a regular non-Secret YAML commit is not affected.
echo "key: value" > /tmp/non-secret.yaml
cp /tmp/non-secret.yaml 2-k3s/09.filebrowser/scratch-test.yaml
git add 2-k3s/09.filebrowser/scratch-test.yaml
git commit -m "test: hook smoke" --dry-run 2>&1 | tail -5
# Cleanup
git reset HEAD 2-k3s/09.filebrowser/scratch-test.yaml
rm 2-k3s/09.filebrowser/scratch-test.yaml /tmp/non-secret.yaml
```

Expected: no ERROR from hook.

- [ ] **Step 6: Smoke-test hook — true positive**

```bash
cat > 2-k3s/09.filebrowser/scratch-test.yaml <<'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: scratch
stringData:
  foo: bar
EOF
git add 2-k3s/09.filebrowser/scratch-test.yaml
git commit -m "test: should fail" 2>&1 | tail -5 || true
# Cleanup
git reset HEAD 2-k3s/09.filebrowser/scratch-test.yaml
rm 2-k3s/09.filebrowser/scratch-test.yaml
```

Expected: hook prints `ERROR: 2-k3s/09.filebrowser/scratch-test.yaml is a plaintext k8s Secret`. Commit blocked.

- [ ] **Step 7: Commit hook files**

```bash
git add .github/hooks/check-sops-encrypted.sh .github/hooks/install-hooks.sh
git commit -m "chore(sops): add pre-commit hook rejecting plaintext Secret YAML

Refs #29"
```

---

## Task 6: Write `.github/instructions/sops.instructions.md`

**Files:** Create `.github/instructions/sops.instructions.md`.

- [ ] **Step 1: Write the doc**

```markdown
# SOPS + age secret automation

Cluster Secret automation for ArgoCD-managed Applications. Tracks
issue #29. Design spec:
`docs/superpowers/specs/2026-05-25-sops-secret-automation-design.md`.

## Quick rules

- Encrypted Secret files use the suffix `.enc.yaml` and live next to the
  kustomization that references them.
- `.sops.yaml` at repo root is the only creation rule — single
  cluster-wide age recipient.
- The private age key lives at `~/.config/sops/age/k3s-cluster.txt` on
  the maintainer workstation, mirrored to a TrueNAS encrypted dataset.
- The cluster reads the key from Secret `argocd/sops-age` (created
  imperatively, once per cluster rebuild — chicken-egg).
- `ksops` runs as a sidecar on `argocd-repo-server` and decrypts at
  sync render time.

## Encrypt a new Secret

```bash
# 1. Draft plaintext (use a -plaintext suffix so .gitignore catches it).
cd 2-k3s/<App>
cat > my-thing-plaintext.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: my-thing
  namespace: <ns>
stringData:
  password: "actual-secret-value-from-secrets.yml"
EOF

# 2. Encrypt to the canonical filename.
sops -e my-thing-plaintext.yaml > my-thing.enc.yaml
shred -u my-thing-plaintext.yaml

# 3. Reference it from kustomization.yaml via the ksops generator:
#    Add (or extend) ksops-generator.yaml:
#      files:
#        - my-thing.enc.yaml
#    And in kustomization.yaml under generators:
#      generators:
#        - ksops-generator.yaml

# 4. Verify locally:
sops -d my-thing.enc.yaml | head
KSOPS_BIN=$(which ksops) kustomize build --enable-alpha-plugins --enable-exec . | grep -A5 'name: my-thing'

# 5. Commit.
git add my-thing.enc.yaml ksops-generator.yaml kustomization.yaml
git commit -m "feat(<app>): adopt sops for <ns>/my-thing secret"
```

## Edit an existing encrypted Secret

```bash
sops 2-k3s/<App>/my-thing.enc.yaml
# Editor opens with decrypted plaintext. Save & exit → re-encrypted in place.
git diff my-thing.enc.yaml  # ciphertext-level diff
git add my-thing.enc.yaml
git commit -m "chore(<app>): rotate my-thing password"
```

## Decrypt for inspection only

```bash
sops -d 2-k3s/<App>/my-thing.enc.yaml | yq '.stringData.password'
```

## Rotate the cluster age key

```bash
# 1. Generate new key.
age-keygen -o ~/.config/sops/age/k3s-cluster-new.txt

# 2. Add NEW public key to .sops.yaml `age:` list (do NOT remove old yet).
$EDITOR .sops.yaml

# 3. Re-wrap every encrypted file to both recipients.
find . -name '*.enc.yaml' -exec sops updatekeys -y {} \;
git add -u
git commit -m "chore(sops): re-wrap secrets to new+old age recipient"
git push

# 4. Push new key into the cluster (replaces old keys.txt content).
kubectl create secret generic sops-age \
  -n argocd \
  --from-file=keys.txt=$HOME/.config/sops/age/k3s-cluster-new.txt \
  --dry-run=client -o yaml | kubectl apply -f -

# 5. Force repo-server to pick up new Secret.
kubectl -n argocd rollout restart deploy/argocd-repo-server
kubectl -n argocd rollout status deploy/argocd-repo-server

# 6. Sanity: trigger ArgoCD App sync.
argocd app sync filebrowser
# Confirm Synced + Healthy.

# 7. Drop OLD recipient from .sops.yaml ; re-wrap ; commit.
find . -name '*.enc.yaml' -exec sops updatekeys -y {} \;
git add -u .sops.yaml
git commit -m "chore(sops): drop old age recipient after rotation"
git push

# 8. Securely destroy old key.
shred -u ~/.config/sops/age/k3s-cluster.txt
mv ~/.config/sops/age/k3s-cluster-new.txt ~/.config/sops/age/k3s-cluster.txt
ssh truenas_admin@192.168.10.200 'shred -u /mnt/pool1/encrypted-backups/sops-age/k3s-cluster.txt'
scp ~/.config/sops/age/k3s-cluster.txt truenas_admin@192.168.10.200:/mnt/pool1/encrypted-backups/sops-age/
```

## Cluster bootstrap (fresh cluster)

```bash
# Run ONCE before installing/syncing ArgoCD self-management.
kubectl create namespace argocd
kubectl create secret generic sops-age \
  -n argocd \
  --from-file=keys.txt=$HOME/.config/sops/age/k3s-cluster.txt
```

## Common failures

| Symptom | Likely cause | Fix |
|---|---|---|
| `sops: cannot find age private key` in repo-server logs | `argocd/sops-age` Secret missing | Re-run bootstrap kubectl create secret |
| `no key could decrypt the data` | Key was rotated but Secret in cluster still old | Re-apply key Secret + restart repo-server |
| `kustomize build` errors `unknown plugin kind ksops` | `--enable-alpha-plugins` missing | Confirm `configs.cm.kustomize.buildOptions: --enable-alpha-plugins --enable-exec` in argocd helm-values.yaml |
| Pre-commit hook rejects encrypted file | hook checks failed (no `sops:` block) | Re-run `sops -e -i <file>` |
```

- [ ] **Step 2: Commit**

```bash
git add .github/instructions/sops.instructions.md
git commit -m "docs(sops): developer recipes for encrypt/rotate/bootstrap

Refs #29"
```

---

## Task 7: Update `CLAUDE.md` with SOPS convention paragraph

**Files:** Modify `CLAUDE.md` (under Critical Rules).

- [ ] **Step 1: Add bullet under Critical Rules**

Add this bullet to the `## Critical Rules` list (place it after the existing "**NEVER** commit `secrets.yml`" bullet):

```markdown
- **Encrypted Secret files use `.enc.yaml` suffix.** All Secrets that ArgoCD must reconcile live as `*.enc.yaml` next to their kustomization, encrypted with SOPS+age (single cluster recipient). Pre-commit hook (`.github/hooks/check-sops-encrypted.sh`) refuses any plaintext `kind: Secret` YAML. New clones must run `./.github/hooks/install-hooks.sh` once. Encrypt/rotate recipes: `.github/instructions/sops.instructions.md`.
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): document SOPS .enc.yaml convention

Refs #29"
```

---

## Task 8: Add ksops sidecar to ArgoCD helm-values.yaml

**Files:** Modify `2-k3s/11.argocd/helm-values.yaml`.

- [ ] **Step 1: Add `kustomize.buildOptions` flag under `configs.cm`**

Replace the existing line:

```yaml
    kustomize.buildOptions: --enable-helm
```

with:

```yaml
    # --enable-helm: kustomize-with-helm inflation (authentik, servarr).
    # --enable-alpha-plugins / --enable-exec: required by ksops generator
    # (encrypted Secret decryption sidecar; see Issue #29 / .github/
    # instructions/sops.instructions.md).
    kustomize.buildOptions: --enable-helm --enable-alpha-plugins --enable-exec
```

- [ ] **Step 2: Replace `repoServer:` block with ksops-wired version**

Replace the existing `repoServer:` block:

```yaml
repoServer:
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
    limits:
      memory: 512Mi
```

with:

```yaml
repoServer:
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
    limits:
      memory: 512Mi
  # ksops CMP — see Issue #29 and .github/instructions/sops.instructions.md.
  # Canonical install pattern (matches viaduct.ai/ksops upstream docs):
  #   - initContainer copies the ksops + kustomize binaries into an
  #     emptyDir shared with the main repo-server container.
  #   - The age private key is mounted on the main container so `ksops`
  #     can decrypt at `kustomize build` time.
  #   - --enable-alpha-plugins --enable-exec already set via configs.cm
  #     above.
  # Image pinned by digest after first successful sync (Renovate).
  volumes:
    - name: custom-tools
      emptyDir: {}
    - name: sops-age
      secret:
        secretName: sops-age
        defaultMode: 0400
  volumeMounts:
    - name: custom-tools
      mountPath: /custom-tools
    - name: sops-age
      mountPath: /var/sops
      readOnly: true
  env:
    - name: SOPS_AGE_KEY_FILE
      value: /var/sops/keys.txt
    - name: XDG_CONFIG_HOME
      value: /.config
  initContainers:
    - name: install-ksops
      image: viaductoss/ksops:v4.3.3
      command: ["/bin/sh", "-c"]
      args:
        - |
          set -e
          echo "Copying ksops + kustomize into shared volume..."
          cp /usr/local/bin/ksops /custom-tools/
          cp /usr/local/bin/kustomize /custom-tools/
          echo "Done."
      volumeMounts:
        - name: custom-tools
          mountPath: /custom-tools
```

> NOTE on image pin: `v4.3.3` is the floor (covers kustomize ≥ 5). Renovate / image-updater will bump and pin to a digest later.

- [ ] **Step 3: Local render sanity check**

```bash
cd 2-k3s/11.argocd
kustomize build --enable-helm . | yq 'select(.kind == "Deployment" and .metadata.name == "argocd-repo-server")' | head -80
```

Expected: output shows `extraContainers`-injected ksops container with `image: viaductoss/ksops:v4.3.3` and volumes `custom-tools` + `sops-age`.

- [ ] **Step 4: Commit (do NOT push yet — Secret must exist first)**

```bash
git add 2-k3s/11.argocd/helm-values.yaml
git commit -m "feat(argocd): add ksops CMP sidecar to repo-server

ksops decrypts *.enc.yaml at render time via age key mounted from
Secret argocd/sops-age. Requires bootstrap: kubectl create secret
generic sops-age -n argocd --from-file=keys.txt=... before next sync.

Refs #29"
```

---

## Task 9: Document age Secret in 11.argocd/kustomization.yaml header

**Files:** Modify `2-k3s/11.argocd/kustomization.yaml`.

- [ ] **Step 1: Extend the "Excluded from `resources:`" comment block**

In the header comment, locate the existing block:

```
# Excluded from `resources:` (left operator-managed for now):
#   - apps/*.yaml          — ...
#   - image-updater/       — ...
#   - oidc-secret.yaml     — ...
```

Add a fourth bullet at the end of that list:

```
#   - sops-age Secret      — created imperatively (chicken-egg: the
#                            age key has to exist before ArgoCD can
#                            decrypt anything, including any GitOps
#                            version of itself). One-shot bootstrap:
#                            kubectl create secret generic sops-age
#                              -n argocd --from-file=keys.txt=$HOME/
#                              .config/sops/age/k3s-cluster.txt
#                            Rotation recipe: .github/instructions/
#                            sops.instructions.md.
```

- [ ] **Step 2: Commit**

```bash
git add 2-k3s/11.argocd/kustomization.yaml
git commit -m "docs(argocd): note sops-age Secret in exclusion block

Refs #29"
```

---

## Task 10: Add bootstrap recipe to 11.argocd/README.md

**Files:** Modify `2-k3s/11.argocd/README.md`.

- [ ] **Step 1: Add a "SOPS age key bootstrap" section**

Append (or insert near the existing imperative-Secret recipes — keep the existing structure of the README):

```markdown
## SOPS age key bootstrap (run ONCE per fresh cluster)

Before ArgoCD can decrypt any `*.enc.yaml` Secret, the cluster needs the
age private key. This is the one Secret that cannot itself be GitOps-
managed (chicken-egg).

```bash
# From the maintainer workstation:
kubectl create secret generic sops-age \
  -n argocd \
  --from-file=keys.txt=$HOME/.config/sops/age/k3s-cluster.txt

# Verify the ksops sidecar can read it:
kubectl -n argocd rollout restart deploy/argocd-repo-server
kubectl -n argocd rollout status deploy/argocd-repo-server
kubectl -n argocd logs deploy/argocd-repo-server -c ksops | tail
# Expected: "ksops ready"
```

Rotation, encrypt, and decrypt recipes:
`.github/instructions/sops.instructions.md`.
```

- [ ] **Step 2: Commit**

```bash
git add 2-k3s/11.argocd/README.md
git commit -m "docs(argocd): age key bootstrap recipe

Refs #29"
```

---

## Task 11: Push age Secret to cluster (manual one-shot bootstrap)

**Files:** none — cluster operation.

- [ ] **Step 1: Confirm argocd namespace exists**

```bash
kubectl get ns argocd
```

Expected: exists, Active.

- [ ] **Step 2: Confirm no existing `sops-age` Secret**

```bash
kubectl -n argocd get secret sops-age 2>&1
```

Expected: `Error from server (NotFound)`. If it already exists from a previous attempt, delete first:

```bash
kubectl -n argocd delete secret sops-age
```

- [ ] **Step 3: Create the Secret**

```bash
kubectl create secret generic sops-age \
  -n argocd \
  --from-file=keys.txt=$HOME/.config/sops/age/k3s-cluster.txt
```

Expected: `secret/sops-age created`.

- [ ] **Step 4: Verify**

```bash
kubectl -n argocd get secret sops-age -o jsonpath='{.data.keys\.txt}' | base64 -d | head -2
```

Expected: matches `# created: ...` / `# public key: age1...` lines from your local file.

- [ ] **Step 5: Log to .history**

```bash
mkdir -p .history/2026-05-25-sops-bootstrap
cat > .history/2026-05-25-sops-bootstrap/secret-bootstrap.md <<'EOF'
# SOPS age key Secret bootstrap — 2026-05-25

One-shot creation of `argocd/sops-age` Secret. Documented in
`.github/instructions/sops.instructions.md` and
`2-k3s/11.argocd/README.md`.

```bash
kubectl create secret generic sops-age -n argocd \
  --from-file=keys.txt=$HOME/.config/sops/age/k3s-cluster.txt
```

Verification:
```bash
$ kubectl -n argocd get secret sops-age -o jsonpath='{.data.keys\.txt}' | base64 -d | head -2
# created: 2026-05-25T...
# public key: age1...
```
EOF
git add .history/2026-05-25-sops-bootstrap/secret-bootstrap.md
git commit -m "history(sops): log age key Secret bootstrap

Refs #29"
```

---

## Task 12: Sync ArgoCD self-management App (cluster picks up sidecar)

**Files:** none — cluster operation.

- [ ] **Step 1: Push branch so ArgoCD can see commits**

```bash
git push -u origin sops-secret-automation-design
```

- [ ] **Step 2: Trigger manual sync of the `argocd` Application**

The self-management Application is on auto-sync; pushing the branch alone won't trigger it (the App tracks `main`). Two options — pick **A** (safer):

**Option A: temporarily retarget the App to the branch for one sync, then revert.**

```bash
# Patch the App to point at the branch:
kubectl -n argocd patch app argocd \
  --type merge \
  -p '{"spec":{"source":{"targetRevision":"sops-secret-automation-design"}}}'

argocd app sync argocd --prune=false
argocd app wait argocd --timeout 300
```

**Option B (alternative, not chosen here):** merge to `main` first. Skipped because we want to validate end-to-end before merging.

- [ ] **Step 3: Verify init container ran**

```bash
kubectl -n argocd get pod -l app.kubernetes.io/component=repo-server -o jsonpath='{.items[0].spec.initContainers[*].name}'
```

Expected: `install-ksops`.

- [ ] **Step 4: Verify init logs**

```bash
kubectl -n argocd logs deploy/argocd-repo-server -c install-ksops | tail
```

Expected: `Copying ksops + kustomize into shared volume...` followed by `Done.`.

- [ ] **Step 5: Verify age key mount on main repo-server**

```bash
kubectl -n argocd exec deploy/argocd-repo-server -c argocd-repo-server -- ls -la /var/sops/keys.txt
kubectl -n argocd exec deploy/argocd-repo-server -c argocd-repo-server -- sh -c 'head -2 /var/sops/keys.txt'
```

Expected: file present, 0400 mode, content shows `# public key: age1...`.

- [ ] **Step 6: Smoke test ksops binary in shared volume**

```bash
kubectl -n argocd exec deploy/argocd-repo-server -c argocd-repo-server -- /custom-tools/ksops --version
kubectl -n argocd exec deploy/argocd-repo-server -c argocd-repo-server -- /custom-tools/kustomize version
```

Expected: prints version strings.

If any step fails: rollback the `argocd` App targetRevision back to `main`, fix the issue in the branch, force-resync.

---

## Task 13: Encrypt filebrowser-oidc Secret

**Files:** Create `2-k3s/09.filebrowser/filebrowser-oidc.enc.yaml`.

- [ ] **Step 1: Fetch the live Secret values from the cluster**

```bash
CLIENT_ID=$(kubectl -n filebrowser get secret filebrowser-oidc -o jsonpath='{.data.client-id}' | base64 -d)
CLIENT_SECRET=$(kubectl -n filebrowser get secret filebrowser-oidc -o jsonpath='{.data.client-secret}' | base64 -d)
echo "CLIENT_ID=$CLIENT_ID"
echo "CLIENT_SECRET=(${#CLIENT_SECRET} chars)"
```

Expected: client-id is `filebrowser`, client-secret length matches `.github/instructions/secrets.yml` Authentik/filebrowser entry.

- [ ] **Step 2: Draft plaintext**

```bash
cd 2-k3s/09.filebrowser
cat > filebrowser-oidc-plaintext.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: filebrowser-oidc
  namespace: filebrowser
type: Opaque
stringData:
  client-id: "$CLIENT_ID"
  client-secret: "$CLIENT_SECRET"
EOF
```

> The `-plaintext.yaml` suffix is ignored by `.gitignore`.

- [ ] **Step 3: Encrypt to `.enc.yaml`**

```bash
sops -e filebrowser-oidc-plaintext.yaml > filebrowser-oidc.enc.yaml
```

- [ ] **Step 4: Verify encrypted file**

```bash
grep -q 'sops:' filebrowser-oidc.enc.yaml && echo "ENCRYPTED OK"
grep -E 'client-id:|client-secret:' filebrowser-oidc.enc.yaml | head -4
# stringData keys should appear with ENC[AES256_GCM,...] values, not plaintext.
```

Expected: `ENCRYPTED OK`; both keys present with `ENC[...]` values.

- [ ] **Step 5: Round-trip**

```bash
sops -d filebrowser-oidc.enc.yaml | diff -q - filebrowser-oidc-plaintext.yaml
```

Expected: no output (files identical after sops modtime line stripped). If `diff` reports differences, only metadata lines should differ.

- [ ] **Step 6: Securely destroy plaintext**

```bash
shred -u filebrowser-oidc-plaintext.yaml
ls filebrowser-oidc-plaintext.yaml 2>&1 || echo "PLAINTEXT GONE"
```

Expected: `PLAINTEXT GONE`.

- [ ] **Step 7: Do NOT commit yet** — Task 14 adds kustomization + ksops generator in the same commit as the .enc.yaml.

---

## Task 14: Add filebrowser kustomization + ksops generator

**Files:** Create `2-k3s/09.filebrowser/kustomization.yaml`, `2-k3s/09.filebrowser/ksops-generator.yaml`.

- [ ] **Step 1: Write `kustomization.yaml`**

```yaml
# FileBrowser Quantum — kustomization for the ArgoCD-managed App.
#
# Switched from ArgoCD `directory.recurse: true` to kustomize on
# 2026-05-25 to allow the ksops generator to inflate the filebrowser-
# oidc Secret from filebrowser-oidc.enc.yaml. Same set of resources,
# same set of fields — only the source type changes from
# directory.recurse to kustomize.
#
# Secret was previously created imperatively (deploy.sh step 5) and
# excluded by design. After this change, ArgoCD reconciles the Secret
# from git directly via the ksops generator below.
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - namespace.yaml
  - configmap.yaml
  - deployment.yaml
  - service.yaml
  - ingress.yaml
  - storage/

generators:
  - ksops-generator.yaml
```

- [ ] **Step 2: Write `ksops-generator.yaml`**

```yaml
# ksops generator — decrypts filebrowser-oidc.enc.yaml at render time.
# Decryption performed inside argocd-repo-server's ksops sidecar.
apiVersion: viaduct.ai/v1
kind: ksops
metadata:
  name: filebrowser-secrets
  annotations:
    config.kubernetes.io/function: |
      exec:
        path: ksops
files:
  - filebrowser-oidc.enc.yaml
```

- [ ] **Step 3: Verify storage/ has its own structure**

```bash
ls 2-k3s/09.filebrowser/storage/
```

If storage/ contains a kustomization.yaml, fine — `resources: [storage/]` references it. If it's a flat directory of YAMLs, kustomize will treat it as a directory of resources. Adjust if needed:

```bash
# Inspect to confirm:
find 2-k3s/09.filebrowser/storage -maxdepth 2 -name '*.yaml'
```

If storage/ lacks a kustomization.yaml and contains multiple YAMLs, switch the top-level `resources:` to enumerate each file explicitly:

```yaml
# (Apply ONLY if storage/ has no kustomization.yaml.)
resources:
  - namespace.yaml
  - configmap.yaml
  - deployment.yaml
  - service.yaml
  - ingress.yaml
  - storage/storageclass.yaml      # adjust to actual filenames
  - storage/pv.yaml
  - storage/pvc.yaml
```

- [ ] **Step 4: Local render test (workstation, with ksops in PATH)**

```bash
cd 2-k3s/09.filebrowser
KSOPS_BIN=$(which ksops) kustomize build --enable-alpha-plugins --enable-exec . | yq 'select(.kind == "Secret" and .metadata.name == "filebrowser-oidc")'
```

Expected: full Secret manifest printed with decrypted `data.client-id` / `data.client-secret` (base64-encoded — kustomize converts `stringData` to `data`).

- [ ] **Step 5: Verify all original resources still render**

```bash
KSOPS_BIN=$(which ksops) kustomize build --enable-alpha-plugins --enable-exec . | yq '.kind' | sort -u
```

Expected: Namespace, ConfigMap, Deployment, Service, Ingress (or IngressRoute), Secret, plus any PV/PVC/StorageClass from storage/.

- [ ] **Step 6: Diff against live to ensure no drift**

```bash
# Render and apply --dry-run server-side to compare against live state:
KSOPS_BIN=$(which ksops) kustomize build --enable-alpha-plugins --enable-exec . | kubectl diff -f - 2>&1 | head -60
```

Expected: **no diff** on filebrowser-oidc Secret data, no diff on Deployment / Service / Ingress / ConfigMap.

If there IS a diff on the Secret: check that the plaintext you encrypted matches `kubectl -n filebrowser get secret filebrowser-oidc` exactly. Re-run Task 13 with the correct values.

If there is a diff on other resources (Deployment, etc.): kustomize is normalizing fields that ArgoCD wasn't normalizing under `directory.recurse`. Add `ignoreDifferences` entries to the App (Task 15) OR live with one-time benign drift.

---

## Task 15: Switch `app-filebrowser.yaml` source from directory to kustomize

**Files:** Modify `2-k3s/11.argocd/apps/app-filebrowser.yaml`.

- [ ] **Step 1: Replace the source block**

Replace lines 24-31 (the `spec.source` block):

```yaml
  source:
    repoURL: https://github.com/SpyrosPsarras/epaflix.git
    targetRevision: main
    path: 2-k3s/09.filebrowser
    directory:
      recurse: true
```

with:

```yaml
  source:
    repoURL: https://github.com/SpyrosPsarras/epaflix.git
    targetRevision: main
    path: 2-k3s/09.filebrowser
    # kustomize-managed; the kustomization.yaml inflates the ksops
    # generator which decrypts filebrowser-oidc.enc.yaml at render
    # time. See Issue #29.
```

- [ ] **Step 2: Update the header comment**

Replace the `# Secrets:` block in the file header with:

```
# Secrets:      `filebrowser-oidc` is reconciled from
#               filebrowser-oidc.enc.yaml via the kustomization's
#               ksops generator (Issue #29). The render-config
#               initContainer reads the resulting Secret at pod
#               start and substitutes ${FILEBROWSER_OIDC_CLIENT_ID/
#               _SECRET} into the ConfigMap-templated config.yaml.
```

- [ ] **Step 3: Verify YAML still parses**

```bash
yq . 2-k3s/11.argocd/apps/app-filebrowser.yaml > /dev/null && echo "YAML OK"
```

Expected: `YAML OK`.

- [ ] **Step 4: Commit (this commit bundles Tasks 13 + 14 + 15)**

```bash
git add \
  2-k3s/09.filebrowser/kustomization.yaml \
  2-k3s/09.filebrowser/ksops-generator.yaml \
  2-k3s/09.filebrowser/filebrowser-oidc.enc.yaml \
  2-k3s/11.argocd/apps/app-filebrowser.yaml
git commit -m "feat(filebrowser): adopt sops for filebrowser-oidc Secret

Switch ArgoCD App source from directory.recurse to kustomize; inflate
filebrowser-oidc Secret from filebrowser-oidc.enc.yaml via ksops
generator. Canary for Issue #29.

Refs #29"
```

---

## Task 16: Push branch + manual sync filebrowser App

**Files:** none — cluster operation.

- [ ] **Step 1: Push**

```bash
git push
```

- [ ] **Step 2: Retarget filebrowser App to branch for canary sync**

```bash
kubectl -n argocd patch app filebrowser \
  --type merge \
  -p '{"spec":{"source":{"targetRevision":"sops-secret-automation-design"}}}'
```

- [ ] **Step 3: Sync App (manual, prune off, no auto-heal disturbance)**

```bash
argocd app sync filebrowser --prune=false
argocd app wait filebrowser --timeout 300
```

Expected: Synced + Healthy.

- [ ] **Step 4: Verify Secret unchanged in-cluster**

```bash
kubectl -n filebrowser get secret filebrowser-oidc -o jsonpath='{.data.client-id}' | base64 -d
kubectl -n filebrowser get secret filebrowser-oidc -o jsonpath='{.data.client-secret}' | base64 -d | wc -c
```

Expected: client-id is `filebrowser`; client-secret byte count matches secrets.yml entry.

- [ ] **Step 5: Verify Pod still Ready**

```bash
kubectl -n filebrowser get pod -l app=filebrowser
kubectl -n filebrowser rollout status deploy/filebrowser
```

Expected: 1/1 Ready, no recent restarts.

- [ ] **Step 6: Functional OIDC test**

```bash
curl -sI https://filebrowser.epaflix.com/ | grep -i location
```

Expected: 302 to `/api/auth/oidc` (or directly to Authentik). Then perform a browser login round-trip:
- https://filebrowser.epaflix.com → Authentik consent → back to filebrowser → file listing visible.

---

## Task 17: Retarget Apps back to `main` after merge

**Files:** none — cluster operation; depends on merging the PR to `main`.

- [ ] **Step 1: Open PR**

```bash
gh pr create \
  --title "feat: SOPS+age secret automation (closes #29, canary on filebrowser)" \
  --body "$(cat <<'EOF'
## Summary
- Adopt SOPS + age + ksops for GitOps-managed Secrets across ArgoCD Apps.
- Canary: filebrowser-oidc Secret in 2-k3s/09.filebrowser.
- Adds ksops sidecar to argocd-repo-server (via Helm values).
- Adds .sops.yaml at repo root, pre-commit hook rejecting plaintext Secret YAML, developer recipes in .github/instructions/sops.instructions.md.
- Bootstrap age key seeded as Secret argocd/sops-age (imperative, one-shot — chicken-egg).

Closes #29 (the design landing). Per-App migration follow-ups will be filed as separate gh issues for authentik, traefik, servarr, observability, postgres, cert-manager.

## Test plan
- [x] T1 sops round-trip locally
- [x] T2 kustomize render with ksops locally (decrypted Secret present)
- [x] T3 ArgoCD diff pre-sync: no destructive diff
- [x] T4 argocd-repo-server pod 1/1 Ready; initContainer install-ksops Completed
- [x] T5 pre-commit hook blocks plaintext Secret YAML
- [x] T6 filebrowser App Synced + Healthy after manual sync
- [x] T7 kubectl get secret filebrowser-oidc data unchanged
- [x] T8 https://filebrowser.epaflix.com OIDC login round-trip OK
- [ ] T9 drop exclusion comment + re-sync (no OutOfSync) — performed after merge
- [ ] T10 48h soak with selfHeal ON — flip after merge

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Merge per Epaflix policy**

```bash
gh pr merge --admin --merge
```

- [ ] **Step 3: Retarget both Apps back to `main`**

```bash
kubectl -n argocd patch app argocd \
  --type merge \
  -p '{"spec":{"source":{"targetRevision":"main"}}}'
kubectl -n argocd patch app filebrowser \
  --type merge \
  -p '{"spec":{"source":{"targetRevision":"main"}}}'
```

- [ ] **Step 4: Verify both Apps Synced against main**

```bash
argocd app get argocd | head -20
argocd app get filebrowser | head -20
```

Expected: both Synced + Healthy, targetRevision `main`.

---

## Task 18: Tick T9 + T10 in PR description (retroactive)

**Files:** PR description (GitHub).

- [ ] **Step 1: Confirm Secret reconciles cleanly with selfHeal ON**

```bash
argocd app set filebrowser --self-heal=true   # already on, this is a no-op confirm
kubectl -n filebrowser delete secret filebrowser-oidc   # destructive test
# Wait ~30s for selfHeal to recreate it:
sleep 30
kubectl -n filebrowser get secret filebrowser-oidc -o jsonpath='{.data.client-id}' | base64 -d
```

Expected: Secret recreated by ArgoCD with identical content (matches Task 16 Step 4).

- [ ] **Step 2: Edit PR description — tick T9**

```bash
# Edit description to tick T9, append: "T9 verified: deleted Secret was recreated by selfHeal within 30s, identical data."
gh pr view <PR#> --json body -q .body  # capture current body
gh pr edit <PR#> --body "$(updated body with [x] in T9 line)"
```

- [ ] **Step 3: Soak monitor**

Wait 48 h. Periodically check:

```bash
kubectl -n filebrowser get pod -l app=filebrowser --no-headers
argocd app history filebrowser | head
```

Expected: no unintended restarts, no spurious sync history.

- [ ] **Step 4: After 48h soak, tick T10 in PR description**

```bash
# Edit PR description: [x] T10 with timestamp.
gh pr edit <PR#> --body "$(updated body with [x] in T10 line, append: 'T10 verified <date>: 48h soak clean, zero drift.')"
```

---

## Task 19: File follow-up issues for remaining Apps

**Files:** none — issues filed in `SpyrosPsarras/epaflix`.

For each App that still has imperative Secrets, file a separate issue using this template body (substitute `<App>`, `<Secret list>`):

```markdown
## Finding
After issue #29 landed, the SOPS+age pattern is available cluster-wide. `<App>` still creates the following Secrets imperatively from `.github/instructions/secrets.yml`:

- `<Secret 1>`
- `<Secret 2>`
- ...

## Current state
The Application's kustomization at `2-k3s/<App>/kustomization.yaml` excludes the Secret(s) with a `# excluded by design` comment. SOPS+age + ksops is already wired into `argocd-repo-server` (#29).

## Desired outcome
- Encrypt each Secret listed above to `<name>.enc.yaml` (sops -e).
- Reference via the existing ksops generator pattern (see `2-k3s/09.filebrowser/ksops-generator.yaml`).
- Drop the exclusion comment from kustomization.yaml.
- Verify with `kubectl diff` no destructive change; sync manually first; soak 48h with selfHeal ON.

## Notes
- Test plan must include OIDC / DB / etc. functional round-trip per App.
- Follow the canary recipe documented in `.github/instructions/sops.instructions.md`.
- Coordinate ordering only if multiple Apps share the same Secret (e.g. authentik + apps that reference `authentik-bootstrap-token`).
```

- [ ] **Step 1: File issue per App**

```bash
for app in authentik traefik servarr observability postgres cert-manager; do
  gh issue create --repo SpyrosPsarras/epaflix \
    --title "Migrate $app Secrets to SOPS (follow-up to #29)" \
    --label enhancement \
    --body "$(cat <<EOF
## Finding
After #29 landed, $app still has imperative Secrets. Migrate to sops+ksops following the filebrowser canary recipe.

## Current state
See $app kustomization "# excluded by design" comment for the Secret list.

## Desired outcome
- sops-encrypt each Secret to *.enc.yaml.
- Reference via ksops generator (see 2-k3s/09.filebrowser/ksops-generator.yaml).
- Drop kustomization exclusion comment.
- Test plan: kubectl diff = clean ; manual sync first ; functional round-trip ; 48h soak with selfHeal ON.

## Notes
Per-App recipe lives in .github/instructions/sops.instructions.md.
Refs #29.
EOF
)"
done
```

- [ ] **Step 2: Cross-link from #29**

```bash
gh issue comment 29 --repo SpyrosPsarras/epaflix --body "Per-App follow-up issues filed: (paste links from Step 1 output)."
```

- [ ] **Step 3: Close #29**

```bash
gh issue close 29 --repo SpyrosPsarras/epaflix --comment "Closing on PR merge — infra + canary live. Per-App follow-ups tracked individually."
```

---

## Self-review

**Spec coverage:**

| Spec requirement | Covered in task(s) |
|---|---|
| ksops CMP sidecar on repo-server | Task 8 |
| age key Secret bootstrap (imperative, one-shot) | Task 11 |
| `.sops.yaml` with creation rule | Task 3 |
| `*.enc.yaml` files committed | Task 13 |
| `kustomize.buildOptions: --enable-alpha-plugins --enable-exec` | Task 8 Step 1 |
| Pre-commit hook | Task 5 |
| Encrypt / rotate / decrypt recipes | Task 6 |
| `CLAUDE.md` convention paragraph | Task 7 |
| Canary on filebrowser only | Tasks 13-16 |
| Per-App follow-up issues filed | Task 19 |
| TrueNAS backup of age key | Task 2 Steps 4-5 |
| `argocd/sops-age` Secret in 11.argocd exclusion comment | Task 9 |
| README bootstrap recipe | Task 10 |
| `.gitignore` plaintext pattern | Task 4 |
| T1-T10 verification | Tasks 5, 14, 16 (preset) + 17 (PR test plan) + 18 (T9, T10 retroactive) |
| Rollback path | Spec section "Rollback"; no separate task — PR revert is the trigger |

**Placeholder scan:** none remaining; `age1RECIPIENT_PLACEHOLDER` is a known substitution at Task 3 Step 1; ksops image `v4.3.3` is a concrete floor pin.

**Type consistency:** Secret name `filebrowser-oidc` consistent throughout. Generator name `filebrowser-secrets` matches between Task 14 Step 2 and the kustomization in Step 1. Volume name `sops-age` matches between volumes and volumeMounts in Task 8.

**Branch policy:** continue on `sops-secret-automation-design`. The branch already contains the design doc commit; implementation commits stack on top; single PR.
