# Truenas setup to help host proxmox iscsi and k3s cluster

This project contains infrastructure configuration for Truenas and how Truenas is configured to help Proxmox virtual machines host the k3s.

## Storage

### Apps
Apps is a pool that has a VDEV in RAIDZ1, and it consists of three SSD disks 250GB each.

### Pool1
Pool1 is a pood that has a VDEV in device GUIDs and it consists of two mechanical disks 10 and 14TB.

`pool1/dataset01` holds both the media library and the qBittorrent downloads, so
space on it cannot be reasoned about with `du`: Sonarr and Radarr import by
hardlink, which means one set of blocks carries two names. On 2026-08-10 `du -sh`
on the qBittorrent temp directory reported 703 G where only 2.4 G was reclaimable.
The correct measurement and its current output live in
[`2-k3s/08.servarr/README.md` → Measuring the qBittorrent temp directory](../2-k3s/08.servarr/README.md#measuring-the-qbittorrent-temp-directory-842) (#842).
For dataset-level numbers trust `zfs list` AVAIL, not `zpool` CAP (#444).

### Encrypted backups dataset
`apps/encrypted-backups` is a ZFS-native-encrypted child of the `apps` pool and
its own encryption root (`keylocation=prompt`). It holds the SOPS cluster
age-key backup at
`/mnt/apps/encrypted-backups/sops-age-backup/k3s-cluster.txt`. Because the key
location is `prompt`, the dataset comes up **locked after every reboot** and must
be manually unlocked before that backup is readable. The unlock procedure (the
single source of truth) lives in
[`.github/instructions/sops.instructions.md` → Post-reboot: unlock the TrueNAS encrypted backup dataset](../.github/instructions/sops.instructions.md#post-reboot-unlock-the-truenas-encrypted-backup-dataset).

### Periodic snapshots, and why a manual "Run Now" leaks forever (#843)

Snapshot tasks, live on 2026-08-10:

| id | dataset | schedule | naming schema | lifetime | enabled |
|----|---------|----------|---------------|----------|---------|
| 1 | `apps` (recursive, excludes `apps/iscsi-masters`, `apps/iscsi-workers`) | 00:00 | `auto-%Y-%m-%d_%H-%M` | 2 WEEK | yes |
| 2 | `apps/iscsi-workers` | 00:00 | `auto-%Y-%m-%d_%H-%M` | 1 HOUR | no |
| 3 | `apps/iscsi-masters` | 00:00 | `auto-%Y-%m-%d_%H-%M` | 1 HOUR | no |
| 4 | `pool1/dataset01` (recursive) | 03:30 | `auto-%Y-%m-%d_%H-%M` | 3 DAY | yes |

**The mechanism.** TrueNAS prunes through `zettarepl`, and a task only deletes a
snapshot it *owns*. Ownership is not "the name matches the schema" - it is "the
timestamp parsed out of the name falls on the task's schedule".
`/usr/lib/python3/dist-packages/zettarepl/snapshot/task/snapshot_owner.py`:

```python
def owns_snapshot(self, dataset: str, parsed_snapshot_name: ParsedSnapshotName):
    return self.periodic_snapshot_task.schedule.should_run(parsed_snapshot_name.datetime)
```

and in `zettarepl/retention/calculate.py` a snapshot is only destroyed when
`snapshot_owners` is non-empty. So a snapshot with **no** owner is never
considered for retention at all - it is not "kept", it is invisible.

A snapshot created by pressing **Run Now** in the UI is stamped with the
wall-clock minute. `pool1/dataset01@auto-2026-08-01_12-37` matched
`auto-%Y-%m-%d_%H-%M` but `12:37` is not `03:30`, so no task owned it, and it
sat there at 6 days old under a 3 DAY retention holding 36.5 G. It was
destroyed during the #609 reclaim; the behaviour that produced it was not
changed until this note.

**The convention.** Never take a manual snapshot under a task's naming schema.
A hand-made snapshot is permanent by definition, so name it so that reads as
permanent and so that no pruner will ever silently claim it:

```bash
ssh truenas_admin@192.168.10.200
# <reason> is short, lowercase, hyphenated - and must be an issue number when
# the snapshot is a rollback target: manual-2026-08-10-issue843-pre-upgrade
sudo zfs snapshot -r pool1/dataset01@manual-$(date +%Y-%m-%d)-<reason>
```

Rules for the `manual-` prefix:

- It does not match any task's naming schema, so no pruner will ever destroy it
  and no pruner will ever be blamed for keeping it. It is yours until you remove it.
- Whoever creates it owns removing it. If it backs a rollback plan, name the
  issue in the snapshot name and destroy it when that issue closes.
- Before destroying any snapshot, grep the open issues for its name and confirm
  nothing still lists it as a rollback target. #515 destroyed one an open issue
  needed.

Do not use TrueNAS "Run Now" on a periodic snapshot task. It produces a snapshot
under the task's schema that the task will not prune.

**The check**, because this class fails silently and nothing reports it:

```bash
scp 0-truenas/scripts/snapshot-retention-audit.py truenas_admin@192.168.10.200:/tmp/
ssh truenas_admin@192.168.10.200 'sudo python3 /tmp/snapshot-retention-audit.py'
```

It reuses the box's own `zettarepl` decision functions (`belongs_to_tree`,
`CronSchedule.should_run`, `parse_snapshot_name`, `calculate_snapshots_to_remove`)
rather than reimplementing them, so it cannot drift from the real pruner. Exit 0
is clean, exit 1 means at least one snapshot is leaking. It reports three cases:

- `ORPHAN` - name matches the schema, timestamp does not fall on the schedule.
  This is the #843 class, and it is what a manual "Run Now" produces.
- `LAST` - owned and expired, but kept because the pruner refuses to destroy the
  only snapshot left for a naming schema. Expected, not a fault.
- `DISABLED` - the only task covering the dataset is disabled, so nothing prunes
  its snapshots.

Real output, 2026-08-10 (26 snapshots leaking, 0.37 G, none on
`pool1/dataset01`):

```
apps/ix-apps/docker@auto-2025-10-13_19-41       0.26G  id=1  300d>2W  ORPHAN timestamp does not fall on the task schedule
apps/ix-apps/docker@auto-2025-10-14_08-29       0.06G  id=1  300d>2W  ORPHAN timestamp does not fall on the task schedule
... 18 more ORPHAN rows from the same two 2025-10 manual runs ...
apps/iscsi-masters@auto-2026-02-25_00-00        0.00G  id=3  166d>1H  DISABLED its only task is disabled, nothing prunes it
... 5 more DISABLED rows on apps/iscsi-{masters,workers} ...

26 snapshot(s) leaking, 0.37G pinned
```

`pool1/dataset01` is clean: exactly the three 03:30 snapshots the 3 DAY
retention should hold (`auto-2026-08-08_03-30`, `auto-2026-08-09_03-30`,
`auto-2026-08-10_03-30`), `usedbysnapshots` 106 G, `available` 3.11 T.

Nothing was destroyed to produce that output. The 26 are tracked separately -
they are on `apps`, they total 0.37 G, and destroying `ix-apps` migration
snapshots is an owner decision, not a side effect of a doc change.

Related: #609 (where this surfaced), #563 (the retention decision), #311
(created task 4), #515 (why you grep the issues first).

## ISCSI targets

Each VM should have its own ISCSI target on the Truenas Server and the targets should be attached to both proxmox servers. The reasoning behind that is that if one HPE server goes down, the other one could take over the VMs for the k3s cluster. There is not going to be any HA on the proxmox level, but its good to have the option.

## NFS targets

The K3s servarr media is now served by **ONE unified NFS export** of the parent
directory `/mnt/pool1/dataset01` (export **id 32**, mapped to user/group apps
`uid=568(apps) gid=568(apps) groups=568(apps)`). All five servarr media pods
(qbittorrent / sonarr / sonarr2 / radarr / cleanuparr) mount this single export
at a single `/media` mount, with downloads + library living as subdirectories
(`/media/{downloads,tvshows,animes,movies}`) on **one on-wire fsid** so
intra-export `link()` works and imports hardlink (`nlink>=2`) instead of landing
as `nlink=1` copies — see issue #195 and `2-k3s/08.servarr/RECOVERY-newtarr-cleanuparr.md`.

- /mnt/pool1/dataset01 (unified export, id 32) — the live media export

The four old per-directory child exports (`/mnt/pool1/dataset01/{animes,
downloads,movies,tvshows}`) are gone. They were kept temporarily as the
soak-window rollback path, then torn down once bazarr/lingarr migrated to the
unified `/media` mount and the soak elapsed (issue #247, PR #514). Export id
32 is the only export for `pool1/dataset01`.

The rest of the NFS targets are proxmox targets and should not be used by the k3s.
Every one of them is host-restricted since #680 - none is world-exported any more.
The authoritative allow-list table (per share: allowed hosts, squash uid, real
consumer) lives in `.github/instructions/truenas.instructions.md` under
"NFS export allow-lists". Update it whenever an export changes.

- /mnt/pool1/dataset01/VMs (id 25) — Proxmox `.10 .11`
- /mnt/pool1/dataset01/ISOs (id 26) — Proxmox `.10 .11`, storage currently disabled
- /mnt/apps/code-server (id 27) — VM 1025 `vscode-tunnel` (`.25`) only
- /mnt/apps/k3s-containers (id 31) — `k3s-master-51` fstab mount only
- /mnt/pool1/k3s-containers-backup (id 29) — deprecated, no consumer, squashed to `nobody` (#699)

## Network

Truenas is using IP 192.168.10.200 and is using a 1GiB ethernet cable. To access the truenas via ssh, if the user logged in the hostOS is spy, then the keys are located in ssh folder and with ssh truenas_admin@192.168.10.200 you can access passwordless. The password for sudo operations is not going to be provided in public files.

## GPU (RTX 2070 SUPER)

One NVIDIA RTX 2070 SUPER at PCI `01:00.0`, driver `570.172.08`, 8 GiB VRAM. It is
**not** isolated for VM passthrough (`isolated_gpu_pci_ids` is empty), so it is shared
by the TrueNAS apps that ask for it.

| App | Uses it for |
|---|---|
| `ix-ollama` | LLM inference, port `30068` |
| `ix-jellyfin` | hardware transcode |

### The 2026-08-08 failure - why the guards below exist

The card wedged itself and **nothing said so for ~23 hours** (2026-08-08 13:23 ->
2026-08-09 12:29). ARC had grown to fill a 31 GB box, the driver hit
`NV_ERR_NO_MEMORY` part-way through initialising the GSP firmware, and the
half-written WPR2 region left the card unusable:

```
NVRM: ... Check failed: Out of memory [NV_ERR_NO_MEMORY]
NVRM: _kgspBootGspRm: unexpected WPR2 already up, cannot proceed with booting GSP
NVRM: GPU 0000:01:00.0: RmInitAdapter failed!
```

That pair of lines is the recurrence signature. Once WPR2 is stuck, only a reset
clears it - a warm reboot may not.

The outage was silent because **everything kept answering**. `nvidia-smi` inside the
container said "No devices were found", but ollama still served requests at 2.97 tok/s
on CPU instead of 67.8 tok/s on the GPU, Jellyfin transcoded on CPU, and netdata
reported nothing. A human noticing slow chat replies is what found it.

### Recovery (no reboot needed)

A PCIe function-level reset cleared WPR2 with the NAS staying up. A cold power cycle
is the fallback.

```bash
# 1. release the device - nothing may hold /dev/nvidia* during the reset
sudo midclt call app.stop ollama
sudo docker stop ix-jellyfin-jellyfin-1
sudo fuser -v /dev/nvidia*          # must print nothing

# 2. unload the driver stack, innermost last
sudo rmmod nvidia_uvm nvidia_drm nvidia_modeset nvidia
lsmod | grep -c nvidia              # expect 0

# 3. function-level reset, then reload
echo 1 | sudo tee /sys/bus/pci/devices/0000:01:00.0/reset
sudo modprobe nvidia
nvidia-smi                          # expect the GPU line, 0 MiB used

# 4. bring the consumers back
sudo docker start ix-jellyfin-jellyfin-1
sudo midclt call app.start ollama
```

Proof it worked, rather than "no errors": ollama went back to **74.9 tok/s** with
`size_vram = 6241480867` (model fully in VRAM, it was `0` while broken), and zero new
`NVRM` lines appeared after the reset.

### The three guards

The reset fixed the symptom. The cause was no memory headroom at driver-init time, so
all three of these are in place:

| Guard | Mechanism | Check it |
|---|---|---|
| ARC capped to 12 GiB | ZFS tunable + `/etc/modprobe.d/zfs.conf` for boot | `awk '/^c_max/ {print $3}' /proc/spl/kstat/zfs/arcstats` -> `12884901888` |
| Driver stays initialised | `nvidia-persistenced`, started at POSTINIT by an init/shutdown script running `scripts/gpu-persistenced.sh` | `nvidia-smi --query-gpu=persistence_mode --format=csv,noheader` -> `Enabled` |
| Loss is alerted | `scripts/gpu-health-check.sh`, cron every 15 min -> ntfy `https://ntfy.epaflix.com` topic `truenas-alerts` | `sudo midclt call cronjob.query \| jq '[.[]\|select(.command\|contains("gpu"))]'` |

Persistence mode is the actual fix for the failure mode: it keeps the driver loaded and
initialised even when no client holds the device, so the driver never has to re-init
under memory pressure - which is exactly when it died. `OLLAMA_KEEP_ALIVE` is
deliberately left at its 5 min default; persistence makes it irrelevant.

### Deploying the scripts

Both live in `scripts/` here and are deployed to `/root/` on the host. They are
idempotent - re-running either is safe.

```bash
scp 0-truenas/scripts/gpu-*.sh truenas_admin@192.168.10.200:/tmp/
ssh truenas_admin@192.168.10.200 \
  'sudo install -m 0750 -o root -g root /tmp/gpu-persistenced.sh  /root/ && \
   sudo install -m 0750 -o root -g root /tmp/gpu-health-check.sh  /root/ && \
   sudo rm -f /tmp/gpu-*.sh'
```

There is no `nvidia-persistenced.service` on TrueNAS SCALE, which is why the POSTINIT
init/shutdown script exists instead of a systemd unit. Both registrations live in
middleware config (`initshutdownscript` + `cronjob`), not in a file on disk, so they
survive an update but are **not** in this repo - re-register them with
`midclt call initshutdownscript.create` / `cronjob.create` after a fresh install.

> **Subscribe to `truenas-alerts` or these alerts go nowhere.** ntfy runs with login
> disabled, so there is no server-side subscription list and nothing to register -
> subscribing is per client. Point the phone app or a browser at
> `https://ntfy.epaflix.com/truenas-alerts`. Alertmanager uses the same ntfy on topic
> `k8s-alertmanager`; host alerts are kept separate on purpose.

### Monitoring exporters (#916 / #917 / #918)

Two Prometheus exporters run on this box as TrueNAS **custom apps**, and the
cluster scrapes them. They are what replaces the 15-minute cron above.

| App | Port | Compose (tracked) | Feeds |
|---|---|---|---|
| `node-exporter` | `9100` | [`custom-apps/node-exporter/`](custom-apps/node-exporter/) | host + ZFS ARC series (`node_zfs_arc_size`, `node_zfs_arc_c_max`, `node_zfs_arc_memory_throttle_count`) → alert group `truenas-memory` |
| `nvidia-gpu-exporter` | `9835` | [`custom-apps/nvidia-gpu-exporter/`](custom-apps/nvidia-gpu-exporter/) | GPU series → alert group `truenas-gpu`, five expressions vendored from `utkuozdemir/nvidia_gpu_exporter` v1.14.0 |

Both are installed with `midclt call app.create` from the tracked compose files.
The exact command, the from-host verification with its must-fail control, and
the delete/rollback are in the `README.md` beside each compose file. The cluster
side is `2-k3s/10.observability/truenas-exporters.yaml` (selector-less Service +
static EndpointSlice + ServiceMonitor per exporter, the #917 convention) and
`2-k3s/10.observability/alertmanager-config/custom-alerts.yaml` (the rules).

**Why they survive a TrueNAS update.** Custom apps are middleware-managed and
their compose lives on the ix-apps pool dataset
(`/mnt/.ix-apps/app_configs/<app>`), so an update redeploys them. Nothing is
installed onto the immutable boot pool. An `apt`/`pip` install there is what
would not survive. What is *not* in git either way is the app **registration**
itself: like the POSTINIT `initshutdownscript` above, it lives in middleware
config, so after a fresh install re-run the `app.create` recipe from each
directory.

**The GPU exporter must run the `-nvml` image flavor.** The XID metric families
exist only on that backend, so on the plain `1.14.0` image `NvidiaGpuXidCritical`
is an expression that can never fire. See
[`custom-apps/nvidia-gpu-exporter/README.md`](custom-apps/nvidia-gpu-exporter/README.md).

**The "Loss is alerted" row of the guards table above is on its way out (#919).**
Once the Prometheus path is proven at the deploy gate, with collection-health
observed firing end-to-end into ntfy and the TargetDown path with it, the
`gpu-health-check.sh` cron is deleted (`midclt call cronjob.delete`) and the
alerting moves to Alertmanager. **The topic changes with it:** GPU alerts then
arrive on `k8s-alertmanager`, not `truenas-alerts`, so subscribe the phone to
that topic *before* the cron goes or the new path delivers into a void. Order
and evidence: `2-k3s/10.observability/README.md`, section "TrueNAS GPU + ARC
monitoring (#916-#919): deploy gate".
