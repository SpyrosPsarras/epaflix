# Pithari storage move Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all 26 `truenas-iscsi` PVCs and the Paperless NFS volume onto encrypted datasets on `pithari`, add snapshots and alerts, then free `apps/k8s` and `hdd/paperless`.

**Architecture:** Point the existing democratic-csi release at `pithari/k8s/iscsi/vols` (StorageClass name unchanged). Each PVC gets a new `-pithari` twin; a throwaway rsync Job copies old to new while the app is stopped and Argo is paused; a per-window PR switches `claimName`. Paperless moves by `zfs send` into `pithari/paperless` and a new NFS PV.

**Tech Stack:** TrueNAS SCALE API (`midclt`), ZFS, democratic-csi `freenas-api-iscsi`, Argo CD, kustomize, promtool.

**Spec:** `docs/superpowers/specs/2026-11-21-pithari-storage-design.md`

## Global Constraints

- Every live change waits for the owner's yes: TrueNAS tasks and datasets, the driver PR merge, each app window, each delete.
- Old copies stay until the owner confirms that app on `pithari`.
- Never print, log or commit secrets or dataset keys. Key export is an owner step.
- Encryption: `pithari/k8s` and `pithari/paperless`, AES-256-GCM, generated key stored on TrueNAS, no passphrase.
- Snapshot task: dataset `pithari`, recursive, daily 00:45, 30 days, naming `auto-%Y-%m-%d_%H-%M`.
- New PVC name: old name with a trailing `-iscsi` removed, then `-pithari` appended. Same namespace, size, access mode, StorageClass `truenas-iscsi`.
- Postgres stays on `local-path`. No Google Drive change. Pool `apps` and `apps/ix-apps` stay.
- PRs: conventional titles, rebased onto `origin/main`, merged by the owner; post-merge Argo syncs follow `/scripts/homelab-ssh.md` (dry-run first).
- Pausing Argo uses the README "Storage maintenance" commands: remove `spec.syncPolicy.automated` from `app-of-apps` and the window's Applications; restore with `app-of-apps` `{"automated":{"selfHeal":true}}` after the switch PR merges.

## Review Focus

- A CronJob or second Deployment mounting the same PVC keeps writing during the copy (Vaultwarden backup, Paperless backup and share-lists). Each window lists every consumer and checks `kubectl get pods -A` shows none before copying.
- The copy pod lands on a node that cannot log in to the target (iSCSI initiator group 12). The window step waits for the Job pod Running and fails the window if the new PVC stays Pending over 5 minutes.
- Ownership or ACLs change during copy (apps running as non-root uids, Home Assistant, servarr). The verify pass uses `rsync -aHAXc -n --delete --itemize-changes` and must print nothing.
- Old disk still mounted on a node when the new pod starts elsewhere is fine for RWO only if the old pod is gone. Each window checks `kubectl get volumeattachment` for the old PV is gone before copying.
- Argo stays paused after a window. Each window ends by checking `kubectl -n argocd get application <app> -o jsonpath='{.spec.syncPolicy.automated}'` is non-empty for every paused Application.

---

### Task 1: Prepare `pithari` on TrueNAS (live, owner yes first)

**Files:** none (TrueNAS API over `ssh truenas`).

**Interfaces:**
- Produces: datasets `pithari/k8s`, `pithari/paperless` (encrypted, unlocked); snapshot task on `pithari`; TrueNAS user and group `paperless`, uid/gid 1000 (no home, no shell, no password login), used by Task 7.

- [ ] **Step 1: Ask the owner** to approve: two encrypted datasets, the snapshot task, user and group `paperless` 1000:1000. TrueNAS has no uid/gid 1000 today (checked 2026-11-21).
- [ ] **Step 2: Create the datasets** with `midclt call pool.dataset.create` for `pithari/k8s` and `pithari/paperless`, `encryption: true`, `encryption_options: {"generate_key": true, "algorithm": "AES-256-GCM"}`, `inherit_encryption: false`.
- [ ] **Step 3: Verify.** `ssh truenas 'zfs get -H -o name,value encryption,keyformat,keystatus pithari/k8s pithari/paperless'` shows `aes-256-gcm`, `hex`, `available` for both.
- [ ] **Step 4: Create the snapshot task** with `midclt call pool.snapshottask.create` using the Global Constraints values. Verify with `pool.snapshottask.query` that it appears next to tasks 1 and 2.
- [ ] **Step 5: Create group then user `paperless`** (`group.create` gid 1000, `user.create` uid 1000, that group, `password_disabled: true`, shell `/usr/sbin/nologin`, no home). Verify with `user.query`.
- [ ] **Step 6: Owner step.** Write `~/handoffs/pithari-key-export.md`: in the TrueNAS UI, Datasets → `pithari/k8s` → Encryption → Export Key, store it as a vault item; repeat for `pithari/paperless`. Wait for the owner to confirm both are stored. No app moves before this.
- [ ] **Step 7: Auto-unlock check** (my call: at the next planned TrueNAS reboot, not a dedicated one, because a reboot stops every app on `apps`). Record it as an open item in the final report until done.

### Task 2: Point democratic-csi at `pithari` (PR + live check)

**Files:**
- Modify: `2-k3s/30.democratic-csi/helm-values.yaml` (`datasetParentName`, `detachedSnapshotsDatasetParentName`)
- Test: `2-k3s/30.democratic-csi/test/iscsi-smoke.yaml` (unchanged, applied by hand)

**Interfaces:**
- Consumes: `pithari/k8s` from Task 1.
- Produces: new `truenas-iscsi` disks land under `pithari/k8s/iscsi/vols`.

- [ ] **Step 1: Edit values** to `pithari/k8s/iscsi/vols` and `pithari/k8s/iscsi/snaps`.
- [ ] **Step 2: Build.** `kustomize build --enable-helm 2-k3s/30.democratic-csi | rg 'datasetParentName|detachedSnapshots'` shows the two new values.
- [ ] **Step 3: Commit and PR** `feat(democratic-csi): provision new disks on pithari`. Body says unmoved disks keep mounting (R1) and lose resize/delete until moved.
- [ ] **Step 4: After merge and Argo sync** (owner yes), wait for the controller pod to restart and be Ready.
- [ ] **Step 5: Smoke test.** `kubectl apply -f 2-k3s/30.democratic-csi/test/iscsi-smoke.yaml`; pod logs a timestamp; `ssh truenas 'zfs list -r pithari/k8s/iscsi/vols'` lists one zvol with `encryption` inherited (`zfs get encryption` shows `aes-256-gcm`).
- [ ] **Step 6: R1 check.** Owner yes, then `kubectl -n servarr rollout restart deploy cleanuparr`; new pod Running with its `apps` disk mounted. If it fails: revert the PR, report, stop (fallback O1 needs a new design pass).
- [ ] **Step 7: Clean up the smoke test.** Delete the pod and PVC (owner yes), then the `Released` PV and its zvol, iSCSI target and extent on TrueNAS.

### Task 3: Alerts for `pithari` (PR)

**Files:**
- Modify: `2-k3s/10.observability/alertmanager-config/custom-alerts.yaml` (group `truenas-pool-capacity`, `TruenasPoolUnhealthy` description)
- Test: `2-k3s/10.observability/alertmanager-config/custom-alerts-tests.yaml`

- [ ] **Step 1: Write failing tests.** `TruenasPithariPoolFreeLow` fires when `node_filesystem_avail_bytes{job="truenas-node-exporter",device="pithari",fstype="zfs"}` stays under 2 TiB for 1h, not at 3 TiB; `TruenasPithariPoolFreeCritical` fires under 1 TiB for 30m.
- [ ] **Step 2: Run** the CI promtool block from `.github/workflows/ci.yml:340-358` locally (`promtool` is at `~/.local/bin`). Expected: the new tests FAIL.
- [ ] **Step 3: Add both rules**, modelled on `TruenasMediaPoolFreeLow/Critical`: raidz2, 4 × 12 TB, 21 TiB usable, holds every iSCSI PVC and Paperless. In `TruenasPoolUnhealthy`, add "pithari is raidz2". In `TruenasAppsPoolFreeLow`, replace "holds all democratic-csi iSCSI zvols" with "held the democratic-csi zvols until the move to pithari".
- [ ] **Step 4: Run** the promtool block. Expected: all PASS, `promtool check rules` SUCCESS.
- [ ] **Step 5: Commit and PR** `feat(observability): alert on pithari free space`.

### Task 4: Write the 26 `-pithari` twins (branch only, no PR)

Argo reports an unbound PVC as Progressing, which would hold later sync waves (remote-pi, discord, home-assistant and others) for weeks. So the twins never merge ahead of their window: branch `t3/pithari-pvc-twins` is the source, and each window's switch PR carries its own twins.

**Files (each gets a second PVC next to the old one):**
- `2-k3s/07.authentik-deployment/storage/pv-pvc.yaml`, `2-k3s/35.discord/media/pvc.yaml`, `2-k3s/35.discord/windmill/pvc.yaml`, `2-k3s/38.forgejo/storage.yaml`, `2-k3s/22.gh-runner/storage.yaml`, `2-k3s/36.home-assistant/{esphome,home-assistant,roborock}.yaml`, `2-k3s/37.rackpeek/storage.yaml`, `2-k3s/17.remote-pi/cpamanager/storage.yaml`, `2-k3s/26.vaultwarden/storage.yaml`, `2-k3s/08.servarr/_shared/storage/{arr-configs,media-app-configs,utility-configs}.yaml`, `2-k3s/08.servarr/{bazarr-autotranslate/bazarr-autotranslate,cleanuparr/cleanuparr,jellysweep/jellysweep,neutarr/neutarr,streamystats/streamystats}.yaml`

**Interfaces:**
- Produces: PVC `<new name>` per Global Constraints, unbound (`WaitForFirstConsumer`), for Tasks 5–6.

- [ ] **Step 1: Add each twin.** Copy the old PVC document, change only `metadata.name`. Keep any annotations (Argo sync options, waves).
- [ ] **Step 2: Verify.** For each changed kustomize root, `kustomize build --enable-helm <root> | rg -c 'name: .*-pithari$'` matches the expected count; total 26 across roots.
- [ ] **Step 3: Commit** on `t3/pithari-pvc-twins`. No PR.

### Task 5: Copy Job template (scratch, not committed)

**Files:** `copy-pvc.yaml` in a `mktemp -d -p .` scratch directory, deleted at the end.

**Interfaces:**
- Produces: a Job `copy-<new name>` in the PVC's namespace, placeholders `__NS__`, `__OLD__`, `__NEW__`, filled with `sed`.

- [ ] **Step 1: Write the Job** in the scratch `copy-pvc.yaml`. `backoffLimit: 0`, `restartPolicy: Never`, `ttlSecondsAfterFinished` unset (logs kept until deleted), runs as root, mounts `OLD` read-only at `/old` and `NEW` at `/new`. Image with rsync built in, pinned by digest (no package install: several namespaces have egress NetworkPolicies). Placeholders `__NS__`, `__OLD__`, `__NEW__`, filled with `sed` (no `envsubst`: it would also expand the script's own variables). The script runs under `sh -e`, so a failing verify rsync stops it even when it only writes to stderr:

```sh
rsync -aHAX --numeric-ids --delete /old/ /new/
sync
diff=$(rsync -aHAXc -n --numeric-ids --delete --itemize-changes /old/ /new/)
test -z "$diff" || { echo "$diff" | head -n 20; exit 1; }
echo COPY_OK
```

- [ ] **Step 2: Dry check.** `kubectl apply --dry-run=server` against the `rackpeek` namespace succeeds.

### Task 6: Move the cluster disks, one window at a time (live)

**Windows, in order** (consumers = every pod and CronJob mounting the window's PVCs):

| # | Applications to pause | Consumers to stop | PVCs | Switch files |
|---|---|---|---|---|
| W1 | rackpeek | deploy rackpeek | rackpeek-config | `37.rackpeek/deployment.yaml` |
| W2 | forgejo | deploy forgejo | forgejo-data | `38.forgejo/deployment.yaml` |
| W3 | vaultwarden | deploy vaultwarden, cronjob vaultwarden-backup | vaultwarden-data-iscsi | `26.vaultwarden/{server,backup}.yaml` |
| W4 | authentik | deploy authentik-worker | authentik-media-pvc-iscsi | `07.authentik-deployment/helm-values.yaml` |
| W5 | home-assistant | deploy esphome, home-assistant, roborock | esphome-config, home-assistant-config, roborock-data | the three `36.home-assistant/*.yaml` |
| W6 | servarr | the 15 servarr Deployments in README "Storage maintenance" plus tdarr | the 15 servarr PVCs | the Deployment files listed in Task 4 plus `08.servarr/{bazarr,lidarr,lingarr,navidrome,prowlarr,qbittorrent,radarr,seerr,sonarr,tdarr}/*.yaml` |
| W7 | discord | deploy openclaw in discord-media and discord-windmill | openclaw-state ×2 | `35.discord/{media,windmill}/deployment.yaml` |
| W8 | gh-runner, remote-pi | deploy gh-runner, cpa-manager-plus | gh-runner-data-iscsi, cpa-manager-plus-data-iscsi | `22.gh-runner/deployment.yaml`, `17.remote-pi/cpamanager/deployment.yaml` |

Fetch any secret needed for the window before W3. Vaultwarden and servarr Applications ignore `/spec/replicas`, so Argo never restores their scale; every window restores replicas by hand from the saved counts.

**Per window:**

- [ ] **Step 1: Prepare the switch PR** `feat(<app>): move storage to pithari`: take the window's twin documents from `t3/pithari-pvc-twins` and change every `claimName` in the switch files to the new name. `kustomize build` of the root shows no old name in any pod spec. Do not merge yet.
- [ ] **Step 2: Ask the owner** for the window (name the apps and the expected outage, a few minutes).
- [ ] **Step 3: Pause Argo** for `app-of-apps` and the window's Applications.
- [ ] **Step 4: Stop consumers.** Suspend CronJobs first, then wait until none of their Jobs is active (`vaultwarden-backup` scales Vaultwarden back to 1 in its exit trap; confirm it did). Then save each Deployment's current replica count, then scale to 0. Wait until no pod mounts the PVCs and `kubectl get volumeattachment` lists none of the old PVs; recheck after one minute that nothing came back.
- [ ] **Step 5: Create the twins and copy.** Create only the window's twin documents with `kubectl create -f`, extracted with python from the PR branch and given `metadata.namespace` explicitly (the discord twins have none in Git; kustomize adds it). Never apply the whole file. Check `kubectl -n <ns> get pvc <new>` in every target namespace. Run the Task 5 Job per PVC. Expected: log ends `COPY_OK`, Job `Complete`. Any other result: save the Job's logs and events and record which PVs the twins bound to (the StorageClass is `Retain`, so they stay as `Released` PVs with zvols on `pithari`; Task 8 lists them for cleanup), delete the Job, wait until its pod is gone and no volumeattachment names the old or new PV, then delete the twins, restore Argo, the saved replica counts and CronJobs, stop the window, report.
- [ ] **Step 6: Merge the switch PR** (owner already approved the window), restore Argo automation, wait for Argo `Synced` at the merged SHA. Argo adopts the hand-made twins. Scale every Deployment back to its saved count and unsuspend CronJobs that Git does not suspend.
- [ ] **Step 7: Verify.** Pods Running on the `-pithari` PVCs (`kubectl get pod -o jsonpath` shows the new claim); app answers its health URL; Application `Healthy`; `kubectl -n argocd get application` shows `automated` restored for every paused app. Delete the copy Job.
- [ ] **Step 8: Owner confirms** the app. Rollback before then, with the owner's yes: pause Argo as in Step 3, stop consumers and check detachment as in Step 4, run the Task 5 Job in reverse (`__OLD__` = the `-pithari` PVC, `__NEW__` = the old PVC) so writes made since the cutover are kept. Every reverse copy must end `COPY_OK` and `Complete`; if one fails, the old volume may already be changed by `--delete`, so keep the app on the `-pithari` claims, restore as in Step 6 and report. Only then merge a PR that reverts only the `claimName` lines (keep the twins in Git, so Argo prune does not delete them) and restore as in Step 6.

W1 is the pilot: after W1, stop and report before W2.

### Task 7: Move Paperless (live)

**Files:**
- Modify: `2-k3s/33.paperless/storage.yaml` (PV renamed `paperless-data-pithari`, `nfs.server: 192.168.10.200`, `nfs.path: /mnt/pithari/paperless/data`, PVC `volumeName` follows)
- Modify: `2-k3s/33.paperless/README.md` (Storage section, "Move to TrueNAS" becomes history in one line)

**Interfaces:**
- Consumes: `pithari/paperless` and user/group `paperless` from Task 1.

- [ ] **Step 1: Prepare the PR** `feat(paperless): serve data from pithari`. `kustomize build 2-k3s/33.paperless` shows the new PV. Do not merge.
- [ ] **Step 2: Ask the owner** for the window and for the NFS share.
- [ ] **Step 3: Pause Argo** (`app-of-apps`, `paperless`), scale `paperless` and `paperless-gpt` to 0, suspend `paperless-backup` and `paperless-share-lists`, wait until no pod in `paperless` mounts `paperless-data`.
- [ ] **Step 4: Snapshot and send** into a new child (a non-raw receive cannot overwrite an encrypted dataset; a new child inherits the parent's key): `ssh shypowder zfs snapshot hdd/paperless@pithari-move`, then `ssh shypowder zfs send hdd/paperless@pithari-move | ssh truenas zfs recv pithari/paperless/data`. Verify: `zfs get -H encryption,encryptionroot,used pithari/paperless/data` shows `aes-256-gcm`, `pithari/paperless`, about 13.3G.
- [ ] **Step 5: Create the NFS share** with `sharing.nfs.create`: path `/mnt/pithari/paperless/data`, hosts `192.168.10.61`–`.65`, `mapall_user`/`mapall_group` `paperless`. Verify from a worker pod or `showmount -e 192.168.10.200` that it is exported.
- [ ] **Step 6: Merge the PR** with Paperless at zero; Argo deletes and recreates PV and PVC; restore automation, which also restores the Git replica counts and CronJob suspend state.
- [ ] **Step 7: Verify.** Paperless UI loads a known document's preview; `kubectl -n paperless create job --from=cronjob/paperless-backup paperless-backup-pithari` completes. Delete that Job.
- [ ] **Step 8: Owner confirms.** Rollback before then: revert the PR (shypowder untouched).

### Task 8: Cleanup and docs (live, every delete needs a yes)

**Files:**
- Modify: the Task 4 files (remove old PVC documents)
- Modify: `README.md` "Storage maintenance" (pool is `pithari`, raidz2 on TrueNAS; consumer list and commands updated; Paperless added as a consumer)
- Modify: `2-k3s/26.vaultwarden/README.md:4` (PVC name)

- [ ] **Step 1: Confirm** every window and Paperless are owner-confirmed.
- [ ] **Step 2: PR** `chore(storage): remove the apps-pool PVCs`, including the README edits. After merge and sync, delete by hand the Vaultwarden and gh-runner PVCs (`prune: false`).
- [ ] **Step 3: List** what will be deleted: `kubectl get pv | rg Released` (26), plus any `Released` PVs recorded from failed copy attempts, and on TrueNAS the iSCSI targets, extents and target-extents of all of them (under `zvol/apps/k8s`, and under `zvol/pithari/k8s` for failed attempts). Show the owner, get a yes.
- [ ] **Step 4: Delete** the 26 PVs and the recorded failed-attempt PVs (by recorded name, never by the cluster-wide `Released` list), then the TrueNAS iSCSI mappings of both sets, then the recorded failed-attempt zvols under `pithari/k8s/iscsi/vols` by exact name, then `zfs destroy -r apps/k8s` and snapshot task 2.
- [ ] **Step 5: Verify.** `ssh truenas 'zfs list -r apps'` shows only `apps` and `apps/ix-apps*`; `iscsi.extent.query` lists only `pithari` disks; `kubectl get pv` has no `Released` democratic-csi PVs.
- [ ] **Step 6: Paperless source.** Owner yes, then remove the `hdd/paperless` NFS export on shypowder, its sanoid config entry, and `zfs destroy -r hdd/paperless`. Update `1-proxmox/shypowder/README.md` and the shypowder `hdd` alerts that name Paperless.
- [ ] **Step 7: Report** live changes by resource name, the open auto-unlock check (Task 1 Step 7) and the backup gap for Forgejo, RackPeek, Home Assistant, Authentik, servarr, openclaw, gh-runner and cpa-manager.
