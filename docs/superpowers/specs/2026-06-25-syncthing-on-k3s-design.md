# Syncthing on K3s (RAID-backed, internal-only) + PBS datastore move — design

> Date: 2026-06-25
> Status: design approved, pending spec review
> Repo for cluster work: `SpyrosPsarras/epaflix` (this repo)
> Proxmox host ops (Phase 0 + disk creation) are out-of-repo, run over SSH with `~/.ssh/id_rsa_k3s`.

## Purpose

Run a self-hosted **Syncthing** node on the K3s cluster as an always-on sync peer for the owner's LAN devices and WireGuard-connected devices. The data is treated as **critical** and must live on **RAID-redundant Proxmox-local storage — never TrueNAS**. ~1 TB capacity. Internal-only: nothing is exposed to the public internet; global discovery and relays are disabled.

Because the data is critical, the work also fixes the backup substrate: today **all** Proxmox Backup Server (PBS) backups sit on a **non-redundant TrueNAS stripe**. Phase 0 moves the PBS datastore onto takaros hardware RAID and grows it so it can actually hold the Syncthing data. This is a cluster-wide durability win, not Syncthing-only.

## Constraints / decisions (locked with owner)

- Storage backend: **Proxmox `local-raid`** (hardware RAID, lvmthin). **No TrueNAS** for this service.
- Capacity: **1 TiB, thin-provisioned**.
- Pod placement: **`k3s-worker-63`** — chosen as the worker with the lowest RAM usage at design time (4.6 GiB / 20%). It runs on **`evanthoulaki`** (VMID `1063`).
- The data disk must be on the same host as the worker VM → disk on **evanthoulaki `local-raid`**.
- GUI: internal hostname only, `syncthing.epaflix.com`, behind **Authentik forward-auth** (cluster SSO standard).
- Sync protocol: routed through the **existing Traefik LB `192.168.10.101`** via a dedicated **TCP entrypoint on 22000** — no new LoadBalancer IP. TCP only (Syncthing falls back from QUIC).
- Backup: PBS stays on **takaros** (LXC `1031`), Syncthing source on **evanthoulaki** → **off-host backup on purpose** (do NOT co-locate). Layered with RAID + Syncthing file versioning.

## Cluster facts gathered (2026-06-25)

- `local-raid` exists on **both** hosts: lvmthin, ~2.18 TB total, ~1.9 TiB free each. Backed by a single hardware-RAID logical volume (`/dev/sda`, model `LOGICAL VOLUME`) → redundant.
- `k3s-worker-63` (VMID `1063`, evanthoulaki): `scsi0 = local-raid:vm-1063-disk-0,size=50G` boot, `scsihw: virtio-scsi-pci`, 8 cores, 22 GiB RAM. New disk attaches as **`scsi1`**.
- LB IP pool: Traefik uses `192.168.10.101` (range `.101-.109`); `range-global` is `.110-.199`. Live LBs: `.101` traefik, `.105-.108` postgres.
- Traefik entrypoints today: `web` (80), `websecure` (443), `metrics` (9100, internal).
- PBS = LXC **`1031`** (`proxmox-backup-server.epaflix.com`) on **takaros**. `rootfs` on `local-raid` (10 G). Datastore is `mp0: VMs:1031/vm-1031-disk-0.raw, mp=/mnt/VMs, size=300G` — a 300 G raw file on the **`VMs` NFS storage = TrueNAS** (`192.168.10.200:/mnt/pool1/dataset01/VMs`). `pool1` is a 2-disk **stripe — no redundancy**.
- PBS datastore today: ~294 GiB total, ~176 GiB used. PVE backup job `backup-ef3c2d49-5f5a` runs **daily 01:00**, mode `snapshot`, zstd, `prune-backups keep-all=1`, and **already includes VMID `1063`**.

## Architecture / data flow

```
GUI:
  browser/WG client -> https://syncthing.epaflix.com
    -> Pi-hole A record -> 192.168.10.101 (Traefik LB)
    -> Traefik IngressRoute (websecure, wildcard *.epaflix.com cert)
       + authentik-forwardauth middleware (Authentik SSO)
    -> Service syncthing-gui:8384 (ClusterIP)
    -> Syncthing pod (pinned to k3s-worker-63)

Sync protocol:
  LAN / WG peer -> tcp://192.168.10.101:22000
    -> Traefik TCP entrypoint `syncthing` (22000)
    -> IngressRouteTCP HostSNI(`*`)  (L4 passthrough; Syncthing does its own TLS)
    -> Service syncthing-sync:22000 -> Syncthing pod

Storage:
  Syncthing pod  ->  local PV (nodeAffinity: k3s-worker-63)
    -> hostPath /mnt/syncthing-data  (ext4 on scsi1, evanthoulaki local-raid, hardware RAID)
       /mnt/syncthing-data/config   (Syncthing config + device certs)
       /mnt/syncthing-data/data     (synced folders + .stversions)
```

---

## Phase 0 — Migrate + grow the PBS datastore (gated infra; Proxmox host ops on takaros)

Goal: move LXC `1031`'s `mp0` datastore off TrueNAS NFS onto takaros `local-raid`, grow to ~1 TB, keep all existing backups, then reclaim the old NFS space.

Steps (run on `root@192.168.10.10` / takaros):

1. **Pre-checks.** Confirm takaros `local-raid` free ≥ ~350 G for the move (have ~1.9 TiB). Confirm no backup job is running (do this outside the 01:00 window). Note current datastore usage (~176 GiB).
2. **Stop PBS LXC:** `pct stop 1031`. (Pauses backups briefly.)
3. **Move the volume (block-level, preserves data):** `pct move-volume 1031 mp0 local-raid --delete 0`. Keep `--delete 0` so the source NFS copy survives until verified; cleanup is step 8. This rewrites `1031.conf` `mp0` to `local-raid:vm-1031-disk-1` (new), mount stays `/mnt/VMs`.
4. **Grow to 1 TB:** `pct resize 1031 mp0 1000G`. For an ext4 mp Proxmox grows the filesystem automatically; verify inside the container (`df -h /mnt/VMs`), run `resize2fs` only if needed.
5. **Start PBS:** `pct start 1031`. Confirm the datastore mounts and is writable.
6. **Integrity verify (the safety gate):** inside the container run
   `proxmox-backup-manager datastore list` and a **verify** of the datastore
   (`proxmox-backup-client` / GC + verify job), confirming existing snapshots are readable on the new location. Also run **garbage-collect**.
7. **Set a real prune policy** (replaces `keep-all=1`, which would silently fill 1 TB): on the PVE side update the job / storage `prune-backups` to e.g. `keep-daily=7,keep-weekly=4,keep-monthly=3`. Run a prune to confirm.
8. **Cleanup (only after step 6 passes):** delete the old NFS raw `VMs:1031/vm-1031-disk-0.raw` to reclaim ~300 G on TrueNAS `pool1`. If `pct move-volume` already left an unreferenced source disk, remove it via `pvesm free VMs:1031/vm-1031-disk-0.raw` (or the PVE UI "Unused Disk" → Remove). Verify it is no longer referenced by `1031.conf` first.
9. **Rename (cosmetic):** the PBS datastore is named `VMs-NFS` and is now misnamed. Rename to `backups-local` (PBS datastore display name + the PVE `pbs:` storage `datastore` field). Optional; do only if low-risk, otherwise leave and note it.

Rollback for Phase 0: if verify fails at step 6, `pct stop 1031`, `pct move-volume 1031 mp0 VMs` back to NFS (source still intact because `--delete 0`), `pct start 1031`. No backup history lost.

Tradeoff (accepted): takaros `local-raid` is redundant but shares a host with the masters + workers 61/62. It is still off-host from the Syncthing source (evanthoulaki), so a Syncthing/evanthoulaki loss is covered. A whole-takaros loss would lose takaros's own VM backups — inherent to a single-PBS design; RAID redundancy was the priority.

---

## Phase 1 — Syncthing on K3s

### 1a. Proxmox disk (gated infra; host op on evanthoulaki)

- Create + attach a **1 TiB thin** disk to worker-63:
  `qm set 1063 -scsi1 local-raid:1024` (lvmthin → thin-provisioned; ~1.9 TiB free).
- Inside the VM (`ubuntu@192.168.10.63`, via the host or jumpbox): identify the new device, `mkfs.ext4`, mount at **`/mnt/syncthing-data`**, add an `fstab` entry (by `UUID`, `nofail`).
- Create subdirs `config/` and `data/`, `chown` to the UID/GID Syncthing runs as (default `1000:1000`).
- Confirm worker-63 stays in PBS job `backup-ef3c2d49-5f5a`, and that `scsi1` is **not** marked `backup=0` (so Phase 0's grown datastore now covers the Syncthing data).

### 1b. K3s app `2-k3s/15.syncthing/` (GitOps)

Mirrors `2-k3s/09.filebrowser/` + `2-k3s/14.searxng/` conventions (kustomize + Traefik IngressRoute + ArgoCD app). No Secret needed unless we choose to set a fixed GUI password (forward-auth fronts it, so a Secret is optional — see open note).

Files:

- `namespace.yaml` — namespace `syncthing`.
- `storage.yaml`:
  - `StorageClass` `syncthing-local` — `provisioner: kubernetes.io/no-provisioner`, `volumeBindingMode: WaitForFirstConsumer`, `reclaimPolicy: Retain`.
  - `PersistentVolume` `syncthing-data-pv` — `1Ti`, `ReadWriteOnce`, `persistentVolumeReclaimPolicy: Retain`, `storageClassName: syncthing-local`, **`local.path: /mnt/syncthing-data`** with **`nodeAffinity`** requiring `kubernetes.io/hostname in [k3s-worker-63]`. (`local` volume + nodeAffinity is the correct node-pinning primitive, vs filebrowser's hostPath which relied on NFS being on every node.)
  - `PersistentVolumeClaim` `syncthing-data` (ns `syncthing`) — `1Ti`, RWO, `storageClassName: syncthing-local`.
- `deployment.yaml`:
  - image `syncthing/syncthing:<pinned-tag>` — pin to current latest release, confirmed at apply time; Renovate (`12.renovate`) manages bumps.
  - **1 replica, `strategy: Recreate`** (RWO single-attach).
  - `nodeAffinity`/`nodeSelector` → `k3s-worker-63` (matches the PV; explicit for clarity).
  - env `PUID=1000`, `PGID=1000` (must match the host `chown`), `STGUIADDRESS=0.0.0.0:8384`.
  - volumeMount the PVC at `/var/syncthing`, with config under `/var/syncthing/config` and synced data under `/var/syncthing/data` (subPaths or a single mount with both subdirs). Aligns with the host `/mnt/syncthing-data/{config,data}`.
  - liveness/readiness probe → `GET /rest/noauth/health` on 8384 (returns `{"status":"OK"}`, no auth).
  - resource requests `cpu: 250m`, `memory: 512Mi`, **no limits** (repo convention; initial 1 TB hashing is CPU-bursty, must not be throttled).
  - `securityContext` consistent with the official image (runs as `PUID:PGID`).
- `service-gui.yaml` — ClusterIP `syncthing-gui`, port `8384` named `http`.
- `service-sync.yaml` — ClusterIP `syncthing-sync`, port `22000` named `sync` (TCP). Target for the IngressRouteTCP.
- `ingress-gui.yaml` — Traefik `IngressRoute`s mirroring the qBittorrent forward-auth pattern (`08.servarr/qbittorrent/`):
  - `websecure`, `Host(\`syncthing.epaflix.com\`)`, priority 10, middleware `authentik-forwardauth@traefik-system` → `syncthing-gui:8384`, `tls.certResolver: cloudflare`, domains `epaflix.com` + `*.epaflix.com`.
  - outpost route `PathPrefix(/outpost.goauthentik.io/)` priority 15 (Authentik callback).
  - `web` route → `redirect-https@traefik-system`.
- `ingressroute-tcp.yaml` — `IngressRouteTCP`, `entryPoints: [syncthing]`, route `HostSNI(\`*\`)` → `syncthing-sync:22000`. (No TLS block → L4 passthrough.)
- `kustomization.yaml` — lists the above.
- `README.md` + `QUICKSTART.md` — what it is, how to verify, how to roll back (repo convention).

Syncthing config hardening (set on first run via the config, or seed a `config.xml`):
- Global discovery **off**, relays **off**, NAT traversal **off**, usage reporting **off** (internal-only; peers use explicit static addresses).
- Local discovery (LAN broadcast 21027/UDP) may stay on for same-subnet auto-find; WG peers are added with static address `tcp://192.168.10.101:22000`.
- GUI listens on `0.0.0.0:8384`; GUI auth left to Authentik forward-auth (built-in GUI password optional, see open note).

### 1c. Traefik TCP entrypoint (GitOps; edits shared Traefik)

In `2-k3s/05.traefik-deployment/values/traefik-values.yaml`:
- Add port/entrypoint `syncthing`: `port: 22000`, `exposedPort: 22000`, `protocol: TCP`.
- Ensure the Traefik LB Service exposes `22000/TCP` (Helm `ports.syncthing` does this on `.101`).
- This triggers a Traefik rollout (small, GitOps-managed). Blast radius noted.

### 1d. DNS (gated; Pi-hole host op)

- Add `syncthing.epaflix.com → 192.168.10.101` to `/etc/dnsmasq.d/10-epaflix.conf` on Pi-hole (`192.168.10.30`). **dnsmasq.d only**, never the web UI / custom.list (golden rule). `pihole reloaddns`.

### 1e. Authentik (gated; Authentik UI/blueprint)

- Create an Authentik **Proxy Provider** (forward-auth, external host `https://syncthing.epaflix.com`) + **Application**, and bind it to the existing forward-auth **outpost** that backs `authentik-forwardauth@traefik-system` — same path the servarr forward-auth rollout used (issue #176). Authorize the appropriate user/group.

### 1f. ArgoCD wiring (GitOps)

- `2-k3s/11.argocd/apps/app-syncthing.yaml` — Application, source path `2-k3s/15.syncthing`, dest namespace `syncthing`, automated sync (mirror `app-searxng.yaml`).
- Add `app-syncthing.yaml` to `2-k3s/11.argocd/apps/kustomization.yaml`.
- Adoption order (CLAUDE.md): commit + push all aligned git **before** the Application reconciles. Since everything here is net-new, the Application creation is the deploy.

---

## Backup strategy (layered; decided in-session, no deferral)

1. **Hardware RAID** (`local-raid`, both hosts) — disk-failure protection for live data and for the PBS datastore. Phase 0 puts the datastore on RAID.
2. **Syncthing staggered file versioning** — enabled per shared folder, stored in `.stversions` on the 1 TB volume. Recovers accidental deletes/edits that peers propagate (Syncthing propagates deletions). The real "oops" net.
3. **PBS** — worker-63's nightly 01:00 job (already includes VMID `1063`) now covers the 1 TB data disk, with incremental dirty-bitmap + dedup (daily cost ≈ change rate), stored off-host on takaros `local-raid`, under a real prune policy (Phase 0 step 7).

## Order of operations (gated steps marked ⛔)

1. ⛔ Phase 0: PBS datastore move + grow + verify + prune policy (takaros).
2. ⛔ Phase 0 cleanup: delete old NFS raw after verify passes; rename datastore (optional).
3. ⛔ Phase 1a: create + attach + format + mount the 1 TiB disk on worker-63 (evanthoulaki).
4. Phase 1b/1c/1f: author `2-k3s/15.syncthing/`, the Traefik values edit, and the ArgoCD app on a branch → PR.
5. ⛔ Phase 1e: Authentik provider/application + outpost binding.
6. ⛔ Phase 1d: Pi-hole DNS record.
7. ⛔ Deploy: merge PR (rebased) → ArgoCD syncs. Verify.

## Verification / acceptance

- Phase 0: `pvesm status -storage pbs-backup-local` shows ~1 TB; a PBS verify job passes; a fresh backup of `1063` succeeds; old NFS raw gone; TrueNAS `pool1` reclaimed ~300 G.
- ArgoCD app `syncthing` **Synced + Healthy**, zero live drift.
- Pod runs on `k3s-worker-63`; `df` inside the pod shows the 1 TB mount; data persists across a pod delete.
- `https://syncthing.epaflix.com` reachable on LAN + over WireGuard, gated by Authentik; GUI loads.
- A second device pairs and syncs over `tcp://192.168.10.101:22000`; no public discovery/relay traffic.
- File versioning visible (`.stversions`) after an edit; a nightly PBS snapshot of `1063` including the data disk appears.

## Risks / tradeoffs

- **Single-node pinning:** Syncthing is tied to worker-63 (and evanthoulaki). Acceptable — Syncthing is single-instance by design; if the node is down, sync pauses, no data loss.
- **Editing shared Traefik values** for the TCP entrypoint = a Traefik rollout. Low risk, reversible.
- **takaros-local PBS** = redundant but not a separate site (see Phase 0 tradeoff).
- **Thin provisioning** on both pools: the 1 TiB data disk and the 1 TB datastore are thin. ~1.9 TiB free per pool today; the prune policy + monitoring keep the datastore bounded. Watch pool fill.
- **Authentik forward-auth** fronts the GUI but Syncthing's own GUI/API is reachable in-cluster; the API key should still be set so the GUI isn't passwordless behind the proxy.

## Follow-ups (open `gh issue` on `SpyrosPsarras/epaflix`)

- Renovate config entry for `syncthing/syncthing` image tag.
- Confirm/return TrueNAS `pool1` space after Phase 0 cleanup; consider whether `pool1` stripe (no redundancy) needs its own remediation issue.
- Optional later: split Syncthing config onto a tiny separate disk if we ever want config-only PBS coverage independent of the data disk.

## Out of scope

- Public exposure / global discovery / relays (explicitly internal-only).
- Multi-replica / HA Syncthing.
- Migrating other backups' retention beyond what Phase 0 sets for this job.
