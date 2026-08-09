# Truenas setup to help host proxmox iscsi and k3s cluster

This project contains infrastructure configuration for Truenas and how Truenas is configured to help Proxmox virtual machines host the k3s.

## Storage

### Apps
Apps is a pool that has a VDEV in RAIDZ1, and it consists of three SSD disks 250GB each.

### Pool1
Pool1 is a pood that has a VDEV in device GUIDs and it consists of two mechanical disks 10 and 14TB.

### Encrypted backups dataset
`apps/encrypted-backups` is a ZFS-native-encrypted child of the `apps` pool and
its own encryption root (`keylocation=prompt`). It holds the SOPS cluster
age-key backup at
`/mnt/apps/encrypted-backups/sops-age-backup/k3s-cluster.txt`. Because the key
location is `prompt`, the dataset comes up **locked after every reboot** and must
be manually unlocked before that backup is readable. The unlock procedure (the
single source of truth) lives in
[`.github/instructions/sops.instructions.md` → Post-reboot: unlock the TrueNAS encrypted backup dataset](../.github/instructions/sops.instructions.md#post-reboot-unlock-the-truenas-encrypted-backup-dataset).

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
| Loss is alerted | `scripts/gpu-health-check.sh`, cron every 15 min -> ntfy `192.168.10.112:8091` topic `truenas-alerts` | `sudo midclt call cronjob.query \| jq '[.[]\|select(.command\|contains("gpu"))]'` |

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
> `http://192.168.10.112:8091/truenas-alerts`. Alertmanager uses the same ntfy on topic
> `k8s-alertmanager`; host alerts are kept separate on purpose.