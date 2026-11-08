# Shypowder

Reinstall notes for `shypowder.epaflix.com`, `192.168.10.13`.

## Hardware

- Gigabyte Z170 board, Intel i5-6600K with 4 cores, 32 GB RAM.
- NVIDIA RTX 2070 SUPER with 8 GB VRAM, PCI functions `01:00.0` through `01:00.3`.
- Mellanox ConnectX MT26448, one 10G port `enp8s0` with `mlx4_en`. The Realtek 1G port is unused.
- Intenso 512 GB SATA SSD for Proxmox, single-disk ZFS `rpool`.
- Seagate 10 TB disk with single-disk ZFS pool `hdd`. It holds `hdd/data`, the Proxmox storage `local-hdd`, and [`hdd/paperless`](#paperless-storage).

## Unattended install and boot

Installed Proxmox VE 9.2 from `proxmox-ve_9.2-1.iso`, with kernel `7.0.14-26-pve`. Use UEFI boot, disable CSM and enable VT-d in the BIOS.

Prepare the installer with:

```sh
proxmox-auto-install-assistant prepare-iso proxmox-ve_9.2-1.iso --fetch-from iso --answer-file answer.toml
```

The non-secret answer fields are:

```toml
[global]
keyboard = "en-us"
country = "no"
fqdn = "shypowder.epaflix.com"
timezone = "Europe/Oslo"

[network]
source = "from-answer"
cidr = "192.168.10.13/24"
gateway = "192.168.10.1"
dns = "192.168.10.30"
filter.ID_NET_NAME_MAC = "<MELLANOX_ID_NET_NAME_MAC>"

[disk-setup]
filesystem = "zfs"
zfs.raid = "raid0"
filter.ID_MODEL = "INTENSO*"
```

Replace the network filter with the Mellanox value from `udevadm info /sys/class/net/enp8s0`. At ISO build time, fetch the password hash and SSH public keys from the vault into the private answer file as `global.root-password-hashed` and `global.root-ssh-keys`. Never commit the completed answer file or installer ISO. The example above is incomplete without those build-time fields.

Do not use an Intenso-brand USB stick for the installer. The disk filter would also match that stick. Installation overwrites the selected disk, so check that only the SATA SSD matches before starting.

## Network and cluster

This is the fourth host in the `e-pavli` cluster. It has only one 10G port, so both networks share `vmbr0` instead of using a separate `vmbr1`.

| Port | Bridge | Address | Use |
| --- | --- | --- | --- |
| 10G `enp8s0` | `vmbr0` | `192.168.10.13/24`, gateway `192.168.10.1` | LAN, corosync link 1 |
| Same port | `vmbr0` | `10.0.0.13/24` | Corosync link 0, migration |

`/etc/network/interfaces` assigns both addresses to `vmbr0`, with `bridge-ports enp8s0`, `bridge-stp off` and `bridge-fd 0`. Both corosync links share the same physical port and do not provide link redundancy.

Storage `local-zfs`, backed by `rpool/data`, allows `patroklos,shypowder`. The boot pool has no disk redundancy. ACME uses the cluster plugin `cloudflare-dns`.

## Paperless storage

Dataset `hdd/paperless`, created with `atime=off`, holds `data` and `media` subdirectories owned by `1000:1000`. Packages: `nfs-kernel-server`, `sanoid` and `prometheus-node-exporter`, installed with their recommends, which add `prometheus-node-exporter-collectors` and its smartmon and nvme timers.

`/etc/exports.d/paperless.exports` exports it to the five k3s workers only:

```text
/hdd/paperless 192.168.10.61(rw,sync,no_subtree_check,all_squash,anonuid=1000,anongid=1000)
/hdd/paperless 192.168.10.62(rw,sync,no_subtree_check,all_squash,anonuid=1000,anongid=1000)
/hdd/paperless 192.168.10.63(rw,sync,no_subtree_check,all_squash,anonuid=1000,anongid=1000)
/hdd/paperless 192.168.10.64(rw,sync,no_subtree_check,all_squash,anonuid=1000,anongid=1000)
/hdd/paperless 192.168.10.65(rw,sync,no_subtree_check,all_squash,anonuid=1000,anongid=1000)
```

`all_squash` maps every client user, root included, to `1000:1000`. Apply changes with `exportfs -ra`. Clients must mount `192.168.10.13`. A mount of `10.0.0.13` arrives from the workers' `10.0.0.6x` addresses and is refused.

`/etc/sanoid/sanoid.conf`:

```ini
[hdd/paperless]
  hourly = 24
  daily = 30
  monthly = 6
  autosnap = yes
  autoprune = yes
```

`sanoid.timer` is enabled and runs every 15 minutes.

The node exporter listens on port 9100 on all addresses and reports pool health as `node_zfs_zpool_state`. The PVE firewall is disabled, so there are no firewall rules for NFS or the exporter.

Verified on 2026-11-08:

- `k3s-worker-61` mounted the export with `nfsvers=4.2`. A file created as root in `data` got owner `1000`.
- `k3s-master-51` got `access denied by server`.
- `sanoid --cron` created `autosnap_` hourly, daily and monthly snapshots.
- `node_zfs_zpool_state{state="online",zpool="hdd"}` returned `1`.

## GPU passthrough

The host binds all four GPU functions to `vfio-pci`. The NVIDIA driver runs in the guest, not on Proxmox.

`/etc/kernel/cmdline`:

```text
root=ZFS=rpool/ROOT/pve-1 boot=zfs iommu=pt initcall_blacklist=sysfb_init
```

`/etc/modprobe.d/vfio.conf`:

```text
options vfio-pci ids=10de:1e84,10de:10f8,10de:1ad8,10de:1ad9 disable_vga=1
softdep nouveau pre: vfio-pci
softdep nvidia pre: vfio-pci
softdep xhci_pci pre: vfio-pci
softdep snd_hda_intel pre: vfio-pci
softdep i2c_nvidia_gpu pre: vfio-pci
```

`/etc/modprobe.d/blacklist-gpu.conf`:

```text
blacklist nouveau
blacklist nvidiafb
```

`/etc/modules-load.d/vfio.conf`:

```text
vfio
vfio_iommu_type1
vfio_pci
```

`/etc/modprobe.d/zfs.conf` caps ARC at 2 GiB:

```text
options zfs zfs_arc_max=2147483648
```

After restoring these files, run `update-initramfs -u -k all` and `proxmox-boot-tool refresh`, then reboot before starting the GPU guest.

## VM 1064

`k3s-worker-64` uses the Ubuntu 24.04 cloud image and cloud-init user `ubuntu`.

| Setting | Value |
| --- | --- |
| Machine and CPU | `q35`, `host`, 1 socket, 4 cores |
| Memory | `20480` MiB, `balloon: 0`. 24 GiB ran the 31 GiB host out of memory, because passthrough pins all guest RAM. |
| GPU | `hostpci0: 0000:01:00,pcie=1`, all four functions |
| Disk | `scsi0: ssd-raid:vm-1064-disk-0,discard=on,size=100G,ssd=1`, on the evanthoulaki SSD RAID 5 over iSCSI ([TrueNAS apps disk](../patroklos/README.md#truenas-apps-disk)) |
| SCSI controller | `virtio-scsi-pci` |
| Cloud-init disk | `ide2: local-zfs:vm-1064-cloudinit,media=cdrom` |
| LAN NIC | VirtIO `net0`, `bridge=vmbr0`, `192.168.10.64/24`, gateway `192.168.10.1` |
| Cluster NIC | VirtIO `net1`, `bridge=vmbr0`, `10.0.0.64/24` |
| DNS | `192.168.10.30` |
| Boot | `order=scsi0`, `onboot: 1` |
| Console and agent | `serial0: socket`, `vga: serial0`, `agent: 1` |

The guest's driver, container runtime, storage access and Kubernetes labels are in [node settings](../../2-k3s/maintenance/node-settings.md#gpu-worker). Zone labels are in [node zones](../../2-k3s/maintenance/node-zones.md).
