# Move cluster storage to `pithari`

## Why

Pool `apps` on TrueNAS is one 99 GB disk without redundancy. It holds all 26 democratic-csi disks (11.2 GB used). Paperless lives on `hdd/paperless` on shypowder, also one disk. Pool `pithari` is a 4 × 12 TB RAIDZ2 (two disks can fail), 21.0T free, empty, `ashift=12`. Everything moves there, then gets daily snapshots, and the space on `apps` is freed.

## Goals

- All 26 PVCs on StorageClass `truenas-iscsi` end up as zvols under `pithari/k8s/iscsi/vols`.
- Paperless data ends up in dataset `pithari/paperless`, served over NFS from TrueNAS.
- New disks, including Immich later, land on `pithari` with no extra setup.
- A recursive daily snapshot task covers `pithari`.
- After the owner confirms each move, the old copy is deleted and `apps/k8s` is gone.

## Non-goals

- CNPG Postgres stays on `local-path`. The owner decided this.
- No Google Drive cloud sync for `pithari`. Cloud sync copies files and cannot read zvols. Paperless already reaches Google Drive through its restic backup to MinIO, and `mpara-tank-lan/minio` is synced.
- No new app backups. Forgejo, RackPeek, Home Assistant, Authentik media, openclaw, servarr configs, gh-runner and cpa-manager have no copy outside TrueNAS. This is a follow-up.
- The pool `apps` and `apps/ix-apps` (TrueNAS's own apps) stay.
- Out of scope: RackPeek nightly sync, tdarr DNS.

## Anti-goals

- Losing data. Each old copy stays until the owner confirms that app works on `pithari`.
- Any live change without the owner's yes: TrueNAS tasks, driver config, each app's outage, each delete.

## Approaches

- **O1.** Second democratic-csi release with StorageClass `pithari-iscsi`, file copy per PVC. Safe, but the release exists only during the move, and all 26 manifests change StorageClass too.
- **O2.** `zfs send` each zvol and hand-make PVs. Rejected. The driver did not create those disks, so iSCSI targets are manual and resize/delete break.
- **O3 (picked by the owner).** Change the existing release's `datasetParentName` to `pithari`, then file copy per PVC. StorageClass `truenas-iscsi` stays, so manifests only change PVC names and Immich needs nothing.

O3 depends on R1: disks still on `apps` must keep mounting after the change. Checked in democratic-csi `next` (commit `8797b2cd`, 2026-10-03): `NodeStageVolume` logs in using `iqn`, `lun` and `portal` from the PV's volume attributes, and the controller has no `PUBLISH_UNPUBLISH_VOLUME`, so mounting never looks up the parent dataset. The pinned image digest may be a different commit; the smoke test below confirms it on the cluster. Only resize and delete through Kubernetes stop working for unmoved disks, and the move needs neither. If the smoke test fails, revert the values and fall back to O1.

## Design

### TrueNAS layout

```
pithari/k8s                      encrypted, key stored on TrueNAS
pithari/k8s/iscsi/vols/<pvc-…>   zvols, created by democratic-csi
pithari/k8s/iscsi/snaps          detached snapshots (unused, snapshotter is off)
pithari/paperless                encrypted, key stored on TrueNAS
pithari/paperless/data           received from shypowder, NFS export
pithari/immich                   later
```

### Encryption

The owner chose encryption with a key that TrueNAS stores and unlocks at boot. Before the driver change, create `pithari/k8s` and `pithari/paperless` as encrypted datasets (AES-256-GCM, generated key, no passphrase) through the TrueNAS API. Children inherit it: democratic-csi zvols under `pithari/k8s/iscsi/vols`, and the Paperless receive, because a non-raw stream received under an encrypted parent is encrypted with the parent's key. `pithari` itself and the later `pithari/immich` stay out of this decision; Immich decides when it is added.

Owner step: export both dataset keys from the TrueNAS UI into the vault. The agent prepares the instructions and never sees the keys. Confirm after a TrueNAS reboot that both datasets unlock by themselves.

### Driver change

In `2-k3s/30.democratic-csi/helm-values.yaml`, set `datasetParentName: pithari/k8s/iscsi/vols` and `detachedSnapshotsDatasetParentName: pithari/k8s/iscsi/snaps`. Same portal, target group, initiator group and name prefix. The driver creates the parent datasets on first provision.

After the Argo sync, verify before any app moves:

1. `2-k3s/30.democratic-csi/test/iscsi-smoke.yaml` provisions a disk under `pithari/k8s/iscsi/vols` and mounts it.
2. Restart one unmoved, low-value pod (for example `cleanuparr`) and confirm its `apps` disk mounts.

### Snapshot task

One TrueNAS periodic snapshot task: dataset `pithari`, recursive, daily at 00:45, kept 30 days, naming `auto-%Y-%m-%d_%H-%M`. The time sits after the existing tasks at 00:00 and 00:30. 30 days instead of 7 is my call: the data is about 25 GB and the pool is 21 TB. Task 2 on `apps/k8s` stays until cleanup.

### Per-PVC move

The PVC's StorageClass stays, but a PVC cannot change its disk, so each one gets a new name with suffix `-pithari` (for example `forgejo-data-pithari`). Old names ending in `-iscsi` keep their suffix in the new name only if the builder finds that clearer; either is fine.

For each app (one window each, owner says yes first):

1. Prepare the window's PR with the new PVCs and the `claimName` switch. The new PVCs do not merge ahead of their window: Argo reports an unbound PVC as Progressing, which holds later sync waves.
2. Pause Argo the way README "Storage maintenance" does: remove `spec.syncPolicy.automated` from `app-of-apps` and the app's Application (all of them have `selfHeal: true`, which would undo a manual scale). Then suspend any CronJob that mounts the disk (`vaultwarden-backup`), wait for its active runs to finish, save the Deployments' replica counts and scale them to 0. My call, because the repo already uses this procedure; the alternative is two PRs per window (replicas 0, then switch).
3. Create the window's new PVCs by hand, then run a throwaway Job (not in Git) that mounts the old PVC read-only and the new one, and runs `rsync -aHAX --numeric-ids --delete old/ new/`, then a second `rsync -n` pass that must report no changes.
4. Merge the app's PR. Restore `automated` on both Applications; Argo syncs the merged commit and adopts the new PVCs. Scale the Deployments back to their saved counts by hand (Vaultwarden and servarr ignore `/spec/replicas` in Argo) and unsuspend CronJobs. Before scaling down, wait for active CronJob runs to finish: `vaultwarden-backup` scales Vaultwarden back up in its exit trap.
5. Owner checks the app. Until then, rollback stops the app, copies the new PVC back onto the old one with the same Job (so writes since the cutover are kept), then reverts only the `claimName` lines; the new PVCs stay in Git so Argo prune does not delete them.

Order: RackPeek first as the pilot (1 GiB, low traffic). Then Forgejo, Vaultwarden, Authentik media, Home Assistant (3 disks, one window). Then servarr (15 disks, one window), the two openclaw disks, gh-runner and cpa-manager.

### Paperless

Follows "Move to TrueNAS" in `2-k3s/33.paperless/README.md`, with these specifics:

1. Pause Argo for `app-of-apps` and `paperless` as above, scale Paperless and paperless-gpt to 0, and suspend `paperless-backup` and `paperless-share-lists`.
2. Take snapshot `hdd/paperless@pithari-move` and send only that one: `ssh shypowder zfs send` piped into `ssh truenas zfs recv pithari/paperless/data`, through this pod. A non-raw receive cannot overwrite the encrypted `pithari/paperless`; the new child inherits its key. No new keys between hosts. 13.3 GB. The 42 sanoid snapshots stay on shypowder until `hdd/paperless` is deleted; restic in MinIO keeps the longer history. My call: nothing on TrueNAS would prune the sanoid names.
3. Add a TrueNAS NFS share for `/mnt/pithari/paperless/data`: hosts `192.168.10.61`–`65`, all clients mapped to uid/gid 1000. Check that TrueNAS has a user and group with id 1000 first; the mapping needs one.
4. PR: replace PV `paperless-data-shypowder` with one pointing at `192.168.10.200:/mnt/pithari/paperless/data`. Argo deletes and recreates PV and PVC. Scale up, resume CronJobs.
5. After the owner confirms: stop the shypowder export. Deleting `hdd/paperless` is a cleanup step.

Update the Storage section of the Paperless README.

### Cleanup (after every app is confirmed)

Each step needs the owner's yes.

1. PR removing the old PVC manifests. Argo prunes the PVCs, except Vaultwarden and gh-runner, which have `prune: false`; delete those two by hand. The PVs are `Retain`, so they go to `Released` and the zvols stay.
2. Delete the old PVs.
3. On TrueNAS, delete the 26 iSCSI targets, extents and target-extent links that point at `apps`, then `zfs destroy -r apps/k8s`. The driver cannot do this now that its parent is `pithari`.
4. Delete snapshot task 2 (`apps/k8s`).
5. Delete `hdd/paperless` on shypowder and its NFS export.

### Monitoring and docs

My call, one PR after the driver switch: add alerts for `pithari` health and free space next to the existing TrueNAS pool alerts, and reword `TruenasAppsPoolFreeLow`, which says `apps` holds every iSCSI disk. Update README "Storage maintenance" and the Paperless README Storage section.

## Risks

- **R1.** Unmoved disks stop mounting after the driver change. Checked in source; the smoke test catches it. Fallback is O1.
- **R2.** Copy runs while something still writes. Step 2 stops every pod and CronJob that mounts the disk; the second `rsync -n` pass proves nothing changed.
- **R3.** Space overhead on RAIDZ2 for small zvol blocks (16K blocks on a 4-wide RAIDZ2 use about 2.25× raw). Irrelevant at 25 GB. Immich media should go in a dataset, not a zvol.
- **R5.** Losing both the dataset keys and the TrueNAS config makes the data unreadable. The vault copy of the keys covers it; the owner step above is required before any app moves.
- **R4.** TrueNAS reboot now takes down every app with a disk, as it does today on `apps`. No change.

## Left to the builder

- Exact PVC names, PR split beyond "driver first, one switch PR per window".
- The copy Job's image and node placement.
- How to confirm each app works, per app.
