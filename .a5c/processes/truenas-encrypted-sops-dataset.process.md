# Process spec — TrueNAS encrypted SOPS backups dataset (issue #57)

**Goal:** move the SOPS cluster age master-key backup off the unencrypted
`pool1/dataset01/sops-age-backup/` onto a new ZFS-encrypted dataset
`pool1/encrypted-backups` (AES-256-GCM, **passphrase** unlock) on TrueNAS
(`192.168.10.200`), then update the rotation recipe in
`.github/instructions/sops.instructions.md` (branch + PR + merge, Closes #57).

## Phases

| # | Task | Kind | Live? | Notes |
|---|------|------|-------|-------|
| 0 | `precheck` | agent | read-only | pool health, locate old key, 3-way sha256 anchor (old↔workstation), confirm target absent, detect `pool.dataset.create` mechanism + TrueNAS version |
| — | **GATE 1** | breakpoint | — | owner; tags destructive + secrets-rotation; approve live writes |
| 1 | `create-dataset` | agent | **LIVE** | create encrypted dataset; passphrase auto-generated → `secrets.yml` key `truenas_zfs_encrypted_backups_passphrase`; passphrase never on a cmdline / in journal / in output |
| 2 | `migrate-key` | agent | **LIVE** | copy → verify sha256 vs workstation canonical → **shred old only on clean 3-way match**; self-aborts on mismatch |
| 3 | `verify-migration` | agent | read-only | independent re-verify: encrypted+passphrase, new key 0600 + sha match, old path gone, passphrase recorded |
| 4 | `author-doc` | agent | local git | edit doc (lines ~13-15 + ~112-113 + any other old-path refs → 0 remaining), branch + 1 commit, no push |
| — | **GATE 2** | breakpoint | — | owner; tags deploy + outward-facing; retry/refine loop (≤3) |
| 5 | `publish-merge` | agent | git/gh | push, rebase onto origin/main, force-with-lease, PR (Closes #57), wait `validate`, merge-commit, close #57 |

## Safety invariants

- Workstation copy (`~/.config/sops/age/k3s-cluster.txt`) and in-cluster KSOPS key are **never touched** → no lockout risk.
- Old backup is **copied + sha256-verified against the workstation canonical before any shred**; mismatch aborts without destroying anything.
- Passphrase recorded only in git-ignored `secrets.yml`; passed to TrueNAS via a shredded temp payload file; never echoed.
- Passphrase-encrypted datasets do **not** auto-mount after a TrueNAS reboot — manual unlock required before the backup is readable (documented in the doc update).

## Out of scope (issue says "later")

Migrating postgres dump retention / app-secret snapshots onto the dataset — remains tracked under #57's notes.
