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

# TrueNAS Hardware Overview
- The TrueNAS server is an workstation with 32GB of RAM, 3 SSD disks on RAIDZ1 with a dataset apps and 2 HDD disks on device GUIDs making a stripe vdev with a dataset pool1. The media files are stored on the pool1 dataset and the VMs are stored on the apps dataset. The TrueNAS server is connected to the switch with 1 GiB ethernet. SSH access is available with passwordless authentication using SSH keys. The TrueNAS server is also connected to the Proxmox VE servers via iSCSI targets for VM storage and NFS shares for shared storage. All credentials are stored in `.github/instructions/secrets.yml`.

The `secrets.yml` file has the following structure:
```yaml
truenas_admin_username: "<username>"
truenas_admin_password: "<password>"
```
