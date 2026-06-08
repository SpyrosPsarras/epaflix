# Truenas setup to help host proxmox iscsi and k3s cluster

This project contains infrastructure configuration for Truenas and how Truenas is configured to help Proxmox virtual machines host the k3s.

## Storage

### Apps
Apps is a pool that has a VDEV in RAIDZ1, and it consists of three SSD disks 250GB each.

### Pool1
Pool1 is a pood that has a VDEV in device GUIDs and it consists of two mechanical disks 10 and 14TB.

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

The **four old child exports** are kept **TEMPORARILY** and will be torn down
after the soak window (follow-up issue):
- /mnt/pool1/dataset01/animes
- /mnt/pool1/dataset01/downloads
- /mnt/pool1/dataset01/movies
- /mnt/pool1/dataset01/tvshows

They are retained for two reasons: (1) bazarr + lingarr still bind the old
movies/tvshows claims pending their migration to the unified `/media` mount, and
(2) they are the soak-window rollback path (ZFS snapshot
`pool1/dataset01@pre-unify-issue195` exists). Do NOT delete them until both the
soak has elapsed AND bazarr/lingarr are migrated.

The rest of the NFS targets are proxmox targets and should not be used by the k3s.
- /mnt/apps/code-server
- /mnt/pool1/dataset01/ISOs
- /mnt/apps/k3s-containers
- /mnt/pool1/k3s-containers-backup (deprecated)

## Network

Truenas is using IP 192.168.10.200 and is using a 1GiB ethernet cable. To access the truenas via ssh, if the user logged in the hostOS is spy, then the keys are located in ssh folder and with ssh truenas_admin@192.168.10.200 you can access passwordless. The password for sudo operations is not going to be provided in public files.