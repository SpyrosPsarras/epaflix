# Syncthing on K3s (RAID-backed, internal-only) + PBS datastore move — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run an internal-only, RAID-backed Syncthing node on K3s with ~1 TB on Proxmox `local-raid`, GUI behind Authentik SSO, sync port via Traefik on `.101`; and first move the PBS datastore off the non-redundant TrueNAS stripe onto takaros hardware RAID so it can back up the data.

**Architecture:** Phase 0 (Proxmox host ops on takaros) moves PBS LXC `1031`'s `mp0` datastore from TrueNAS NFS to takaros `local-raid` and grows it to ~1 TB. Phase 1 attaches a 1 TiB thin disk to `k3s-worker-63` (evanthoulaki `local-raid`), exposes it as a node-pinned `local` PV, and deploys Syncthing the GitOps way (kustomize app `2-k3s/15.syncthing/` + ArgoCD Application). Networking: GUI via Traefik `websecure` + Authentik forward-auth; sync protocol via a new Traefik TCP entrypoint on `192.168.10.101:22000`.

**Tech Stack:** Proxmox VE (`pct`/`qm`/`pvesm`), Proxmox Backup Server, K3s, Kustomize, ArgoCD, Traefik v3 (Helm), Authentik forward-auth, Syncthing.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-06-25-syncthing-on-k3s-design.md`.
- Repo for cluster work: `SpyrosPsarras/epaflix`. Default branch `main`. Branch `syncthing-on-k3s` already exists (spec committed there).
- kubectl context: **`--context epaflix`** (default context is a different cluster).
- Proxmox host SSH: `ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.10` (takaros) / `…11` (evanthoulaki). No password is stored; this key only.
- **`kubectl exec` is blocked** in this environment — never rely on it; use the GUI or REST over the IngressRoute for in-pod actions.
- Storage: Proxmox `local-raid` only. **No TrueNAS** for Syncthing data. Disk 1 TiB **thin**.
- Placement: pod pinned to `k3s-worker-63` (VMID `1063`, evanthoulaki). Data disk = `scsi1` on `1063`.
- Hostname: `syncthing.epaflix.com` → `192.168.10.101`. Sync peers use `tcp://192.168.10.101:22000`.
- UID/GID Syncthing runs as: **`1000:1000`** (host mount chowned to match).
- Merge policy: semi-linear, mandatory rebase onto `origin/main`, merge via `gh pr merge <n> --merge`. **Never push to main, never force-push, never merge without explicit owner OK.**
- ⛔ = gated step requiring explicit owner sign-off before running (destructive / deploy / infra / secrets). Pause and ask.
- Every follow-up gets a `gh issue` on `SpyrosPsarras/epaflix`.

---

## Task 1: ⛔ Phase 0 — Move + grow the PBS datastore (takaros host op)

**Files:**
- No repo files. Modifies LXC `1031` config + storage on `takaros` (`192.168.10.10`).

**Interfaces:**
- Consumes: PBS LXC `1031` with `mp0: VMs:1031/vm-1031-disk-0.raw, mp=/mnt/VMs, size=300G` (TrueNAS NFS).
- Produces: `mp0` backed by `local-raid` (`vm-1031-disk-1`), grown to ~1 TB, PBS running, old NFS source still present (cleaned in Task 2).

- [ ] **Step 1: Pre-flight (read-only)**

```bash
ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.10 '
  pct config 1031 | grep -E "^mp0|^rootfs"
  pvesm status -storage local-raid
  pvesm status -storage pbs-backup-local
  qm list >/dev/null; date'   # confirm NOT near 01:00 backup window
```
Expected: `mp0` on `VMs`, `local-raid` shows ≥ ~400 G free, datastore ~176 GiB used, current time well clear of 01:00.

- [ ] **Step 2: Stop the PBS container**

```bash
ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.10 'pct stop 1031 && pct status 1031'
```
Expected: `status: stopped`.

- [ ] **Step 3: Move the datastore volume to local-raid (keep source)**

```bash
ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.10 'pct move-volume 1031 mp0 local-raid --delete 0'
```
Expected: copies ~176 GiB; ends `… mount point 'mp0' moved successfully`. `--delete 0` keeps the NFS source for rollback.

- [ ] **Step 4: Verify config now points at local-raid**

```bash
ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.10 'pct config 1031 | grep -E "^mp0|^unused"'
```
Expected: `mp0: local-raid:vm-1031-disk-1,mp=/mnt/VMs,size=300G` and an `unusedN:` entry referencing the old `VMs:1031/vm-1031-disk-0.raw`.

- [ ] **Step 5: Grow the datastore to ~1 TB**

```bash
ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.10 'pct resize 1031 mp0 1000G && pct config 1031 | grep ^mp0'
```
Expected: `size=1000G`. For ext4, Proxmox grows the FS automatically.

- [ ] **Step 6: Start PBS and confirm the datastore mount + size**

```bash
ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.10 'pct start 1031 && sleep 8 && pct exec 1031 -- df -h /mnt/VMs'
```
Expected: PBS running; `/mnt/VMs` shows ~1 TB total, used ≈ prior ~176 GiB. (If FS not grown: `pct exec 1031 -- resize2fs /dev/<dev>`; re-check.)

- [ ] **Step 7: Commit-equivalent — record outcome to history**

```bash
ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.10 'pvesm status -storage pbs-backup-local'
```
Append the before/after to `.history/` per repo convention (`.history/2026-06-25-pbs-datastore-move.md`). No git commit yet (host op).

Rollback if any step fails: `pct stop 1031; pct move-volume 1031 mp0 VMs --delete 0; pct start 1031` (source intact).

---

## Task 2: ⛔ Phase 0 — Verify integrity, set prune policy, cleanup, rename

**Files:**
- No repo files. PBS datastore + PVE storage config on `takaros`.

**Interfaces:**
- Consumes: PBS datastore on `local-raid` at ~1 TB (Task 1).
- Produces: verified backups, a bounded prune policy, freed TrueNAS space, accurate datastore name.

- [ ] **Step 1: Run a PBS verify + garbage-collect (the safety gate)**

```bash
ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.10 '
  pct exec 1031 -- proxmox-backup-manager datastore list
  pct exec 1031 -- proxmox-backup-manager verify VMs-NFS
  pct exec 1031 -- proxmox-backup-manager garbage-collection start VMs-NFS'
```
Expected: datastore listed; verify reports all snapshots `OK`; GC completes. **Do not proceed to cleanup unless verify is clean.**

- [ ] **Step 2: Confirm a fresh backup writes to the new location**

```bash
ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.10 'vzdump 1031 --storage pbs-backup-local --mode snapshot --notes-template "phase0 smoke {{guestname}}"'
```
Expected: backup completes; target is `pbs-backup-local`. (Backing up the PBS LXC itself is fine.)

- [ ] **Step 3: Set a bounded prune policy (replaces keep-all=1)**

```bash
ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.10 '
  pvesm set pbs-backup-local --prune-backups keep-daily=7,keep-weekly=4,keep-monthly=3
  grep -A2 "pbs-backup-local" /etc/pve/storage.cfg'
```
Expected: `prune-backups keep-daily=7,keep-weekly=4,keep-monthly=3`. Then run a prune to apply:
```bash
ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.10 'pct exec 1031 -- proxmox-backup-manager prune --keep-daily 7 --keep-weekly 4 --keep-monthly 3 VMs-NFS' 2>/dev/null || true
```

- [ ] **Step 4: ⛔ Delete the old NFS source (only after Step 1 clean)**

```bash
ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.10 '
  pct config 1031 | grep -E "^unused"          # note the unusedN id + volume
  # remove the unused mountpoint reference, then free the volume:
  pct set 1031 --delete unused0
  pvesm free VMs:1031/vm-1031-disk-0.raw
  pvesm status -storage VMs'
```
Expected: `unused0` gone from config; volume freed; TrueNAS `VMs` storage shows ~300 G reclaimed.

- [ ] **Step 5: Rename the now-misnamed datastore (cosmetic, owner-approved)**

PBS datastore display name `VMs-NFS` is misleading post-move. Rename in the PBS UI (Datastore → Options) or leave the datastore name and only fix the PVE storage label. Lowest-risk path: keep the PBS datastore name `VMs-NFS` (renaming a PBS datastore re-points its mount), and instead leave a note in `.history/`. If the owner wants a true rename, do it as a separate change. Record the decision in `.history/2026-06-25-pbs-datastore-move.md`.

- [ ] **Step 6: Record outcome**

Append verify output, prune policy, and reclaimed space to `.history/2026-06-25-pbs-datastore-move.md`.

---

## Task 3: ⛔ Phase 1a — Create, attach, format, mount the 1 TiB disk on worker-63

**Files:**
- No repo files. Modifies VM `1063` on `evanthoulaki` (`192.168.10.11`) and its guest OS (`10.0.0.63`).

**Interfaces:**
- Consumes: VM `1063` (`scsi0` boot, `scsihw: virtio-scsi-pci`), evanthoulaki `local-raid` ~1.9 TiB free.
- Produces: `/mnt/syncthing-data` ext4 mount inside worker-63, in `/etc/fstab`, chowned `1000:1000`, with `config/` + `data/` subdirs. Node `k3s-worker-63` ready for the `local` PV at `/mnt/syncthing-data`.

- [ ] **Step 1: Create + attach a 1 TiB thin disk as scsi1**

```bash
ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.11 'qm set 1063 -scsi1 local-raid:1024 && qm config 1063 | grep -E "^scsi1"'
```
Expected: `scsi1: local-raid:vm-1063-disk-1,size=1024G`. (lvmthin → thin-provisioned.)

- [ ] **Step 2: Identify the new block device inside the guest**

The agent reaches the guest via the Proxmox host. Use `qm guest exec` (QEMU guest agent) since direct `ubuntu@` SSH and `kubectl exec` are unavailable here:
```bash
ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.11 'qm guest exec 1063 -- lsblk -dno NAME,SIZE,TYPE'
```
Expected: a new ~1 TiB `sdX` (likely `sdb`) with no partitions. Confirm it is the 1 TiB disk, not the 50 G boot disk.

- [ ] **Step 3: Format ext4 and create the mount target**

```bash
ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.11 '
  qm guest exec 1063 -- bash -lc "mkfs.ext4 -L syncthing /dev/sdb && mkdir -p /mnt/syncthing-data"'
```
Expected: `mkfs.ext4` succeeds. (Adjust `/dev/sdb` to the device from Step 2.)

- [ ] **Step 4: Persist in fstab by UUID and mount**

```bash
ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.11 '
  qm guest exec 1063 -- bash -lc "
    UUID=\$(blkid -s UUID -o value /dev/sdb);
    grep -q \"\$UUID\" /etc/fstab || echo \"UUID=\$UUID /mnt/syncthing-data ext4 defaults,nofail 0 2\" >> /etc/fstab;
    mount -a && df -h /mnt/syncthing-data"'
```
Expected: mount present, ~1 TiB available, `nofail` set.

- [ ] **Step 5: Create subdirs and chown to 1000:1000**

```bash
ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.11 '
  qm guest exec 1063 -- bash -lc "mkdir -p /mnt/syncthing-data/config /mnt/syncthing-data/data && chown -R 1000:1000 /mnt/syncthing-data && ls -ln /mnt/syncthing-data"'
```
Expected: `config` and `data` owned by `1000:1000`.

- [ ] **Step 6: Confirm worker-63 stays PBS-backed with the data disk included**

```bash
ssh -i ~/.ssh/id_rsa_k3s root@192.168.10.11 'grep 1063 /etc/pve/jobs.cfg; qm config 1063 | grep -E "^scsi1"'
```
Expected: `1063` still in the nightly job; `scsi1` has **no** `backup=0` flag (so it is backed up).

- [ ] **Step 7: Record to history** — append device/UUID/mount to `.history/2026-06-25-syncthing-worker63-disk.md`.

---

## Task 4: Phase 1b — Scaffold the K3s app: namespace + node-pinned storage

**Files:**
- Create: `2-k3s/15.syncthing/namespace.yaml`
- Create: `2-k3s/15.syncthing/storage.yaml`

**Interfaces:**
- Consumes: node `k3s-worker-63` with `/mnt/syncthing-data` (Task 3).
- Produces: namespace `syncthing`; StorageClass `syncthing-local`; PV `syncthing-data-pv` (1Ti, node-pinned); PVC `syncthing-data` (1Ti, RWO) — consumed by Task 5's Deployment.

- [ ] **Step 1: Write `namespace.yaml`**

```yaml
---
apiVersion: v1
kind: Namespace
metadata:
  name: syncthing
  labels:
    app: syncthing
```

- [ ] **Step 2: Write `storage.yaml`**

```yaml
---
# Node-pinned local storage for Syncthing. The data lives on a 1 TiB ext4
# disk on evanthoulaki local-raid (hardware RAID), mounted at
# /mnt/syncthing-data inside k3s-worker-63. A `local` volume + nodeAffinity
# pins the pod to that node (the disk is host-local, not network storage).
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: syncthing-local
provisioner: kubernetes.io/no-provisioner
volumeBindingMode: WaitForFirstConsumer
reclaimPolicy: Retain
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: syncthing-data-pv
spec:
  capacity:
    storage: 1Ti
  accessModes:
    - ReadWriteOnce
  persistentVolumeReclaimPolicy: Retain
  storageClassName: syncthing-local
  local:
    path: /mnt/syncthing-data
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values:
                - k3s-worker-63
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: syncthing-data
  namespace: syncthing
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: syncthing-local
  resources:
    requests:
      storage: 1Ti
```

- [ ] **Step 3: Validate the two files parse**

Run: `kubectl --context epaflix apply --dry-run=client -f 2-k3s/15.syncthing/namespace.yaml -f 2-k3s/15.syncthing/storage.yaml`
Expected: all four objects `(dry run)` validate, no errors.

- [ ] **Step 4: Commit**

```bash
git add 2-k3s/15.syncthing/namespace.yaml 2-k3s/15.syncthing/storage.yaml
git commit -m "feat(syncthing): namespace + node-pinned local PV/PVC on worker-63"
```

---

## Task 5: Phase 1b — Syncthing Deployment + Services

**Files:**
- Create: `2-k3s/15.syncthing/deployment.yaml`
- Create: `2-k3s/15.syncthing/service-gui.yaml`
- Create: `2-k3s/15.syncthing/service-sync.yaml`

**Interfaces:**
- Consumes: PVC `syncthing-data` (Task 4).
- Produces: Deployment `syncthing` (pinned to worker-63); Service `syncthing-gui:8384` (consumed by Task 6 GUI IngressRoute); Service `syncthing-sync:22000` (consumed by Task 6 IngressRouteTCP).

- [ ] **Step 1: Write `deployment.yaml`** (pin the image tag to the current `syncthing/syncthing` release at apply time)

```yaml
---
# Syncthing — single always-on sync node. Pinned to k3s-worker-63 where the
# 1 TiB RAID-backed data disk lives. Config + synced data both on the local PV
# (subdirs /var/syncthing/config and /var/syncthing/data). Runs as 1000:1000 to
# match the host chown. Global discovery / relays / usage-reporting are turned
# OFF post-deploy via the GUI (internal-only); peers use tcp://192.168.10.101:22000.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: syncthing
  namespace: syncthing
  labels:
    app: syncthing
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: syncthing
  template:
    metadata:
      labels:
        app: syncthing
    spec:
      nodeSelector:
        kubernetes.io/hostname: k3s-worker-63
      securityContext:
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
      containers:
        - name: syncthing
          image: syncthing/syncthing:1.30.0   # pin to current release at apply time; Renovate manages bumps
          env:
            - name: PUID
              value: "1000"
            - name: PGID
              value: "1000"
            - name: STGUIADDRESS
              value: "0.0.0.0:8384"
            - name: HOME
              value: /var/syncthing/config
            - name: TZ
              value: "Europe/Oslo"
          args:
            - "--home=/var/syncthing/config"
            - "--no-browser"
            - "--no-restart"
          ports:
            - containerPort: 8384
              name: gui
            - containerPort: 22000
              name: sync-tcp
              protocol: TCP
          volumeMounts:
            - name: data
              mountPath: /var/syncthing
          livenessProbe:
            httpGet:
              path: /rest/noauth/health
              port: gui
            initialDelaySeconds: 30
            periodSeconds: 30
            timeoutSeconds: 10
          readinessProbe:
            httpGet:
              path: /rest/noauth/health
              port: gui
            initialDelaySeconds: 10
            periodSeconds: 10
            timeoutSeconds: 5
          resources:
            requests:
              cpu: "250m"
              memory: "512Mi"
      terminationGracePeriodSeconds: 30
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: syncthing-data
```

- [ ] **Step 2: Write `service-gui.yaml`**

```yaml
---
apiVersion: v1
kind: Service
metadata:
  name: syncthing-gui
  namespace: syncthing
  labels:
    app: syncthing
spec:
  type: ClusterIP
  selector:
    app: syncthing
  ports:
    - name: http
      port: 8384
      targetPort: gui
```

- [ ] **Step 3: Write `service-sync.yaml`**

```yaml
---
# Backing Service for the sync protocol (BEP). Reached via the Traefik
# `syncthing` TCP entrypoint on 192.168.10.101:22000 (IngressRouteTCP).
apiVersion: v1
kind: Service
metadata:
  name: syncthing-sync
  namespace: syncthing
  labels:
    app: syncthing
spec:
  type: ClusterIP
  selector:
    app: syncthing
  ports:
    - name: sync
      port: 22000
      targetPort: sync-tcp
      protocol: TCP
```

- [ ] **Step 4: Validate**

Run: `kubectl --context epaflix apply --dry-run=client -f 2-k3s/15.syncthing/deployment.yaml -f 2-k3s/15.syncthing/service-gui.yaml -f 2-k3s/15.syncthing/service-sync.yaml`
Expected: Deployment + 2 Services validate.

- [ ] **Step 5: Commit**

```bash
git add 2-k3s/15.syncthing/deployment.yaml 2-k3s/15.syncthing/service-gui.yaml 2-k3s/15.syncthing/service-sync.yaml
git commit -m "feat(syncthing): deployment (pinned to worker-63) + gui/sync services"
```

---

## Task 6: Phase 1b — GUI IngressRoute (Authentik forward-auth) + sync IngressRouteTCP

**Files:**
- Create: `2-k3s/15.syncthing/ingress-gui.yaml`
- Create: `2-k3s/15.syncthing/ingressroute-tcp.yaml`

**Interfaces:**
- Consumes: Service `syncthing-gui:8384` and `syncthing-sync:22000` (Task 5); middleware `authentik-forwardauth@traefik-system`, `redirect-https@traefik-system`; Service `authentik-server` in ns `app-authentik`; the `syncthing` Traefik entrypoint (Task 7).
- Produces: HTTPS GUI route gated by Authentik; HTTP→HTTPS redirect; outpost callback route; L4 passthrough for the sync port.

- [ ] **Step 1: Write `ingress-gui.yaml`** (mirrors the qBittorrent forward-auth pattern; no `/api` exemption — Syncthing peers use 22000, not the GUI)

```yaml
---
# Syncthing GUI — internal-only HTTPS, fronted by Authentik forward-auth SSO
# (same pattern as 08.servarr/qbittorrent/ingressroute.yaml, issue #176).
# syncthing.epaflix.com resolves to 192.168.10.101 (Traefik) only on the LAN /
# over WireGuard; nothing is published to the public internet.
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: syncthing-https
  namespace: syncthing
spec:
  entryPoints:
    - websecure
  routes:
    - match: Host(`syncthing.epaflix.com`)
      kind: Rule
      priority: 10
      middlewares:
        - name: authentik-forwardauth
          namespace: traefik-system
      services:
        - name: syncthing-gui
          port: 8384
  tls:
    certResolver: cloudflare
    domains:
      - main: epaflix.com
        sans:
          - "*.epaflix.com"
---
# Outpost route - handles the Authentik auth callback on syncthing.epaflix.com
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: syncthing-outpost-https
  namespace: syncthing
spec:
  entryPoints:
    - websecure
  routes:
    - match: Host(`syncthing.epaflix.com`) && PathPrefix(`/outpost.goauthentik.io/`)
      kind: Rule
      priority: 15
      services:
        - name: authentik-server
          namespace: app-authentik
          port: 80
  tls:
    certResolver: cloudflare
    domains:
      - main: epaflix.com
        sans:
          - "*.epaflix.com"
---
# HTTP to HTTPS redirect
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: syncthing-http
  namespace: syncthing
spec:
  entryPoints:
    - web
  routes:
    - match: Host(`syncthing.epaflix.com`)
      kind: Rule
      middlewares:
        - name: redirect-https
          namespace: traefik-system
      services:
        - name: syncthing-gui
          port: 8384
```

- [ ] **Step 2: Write `ingressroute-tcp.yaml`** (raw L4 passthrough — Syncthing does its own TLS with device certs; no `tls` block)

```yaml
---
# Syncthing sync protocol (BEP) over the dedicated Traefik `syncthing` TCP
# entrypoint (192.168.10.101:22000, defined in traefik-values.yaml). HostSNI(`*`)
# catches all traffic on the entrypoint; no TLS termination (passthrough).
apiVersion: traefik.io/v1alpha1
kind: IngressRouteTCP
metadata:
  name: syncthing-sync-tcp
  namespace: syncthing
spec:
  entryPoints:
    - syncthing
  routes:
    - match: HostSNI(`*`)
      services:
        - name: syncthing-sync
          port: 22000
```

- [ ] **Step 3: Validate**

Run: `kubectl --context epaflix apply --dry-run=client -f 2-k3s/15.syncthing/ingress-gui.yaml -f 2-k3s/15.syncthing/ingressroute-tcp.yaml`
Expected: 3 IngressRoutes + 1 IngressRouteTCP validate (CRDs already in-cluster).

- [ ] **Step 4: Commit**

```bash
git add 2-k3s/15.syncthing/ingress-gui.yaml 2-k3s/15.syncthing/ingressroute-tcp.yaml
git commit -m "feat(syncthing): GUI IngressRoute (authentik forward-auth) + sync IngressRouteTCP"
```

---

## Task 7: Phase 1c — Add the Traefik `syncthing` TCP entrypoint (shared Traefik values)

**Files:**
- Modify: `2-k3s/05.traefik-deployment/values/traefik-values.yaml` (the `ports:` block, lines ~19-32)

**Interfaces:**
- Consumes: nothing new.
- Produces: a Traefik entrypoint named `syncthing` on `22000/TCP`, exposed on the `.101` LoadBalancer service — the entrypoint referenced by Task 6's IngressRouteTCP. **Triggers a Traefik rollout when synced.**

- [ ] **Step 1: Add the entrypoint under `ports:`**

In `traefik-values.yaml`, after the `websecure:` block (line ~32), add:
```yaml
  # Syncthing sync protocol (BEP). Dedicated TCP entrypoint so the sync port
  # rides the existing Traefik LB (192.168.10.101) instead of a new LB IP.
  # Routed by 2-k3s/15.syncthing/ingressroute-tcp.yaml (HostSNI(`*`), passthrough).
  syncthing:
    port: 22000
    exposedPort: 22000
    protocol: TCP
```

- [ ] **Step 2: Validate the values still render**

Run: `kustomize build --enable-helm 2-k3s/05.traefik-deployment | grep -A2 -iE "name: syncthing|22000" | head`
Expected: the Helm render now includes a `syncthing` entrypoint / container port `22000` and a `22000` service port. (If `kustomize build --enable-helm` is unavailable locally, `helm template` the chart with these values; the chart maps `ports.<name>` → entrypoint + service port.)

- [ ] **Step 3: Commit**

```bash
git add 2-k3s/05.traefik-deployment/values/traefik-values.yaml
git commit -m "chore(traefik): add syncthing TCP entrypoint on 22000 for the sync port"
```

---

## Task 8: Phase 1b — kustomization + README/QUICKSTART; full app render

**Files:**
- Create: `2-k3s/15.syncthing/kustomization.yaml`
- Create: `2-k3s/15.syncthing/README.md`
- Create: `2-k3s/15.syncthing/QUICKSTART.md`

**Interfaces:**
- Consumes: all `2-k3s/15.syncthing/*.yaml` from Tasks 4-6.
- Produces: a kustomization ArgoCD can build — consumed by the Application in Task 9.

- [ ] **Step 1: Write `kustomization.yaml`**

```yaml
# Syncthing — kustomization for the ArgoCD-managed App. Plain kustomize
# (no Helm, no Secret): config is generated on the PV on first run, and
# Syncthing peer/folder state is intentionally not in git.
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
  - namespace.yaml
  - storage.yaml
  - deployment.yaml
  - service-gui.yaml
  - service-sync.yaml
  - ingress-gui.yaml
  - ingressroute-tcp.yaml
```

- [ ] **Step 2: Write `README.md`** — cover: what it is (internal Syncthing, RAID-backed on worker-63), storage (1 TiB local PV on evanthoulaki local-raid), networking (GUI via `syncthing.epaflix.com` + Authentik; sync via `tcp://192.168.10.101:22000`), backup (RAID + Syncthing versioning + PBS), and how to roll back (delete the ArgoCD app; PV is `Retain` so data survives). Keep it factual and short, matching `2-k3s/14.searxng/README.md` length.

- [ ] **Step 3: Write `QUICKSTART.md`** — the one-time post-deploy hardening + verification checklist (the GUI steps from Task 13), so it lives with the app.

- [ ] **Step 4: Validate the full render**

Run: `kubectl --context epaflix kustomize 2-k3s/15.syncthing`
Expected: all 9 objects (Namespace, StorageClass, PV, PVC, Deployment, 2 Services, 3 IngressRoutes, 1 IngressRouteTCP) render with no errors.

- [ ] **Step 5: Commit**

```bash
git add 2-k3s/15.syncthing/kustomization.yaml 2-k3s/15.syncthing/README.md 2-k3s/15.syncthing/QUICKSTART.md
git commit -m "feat(syncthing): kustomization + README/QUICKSTART"
```

---

## Task 9: Phase 1f — ArgoCD Application wiring

**Files:**
- Create: `2-k3s/11.argocd/apps/app-syncthing.yaml`
- Modify: `2-k3s/11.argocd/apps/kustomization.yaml` (add to `resources:`)

**Interfaces:**
- Consumes: kustomization `2-k3s/15.syncthing` (Task 8); the app-of-apps that reconciles `2-k3s/11.argocd/apps`.
- Produces: ArgoCD Application `syncthing` (auto-sync, selfHeal, prune).

- [ ] **Step 1: Write `app-syncthing.yaml`** (mirror `app-searxng.yaml`; the Service clusterIP ignoreDifferences avoids spurious drift)

```yaml
---
# ArgoCD Application: Syncthing (internal-only, RAID-backed sync node).
# Source:      this repo, path 2-k3s/15.syncthing, plain kustomize.
# Destination: in-cluster `syncthing` namespace.
# Sync:        selfHeal on, prune enabled.
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: syncthing
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/SpyrosPsarras/epaflix.git
    targetRevision: main
    path: 2-k3s/15.syncthing
  destination:
    server: https://kubernetes.default.svc
    namespace: syncthing
  syncPolicy:
    automated:
      selfHeal: true
      prune: true
  ignoreDifferences:
    - group: ""
      kind: Service
      jsonPointers:
        - /spec/clusterIP
        - /spec/clusterIPs
        - /status
```

- [ ] **Step 2: Add to the apps kustomization** — insert `  - app-syncthing.yaml` in `2-k3s/11.argocd/apps/kustomization.yaml` after `  - app-servarr.yaml` (keep the alpha-ish ordering of the file).

- [ ] **Step 3: Validate**

Run: `kubectl --context epaflix kustomize 2-k3s/11.argocd/apps | grep -c "kind: Application"`
Expected: count increases by 1 vs before; `app-syncthing` present (`kubectl --context epaflix kustomize 2-k3s/11.argocd/apps | grep -A1 "name: syncthing"`).

- [ ] **Step 4: Commit**

```bash
git add 2-k3s/11.argocd/apps/app-syncthing.yaml 2-k3s/11.argocd/apps/kustomization.yaml
git commit -m "feat(syncthing): ArgoCD Application + register in app-of-apps"
```

---

## Task 10: ⛔ Phase 1d — Pi-hole DNS record

**Files:**
- Modify: `/etc/dnsmasq.d/10-epaflix.conf` on Pi-hole (`192.168.10.30`) — **host op, not in repo**.

**Interfaces:**
- Consumes: nothing.
- Produces: `syncthing.epaflix.com` → `192.168.10.101` resolvable on the LAN/WireGuard.

- [ ] **Step 1: Add the record (dnsmasq.d only — never the web UI / custom.list)**

Follow `.github/instructions/pihole.instructions.md`. Add to `/etc/dnsmasq.d/10-epaflix.conf` the same `address=/syncthing.epaflix.com/192.168.10.101` form already used for the other `*.epaflix.com` services (verify the exact existing syntax in that file first — it may be a single wildcard line that already covers `syncthing`).

- [ ] **Step 2: Reload + verify**

```bash
nslookup syncthing.epaflix.com 192.168.10.30
```
Expected: resolves to `192.168.10.101`. (If a `*.epaflix.com` wildcard already exists in `10-epaflix.conf`, no edit is needed — just confirm resolution and skip Step 1.)

---

## Task 11: ⛔ Phase 1e — Authentik provider/application + outpost binding

**Files:**
- None in repo. Authentik admin UI (`https://authentik.epaflix.com` or the configured host).

**Interfaces:**
- Consumes: the existing forward-auth outpost backing `authentik-forwardauth@traefik-system`.
- Produces: `syncthing.epaflix.com` gated by Authentik SSO.

- [ ] **Step 1: Create a Proxy Provider** — type **Forward auth (single application)**, External host `https://syncthing.epaflix.com`, an appropriate auth/authz flow (match what qBittorrent uses).
- [ ] **Step 2: Create an Application** bound to that provider (slug `syncthing`), authorize the intended user/group (the group used for the servarr forward-auth apps).
- [ ] **Step 3: Add the application to the existing forward-auth outpost** so the outpost serves `/outpost.goauthentik.io/` for this host.
- [ ] **Step 4: Verify** — after deploy (Task 12), hitting `https://syncthing.epaflix.com` redirects to Authentik login, then to the Syncthing GUI. Record the provider/app names in `.history/`.

---

## Task 12: ⛔ Deploy — PR, rebase, merge, ArgoCD sync

**Files:**
- None new. Integrates the `syncthing-on-k3s` branch.

**Interfaces:**
- Consumes: all GitOps commits (Tasks 4-9).
- Produces: live `syncthing` ArgoCD app.

- [ ] **Step 1: Push the branch + open a PR** (use the `create-pull-request` flow; include the spec link and a test plan that mirrors Task 13). Do **not** merge yet.

```bash
git push -u origin syncthing-on-k3s
```

- [ ] **Step 2: Rebase onto origin/main** (required before merge — strict up-to-date block)

```bash
git fetch origin && git rebase origin/main
git push --force-with-lease   # ⛔ force-push needs explicit owner OK
```

- [ ] **Step 3: ⛔ Merge (owner-approved only)**

```bash
gh pr merge <n> --merge
```

- [ ] **Step 4: Watch ArgoCD reconcile**

```bash
kubectl --context epaflix get applications -n argocd syncthing -w
kubectl --context epaflix get pods -n syncthing -o wide
```
Expected: app `Synced` + `Healthy`; pod `Running` on `k3s-worker-63`; PVC `Bound`. Traefik app re-syncs with the new entrypoint.

- [ ] **Step 5: Confirm the Traefik LB now exposes 22000**

```bash
kubectl --context epaflix get svc -n traefik-system traefik -o jsonpath='{.spec.ports[*].port}'; echo
```
Expected: includes `22000`.

---

## Task 13: ⛔ Post-deploy hardening + end-to-end verification + follow-ups

**Files:**
- None in repo. Syncthing GUI (one-time), then `gh issue` for follow-ups.

**Interfaces:**
- Consumes: the live deployment.
- Produces: a hardened, verified Syncthing node + follow-up issues.

- [ ] **Step 1: One-time GUI hardening** (kubectl --context epaflix exec is blocked — do this in the GUI at `https://syncthing.epaflix.com`, after Authentik login):
  - Settings → Connections: **uncheck** Global Discovery, Enable Relaying, Enable NAT traversal. Leave Local Discovery on. Confirm Sync Protocol Listen Address `tcp://0.0.0.0:22000`.
  - Settings → General: Usage reporting → **No**.
  - Settings → GUI: set a GUI user + password (defense-in-depth behind Authentik).
  - Note the device ID + the address peers should use: `tcp://192.168.10.101:22000`.
- [ ] **Step 2: Storage check** — Syncthing GUI shows the data path under `/var/syncthing`; verify free space ≈ 1 TiB.
- [ ] **Step 3: Persistence check** — `kubectl --context epaflix delete pod -n syncthing -l app=syncthing`; after it restarts on worker-63, config + device ID persist (no re-pairing).
- [ ] **Step 4: Peer sync check** — add a second device (LAN and/or WireGuard) using `tcp://192.168.10.101:22000`; share a small folder; confirm it syncs and `.stversions` appears after an edit (file versioning).
- [ ] **Step 5: Backup check** — after the next 01:00 run (or trigger `vzdump 1063 --storage pbs-backup-local --mode snapshot`), confirm a snapshot of `1063` including the data disk exists in PBS.
- [ ] **Step 6: Update the PR test plan** — tick the boxes / append results **in the PR description** (never a new comment), per repo rule.
- [ ] **Step 7: Open follow-up `gh issue`s** on `SpyrosPsarras/epaflix`:
  - Renovate config entry for `syncthing/syncthing`.
  - TrueNAS `pool1` is a non-redundant stripe — track remediation now that backups no longer rely on it.
  - (Optional) split Syncthing config onto a small separate disk for config-only PBS coverage.

---

## Self-review notes

- **Spec coverage:** Phase 0 → Tasks 1-2; Phase 1a → Task 3; app manifests (1b) → Tasks 4-6, 8; Traefik entrypoint (1c) → Task 7; DNS (1d) → Task 10; Authentik (1e) → Task 11; ArgoCD wiring (1f) → Task 9; deploy → Task 12; layered backup + verification + follow-ups → Tasks 2/3/13. All spec sections mapped.
- **Image tag** `1.30.0` is a placeholder-to-pin: confirm the current `syncthing/syncthing` release at apply time (Task 5 Step 1 says so explicitly). Renovate then manages it.
- **Type/name consistency:** Service names `syncthing-gui` (8384) / `syncthing-sync` (22000), entrypoint `syncthing`, PVC `syncthing-data`, PV `syncthing-data-pv`, SC `syncthing-local`, node `k3s-worker-63`, UID/GID `1000` — used identically across Tasks 4-9.
- **Gates:** Tasks 1, 2, 3, 10, 11, 12 (and Step 2 of 12 force-push) are ⛔ — pause for owner sign-off.
