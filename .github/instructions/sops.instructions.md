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
- **The private age key is no longer on the workstation filesystem.** As of
  2026-08-21 `~/.config/sops/age/` is empty: `k3s-cluster.txt` was
  `shred -u`'d and the `keys.txt` symlink removed. The workstation copy now
  lives in the maintainer's KeePassXC database as entry
  **`sops-age-k3s-cluster`** (the password field holds the single
  `AGE-SECRET-KEY-1` line, 74 chars, not the two `#` comment lines the
  file had). Plain `sops` still works because
  `~/.pi/profiles/epaflix/bin/sops` is a symlink to a `sops-kpx` wrapper on
  that profile's PATH, which reads the entry over the Secret Service D-Bus
  API and exports `SOPS_AGE_KEY` into the sops process environment. What that
  buys is that the key is no longer at rest on disk. It is still readable at
  `/proc/<pid>/environ` by this uid for the life of that one process, and it
  never reaches a file or an argv element. Consequences:
  - **A locked KeePassXC means no decryption.** The wrapper exits `2` and
    prints the break-glass paths. Unlock the KeePassXC window and re-run.
    `sops -e` and `sops -e -i` are exempt: encryption needs only the public
    recipient from `.sops.yaml`, so the wrapper execs straight through for a
    pure encrypt and works with KeePassXC locked. Anything that decrypts,
    including `sops <file>` to edit and `sops updatekeys`, does not.
  - **Outside that pi profile, plain `sops -d` fails** with
    `Failed to get the data key required to decrypt the SOPS file` (verified
    wording, sops 3.13.3) unless you call
    `~/.pi/profiles/epaflix/bin/sops-kpx` or export `SOPS_AGE_KEY` yourself.
  - **ksops is not covered by the wrapper.** A local `kustomize build` with the
    ksops exec plugin decrypts through the sops Go library and never runs the
    `sops` binary, so it needs `SOPS_AGE_KEY` in its own environment. Recipe in
    the header comments of the two `ksops-generator.yaml` files that carry a
    local-build recipe, `05.traefik-deployment` and `14.searxng`.
  - Remove any leftover `export SOPS_AGE_KEY_FILE=` from your shell rc. An
    older plan doc told you to add one, and the wrapper now refuses an
    *unreadable* key file rather than silently obeying it. A readable one
    holding the wrong key is still obeyed, and fails with the canonical
    `Failed to get the data key` error.
  - The wrapper exists only on the maintainer workstation, so CI and the
    cluster are untouched by it. It also honours a pre-set `SOPS_AGE_KEY`,
    so a local override still works. A pre-set but unreadable
    `SOPS_AGE_KEY_FILE` is rejected rather than silently obeyed, because a
    stale one used to produce a bare `no key could decrypt the data`.
  - The wrapper lives under `~/.pi/`, which is **not** in this repo and does
    not survive a fresh clone or a new machine. Tracked in #1069.
  The other two copies are the in-cluster Secret `argocd/sops-age` and the
  TrueNAS mirror below. TrueNAS mirror:
  the ZFS-encrypted dataset at
  `/mnt/apps/encrypted-backups/sops-age-backup/k3s-cluster.txt`
  (ZFS native encryption, passphrase unlock; the passphrase is in the credential
  store `.github/instructions/secrets.enc.yaml` under
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

## Plaintext placeholder templates and the Secret guard

Readable `kind: Secret` templates may remain plaintext only when the generic
content classifier accepts them. There is no per-file template allowlist and
no fixed key schema: adding or removing a legitimate field must not require a
policy update.

For each plaintext Secret document, the guard requires all four of these:

1. A value under a key matching `pass|password|secret|token|key|crt|cert|credential|auth`
   (case-insensitive) must be exactly one approved placeholder form:
   `<UPPER_SNAKE_CASE>`, `REPLACE_WITH_UPPER_SNAKE_CASE`, or `CHANGEME`.
   The only lowercase angle-bracket forms are the three exact legacy values
   already used by the origin-certificate and Renovate templates; arbitrary
   angle-bracket text such as a passphrase is not a placeholder. A value that
   only contains a placeholder does not pass. Sensitive scalar keys remain
   hard-gated when their names end in `name`, `ref`, `reference`, or `id`.
   Only constrained mapping-shaped reference objects, plus pve.yml's exact
   `token_name` identifier paired with an approved `token_value` placeholder,
   are treated as non-secret references.
2. No scalar anywhere in the document, including parsed YAML/JSON embedded in
   a block scalar, may match the guard's credential heuristics: known token
   prefixes, private material, JWTs, long base64/hex runs, or high entropy
   across printable Unicode and internal whitespace (including
   punctuation-heavy values).
3. A scalar of 8 to 15 characters, below the entropy band's 16-character floor,
   is rejected when it is one unbroken alphanumeric token that mixes at least
   two of lowercase, uppercase and digits, has at least 6 distinct characters,
   and reaches 2.5 bits of Shannon entropy per character. Template identifiers
   here are DNS-1123 lowercase words joined by `-`, `.` or `/`, so each
   unbroken token carries a single character class, while a short credential is
   one unbroken run that mixes letter case, digits, or both. Measured against
   every scalar of the 14 tracked plaintext Secret documents this band rejects
   nothing, so values like `jellyseerr`, `prowlarr` and `sonarr2-database` stay
   editable with no per-file key allowlist.
4. An opaque scalar longer than 2048 characters is rejected outright instead of
   being analysed. Entropy analysis has to be bounded somewhere, and any bound
   is a padding bypass, so oversized opaque values fail closed. The longest
   opaque scalar in the tracked plaintext Secrets is 82 characters, so the limit
   constrains no real template. A scalar that parses as embedded YAML or JSON is
   decomposed rather than measured, and every leaf it yields is held to the same
   2048-character limit, so padding an embedded document buys nothing either.

Kubernetes `kind: List` and `kind: SecretList` documents are rejected instead
of allowing Secret objects nested below `items` to bypass these checks.

The only concrete plaintext Secret exception is
`2-k3s/06.postgres/operator-kustomization/barman-manifest.yaml`. Its payload is
restricted to the generated barman sidecar image field and the decoded value
must be an OCI image reference. This is not a blanket path exemption. Paths
under `charts/` are not skipped.

SOPS validation is document-local. Every Secret document with SOPS metadata
must use `.enc.yaml`; have the expected SOPS+age metadata fields; and use a
canonical `ENC[AES256_GCM,data:...,iv:...,tag:...,type:...]` envelope with
valid base64 fields and SOPS-sized IV/tag fields for every non-empty
`data`/`stringData` leaf. A bare `ENC[...]` marker or stub `sops:` mapping does
not pass, and one document's metadata cannot bless a second document. The guard
reads staged blobs from Git's index by default; CI also checks the complete
tracked tree:

```bash
./.github/hooks/test-check-sops-encrypted.sh
./.github/hooks/check-sops-encrypted.sh --full-tree
```

This is not a general secret scanner, but no scalar size is a free pass. Both
new rules fail closed in the required `validate` job: the guard exits non-zero
and prints a fixed reason string plus the file, document number and key path.
It never prints the offending value, in any encoding (#740).

The residual limitations are:

- A value shorter than 8 characters is not classified. It cannot carry usable
  credential entropy and it is indistinguishable from ordinary short template
  values such as `admin`, `5432` or `v1`.
- Rule 3 counts character classes, not meaning. A short credential built from a
  single character class, such as an all-lowercase dictionary word, still
  passes.
- Rule 3 has a cost in the other direction: a short all-lowercase identifier
  that gains a digit inside the same unbroken token, such as `sonarr2user`, is
  rejected. Give it a separator (`sonarr2-user`) or a placeholder. That is the
  deliberate price of catching `admin123`-shaped credentials without a per-file
  key allowlist.
- A CamelCase enum value such as `ClusterIP` would also be rejected by rule 3
  if it ever appeared inside a Secret document. No tracked Secret contains one.

Sensitive key names remain hard-gated, and review plus required CI remain part
of the trust boundary.

## The credential store (`.github/instructions/secrets.enc.yaml`)

Everything above is about Secrets the **cluster** consumes. This one is the
store **humans and agents** read from - the replacement for the pre-SOPS
plaintext credential file, which is gone and stays gone: `.gitignore` still
blocks that filename permanently so it cannot come back. See
`general.instructions.md` for the full history.

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

The read and write recipes below decrypt, so each needs an unlocked KeePassXC on
the maintainer workstation. See the key-location bullet at the top of this file.
The `grep -c 'ENC\['` check above does not: it needs no key at all.

**Read one value** (never decrypt the whole file for one key, never echo it):

```bash
TOKEN=$(sops -d --extract '["<key_name>"]' .github/instructions/secrets.enc.yaml)
TOKEN=$(sops -d --extract '["epaflix_bot"]["proxmox_token"]' .github/instructions/secrets.enc.yaml)
echo "${#TOKEN}"   # a length is safe to print; the value is not
```

**Write one key** - `sops set` edits a single index in place, so you never open
the whole store to change one value:

```bash
printf %s "$VALUE" | jq -Rs . \
  | sops set --value-stdin .github/instructions/secrets.enc.yaml '["<key_name>"]'
```

Two traps in that one line, both of which fail loudly rather than silently, but
both of which cost a round trip:

- Argument order is `sops set [options] <file> <index>` - file first, index
  second. Reversing them fails with `Invalid set index format`.
- `--value-stdin` still expects **JSON**, not a bare string. Piping the raw value
  fails with `Value for --set is not valid JSON` and leaves the stored value
  untouched. `jq -Rs .` does the encoding: `-R` reads the input as raw text, `-s`
  slurps it as one string. It also escapes quotes and backslashes correctly, so
  the #545 quote trap cannot come back through the write path either.

`--value-stdin` keeps the value out of `argv`, so it never lands in a process
listing or a transcript (same reason as `--value-file`). `printf %s` adds no
trailing newline, so what you read back is byte-for-byte what you wrote.

Measured on the store: writing one key produced a **6-line diff** - the one key
plus the `lastmodified` and `mac` footer lines. The other 141 values stayed
byte-identical and the whole file still decrypted. `sops set --idempotent` does
nothing when the index already holds that value - a 0-line diff and an unchanged
file `sha256` - which makes it the right way to reconcile the store against a
deployed value without churning the file.

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

Since 2026-08-21 the workstation copy is a KeePassXC entry rather than a file,
which adds one more way to lose it: a lost or corrupted `Passwords.kdbx`, or a
forgotten master password, takes the workstation copy with it. That leaves
`argocd/sops-age` and the TrueNAS mirror, and the TrueNAS mirror needs a
passphrase that is itself in the SOPS store.

**As of 2026-08-21 no offline copy exists.** Every surviving copy needs a live
system: the cluster, TrueNAS, or an unlocked `Passwords.kdbx`. `Passwords.kdbx`
itself is replicated by Syncthing, which is not a backup. Making a real offline
copy of the 74-char `AGE-SECRET-KEY-1` line is tracked in #1070.

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

The passphrase is the credential store value
`truenas_zfs_encrypted_backups_passphrase` (NEVER inline it; paste it at the
prompt / UI field).

Read it without decrypting the whole file, and never echo it:

```bash
PASSPHRASE=$(sops -d --extract '["truenas_zfs_encrypted_backups_passphrase"]' .github/instructions/secrets.enc.yaml)
echo "${#PASSPHRASE}"   # a length is safe to print; the value is not
```

`truenas_admin` can read ZFS props directly and **does have password-prompted
`sudo`** (it has **no _passwordless_ sudo**, so non-interactive `sudo zfs ...`
without a password will fail). Any of the following are valid ways to unlock —
pick whichever fits the access you have. Paste the passphrase from the credential store
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
  password: "<value-from-the-credential-store>"   # read it with sops -d --extract
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
#    Key NAMES only. `sops -d | head` would print the value.
sops -d my-thing.enc.yaml | yq '.stringData | keys'
#    ksops decrypts with the sops Go library, not the `sops` binary, so the
#    KeePassXC wrapper on PATH does NOT cover a kustomize render. The key has
#    to be in that process's environment. Count the match, never print the
#    rendered Secret: kustomize emits stringData in cleartext.
SOPS_AGE_KEY=$(~/.pi/shared/skills/keepassxc-secrets/scripts/kpx.sh get sops-age-k3s-cluster) \
  kustomize build --enable-alpha-plugins --enable-exec . | grep -c 'name: my-thing'   # expect 1

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

The steps below still write the new key to a temporary file, because
`age-keygen` and `scp` need a path. Step 8 is what puts it back under
KeePassXC and removes the file again, so do not stop before it.

**Step 8a has never been run end to end.** There has been no rotation since
the key moved into KeePassXC on 2026-08-21. Treat it as untested and verify
each sub-step before running the next.

Two things about the window between step 7 and step 8a: the files are wrapped to
the new recipient alone while KeePassXC still holds the old key, so the wrapper
cannot decrypt anything. Use
`SOPS_AGE_KEY_FILE=~/.config/sops/age/k3s-cluster-new.txt` for any decrypt in
that window, which the wrapper honours because the file is readable. And the
halting checks below use `return 1 2>/dev/null || exit 1`, which closes an
interactive shell and takes `$NEW` with it: run step 8 from a sourced script if
you want to keep the session.

```bash
# 1. Generate new key. Temporary: step 8 shreds this file.
age-keygen -o ~/.config/sops/age/k3s-cluster-new.txt

# 2. Add the NEW public key to BOTH `age:` lists in .sops.yaml (the
#    secrets.enc.yaml rule and the generic .enc.yaml rule are separate
#    creation rules with separate recipient lists; updating only one leaves
#    the credential store wrapped to the old key alone, and step 7 then
#    makes it unreadable). Do NOT remove the old recipient yet.
$EDITOR .sops.yaml
grep -o 'age1[0-9a-z]*' .sops.yaml | wc -l   # expect 4 in the dual-recipient window

# 3. Re-wrap every encrypted file to both recipients.
find . -name '*.enc.yaml' -exec sops updatekeys -y {} \;
git add -u
git commit -m "chore(sops): re-wrap secrets to new+old age recipient"
git push

# 4. Push new key into the cluster (replaces old keys.txt content).
kubectl --context epaflix create secret generic sops-age \
  -n argocd \
  --from-file=keys.txt=$HOME/.config/sops/age/k3s-cluster-new.txt \
  --dry-run=client -o yaml | kubectl --context epaflix apply -f -

# 5. Force repo-server to pick up new Secret.
kubectl --context epaflix -n argocd rollout restart deploy/argocd-repo-server
kubectl --context epaflix -n argocd rollout status deploy/argocd-repo-server

# 6. Sanity: trigger ArgoCD App sync.
argocd app sync servarr
# Confirm Synced + Healthy.

# 7. Drop OLD recipient from .sops.yaml ; re-wrap ; commit.
find . -name '*.enc.yaml' -exec sops updatekeys -y {} \;
git add -u .sops.yaml
git commit -m "chore(sops): drop old age recipient after rotation"
git push

# 8. Replace the workstation copy (KeePassXC) and the TrueNAS mirror, then
#    destroy the on-disk key. Run 8a, 8b and 8c one at a time, in order.
#    Nothing here prints the key.

# 8a. Put the new key in KeePassXC. Do this in the GUI, NOT with
#     `secret-tool store`: secret-tool writes its own attribute set
#     ({Title, xdg:schema}), which does not match a KeePassXC entry's
#     attributes ({Title, UserName, URL, Notes, Path, Uuid}), so libsecret
#     ADDS a second item instead of replacing the first. Two items with the
#     same title make kpx.sh exit 4 (ambiguous) and every `sops -d` on this
#     workstation stops working.
#       KeePassXC > entry `sops-age-k3s-cluster` > edit > paste the single
#       AGE-SECRET-KEY-1 line into Password > Ctrl+S
#     Then confirm exactly one entry exists and that it matches the file:
KPX=~/.pi/shared/skills/keepassxc-secrets/scripts/kpx.sh
"$KPX" find sops-age-k3s-cluster            # expect exactly ONE row
"$KPX" get sops-age-k3s-cluster >/dev/null \
  || { echo 'kpx get failed: locked DB, or a duplicate entry (exit 4)'; return 1 2>/dev/null || exit 1; }
NEW=$(grep '^AGE-SECRET-KEY' ~/.config/sops/age/k3s-cluster-new.txt)
[ "$("$KPX" get sops-age-k3s-cluster)" = "$NEW" ] \
  || { echo 'MISMATCH: the KeePassXC entry is not the new key, fix it before 8b'; return 1 2>/dev/null || exit 1; }
echo 'kpx entry matches the new key file'

# 8b. Refresh the TrueNAS mirror. Upload and verify BEFORE removing the old
#     copy: `apps/encrypted-backups` comes up locked after every TrueNAS
#     reboot, and writing to a locked mountpoint puts a plaintext private
#     key on the unencrypted parent dataset where the next mount hides it.
[ "$(ssh truenas_admin@192.168.10.200 'zfs get -H -o value keystatus,mounted apps/encrypted-backups' | tr '\n' ' ')" = 'available yes ' ] \
  || { echo 'dataset locked or unmounted: unlock it first, do not run the scp'; return 1 2>/dev/null || exit 1; }
scp ~/.config/sops/age/k3s-cluster-new.txt truenas_admin@192.168.10.200:/mnt/apps/encrypted-backups/sops-age-backup/k3s-cluster.txt.new
REMOTE_SUM=$(ssh truenas_admin@192.168.10.200 'sha256sum /mnt/apps/encrypted-backups/sops-age-backup/k3s-cluster.txt.new' | cut -d' ' -f1)
LOCAL_SUM=$(sha256sum ~/.config/sops/age/k3s-cluster-new.txt | cut -d' ' -f1)
[ "$REMOTE_SUM" = "$LOCAL_SUM" ] \
  || { echo 'upload digest mismatch: do NOT remove the old mirror'; return 1 2>/dev/null || exit 1; }
ssh truenas_admin@192.168.10.200 'cd /mnt/apps/encrypted-backups/sops-age-backup && chmod 600 k3s-cluster.txt.new && shred -u k3s-cluster.txt && mv k3s-cluster.txt.new k3s-cluster.txt'
unset REMOTE_SUM LOCAL_SUM

# 8c. Remove the on-disk key and confirm decryption still works through
#     KeePassXC. Prints a marker, never a value.
shred -u ~/.config/sops/age/k3s-cluster-new.txt
unset NEW
V=$(sops -d --extract '["truenas_ip"]' .github/instructions/secrets.enc.yaml)
if [ -n "$V" ]; then echo 'decrypt via KeePassXC OK'; else
  echo 'DECRYPT FAILED. First: unset SOPS_AGE_KEY_FILE (the step-7 workaround now points at the shredded file).'
  echo 'If that was not it, the on-disk key is gone, so recover from a break-glass copy:'
  echo '  TrueNAS /mnt/apps/encrypted-backups/sops-age-backup/k3s-cluster.txt'
  echo '  kubectl --context epaflix -n argocd get secret sops-age'
fi
unset V
```

## Cluster bootstrap (fresh cluster)

There is no longer a key file to point `--from-file` at, so materialise the
KeePassXC entry into the Secret without writing it to disk:

This form has **not** been exercised against a real cluster yet. Tick it at the
next rebuild.

```bash
# Run ONCE before installing/syncing ArgoCD self-management.
KPX=~/.pi/shared/skills/keepassxc-secrets/scripts/kpx.sh
KEY=$("$KPX" get sops-age-k3s-cluster)
[ ${#KEY} -eq 74 ] \
  || { echo 'no key from KeePassXC (locked?): refusing to create an empty Secret'; return 1 2>/dev/null || exit 1; }
kubectl --context epaflix create namespace argocd
kubectl --context epaflix create secret generic sops-age \
  -n argocd \
  --from-file=keys.txt=/dev/stdin <<<"$KEY"
unset KEY
```

The length check matters: a locked KeePassXC yields an empty string, and
`--from-file=keys.txt=/dev/stdin` would happily create a Secret holding one
newline, which only surfaces later as repo-server decrypt errors.

The here-string keeps the key out of argv, and out of the filesystem on bash
5.1 and newer, where a short here-string goes through a pipe rather than a temp
file. Verify with
`kubectl --context epaflix -n argocd get secret sops-age -o jsonpath='{.data.keys\.txt}' | base64 -d | wc -c`,
expect 75 (74-char key + newline), and never pipe it anywhere that prints. The
Secret currently in the cluster is 189 bytes: it predates this change and still
holds the whole old key file, comment lines included. Both decrypt fine, because
sops ignores `#` lines, so do not "fix" that 189-byte Secret unless you are
rotating anyway.

## Common failures

| Symptom | Likely cause | Fix |
|---|---|---|
| `sops: cannot find age private key` in repo-server logs | `argocd/sops-age` Secret missing | Re-run bootstrap kubectl --context epaflix create secret |
| `sops-kpx: KeePassXC is locked or its Secret Service is unavailable` | kdbx locked, or KeePassXC not running | Unlock the KeePassXC window and re-run; the agent cannot unlock it |
| `sops-kpx: more than one KeePassXC entry matches` | a duplicate `sops-age-k3s-cluster` item, usually from `secret-tool store` | Delete the duplicate in the KeePassXC GUI, confirm with `kpx.sh find sops-age-k3s-cluster` |
| `sops-kpx: SOPS_AGE_KEY_FILE points at an unreadable path` | leftover export naming the shredded key file | `unset SOPS_AGE_KEY_FILE` and remove it from your shell rc |
| `Failed to get the data key required to decrypt the SOPS file` on the workstation | called a `sops` that is not the `sops-kpx` wrapper (different shell, cron, another pi profile) | Call `~/.pi/profiles/epaflix/bin/sops-kpx`, or export `SOPS_AGE_KEY` from the KeePassXC entry first |
| `kustomize build` fails with `trouble decrypting file: Error getting data key` | ksops decrypts via the sops library, so the PATH wrapper does not apply | Prefix the build with `SOPS_AGE_KEY=$(kpx.sh get sops-age-k3s-cluster)` |
| `no key could decrypt the data` | Key was rotated but Secret in cluster still old | Re-apply key Secret + restart repo-server |
| `kustomize build` errors `unknown plugin kind ksops` | `--enable-alpha-plugins` missing | Confirm `configs.cm.kustomize.buildOptions: --enable-alpha-plugins --enable-exec` in argocd helm-values.yaml |
| Pre-commit hook rejects encrypted file | hook checks failed (no `sops:` block) | Re-run `sops -e -i <file>` |
