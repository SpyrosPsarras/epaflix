# Syncthing

Internal-only, RAID-backed Syncthing node on K3s. Not exposed to the public internet.

## Design

- ArgoCD-managed (`app-syncthing`), plain kustomize. No Helm, no SOPS Secret — config is generated on the PV on first run.
- Pod pinned to `k3s-worker-63` (evanthoulaki) via `nodeSelector`, which is where the data disk lives.
- Runs as `1000:1000` to match the host mount chown.

## Storage

1 TiB ext4 disk on evanthoulaki `local-raid` (hardware RAID), attached as `scsi1` to VM `1063`, mounted at `/mnt/syncthing-data` inside the guest. Exposed to K3s as a `local` PV (`syncthing-data-pv`, `Retain` reclaim policy). Config and synced data both live under `/var/syncthing` on the PV.

## Networking

- GUI: `http://192.168.10.110:8384` — kube-vip `LoadBalancer` (`syncthing-gui`). LAN/WireGuard only, RFC1918 (not internet-routable). No Traefik ingress, no Authentik; GUI auth is Syncthing's own username/password (see QUICKSTART.md).
- Sync (BEP): `tcp://192.168.10.101:22000` → Traefik `syncthing` TCP entrypoint → `syncthing-sync:22000`. Peers use this address. No relay, no global discovery (turn off in GUI after first deploy — see QUICKSTART.md).

## Backup

Three layers:
1. Hardware RAID on evanthoulaki (`local-raid`) — disk-level redundancy.
2. Syncthing file versioning (configure in GUI).
3. Nightly PBS snapshot of VM `1063` including `scsi1` (the data disk).

## Roll back

Delete the ArgoCD `syncthing` Application; ArgoCD prunes the namespace and workloads. The PV reclaim policy is `Retain`, so `/mnt/syncthing-data` on `k3s-worker-63` is not touched — data survives. Re-deploy by re-adding `app-syncthing.yaml` to the app-of-apps.
