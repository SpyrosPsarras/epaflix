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

There are multiple targets that all the K3s VMs should be mounting. The k3s mounts should have access with user apps
`uid=568(apps) gid=568(apps) groups=568(apps)`
- /mnt/pool1/dataset01/animes
- /mnt/pool1/dataset01/downloads
- /mnt/pool1/dataset01/movies
- /mnt/pool1/dataset01/tvshows

The rest of the NFS targets are proxmox targets and should not be used by the k3s.
- /mnt/apps/code-server
- /mnt/pool1/dataset01/ISOs
- /mnt/apps/k3s-containers
- /mnt/pool1/k3s-containers-backup (deprecated)

## Network

Truenas is using IP 192.168.10.200 and is using a 1GiB ethernet cable. To access the truenas via ssh, if the user logged in the hostOS is spy, then the keys are located in ssh folder and with ssh truenas_admin@192.168.10.200 you can access passwordless. The password for sudo operations is not going to be provided in public files.