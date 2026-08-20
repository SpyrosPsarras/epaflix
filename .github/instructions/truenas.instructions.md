---
applyTo: "0-truenas/**"
description: "Instructions for TrueNAS setup"
---
# TrueNAS-Specific Instructions

When working with files in the `0-truenas/` directory, follow these TrueNAS-specific guidelines.

**Credential Placeholders:**
All commands use placeholders for sensitive information. Replace them with values from the credential store `.github/instructions/secrets.enc.yaml`:
- `<TRUENAS_USER>` → truenas_admin_username
- `<TRUENAS_PASSWORD>` → truenas_admin_password
- `<TRUENAS_IP>` → TrueNAS server IP address

Read one key at a time. Never decrypt the whole file, never echo the value:

```bash
VALUE=$(sops -d --extract '["<key_name>"]' .github/instructions/secrets.enc.yaml)
echo "${#VALUE}"   # a length is safe to print; the value is not
```

## Quick Actions Reference

### Access TrueNAS via SSH
```bash
# Connect using credentials from the credential store
# Username: truenas_admin_username
# Password: truenas_admin_password (for sudo operations)
# Host IP: Defined in TrueNAS configuration

ssh <TRUENAS_USER>@<TRUENAS_IP>

# For sudo commands, use the truenas_admin_password value from the credential store:
echo '<TRUENAS_PASSWORD>' | sudo -S <command>
```

### Common TrueNAS Commands
```bash
# List all iSCSI targets
ssh <TRUENAS_USER>@<TRUENAS_IP> "echo '<TRUENAS_PASSWORD>' | sudo -S midclt call iscsi.target.query | jq -r '.[].name'"

# List all iSCSI extents
ssh <TRUENAS_USER>@<TRUENAS_IP> "echo '<TRUENAS_PASSWORD>' | sudo -S midclt call iscsi.extent.query | jq -r '.[].name'"

# Check iSCSI service status
ssh <TRUENAS_USER>@<TRUENAS_IP> "echo '<TRUENAS_PASSWORD>' | sudo -S midclt call service.query | jq -r '.[] | select(.service==\"iscsitarget\")'"

# List NFS shares
ssh <TRUENAS_USER>@<TRUENAS_IP> "echo '<TRUENAS_PASSWORD>' | sudo -S midclt call sharing.nfs.query"
```

**Important:**
- All credentials live in the credential store `.github/instructions/secrets.enc.yaml`
- Replace placeholders: `<TRUENAS_USER>`, `<TRUENAS_IP>`, `<TRUENAS_PASSWORD>`
- Never commit actual credentials - always use placeholders in documentation
- The credential store is committed, not gitignored: it is SOPS+age encrypted, so values are ciphertext and only key names stay readable

### midclt -j job methods (TrueNAS 25.10 caveat)
On TrueNAS **25.10.0.1**, `midclt call -j <job-method>` (job-based methods such as `pool.dataset.create` / `pool.dataset.delete`) runs the job **successfully server-side**, but the midclt client then crashes while polling the already-finished job:

```
TypeError: unhashable type: 'dict'
```

The client **exits non-zero** even though the operation completed. Automation that trusts the client exit code will mistake a success for a failure. During #57 / PR #122 this caused a successful `pool.dataset.create` to leave an orphaned empty dataset whose passphrase was never captured; it was recovered by destroying it and cleanly re-creating, **printing the passphrase to stdout BEFORE the midclt call**.

Notes:
- **No `@file` payload expansion.** midclt does not read payloads from files — pass the payload as an inline positional JSON string:
  ```bash
  ssh <TRUENAS_USER>@<TRUENAS_IP> "echo '<TRUENAS_PASSWORD>' | sudo -S midclt call -j pool.dataset.create \"\$PAYLOAD\""
  ```
- **Treat post-completion midclt `TypeError`s as cosmetic.** ALWAYS verify the real outcome with `zfs get` / `zfs list` rather than the client exit code:
  ```bash
  ssh <TRUENAS_USER>@<TRUENAS_IP> "echo '<TRUENAS_PASSWORD>' | sudo -S zfs list <dataset>"
  ```
- For passphrase-bearing creates, **print the passphrase before the call** so a client crash cannot lose it.

### Before destroying a named ZFS snapshot (disk-reclaim guard)

A disk-reclaim pass frees space by destroying snapshots. Snapshots with a **deliberate name** (`@pre-<something>`, `@before-<issue>`) are usually somebody's rollback plan, and that plan may live in an open issue - not in the snapshot's own metadata. Destroying one early silently removes another issue's safety net.

Check open issues **before** the destroy:
```bash
SNAP='pool1/dataset01@pre-unify-issue195'
gh issue list --repo SpyrosPsarras/epaflix --state open --search "${SNAP##*@}"   # snapshot short name
gh issue list --repo SpyrosPsarras/epaflix --state open --search "${SNAP%@*}"    # dataset name
```
- **Hit** - read the referencing issue. Destroy only if its own stated gates (soak window elapsed, migration done, dependent app moved off) are already met, or the owner signs off. Record which gates you checked in the reclaim issue.
- **No hit** - destroy.

Verify what you are about to remove first (`zfs list -t snapshot -o name,used,creation <dataset>`); `used` is the space that actually comes back.

Background: #444 (pool1 reclaim) destroyed `pool1/dataset01@pre-unify-issue195` while open teardown #247 still named that exact snapshot as its rollback. Both of #247's retention gates happened to be met already, so nothing broke - but that was a coincidence, not a checked precondition (#515).

### NFS export allow-lists (authoritative record)

The live allow-lists live only in the TrueNAS config DB - **this table is the repo's record of what they are supposed to be**. Re-check with `midclt call sharing.nfs.query` (and `sudo cat /etc/exports` for the generated squash flags) before assuming. **No export may have an empty `hosts` list** - empty means world, any host on `192.168.10.0/24`.

| id | path | allowed hosts | squash (anonuid) | real consumer |
|----|------|---------------|------------------|---------------|
| 25 | `/mnt/pool1/dataset01/VMs` | `.10 .11` | `all_squash` → **root (0)** | PVE storage `VMs` on both Proxmox hosts, mounted at `/mnt/pve/VMs`. Dir is `root:root drwxrwx---`, so mapall root is load-bearing |
| 26 | `/mnt/pool1/dataset01/ISOs` | `.10 .11` | `all_squash` → `libvirt-qemu` (986) | PVE storage `ISOs`, defined on both hosts but currently `disable`d. No live mount |
| 27 | `/mnt/apps/code-server` | `.25` | `all_squash` → `libvirt-qemu` (986) | VM 1025 `vscode-tunnel` only. No live mount (VM unreachable). Holds a home dir incl. `.ssh` |
| 29 | `/mnt/pool1/k3s-containers-backup` | `.10 .11` | `all_squash` → `nobody` (65534) | **none - deprecated, empty** (#699 tracks removing it). Squashed to `nobody` against a `libvirt-qemu drwxrwx---` dir ⇒ effectively no access |
| 31 | `/mnt/apps/k3s-containers` | `.51` | `all_squash` → `apps` (568) | `k3s-master-51` fstab mount at `/mnt/k3s-containers`. A Proxmox target, **not** for k3s use - no PV, no pod hostPath |
| 32 | `/mnt/pool1/dataset01` | the 7 k3s nodes: `.51 .52 .53 .61 .62 .63 .65` | `all_squash` → `apps` (568) | all media pods |
| 33 | `/mnt/apps/odysseus-bastion` | `.61 .62 .63 .65 .43` | `all_squash` → `apps` (568) | odysseus pod + bastion VM |

Share 32 was unrestricted until #537. It carries all media plus `backups/sonarr2`, whose zip contains `config.xml` (API key) and `sonarr.db` (indexer credentials, download-client passwords), so any host on `192.168.10.0/24` could mount and read it.

Shares 25, 26, 27, 29 and 31 were unrestricted until #680. Two notes from that sweep:

- **`all_squash` with `anonuid=0` is worse than no squash at all.** Share 25 squashed *every* client identity to **root** - so while it was world-exported, any LAN host had unconditional root write to the Proxmox VM/backup dir. Share 29, which #680 called out as the root-write one, actually had **no** squash options ⇒ the kernel default `root_squash` applied and root was already mapped to `nobody`. When judging exposure, read `/etc/exports`, not the `maproot`/`mapall` fields alone.
- **Squash is not a substitute for an allow-list, and an allow-list is not a substitute for squash.** Fix both.

Both masters and workers need share 32: workers mount the dataset root at `/mnt/k3s-media`, masters mount the per-directory paths (`/mnt/k3s-movies`, `/mnt/k3s-tvshows`, `/mnt/k3s-animes`, `/mnt/k3s-downloads`). **Adding a new k3s node means adding its IP here**, or its media mounts fail.

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP> "midclt call sharing.nfs.update 32 '{\"hosts\": [\"192.168.10.51\", ...]}'"
```

**Verification gotcha - a successful `mount` does NOT mean the restriction failed.** Under NFSv4 the server publishes parents of any export in the v4 **pseudo-filesystem**. A denied client can still `mount` such a path and get a pseudo-fs node - it just cannot list anything it is not allowed. Checking `mountpoint -q` alone gives a false pass. Two shapes of denial, both correct:

- `mount` returns **0** and `ls` returns `Permission denied` - path sits under a pseudo-fs parent (`/mnt/pool1/dataset01`, `/mnt/pool1/dataset01/VMs`).
- `mount` fails with `reason given by server: No such file or directory` - no pseudo-fs path leads there (`/mnt/apps/*`).

Always verify by **listing**, never by mount status, and test both directions - an allowed host must still list:

```bash
# NEGATIVE - from a host NOT on that share's allow-list (pick one per share; .177 workstation works for all)
ssh ubuntu@<denied-host> "sudo mount -t nfs4 -o ro,soft,timeo=50,retrans=1 192.168.10.200:<export> /tmp/t
  sudo ls /tmp/t   # expect: Permission denied  (or the mount itself fails)
  sudo umount -f /tmp/t"

# POSITIVE - from an allowed consumer, against its real mountpoint
ssh root@192.168.10.10 "ls /mnt/pve/VMs && pvesm status --storage VMs"
ssh ubuntu@192.168.10.51 "ls /mnt/k3s-containers"
```

Tightening an export is safe for clients that stay on the list - existing NFSv4 mounts are not dropped. Enumerate the real client set first, or you will cut off a mount you did not know about:

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP> "sudo ls /proc/fs/nfsd/clients/ | while read c; do sudo grep address /proc/fs/nfsd/clients/\$c/info; done"
```

That lists client IPs but not which export each one uses, so confirm per host with `mount -t nfs,nfs4 | grep <TRUENAS_IP>`.

# TrueNAS Hardware Overview
- The TrueNAS server is an workstation with 32GB of RAM, 3 SSD disks on RAIDZ1 with a dataset apps and 2 HDD disks on device GUIDs making a stripe vdev with a dataset pool1. The media files are stored on the pool1 dataset and the VMs are stored on the apps dataset. The TrueNAS server is connected to the switch with 1 GiB ethernet. SSH access is available with passwordless authentication using SSH keys. The TrueNAS server is also connected to the Proxmox VE servers via iSCSI targets for VM storage and NFS shares for shared storage. All credentials are stored in the credential store `.github/instructions/secrets.enc.yaml`.

The credential store `.github/instructions/secrets.enc.yaml` is a flat
`key: value` file: values are encrypted, key names stay in cleartext, so the
committed file doubles as an index of which credentials exist. The TrueNAS keys
are:

- `truenas_admin_username`
- `truenas_admin_password`

Read one key at a time. Never decrypt the whole file, never echo the value:

```bash
VALUE=$(sops -d --extract '["truenas_admin_password"]' .github/instructions/secrets.enc.yaml)
echo "${#VALUE}"   # a length is safe to print; the value is not
```
