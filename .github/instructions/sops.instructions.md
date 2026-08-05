# SOPS + age secret automation

Cluster Secret automation for ArgoCD-managed Applications. Tracks
issue #29. Design spec:
`docs/superpowers/specs/2026-05-25-sops-secret-automation-design.md`.

## Quick rules

- Encrypted Secret files use the suffix `.enc.yaml` and live next to the
  kustomization that references them.
- `.sops.yaml` at repo root holds all creation rules — single
  cluster-wide age recipient. Rules match **top-down, first match wins**,
  so the `secrets.enc.yaml` rule must stay above the generic
  `\.enc\.yaml$` rule (see "The credential store" below for why).
- The private age key lives at `~/.config/sops/age/k3s-cluster.txt` on the
  maintainer workstation, with `~/.config/sops/age/keys.txt` symlinked to
  it so plain `sops -d` finds it with no env var. Verified present
  2026-08-04 (decrypted `airvpn-api-key.enc.yaml`, exit 0), which
  supersedes the earlier "that copy does NOT exist" note from #580 — the
  key was restored on 2026-08-03. The other two copies are the in-cluster
  Secret `argocd/sops-age` and the TrueNAS mirror below. TrueNAS mirror:
  the ZFS-encrypted dataset at
  `/mnt/apps/encrypted-backups/sops-age-backup/k3s-cluster.txt`
  (ZFS native encryption, passphrase unlock; the passphrase is recorded in
  the git-ignored `.github/instructions/secrets.yml` under
  `truenas_zfs_encrypted_backups_passphrase`). The dataset must be manually
  unlocked after a TrueNAS reboot before this backup is readable — see
  [Post-reboot: unlock the TrueNAS encrypted backup dataset](#post-reboot-unlock-the-truenas-encrypted-backup-dataset).
  As of 2026-06-07 (#149) this backup lives on the redundant `apps` pool
  (3×SSD RAIDZ1) — it was relocated off the non-redundant single-disk
  `pool1` stripe (where a single disk failure meant total loss); the
  retired `pool1/encrypted-backups` dataset has been destroyed. Same
  passphrase and ZFS encryption params (AES-256-GCM, keyformat=passphrase,
  keylocation=prompt, pbkdf2iters=350000), no key rotation.
- The cluster reads the key from Secret `argocd/sops-age` (created
  imperatively, once per cluster rebuild — chicken-egg).
- `ksops` runs on `argocd-repo-server` (initContainer that copies the
  binary into a shared volume) and decrypts at sync render time.

## The credential store (`.github/instructions/secrets.enc.yaml`)

Everything above is about Secrets the **cluster** consumes. This one is the
store **humans and agents** read from - the replacement for the old plaintext,
git-ignored `.github/instructions/secrets.yml`, which is gone.

It differs from every other `.enc.yaml` in the repo in one way: it has **no
`encrypted_regex`**. It is a flat key/value file with no `data:`/`stringData:`
key, so the generic rule's `encrypted_regex: ^(data|stringData)$` would match
nothing and write the whole file back in **cleartext**. Its own rule sits
**above** the generic one in `.sops.yaml` for exactly this reason. If you ever
reorder those rules, or add a rule above them, re-verify with:

```bash
sops -e -i .github/instructions/secrets.enc.yaml   # after an edit
grep -c 'ENC\[' .github/instructions/secrets.enc.yaml   # expect: many, not 0
```

Consequence of no `encrypted_regex`: values are encrypted, key **names** stay
readable. That is deliberate - the committed file doubles as an index of which
credentials exist, and a PR diff shows *which* credential changed without
showing the value.

**Read one value** (never decrypt the whole file for one key, never echo it):

```bash
TOKEN=$(sops -d --extract '["<key_name>"]' .github/instructions/secrets.enc.yaml)
TOKEN=$(sops -d --extract '["epaflix_bot"]["proxmox_token"]' .github/instructions/secrets.enc.yaml)
echo "${#TOKEN}"   # a length is safe to print; the value is not
```

**Add or change a credential** - in-place edit, decrypt to `$EDITOR` and
re-encrypt on save. The plaintext never touches the disk in the repo path:

```bash
sops .github/instructions/secrets.enc.yaml
```

### Recovery loop - read this before you trust the encrypted store

`truenas_zfs_encrypted_backups_passphrase` is a key in this store, and that
passphrase unlocks the ZFS dataset holding the **backup of the age key**. So
the store cannot be the only route to it:

- lose the workstation key **and** the in-cluster `argocd/sops-age` Secret, and
  you can no longer read the passphrase that would get you the TrueNAS copy.

The workstation key file is 189 bytes. Keep a copy somewhere that does not
depend on SOPS, the cluster, or TrueNAS being up.

The store also lives in a **public** repo as ciphertext, so the age key is now a
single point of total, retroactive compromise: anyone who gets it can decrypt
every credential from any point in git history. Treat key rotation as a real
recurring task, not a one-off.

## Post-reboot: unlock the TrueNAS encrypted backup dataset

`apps/encrypted-backups` is its own ZFS encryption root with
`keylocation=prompt`, so it does **not** auto-unlock on boot. After **every**
TrueNAS reboot it comes up **locked**, and the age-key backup at
`/mnt/apps/encrypted-backups/sops-age-backup/k3s-cluster.txt` is unreadable
until you load the key. Same passphrase and encryption params as before (#149,
#57) — no rotation; only the dataset path changed.

The passphrase is the git-ignored `secrets.yml` value
`truenas_zfs_encrypted_backups_passphrase` (NEVER inline it; paste it at the
prompt / UI field).

`truenas_admin` can read ZFS props directly and **does have password-prompted
`sudo`** (it has **no _passwordless_ sudo**, so non-interactive `sudo zfs ...`
without a password will fail). Any of the following are valid ways to unlock —
pick whichever fits the access you have. Paste the passphrase from `secrets.yml`
(`truenas_zfs_encrypted_backups_passphrase`) at the prompt / UI field in place of
the placeholders below; **NEVER inline the real value**.

> **`pool.dataset.unlock` is a long-running JOB method.** Invoke it with the
> job flag (`midclt -j call ...`) so the client follows the job. Per the
> truenas.instructions.md #125 caveat, the job-method `midclt` client may still
> **crash on poll even though the server-side unlock SUCCEEDED** — so do not
> trust `midclt`'s exit code. Always confirm with the `zfs get
> keystatus,mounted` verify step below (keystatus=available, mounted=yes).

```bash
# Option A (recommended) — middleware unlock via SSH as truenas_admin (job method).
# Works for truenas_admin (in builtin_administrators); also mounts on success.
ssh truenas_admin@192.168.10.200 \
  'midclt -j call pool.dataset.unlock apps/encrypted-backups \
     "{\"datasets\": [{\"name\": \"apps/encrypted-backups\", \"passphrase\": \"<PASSPHRASE>\"}]}"'
```

```bash
# Option B — truenas_admin via password-prompted sudo (interactive shell on the box):
sudo zfs load-key apps/encrypted-backups        # prompts for the SUDO password,
                                                #   then the dataset passphrase
sudo zfs mount apps/encrypted-backups           # only if not auto-mounted on unlock

# ...or non-interactively pipe the SUDO password to sudo -S (passphrase still
#    prompted by zfs unless keylocation provides it):
echo '<SUDO_OR_PASSPHRASE>' | sudo -S zfs load-key apps/encrypted-backups
sudo zfs mount apps/encrypted-backups
```

```bash
# Option C — raw zfs as root (console or `ssh root@...`):
zfs load-key apps/encrypted-backups && zfs mount apps/encrypted-backups
```

Option D — TrueNAS UI: **Storage > Datasets >** select `apps/encrypted-backups`
**> Unlock**, paste the passphrase, leave "Unlock Children" as needed.

Verify (read-only, works as `truenas_admin` with no privilege):

```bash
ssh truenas_admin@192.168.10.200 \
  'zfs get keystatus,mounted apps/encrypted-backups'
# expect: keystatus=available, mounted=yes
ssh truenas_admin@192.168.10.200 \
  'ls -l /mnt/apps/encrypted-backups/sops-age-backup/k3s-cluster.txt'
# expect: the age-key backup file is now listable (mode 0600, owner truenas_admin)
```

## Encrypt a new Secret

> **Why the file must already be named `*.enc.yaml` before you encrypt:** the
> `.sops.yaml` creation rule matches on `path_regex: \.enc\.yaml$`, i.e. it keys
> off the **file path being encrypted**. A `*-plaintext.yaml` input name does
> **not** match that regex, so SOPS fails with `no matching creation rules`
> (surfaced during #230). Always create the file with its final `*.enc.yaml`
> name first, then encrypt **in place** — never `sops -e <plaintext> > <enc>`
> (that redirect form was the broken recipe).

```bash
# 1. Draft the Secret directly into its canonical *.enc.yaml filename (this is
#    what .sops.yaml matches; .gitignore still catches a stray plaintext body
#    until it is encrypted in step 2, so do NOT commit between 1 and 2).
cd 2-k3s/<App>
cat > my-thing.enc.yaml <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: my-thing
  namespace: <ns>
stringData:
  password: "actual-secret-value-from-secrets.yml"
EOF

# 2. Encrypt IN PLACE (the .enc.yaml path matches .sops.yaml's creation rule).
sops -e -i my-thing.enc.yaml          # or: sops --encrypt --in-place my-thing.enc.yaml

# 3. Reference it from kustomization.yaml via the ksops generator:
#    Add (or extend) ksops-generator.yaml:
#      files:
#        - my-thing.enc.yaml
#    And in kustomization.yaml under generators:
#      generators:
#        - ksops-generator.yaml

# 4. Verify locally (the file is encrypted in place; the pre-commit hook will
#    reject it if it is still plaintext):
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
argocd app sync servarr
# Confirm Synced + Healthy.

# 7. Drop OLD recipient from .sops.yaml ; re-wrap ; commit.
find . -name '*.enc.yaml' -exec sops updatekeys -y {} \;
git add -u .sops.yaml
git commit -m "chore(sops): drop old age recipient after rotation"
git push

# 8. Securely destroy old key.
shred -u ~/.config/sops/age/k3s-cluster.txt
mv ~/.config/sops/age/k3s-cluster-new.txt ~/.config/sops/age/k3s-cluster.txt
ssh truenas_admin@192.168.10.200 'shred -u /mnt/apps/encrypted-backups/sops-age-backup/k3s-cluster.txt'
scp ~/.config/sops/age/k3s-cluster.txt truenas_admin@192.168.10.200:/mnt/apps/encrypted-backups/sops-age-backup/
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
