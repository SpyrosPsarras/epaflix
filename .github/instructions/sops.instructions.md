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
  the maintainer workstation, mirrored to TrueNAS at
  `pool1/dataset01/sops-age-backup/` (to be moved to a ZFS-encrypted
  dataset — tracked in issue #57).
- The cluster reads the key from Secret `argocd/sops-age` (created
  imperatively, once per cluster rebuild — chicken-egg).
- `ksops` runs on `argocd-repo-server` (initContainer that copies the
  binary into a shared volume) and decrypts at sync render time.

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
kustomize build --enable-alpha-plugins --enable-exec . | grep -A5 'name: my-thing'

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
ssh truenas_admin@192.168.10.200 'shred -u /mnt/pool1/dataset01/sops-age-backup/k3s-cluster.txt'
scp ~/.config/sops/age/k3s-cluster.txt truenas_admin@192.168.10.200:/mnt/pool1/dataset01/sops-age-backup/
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
