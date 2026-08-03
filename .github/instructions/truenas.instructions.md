---
applyTo: "0-truenas/**"
description: "Instructions for TrueNAS setup"
---
# TrueNAS-Specific Instructions

When working with files in the `0-truenas/` directory, follow these TrueNAS-specific guidelines.

**Credential Placeholders:**
All commands use placeholders for sensitive information. Replace with values from `.github/instructions/secrets.yml`:
- `<TRUENAS_USER>` → truenas_admin_username
- `<TRUENAS_PASSWORD>` → truenas_admin_password
- `<TRUENAS_IP>` → TrueNAS server IP address

## Quick Actions Reference

### Access TrueNAS via SSH
```bash
# Connect using credentials from secrets.yml
# Username: truenas_admin_username
# Password: truenas_admin_password (for sudo operations)
# Host IP: Defined in TrueNAS configuration

ssh <TRUENAS_USER>@<TRUENAS_IP>

# For sudo commands, use password from secrets.yml:
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
- All credentials stored in `.github/instructions/secrets.yml`
- Replace placeholders: `<TRUENAS_USER>`, `<TRUENAS_IP>`, `<TRUENAS_PASSWORD>`
- Never commit actual credentials - always use placeholders in documentation
- The secrets file is gitignored but exists in the repository locally

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

### NFS export allow-lists (live-only state)

NFS shares are **not codified anywhere in this repo** - they exist only in the TrueNAS config DB. Treat the list below as the record of what the allow-lists are supposed to be, and re-check it with `midclt call sharing.nfs.query` before assuming.

| id | path | allowed hosts |
|----|------|---------------|
| 32 | `/mnt/pool1/dataset01` | the 7 k3s nodes: `.51 .52 .53 .61 .62 .63 .65` |
| 33 | `/mnt/apps/odysseus-bastion` | `.61 .62 .63 .65 .43` |
| 25, 26, 27, 29, 31 | VMs, ISOs, code-server, k3s-containers-backup, k3s-containers | **unrestricted** - any LAN host |

Share 32 was unrestricted until #537. It carries all media plus `backups/sonarr2`, whose zip contains `config.xml` (API key) and `sonarr.db` (indexer credentials, download-client passwords), so any host on `192.168.10.0/24` could mount and read it.

Both masters and workers need share 32: workers mount the dataset root at `/mnt/k3s-media`, masters mount the per-directory paths (`/mnt/k3s-movies`, `/mnt/k3s-tvshows`, `/mnt/k3s-animes`, `/mnt/k3s-downloads`). **Adding a new k3s node means adding its IP here**, or its media mounts fail.

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP> "midclt call sharing.nfs.update 32 '{\"hosts\": [\"192.168.10.51\", ...]}'"
```

**Verification gotcha - a successful `mount` does NOT mean the restriction failed.** Under NFSv4, any child path that is still world-exported (here `/VMs` and `/ISOs`, shares 25/26) forces the server to publish the parent in the v4 **pseudo-filesystem**. A denied client can still `mount` `/mnt/pool1/dataset01` and get a pseudo-fs node - it just cannot see anything except the child exports it is allowed. Checking `mountpoint -q` alone gives a false pass.

Always verify by **listing and reading**, from a host that is not on the allow-list:

```bash
ssh root@192.168.10.10 "mount -t nfs4 -o ro,soft 192.168.10.200:/mnt/pool1/dataset01 /tmp/t
  ls /tmp/t          # expect ONLY the still-world-exported children (ISOs, VMs)
  ls /tmp/t/backups  # expect: No such file or directory
  umount -f /tmp/t"
```

Tightening an export is safe for clients that stay on the list - existing NFSv4 mounts are not dropped. Enumerate the real client set first, or you will cut off a mount you did not know about:

```bash
ssh <TRUENAS_USER>@<TRUENAS_IP> "sudo ls /proc/fs/nfsd/clients/ | while read c; do sudo grep address /proc/fs/nfsd/clients/\$c/info; done"
```

That lists client IPs but not which export each one uses, so confirm per host with `mount -t nfs,nfs4 | grep <TRUENAS_IP>`.

# TrueNAS Hardware Overview
- The TrueNAS server is an workstation with 32GB of RAM, 3 SSD disks on RAIDZ1 with a dataset apps and 2 HDD disks on device GUIDs making a stripe vdev with a dataset pool1. The media files are stored on the pool1 dataset and the VMs are stored on the apps dataset. The TrueNAS server is connected to the switch with 1 GiB ethernet. SSH access is available with passwordless authentication using SSH keys. The TrueNAS server is also connected to the Proxmox VE servers via iSCSI targets for VM storage and NFS shares for shared storage. All credentials are stored in `.github/instructions/secrets.yml`.

The `secrets.yml` file has the following structure:
```yaml
truenas_admin_username: "<username>"
truenas_admin_password: "<password>"
```
