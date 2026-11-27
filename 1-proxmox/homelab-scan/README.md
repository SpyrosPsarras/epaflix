# homelab-scan

Read-only host scanner for the Homelable homelab index. The nightly `homelab-sync` Job in namespace `homelable` connects with the scan key, and the key's forced command runs this script. It prints one JSON document and takes no input: it ignores its arguments, stdin and `SSH_ORIGINAL_COMMAND`.

Installed as `/usr/local/sbin/homelab-scan` (mode 0755, owner root) on every host in the allowlist of `docs/superpowers/specs/2026-11-19-rackpeek-homelab-index-design.md`.

On TrueNAS `/usr` is read-only, so it lives at `/root/homelab-scan` and the forced command is `sudo -n python3 /root/homelab-scan`.

## What it reads

- OS: `/etc/os-release`, kernel, uptime.
- Hardware: `dmidecode` (system, CPU, memory), `lsblk -J`, `ip -j addr`.
- Packages: `dpkg-query`; config files whose md5 differs from the record in `/var/lib/dpkg/status`; files under `/etc` that no package owns.
- Files: `/usr/local`, `/etc/systemd/system`, cron dirs and crontabs, `/etc/fstab`, `/etc/hosts`, sysctl, network config, everything under `/root`, and `/etc/pve` on Proxmox. At most 5000 files per folder.
- Proxmox: `pvs`, `lvs`, `zpool status -P`. The online node whose name sorts first also reads the cluster with `pvesh`: resources, every guest's config and storage.
- TrueNAS: pools, datasets, NFS shares, iSCSI extents and apps through `midclt`.

`/var/lib` is never read.

## Masking

Masking happens here, before anything leaves the host.

1. Name only (name, size and mtime, no hash, no content): `/etc/ssh`, `/etc/pve/priv`, `/etc/rancher`, `/etc/proxmox-backup`, `/etc/proxmox-datacenter-manager`, `/etc/pihole`, `/etc/wireguard`, `/etc/ssl/private`, `/etc/shadow*`, `/etc/gshadow*`, `*.key`, `*.pem`, `*.pw`, `id_*`, and every dotfile and dotdir under `/root`.
2. Binary files: name and sha256 of the raw file.
3. Text files: sha256 of the raw file. Text up to 64 KiB is sent after masking: a value whose key contains `pass`, `pwhash`, `secret`, `token`, `key`, `credential` or `auth` becomes `***`, and so do the password part of a URL, the value after a command-line flag such as `--cipassword`, and URL path segments of 32 or more token characters (webhook tokens). `.git` folders are skipped.

The sync runs gitleaks over everything again before it stores anything.

## Access

The scan key goes into the SSH user's `authorized_keys` with:

```text
restrict,from="<k3s node addresses>",command="/usr/local/sbin/homelab-scan" ssh-ed25519 ... homelab-scan
```

For a user that is not root, the command is `sudo -n /usr/local/sbin/homelab-scan`, and `/etc/sudoers.d/homelab-scan` allows only that file:

```text
<user> ALL=(root) NOPASSWD: /usr/local/sbin/homelab-scan
```

## Tests

```sh
python3 -m unittest 1-proxmox/homelab-scan/test_homelab_scan.py -v
```
