# Homelable replaces RackPeek

Research: [docs/research/2026-11-24-homelable.md](../../research/2026-11-24-homelable.md).

## Why

Spyros wants Homelable as the homelab index instead of RackPeek. Homelable adds a drawn network and rack canvas, live up/down checks and its own network scan. The index must keep what RackPeek gives today: every host, guest, Argo app and Kubernetes workload from the nightly sync, notes that survive syncs, and history in Forgejo.

## Goals

- G1. Homelable 3.8.0 runs in k3s, deployed by Argo CD from `2-k3s/40.homelable`, at `https://homelable.epaflix.com` on the internal entrypoint only.
- G2. The existing nightly sync (`images/rackpeek-sync`) writes its items to Homelable instead of RackPeek. Scanner, Kubernetes reader, gitleaks and the `homelab-live` commits stay.
- G3. Notes and the `gone-since` behaviour carry over. Notes written in RackPeek today are copied once.
- G4. Synced hosts and guests show live status (ping).
- G5. Homelable's state reaches Forgejo every night.
- G7. Every RackPeek name goes: the image, CronJob, RBAC, Secrets and ConfigMaps become `homelab-sync*`, `homelab-scan-key` and `homelab-known-hosts`; the scanner becomes `1-proxmox/homelab-scan` and `/usr/local/sbin/homelab-scan` on the 17 hosts (key comment and sudoers file too); the Forgejo bot becomes `homelab-bot` (Spyros renames it; its tokens keep working). Old design and plan documents keep their names as history.
- G6. After a parallel run and Spyros's yes, `2-k3s/37.rackpeek` and its data are removed.

## Non-goals

- `hostNetwork`, MAC addresses and mDNS. Revisit only if the network scan misses too much.
- The MCP container. Add it later with a key from SOPS.
- Homelable's Proxmox, UniFi, Unraid, Zigbee and Z-Wave importers. The sync already reads Proxmox through `pvesh`; a second source would make duplicate nodes.
- Changing what the host scanner reads.
- A drawn layout. The sync places nodes on a grid; people may move them.

## Anti-goals

- Any login to Homelable that skips Authentik, or any member outside `authentik Admins` getting in.
- `admin/admin`, `svc_changeme` or any default key left in place, even for a minute after first start.
- The sync deleting or rewriting a node a person drew.
- Anything from `t3code-work` or `mcp-hub-work` in Homelable, its Git repo or its logs.

## Approaches

- O4 (picked). Keep the sync and point its writer at Homelable. Keeps Kubernetes workloads in the index.
- W2 (picked) over W1 for workloads. W1 (every workload a node inside an app box) would add about 130 canvas cards and 130 Device Inventory rows, and draw apps with the Proxmox box style. W2 keeps one node per Argo app and puts its workloads in a sync-owned document. Cost: notes are per app, not per workload.
- O5. Homelable alone with its Proxmox import and scan. Rejected: loses the Kubernetes and host-config index, RackPeek's main job.
- O1 vs O2 for the pod. O1 picked: normal pod network with `NET_RAW`. O2 (`hostNetwork` on one node) rejected for now: a root container on a node's LAN interface outside NetworkPolicy, for MACs and mDNS only.
- O3 (Proxmox LXC from community scripts). Rejected: outside Argo CD, Renovate and the sync.

## Facts the design rests on (Homelable v3.8.0)

- One backend (FastAPI, SQLite at `/app/data`, port 8000, runs as root) and one nginx frontend (port 80, proxies `/api` and `/ws` to `BACKEND_UPSTREAM`). Images `ghcr.io/pouzor/homelable-{backend,frontend}`, digests in the research note.
- `POST /api/v1/canvas/save` takes one design's full node and edge list. It updates nodes by `id`, creates missing ones and deletes every node and edge of that design that is not in the payload. It also overwrites the design's `viewport` and `custom_style` (`backend/app/api/routes/canvas.py`).
- A node's `label`, `type`, `ip`, `notes`, `properties` and `check_*` live on a Device Inventory record, not on the node. Save matches a node to a record by `device_id`, then IP, then MAC, and overwrites the record (`backend/app/services/inventory_sync.py`, `find_device_for`, `link_facts`). Two nodes with the same IP on any designs share one record. Every non-furniture node gets a record; `group`, `groupRect` and `text` get none.
- Only type `proxmox` draws as a box when `container_mode` is on (`ProxmoxGroupNode.tsx`).
- The UI also saves the whole design, so a tab opened before a sync run and saved after it deletes the nodes that run added.
- A node has `notes`, `properties` (list of `{key, value, icon?}`), `parent_id` and `container_mode` for nesting, `check_method` and `check_target` for status, `pos_x` and `pos_y`.
- In OIDC mode local login is off. A script can only authenticate with `X-MCP-Service-Key`, which opens every endpoint.
- `POST /api/v1/git-sync` pushes documents and diagrams to a configured Git repo.
- Settings saved in the UI go to `/app/data/scan_config.json` and override the environment.

## Design

### Deployment (`2-k3s/40.homelable`)

Follow `2-k3s/37.rackpeek` file for file.

- Namespace `homelable`. Argo CD Application `homelable`, automated with prune, same as `app-rackpeek.yaml`.
- Deployment `homelable`, one replica, `Recreate`, two containers in one pod: `backend` and `frontend`, with `BACKEND_UPSTREAM=127.0.0.1:8000`. One pod keeps the backend off the Service, so only the frontend port is exposed.
- `backend` runs as root (the image needs it), `allowPrivilegeEscalation: false`, drop `ALL`, add `NET_RAW`. `frontend` drops `ALL` and adds only what nginx needs to start, found by testing.
- PVC `homelable-data-pithari`, 1Gi, `truenas-iscsi`, at `/app/data`.
- Probes: backend `GET /api/v1/health` on 8000, frontend `GET /` on 80.
- Images pinned as `3.8.0@sha256:...` in `kustomization.yaml` so Renovate updates them like RackPeek.
- Env from Secret `homelable-secrets` (SOPS, ksops): `SECRET_KEY` (at least 32 bytes, enforced in OIDC mode), `MCP_SERVICE_KEY`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `GIT_SYNC_TOKEN`. `LIVEVIEW_KEY`, `DOCS_VIEW_KEY` and `HOMEPAGE_API_KEY` stay unset: unset turns those no-login endpoints off, and any value turns them on (`backend/app/core/config.py`). `admin/admin` does not apply in OIDC mode, and `svc_changeme` is only the MCP container's default. Plain env: `AUTH_MODE=oidc`, `OIDC_*` URLs, `CORS_ORIGINS=https://homelable.epaflix.com`, `SCANNER_RANGES=192.168.10.0/24`, `STATUS_CHECKER_INTERVAL=60`, `GIT_SYNC_*` repo settings. The agent generates every random value inside the command that encrypts it.
- `scan_config.json` wins over env. An init container deletes it at every start, so Git stays the source of truth. Shortcut: UI setting changes are lost on restart, which is intended.
- Service `homelable` on port 80 (frontend).
- IngressRoute on entrypoint `internal`, host `homelable.epaflix.com`, middlewares `security-headers` and a namespace-local headers middleware that removes `X-MCP-Service-Key` from requests. The service key then works only inside the cluster. No forward auth: OIDC is the gate, as with Immich.
- Authentik: provider and application `homelable` in the encrypted IaC blueprint, redirect URI `https://homelable.epaflix.com/api/v1/auth/oidc/callback`, policy binding to `authentik Admins`. Homelable has no group check, so this binding is the only gate. Client id and secret change in both places in the same commit.
- NetworkPolicy ingress: Traefik pods, and the sync pod to port 80. Egress: kube-dns, Traefik 8443 (Authentik OIDC and `git-internal.epaflix.com`), `192.168.10.0/24` and `10.0.0.0/24` for scans and status checks. Nothing to the internet; the browser's GitHub and jsDelivr calls come from the browser, not the pod.
- DNS: `address=/homelable.epaflix.com/192.168.10.102` in `1-proxmox/pihole/dnsmasq.d/10-epaflix.conf` and the matching Unbound `local-zone`, copied to Pi-hole with Spyros's approval, as in the RackPeek README.
- README in the folder, same shape as RackPeek's.

### Sync

The CronJob moves from namespace `rackpeek` to `homelable` and is renamed `homelab-sync`; the image becomes `ghcr.io/spyrospsarras/homelab-sync` (folder `images/homelab-sync`, workflow renamed). RBAC, known hosts, scan key Secret, NetworkPolicy and schedule move with it unchanged apart from names. `build.py` and the scanner stay as they are; only the writer and the RackPeek I/O in `sync.py` change.

Writer:

1. The sync owns one design named `Homelab index`. It finds it by name (`GET /api/v1/designs`) and creates it on first run. People draw on other designs; the sync never writes to them.
2. Node `id` is `uuid5(fixed namespace, item name)`, so the same item keeps its id across runs.
2a. IPs: hosts and VMs in the `HOSTS` env get that address; LXCs get `net0 ip=` and cloud-init VMs `ipconfig0` from the existing `pve.guests` config. DHCP guests get none. `hw/<host>` gets the address and `pve/<host>` does not, so the two never share a record. A synced node with an IP shares its inventory record with Homelable's own scan and with any node drawn elsewhere at that IP. That is Homelable's model (a device is the same everywhere); the sync owns `label`, `type` and its properties on those records, people own `notes` and other properties. Nodes without an IP get their own record.
3. Each RackPeek item except kind `Service` becomes one node: `label` = item name, `type` from item kind (Server, Desktop, Proxmox host, VM, LXC, NAS, generic for k3s objects), `properties` = item labels as `key`/`value`, plus `sync: yes`. `notes` comes from the stored node.
4. Nesting: guests have `parent_id` of their `pve/<host>` node (type `proxmox`, `container_mode: true`).
4a. Items of kind `Service` (Kubernetes workloads `<ns>/<kind>/<name>` and `truenas/app/<name>`, about 130) are not nodes. Each node that is the `runs_on` parent of such items (an `app/*` node or `vm/truenas`) gets one sync-owned document, title `<node label> workloads`, attached by `node_id`. Its body is a Markdown table, one row per workload, with the item's labels as columns (image, nodes, volumes, Git status). A workload that disappears leaves the table; the document's revision history shows when. The sync creates it with `POST /api/v1/documents` and rewrites it with `PATCH /api/v1/documents/{id}` and `revision_reason` set to the sync, only when the body changed, so the 50 kept revisions are real changes. People write in the node's notes, not in this document; the README says so. A failed source leaves its documents untouched.
5. Status: host and guest nodes with an IP get `check_method: ping`, `check_target: <ip>`. Others get none.
6. Carry-over before save: read the design (`GET /api/v1/canvas?design_id=...`). Send back the stored edges, `viewport` and `custom_style`. For each stored node keep `notes`, position, size, custom colours and icon, and properties that the sync does not set. A stored node a successful source no longer reports gets property `gone-since: <date>` and stays. Nodes from a failed source are sent back unchanged. A stored node without property `sync` is sent back unchanged. This replaces `build.carry_over` for the Homelable shape; the rules are the ones in the RackPeek sync plan.
7. New nodes get grid positions inside their parent.
8. One `POST /api/v1/canvas/save` with the full list. If the read in step 6 failed, nothing is saved and the Job exits 1, because a save without the stored nodes would delete them.
9. Then `POST /api/v1/git-sync`. Failure sets exit 1 but keeps the save. Pushed diagrams come from `load_canvas` and include notes and properties (`routes/git_sync.py`, `_collect`). They also include `status` and `last_seen`, so expect one commit a night.
10. Auth header `X-MCP-Service-Key` from Secret `homelab-sync-homelable` (same value as in `homelable-secrets`). URL `http://homelable.homelable.svc`.

Labels and notes pass through `build.scrub` as today.

### History

Forgejo repo `homelab-bot/homelable-config` (private), pushed over `https://git-internal.epaflix.com` with a token for the bot, as RackPeek does. The Forgejo network policy admits the `homelable` namespace next to `rackpeek`; the `rackpeek` line goes in cut-over step 5. Git sync settings come from env only, and the first push works on an empty repo. Spyros creates the token (owner step); the agent adds it to SOPS through a hidden-input card.

### Migration and cut-over

1. Deploy Homelable with the sync still writing to RackPeek.
2. A one-off Job reads RackPeek `GET /api/inventory`, builds the same nodes the sync would and carries RackPeek notes and non-`sync` tags into them. Notes on `Service` items are appended to their parent node's notes under a `### <item name>` heading. Items without tag `sync` (made by hand in RackPeek) come over as nodes without `sync`. Before it runs, the agent reports how many `Service` items have notes.
3. Switch the sync to Homelable. Moving the CronJob makes Argo delete `rackpeek/rackpeek-sync`, so RackPeek stops updating and stays as a read-only copy. The one-off copy from step 2 runs in this same step, so nothing written to RackPeek before the switch is lost. From here Homelable is the only place for notes, including the homelab agent's notes duty.
4. Keep RackPeek for at least a week as a fallback. Compare item counts at the switch and spot-check notes.
5. With Spyros's yes: a PR removes `2-k3s/37.rackpeek`, `app-rackpeek.yaml`, the DNS entries and the Forgejo policy line, and the RackPeek repo `homelab-bot/rackpeek-config` is archived, not deleted. The PVC goes with the namespace, so this is the data-deletion step.
6. Update references in step 3's PR: `2-k3s/13.t3code/env/isolated-runtime.md` (Forgejo ingress sentence), `README.md`, `1-proxmox/homelab-scan/README.md`, and the homelab agent's notes duty (`t3code-homelab` item notes now go to Homelable via the service key).

## Risks

- R1. The service key opens everything and the frontend forwards it. Mitigations: random key in SOPS, internal entrypoint only, OIDC in front of the UI. A LAN client that knows the key still has full access.
- R2. `canvas/save` deletes absent nodes. A bug in carry-over loses notes. The test suite covers: failed read means no save, failed source keeps its nodes, hand-made nodes survive, notes and positions survive.
- R3. About 95 nodes on one canvas (225 items from the 2026-11-26 sync, minus about 130 workloads). The grid may be hard to read. Spyros can split it later; the design does not depend on layout.
- R4. Root container with `NET_RAW`. Contained by NetworkPolicy, no `hostNetwork`, drop `ALL`.
- R5. One-person upstream. Pinned digest and Renovate; SQLite file and Forgejo copy make leaving easy.
- R7. A stale browser tab saved on `Homelab index` after a sync deletes that night's new nodes. The next run recreates them. Accepted; the README says to draw on other designs and leave `Homelab index` to the sync.
- R8. Every synced node adds a Device Inventory row. With workloads in documents that is about 95 rows: machines, guests and Argo apps.
- R6. Upgrades: the backend copies the DB to `homelab.db.back-<version>` once per version. No other backup beyond the TrueNAS volume snapshots and the Forgejo push.

## Left to the builder

- Whether the frontend container needs `CHOWN`, `SETUID`, `SETGID` or `NET_BIND_SERVICE` to start, and whether `readOnlyRootFilesystem` works for either container.
- The exact `type` value per item kind, from Homelable's 15 node types.
- The grid sizes.
- Renovate grouping for the two images, and no automerge for Homelable minor or major versions, because upgrades migrate the SQLite file one way.
