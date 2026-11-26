# PDM

Proxmox Datacenter Manager for the `e-pavli` cluster and the backup server. VM 1022 `pdm` on evanthoulaki, `pdm.epaflix.com`, `192.168.10.22`. It replaced PegaProx. See `docs/superpowers/specs/2026-11-26-pdm-design.md`.

## VM

| Setting | Value |
| --- | --- |
| Storage | `local-raid`, 40 GB `scsi0`, EFI disk |
| CPU, RAM | 2 cores `host`, 4 GiB |
| NIC | virtio on `vmbr0`, MAC `BC:24:11:22:0D:22` |
| Options | `onboot 1`, `agent 1`, OVMF |

Root's password is in the vault. Root's SSH keys are the ones in evanthoulaki's `/root/.ssh/authorized_keys` at install time.

## Unattended install

Installed from `proxmox-datacenter-manager_1.1-1.iso` (SHA256 `11a55a069ba564220bd986241b57920a83781d40be18d6f2bf7b9b12696ae2cc` from `https://enterprise.proxmox.com/iso/SHA256SUMS`). Prepare the ISO on a host that has `proxmox-auto-install-assistant`:

```sh
proxmox-auto-install-assistant validate-answer answer.toml
proxmox-auto-install-assistant prepare-iso proxmox-datacenter-manager_1.1-1.iso --fetch-from iso --answer-file answer.toml
```

The non-secret answer fields:

```toml
[global]
keyboard = "en-us"
country = "no"
fqdn = "pdm.epaflix.com"
mailto = "root@localhost"
timezone = "Europe/Oslo"

[network]
source = "from-answer"
cidr = "192.168.10.22/24"
gateway = "192.168.10.1"
dns = "192.168.10.30"
filter.ID_NET_NAME_MAC = "*bc2411220d22"

[disk-setup]
filesystem = "ext4"
disk-list = ["sda"]
```

At build time, add `root-password-hashed` (`openssl passwd -6` of the vault password) and `root-ssh-keys` to `[global]` in a private copy. Never commit the completed answer file or the prepared ISO.

Create the VM on evanthoulaki, boot the prepared ISO, and remove the CD-ROM afterwards:

```sh
qm create 1022 --name pdm --memory 4096 --cores 2 --cpu host --machine q35 --bios ovmf \
  --efidisk0 local-raid:1,efitype=4m,pre-enrolled-keys=0 --scsihw virtio-scsi-single --scsi0 local-raid:40 \
  --net0 virtio=BC:24:11:22:0D:22,bridge=vmbr0 --agent 1 --onboot 1 --ostype l26 \
  --cdrom local:iso/proxmox-datacenter-manager_1.1-1-auto-from-iso.iso
qm start 1022
```

## Repositories

`pdm-no-subscription` enabled, the enterprise source disabled.

## Login

Realm `authentik` (type `openid`), issuer `https://auth.epaflix.com/application/o/pdm-sso/`, username claim `preferred_username`, no autocreate. The Authentik side is `pdm-sso` and `pdm-forwardauth` in `2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml`, both limited to the Authentik group `PDM Admins`. The client ID and secret are in the vault. Each admin is a PDM user `<authentik-username>@authentik` with `Administrator` on `/`, created before the first login, and a member of `PDM Admins` in Authentik. The group is created empty by the blueprint: after the first sync, add the admin to it in Authentik, or both Authentik apps deny the login.

If Authentik is down, log in at `https://192.168.10.22:8443` as `root@pam`.

## Remotes

| Remote | Endpoints | Token | Role |
| --- | --- | --- | --- |
| `e-pavli` (PVE) | `takaros`, `evanthoulaki`, `patroklos`, `shypowder` `.epaflix.com:8006` | `pdm@pve!pdm`, secret in the vault | `PVEVMAdmin` and `PVEAuditor` on `/` |
| `proxmox-backup` (PBS) | `192.168.10.31:8007` with the certificate fingerprint | `pdm@pbs!pdm`, secret in the vault | `Audit` on `/` |

The PVE nodes use Let's Encrypt certificates, so they need no fingerprint. The PBS certificate is self-signed. When it changes, PDM shows a fingerprint error. Read the new value with `openssl x509 -in /etc/proxmox-backup/proxy.pem -noout -fingerprint -sha256` on the PBS and update the remote under Remotes, Edit.

`PVEVMAdmin` lets PDM start, stop, migrate and snapshot guests. `PVEAuditor` lets it read nodes, storage and tasks. PDM cannot manage users, permissions, storage or packages on the cluster. `Audit` on the PBS only shows datastores, tasks and status.
