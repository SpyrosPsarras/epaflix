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

## Upgrading past 1.30.0 - the v2 major (#593)

The pin is `1.30.0`, the **last v1 release**. The next bump Renovate offers is `2.x` (current stable `2.1.2`), so the first PR on this image is a whole major, not a routine step. Majors never auto-merge here (only the repo-wide patch rule sets `automerge`), so it arrives as a normal PR - merge it deliberately.

Reviewed 2026-08-03 against the v2.0.0 release notes and the v2.1.2 source. What actually changes for this deployment:

| Change in 2.0 | Effect here |
|---|---|
| Database backend LevelDB -> SQLite, one-time migration on first start ("can be lengthy for larger setups") | Small setup: index `index-v0.14.0.db` is 139 MB, synced data 9.7 GB, 946 GB free on the PV. Migration is short and has room. |
| CLI options modernised, some renamed, `--verbose`/`--logflags` removed | Our three args survive unchanged. `--home`, `--no-browser` and `--no-restart` are all still valid in v2.1.2 (`cmd/syncthing/main.go`: `name:"home"`, `NoBrowser`, `NoRestart`). **No manifest change needed.** |
| Structured logging, new WARNING level, INFO more verbose | Cosmetic - nothing here parses Syncthing logs or alerts on them. |
| Deleted items forgotten after six months | Fine for this use. Only matters if a peer can stay offline longer than six months; then set `--db-delete-retention-interval=0`. |
| Multiple connections (3) between v2 devices by default | Device-to-device only, and only when both ends are v2. v1 peers keep one connection. |
| No "default folder" created on first startup | Fresh installs only. Our `config.xml` already exists. |
| Prebuilt binaries dropped for some platforms | Not us - `linux/amd64`. |

**Rollback window is 14 days.** The migration renames the old LevelDB directory to `index-v0.14.0.db-migrated` and keeps it; `cleanConfigDirectory()` in `cmd/syncthing/main.go` deletes it after 14 days (`config.xml.v<n>` backups are kept 30). So reverting to `1.30.0` inside 14 days means: scale to 0, rename the directory back, revert the tag. After that there is no automatic way back.

Doing the bump:
1. `kubectl --context epaflix -n syncthing scale deploy/syncthing --replicas=0`, then `cp -a` the 139 MB `config/` directory on `k3s-worker-63` - cheap insurance on top of the `-migrated` directory.
2. Merge the PR and let ArgoCD roll it. `strategy: Recreate`, one replica, RWO local PV - a short outage, never two writers.
3. Watch the first start for the migration, then confirm every folder is back to "Up to Date" in the GUI before calling it done.

## Roll back

Delete the ArgoCD `syncthing` Application; ArgoCD prunes the namespace and workloads. The PV reclaim policy is `Retain`, so `/mnt/syncthing-data` on `k3s-worker-63` is not touched — data survives. Re-deploy by re-adding `app-syncthing.yaml` to the app-of-apps.
