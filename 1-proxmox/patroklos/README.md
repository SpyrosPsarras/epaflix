# Patroklos

Reinstall notes for `patroklos.epaflix.com`, `192.168.10.12`.

## Hardware

- Dell PowerEdge R730xd, 2x Xeon E5-2650 v3, 128 GB RAM.
- PERC H730 Mini, firmware `25.5.9.0001`, in HBA mode.
- rNDC Intel X520, 2x 10G SFP+ with `ixgbe`, and I350, 2x 1G with `igb`.
- BIOS `2.19.0`, iDRAC8 `2.86.86.86`.
- iDRAC static address `192.168.10.4`, DNS `idrac-r730xd.epaflix.lan`.

## Install and boot

Install Proxmox VE with ZFS RAID1 `rpool` on the two 120 GB SATA SSDs in rear bays 12 and 13. Use UEFI boot with systemd-boot through `proxmox-boot-tool`.

For the serial console, SSH to iDRAC and run `console com2`. Leave with `Ctrl+\`. The host console is `ttyS0` at 115200 baud.

Set these BIOS attributes through iDRAC:

| Attribute | Value |
| --- | --- |
| `BootMode` | `Uefi` |
| `ProcX2Apic` | `Enabled` |
| `IoatEngine` | `Enabled` |
| `SriovGlobalEnable` | `Enabled` |

Set PERC HBA mode over iDRAC SSH:

```sh
racadm set Storage.Controller.1.RequestedControllerMode HBA
racadm jobqueue create RAID.Integrated.1-1 -s TIME_NOW -r forced
```

Redfish/SCP import of `RequestedControllerMode` fails on this firmware with "Invalid Attribute Name". Use the commands above.

## Prevent the I350 machine-check crash

On kernels 6.17 and newer, loading `igb` for the onboard I350 resets the server. iDRAC logs "CPU 1 machine check error detected" and Intel ME `PWR2262` / `CPU0000`. Proxmox forum thread [186063](https://forum.proxmox.com/threads/186063/) reports the same on R730/R630 hosts and a BIOS workaround that enables X2APIC, I/OAT and SR-IOV Global.

The root cause found on this host was PCI resource reallocation. With SR-IOV Global disabled, as it was when the server arrived, the X520 SR-IOV VF BARs stay unassigned. The kernel then enables PCI resource reallocation itself. It releases the I350 bridge window behind root port `00:1c.4` at `0x91a00000-0x91bfffff` and reassigns it to `0x92200000-0x924fffff`. When `igb` touches the device at the new address, it triggers the machine check.

Add `pci=realloc=off` to `/etc/kernel/cmdline`, then run:

```sh
proxmox-boot-tool refresh
```

The current complete `/etc/kernel/cmdline` is:

```text
root=ZFS=rpool/ROOT/pve-1 boot=zfs console=tty0 console=ttyS0,115200 pci=realloc=off
```

The fix does not depend on those three BIOS settings. With X2APIC, I/OAT and SR-IOV all Disabled, the host booted, `igb` loaded and no machine check occurred. A full BIOS reset also sets `BootMode` back to `Bios`, and this UEFI install does not boot that way. After a BIOS reset, set `BootMode` to `Uefi` again. `pci=norom` did not help. Keep the BIOS settings enabled as a second layer.

## Network and cluster

Use the same layout as the other two cluster hosts:

| Port | Bridge | Address | Use |
| --- | --- | --- | --- |
| 10G `nic0` | `vmbr0` | `192.168.10.12/24`, gateway `192.168.10.1` | LAN, corosync link 1 |
| 10G `nic1` | `vmbr1` | `10.0.0.12/24` | Corosync link 0, migration |

The onboard 1G I350 ports are unused. This host is a member of the `e-pavli` cluster, with three nodes and no QDevice. Storage `local-zfs`, backed by `rpool/data`, is restricted to `patroklos`.

## Re-sector used SAS drives

Used SAS drives from storage arrays can have 4160- or 520-byte sectors. The PERC in HBA mode does not expose these drives to Linux, so there is no `/dev/sdX`. The HGST HC520 `HUH721212AL4200` drives here arrived with 4160-byte sectors.

`mrscsi.py` sends SCSI commands through the `megaraid_sas` MFI pass-through ioctl, the same path as `smartctl -d megaraid,N`. Its hardware-free check, run from this directory, is:

```sh
python3 mrscsi.py selftest
```

Run the following commands as root on the host, from this directory. `0` is the `megaraid_sas` SCSI host number. The comma-separated values are PERC device IDs, the same IDs as `smartctl -d megaraid,N`. Check `block_len` first:

```sh
python3 mrscsi.py cap 0 0,1,2,3
```

> **Warning:** formatting destroys all data on the selected drives. Confirm the device IDs before proceeding.

Replace `<ids>` with the comma-separated IDs to format:

```sh
python3 mrscsi.py format 0 <ids>
python3 mrscsi.py progress 0 <ids>
```

The format action sends MODE SELECT(10) with a 4096-byte block descriptor, then FORMAT UNIT with IMMED. During formatting, progress shows sense key `2`, ASC `04`, ASCQ `04`, meaning "format in progress", and a percentage.

A 12 TB HC520 takes about 14 hours, about 1.15% per 10 minutes. All drives format in parallel. Do not power off during a format. After it finishes, reboot so the PERC exposes the drives, then run a SMART long test.
