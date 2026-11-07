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

## Keep etcd responsive on rpool

`rpool` holds the disks of VM 1052 (k3s-master-52, an etcd member), VM 1063 (k3s-worker-63) and the TrueNAS boot disk (VM 1200 `scsi0`). The two SSDs are `VK0120GEYJP`. Until 2026-11-07 it also held the TrueNAS `apps` disk (`scsi5`); see [TrueNAS apps disk](#truenas-apps-disk).

On 2026-11-06, two Prometheus queries over the data kept since 2026-11-01 counted 21 and 18 episodes in which master-52's etcd WAL fsync p99 exceeded 0.5 s. The worst episodes reached 8 s, the histogram's top bucket. master-51 had 2 such episodes and master-53 had 3. In 16 of the 18 episodes in the 7-day query, worker-63's writes peaked at 43-128 MB/s, for example while `t3code-image-prepull` pulled the t3code image. Its normal rate is under 7 MB/s in 99% of minutes. On 2026-11-05 and 2026-11-06, etcd changed leader during these stalls, and kube-vip failed to renew its `plndr-cp-lock` lease, which holds the API VIP. In the other 2 episodes worker-63 was idle and pve-exporter has no data, so their cause is unknown. TrueNAS's `scsi5` disk, then on `rpool`, was not capped and was one candidate. It moved off `rpool` on 2026-11-07.

`zpool iostat -w rpool` was read on 2026-11-06 before any test and covers about 5.4 days since boot. It showed no disk write slower than its 268 ms bucket. Over a million sync writes waited in the ZFS sync queue for 1 s or more, some for 17-34 s. The likely mechanism: guest flushes turn worker-63's bulk writes into sync writes, and those queue ahead of etcd's fdatasync. With `zfs_dirty_data_max` at 256 MiB in both runs, setting `sync=disabled` on worker-63's disk cut the test's worst fsync from 28 s to 3.6 s, which supports this.

These commands fix the worker-63 case. The one-off trim matters on an aged pool (FRAG was 68%), because `autotrim` only trims blocks freed after it is enabled:

```sh
zpool trim -w -r 64M rpool
zpool set autotrim=on rpool
qm set 1063 --scsi0 local-zfs:vm-1063-disk-0,discard=on,format=raw,size=50G,ssd=1,mbps_wr=25
```

The 25 MB/s write cap applies only to worker-63's disk. It slows that VM's write bursts, including image pulls and any write burst from the Postgres instance there. Keep `sync=standard` on worker-63, because CNPG instance `postgres-cluster-11` runs there on a local-path volume. In November 2026 it was the primary. `sync=disabled` could lose committed transactions on a host crash.

Test with two commands at once. On master-52, run a loop that writes 4 KB with `fdatasync` every 100 ms for 120 s and records the worst latency. On worker-63, run `sudo dd if=/dev/urandom of=/var/tmp/burst.dat bs=1M count=3072 conv=fdatasync`, then delete the file. Results on 2026-11-06, one 120 s run per row except the range, which covers two runs:

| State | p99 | Worst |
| --- | --- | --- |
| No burst | 2 ms | 3 ms |
| Defaults | 26.9 s | 44.5 s |
| Trim, `mbps_wr=25` | 198-247 ms | 0.46-0.53 s |
| Final: trim, `autotrim=on`, `mbps_wr=25` | 178 ms | 0.67 s |

The final worst case is above etcd's 500 ms heartbeat interval, so a leader can send a heartbeat late. It stays below etcd's 1 s slow-fdatasync warning and well inside the 5 s election timeout. That timeout and the heartbeat are k3s defaults (`pkg/etcd/etcd.go` in v1.36.5+k3s1). All three masters' `etcd-arg` entries set only compaction and quota, so they keep these defaults. The warning threshold is `warnSyncDuration` in `server/storage/wal/wal.go` of k3s-io/etcd v3.6.14-k3s3.

These made no consistent difference beyond run-to-run noise: lowering `zfs_dirty_data_max` or `zfs_vdev_async_write_max_active` once the cap was in place, `logbias=throughput` with a 50 MB/s cap, and the `none` I/O scheduler.

## TrueNAS apps disk

The TrueNAS `apps` pool, which holds every democratic-csi iSCSI zvol, is one virtual disk: VM 1200 `scsi5`, serial `APPSSSD0`, 100G. It lives on storage `ssd-raid`, an LVM volume group on the evanthoulaki SSD RAID 5 (3 × 240 GB, P440ar logical drive 1). evanthoulaki shares that drive over iSCSI as `iqn.2026-11.com.epaflix:evanthoulaki.ssd-raid5` on `10.0.0.11:3260`. Its LIO ACL lists the initiators of shypowder and patroklos. `ssd-iscsi` and `ssd-raid` are enabled on those two nodes, and `ssd-raid` has `shared 1`. shypowder uses the same volume group for the k3s-worker-64 disk.

While evanthoulaki or the 10G link is down, the `apps` pool and every iSCSI PVC are offline. If patroklos boots while the target is unreachable, Proxmox cannot activate `scsi5`, so expect VM 1200 to stay stopped until it is back.

On 2026-11-07 the disk moved off `rpool` with `qm disk move 1200 scsi5 ssd-raid --delete 1` while TrueNAS was running. The 60G copy took 18 minutes. `rpool` went from 82.8G to 42.0G allocated.

To grow the disk:

```sh
qm disk resize 1200 scsi5 <size>     # on patroklos
sgdisk -e /dev/<dev>                 # on TrueNAS: move the backup GPT header to the new end
midclt call -j pool.expand <pool-id> # on TrueNAS: grow partition 1 and the pool
```

Replace `<dev>` with the disk whose serial is `APPSSSD0` in `lsblk -o NAME,SERIAL` (`sdf` on 2026-11-07), and `<pool-id>` with the `apps` ID from `midclt call pool.query` (`2`). Without `sgdisk -e`, `pool.expand` fails with "Could not change partition 1's type code to BF01!" and writes nothing.

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
