# Homelable replaces RackPeek Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Homelable 3.8.0 runs in k3s behind Authentik and becomes the homelab index, fed by the existing nightly sync, after which RackPeek is removed.

**Architecture:** One pod (backend + nginx frontend) in namespace `homelable`, deployed by Argo CD from `2-k3s/40.homelable`. The sync image keeps its scanner, Kubernetes reader and `homelab-live` commits; a new module converts its items to one Homelable design (`Homelab index`) and per-app workload documents, written with `X-MCP-Service-Key`. First run seeds notes from RackPeek.

**Tech Stack:** Kubernetes, kustomize + ksops/SOPS, Argo CD, Traefik IngressRoute and Middleware, Authentik IaC blueprint, Python 3 standard library (`unittest`), GitHub Actions + GHCR.

**Spec:** `docs/superpowers/specs/2026-11-24-homelable-design.md`. Research: `docs/research/2026-11-24-homelable.md`.

## Global Constraints

- Images: `ghcr.io/pouzor/homelable-backend:3.8.0@sha256:f4f8b8061af27084c793803614e0009f3440fbbf7cc12033438868a86452d60a`, `ghcr.io/pouzor/homelable-frontend:3.8.0@sha256:629a18054f13b54e9c6dfbab059aca27bbf4bcc083a8366d43706d59aa5c6520`, pinned in `kustomization.yaml` `images:`.
- Host `homelable.epaflix.com`, Traefik entrypoint `internal` only, address `192.168.10.102`.
- `AUTH_MODE=oidc`; redirect URI `https://homelable.epaflix.com/api/v1/auth/oidc/callback`; Authentik policy binding to group `authentik Admins`.
- `LIVEVIEW_KEY`, `DOCS_VIEW_KEY`, `HOMEPAGE_API_KEY` are never set. No MCP container.
- `SECRET_KEY` at least 32 bytes. Secrets are generated inside the command that encrypts them; no secret value in output, logs or Git plaintext.
- Design name `Homelab index`. Node id `str(uuid5(NS, item_name.lower()))` with one fixed `NS` constant.
- Node property `sync` = `yes` marks sync-owned nodes. The sync never writes another design.
- Never read, list or name anything in `t3code-work` or `mcp-hub-work` (existing `build.excluded`).
- Schedule `30 3 * * *`, `Europe/Athens`, `concurrencyPolicy: Forbid`, unchanged.
- No comments in code. Conventional commit titles, no co-author.
- Deleting RackPeek (Task 8) needs Spyros's explicit yes. Copying DNS files to Pi-hole and renaming the scanner on hosts are approved (2026-11-24).
- Names: image and CronJob `homelab-sync`, ClusterRoles `homelab-sync-reader` and `homelab-sync-cluster-reader`, Secrets `homelab-scan-key` and `homelab-sync-secrets`, ConfigMap `homelab-known-hosts`, scanner `1-proxmox/homelab-scan/homelab-scan.py` installed as `/usr/local/sbin/homelab-scan`, key comment `homelab-scan`, sudoers `/etc/sudoers.d/homelab-scan`, Forgejo bot `homelab-bot` (owner renames `rackpeek-bot`), Git author `homelab-sync <homelab-bot@epaflix.com>`.

## PRs

- PR A: Tasks 1 to 3 (Homelable runs, empty).
- PR B: Tasks 4 to 7 (sync writes Homelable, seeds from RackPeek).
- PR C: Task 8, at least a week after PR B and only with Spyros's yes.

## Review Focus

1. Homelable unreachable or returns 5xx on the canvas read: nothing is saved, no document is written, exit 1. Test in Task 6.
2. A node drawn by hand on `Homelab index` (no `sync` property), and an edge someone drew: both are in the save payload unchanged. Test in Task 5.
3. The Kubernetes source fails: app nodes keep their last facts and their workload documents are not rewritten. Test in Task 6.
4. A request through the ingress carrying `X-MCP-Service-Key` gets no access. Check in Task 3.
5. Seed when RackPeek is down: no design is created, nothing saved, exit 1, and the next run seeds again. Test in Task 6.

---

### Task 1: Homelable app folder

**Files:**
- Create in `2-k3s/40.homelable/`: `namespace.yaml`, `storage.yaml`, `deployment.yaml`, `service.yaml`, `ingress.yaml`, `network-policy.yaml`, `ksops-generator.yaml`, `secrets.enc.yaml`, `kustomization.yaml`, `README.md`
- Create: `2-k3s/11.argocd/apps/app-homelable.yaml`
- Modify: `2-k3s/11.argocd/apps/kustomization.yaml`, `.github/renovate.json`, `1-proxmox/pihole/dnsmasq.d/10-epaflix.conf`, `1-proxmox/pihole/unbound-no-aaaa-leak.conf`

**Interfaces:**
- Produces: Service `homelable.homelable.svc` port 80; Secret `homelable-secrets` keys `SECRET_KEY`, `MCP_SERVICE_KEY`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `GIT_SYNC_TOKEN`; pod label `app: homelable`.

- [ ] **Step 1: Manifests.** Copy the shape of `2-k3s/37.rackpeek` (namespace, PVC, Deployment, Service, one IngressRoute without the Authentik outpost route, NetworkPolicy, ksops). Differences, all from the spec's Deployment section:
  - PVC `homelable-data-pithari`, 1Gi, `truenas-iscsi`, mounted at `/app/data` in `backend`.
  - Init container `reset-settings` (backend image) runs `rm -f /app/data/scan_config.json`.
  - `backend`: `allowPrivilegeEscalation: false`, drop `ALL`, add `NET_RAW`; env from `homelable-secrets` plus `AUTH_MODE=oidc`, `OIDC_*` URLs per Homelable `docs/oidc-auth.md` for Authentik slug `homelable`, `CORS_ORIGINS=https://homelable.epaflix.com`, `SCANNER_RANGES=192.168.10.0/24`, `STATUS_CHECKER_INTERVAL=60`, `GIT_SYNC_*` for `https://git-internal.epaflix.com/homelab-bot/homelable-config.git` branch `main` user `rackpeek-bot`. Probe `GET /api/v1/health` port 8000.
  - `frontend`: `BACKEND_UPSTREAM=127.0.0.1:8000`, drop `ALL`, add back only the capabilities nginx needs (find by running it in Task 3). Probe `GET /` port 80.
  - Middleware `strip-service-key` (headers, `customRequestHeaders: {X-MCP-Service-Key: ""}`) in namespace `homelable`, on the IngressRoute after `security-headers`. No forward auth.
  - NetworkPolicy ingress: Traefik pods to 80; pods labelled `app: homelab-sync` to 80. Egress: kube-dns, Traefik 8443, `192.168.10.0/24`, `10.0.0.0/24`.
- [ ] **Step 2: Secrets.** Write `secrets.enc.yaml` (Secret `homelable-secrets`) with `vault run --name sops-age-k3s-cluster ... sops set`/`sops -e -i`, generating `SECRET_KEY` and `MCP_SERVICE_KEY` with `openssl rand -hex 32` inside that command. `OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET` are generated here too and reused in Task 2. `GIT_SYNC_TOKEN` copies `GIT_TOKEN` from `2-k3s/37.rackpeek/secrets.enc.yaml` (rackpeek-bot's token) by decrypting both to a private temp dir and deleting it after.
- [ ] **Step 3: Argo, Renovate, DNS, README.** `app-homelable.yaml` copies `app-rackpeek.yaml` with path `2-k3s/40.homelable`, namespace `homelable`. Renovate: one `packageRules` entry grouping `ghcr.io/pouzor/homelable-backend` and `-frontend` (`groupName: homelable`) with `automerge: false` for minor and major. DNS: `address=/homelable.epaflix.com/192.168.10.102` and `local-zone: "homelable.epaflix.com." static` next to the rackpeek lines. README: what it is, login, data, secrets, the "leave `Homelab index` to the sync" rule, and the stale-tab risk.
- [ ] **Step 4: Verify.** `vault run --name sops-age-k3s-cluster --password-env VAULT_SOPS_AGE_KEY -- sh -c 'SOPS_AGE_KEY="$VAULT_SOPS_AGE_KEY" kustomize build --enable-alpha-plugins --enable-exec 2-k3s/40.homelable > <private tmp>/out.yaml'`, then `kubectl apply --dry-run=server -f <private tmp>/out.yaml` succeeds and `grep -c 'kind: Secret'` shows 1. Delete the temp file. `git diff` shows no plaintext secret.
- [ ] **Step 5: Commit** `feat(homelable): deploy Homelable 3.8.0 behind Authentik OIDC`.

### Task 2: Authentik OIDC application

**Files:**
- Modify: `2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml`

**Interfaces:**
- Consumes: `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` from Task 1 (same values).

- [ ] **Step 1:** Decrypt to a private temp file, copy the `immich` provider, application and policy binding entries, rename to `homelable`, redirect URI from Global Constraints, bind only `authentik Admins`, drop the Immich-only scope mapping. Set the client id and secret from `homelable-secrets` inside the same command. Re-encrypt in place, delete the temp file.
- [ ] **Step 2: Verify** with `2-k3s/07.authentik-deployment/files/check_authentik_blueprint.py` the way its CronJob runs it, on the decrypted temp copy. Expected: no errors.
- [ ] **Step 3: Commit** `feat(authentik): homelable OIDC application for authentik Admins`.

### Task 3: Deploy and smoke test (after PR A merges)

- [ ] **Step 1:** Ask Spyros to approve copying the two DNS files to Pi-hole, then do it as in `1-proxmox/pihole/README.md`.
- [ ] **Step 2:** Request the Argo sync of `homelable` at the merged SHA (dry-run first, per the access guide).
- [ ] **Step 3: Checks**, record each result in the PR:
  - Pod Ready; frontend started with the capabilities set in Task 1 (adjust in a follow-up commit if not).
  - `curl -s -o /dev/null -w '%{http_code}' -H 'X-MCP-Service-Key: x' https://homelable.epaflix.com/api/v1/designs` from the LAN returns 401. The same from inside the namespace with the real key (key read inside the command, never printed) returns 200.
  - Login through Authentik as an `authentik Admins` member works; a user outside the group is refused by Authentik.
  - `kubectl -n homelable exec deploy/homelable -c backend -- ping -c1 192.168.10.30` succeeds.
  - `/liveview`, `/docsview` and `/api/v1/stats/summary` refuse without login.

### Task 4: Rename the sync image

**Files:**
- Move: `images/rackpeek-sync/` to `images/homelab-sync/`; `.github/workflows/build-rackpeek-sync.yml` to `build-homelab-sync.yml`; `1-proxmox/rackpeek-scan/` to `1-proxmox/homelab-scan/` (`homelab-scan.py`, `test_homelab_scan.py`)
- Modify: the workflow (paths, image `ghcr.io/spyrospsarras/homelab-sync`), `Dockerfile` (COPY paths, user name), `test.sh` default tag, `sync.py` scanner import path and Git author, the scanner README

- [ ] **Step 1:** `git mv`, then update names. No behaviour change.
- [ ] **Step 2: Verify:** `cd images/homelab-sync && python3 -m unittest discover -p 'test_*.py'` passes with the same count as before the move.
- [ ] **Step 3: Commit** `refactor(homelab-sync): rename the rackpeek-sync image`.

### Task 5: Item to Homelable conversion

**Files:**
- Create: `images/homelab-sync/homelable.py`, `images/homelab-sync/test_homelable.py`
- Modify: `images/homelab-sync/Dockerfile` (COPY both files)

**Interfaces:**
- Consumes: item dicts from `build.build_items` and `build.carry_over` (`name`, `kind`, `tags`, `labels`, `notes`, `runsOn`).
- Produces:
  - `DESIGN = "Homelab index"`, `NS: uuid.UUID`
  - `node_id(name: str) -> str`
  - `node_type(name: str) -> str`: prefix `hw/` server, `pve/` proxmox, `vm/` vm, `lxc/` lxc, else generic
  - `ips(hosts: dict[str, str], scans: dict) -> dict[str, str]`: item name to IPv4. `hw/<alias>` for scans with `pve`, `vm/<alias>`/`lxc/<alias>` for other `HOSTS` aliases (value after `@`); unscanned guests from `pve.guests[vmid]` `net0` (`ip=`) or `ipconfig0` (`ip=`), CIDR suffix dropped, `dhcp` skipped. `pve/*` never.
  - `stored_items(canvas: dict) -> tuple[list[dict], list[dict]]`: sync nodes as items (label to `name`, property `sync` to tag `sync`, property `tags` to the other tags, remaining properties to `labels`, `notes`) and the raw non-sync nodes
  - `seed_items(resources: list[dict]) -> list[dict]`: RackPeek resources with each `Service` item's notes appended to its `runsOn[0]` item's notes as `\n\n### <name>\n<notes>`, `Service` items dropped
  - `payload(items: list[dict], canvas: dict, ip_map: dict[str, str], design_id: str) -> dict`: the `canvas/save` body
  - `workload_docs(items: list[dict]) -> dict[str, str]`: parent item name to Markdown body

- [ ] **Step 1: Write failing tests** in `test_homelable.py`, using `fixtures.py` data:
  - `test_node_id_stable`: `node_id("App/Forgejo") == node_id("app/forgejo")`, and it parses as a UUID.
  - `test_ips`: from `fixtures.scans()` and `HOSTS`-style dict, `hw/takaros` and `vm/k3s-master-51` get their `HOSTS` address; a guest with `net0: "name=eth0,bridge=vmbr0,ip=192.168.10.77/24"` gets `192.168.10.77`; `ip=dhcp` gets none; no `pve/*` key.
  - `test_roundtrip`: `stored_items(canvas_from(payload(items,...)))` returns the same names, tags, labels and notes.
  - `test_service_items_not_nodes`: no node label in `payload(...)["nodes"]` starts with a workload name from `build.build_items`.
  - `test_guest_nested`: `vm/*` node `parent_id == node_id("pve/<host>")`; `pve/*` node has `type == "proxmox"` and `container_mode is True`.
  - `test_ping_check`: nodes in `ip_map` have `ip`, `check_method == "ping"`, `check_target == ip`; others have neither.
  - `test_hand_made_kept`: a stored node without property `sync`, and a stored edge, appear unchanged in the payload; stored `viewport` and `custom_style` are copied.
  - `test_geometry_kept`: a stored sync node's `pos_x`, `pos_y`, `width`, `height`, `custom_colors`, `custom_icon` survive; a new node gets a position no other node has.
  - `test_workload_docs`: one body per parent, one table row per `Service` item, sorted by name, no row for an excluded namespace.
  - `test_seed_notes`: a `Service` item with notes adds `### <name>` and the notes to its parent's notes; parent without notes gets only that section.
- [ ] **Step 2: Run** `python3 -m unittest test_homelable -v` in `images/homelab-sync`. Expected: ImportError.
- [ ] **Step 3: Implement** the functions above in `homelable.py`. Properties are `{"key": k, "value": v}`; tags other than `sync` go in one property `tags` joined by `, `. Grid for new nodes: rows by prefix (`hw`, `pve`, `vm`/`lxc` inside their parent, `app`, rest), fixed spacing.
- [ ] **Step 4: Run** the tests. Expected: all pass, and the existing `test_build`/`test_sync` still pass.
- [ ] **Step 5: Commit** `feat(homelab-sync): convert sync items to Homelable nodes and workload pages`.

### Task 6: Sync writes Homelable

**Files:**
- Modify: `images/homelab-sync/sync.py`, `images/homelab-sync/test_sync.py`, `images/homelab-sync/test.sh` (env list in check 4 if it names variables)

**Interfaces:**
- Consumes: Task 5 functions.
- Produces: env `HL_URL`, `HL_SERVICE_KEY` (required); `RPK_URL`, `RPK_API_KEY` (optional, seed only). `RealIO` methods: `designs() -> list[dict]`, `create_design(name: str) -> str`, `canvas(design_id: str) -> dict`, `save(body: dict) -> None`, `documents() -> list[dict]`, `document(doc_id: str) -> dict`, `create_document(body: dict) -> None`, `update_document(doc_id: str, body: dict) -> None`, `git_sync() -> None`, `rpk_get() -> dict`. All Homelable calls under `/api/v1/` with header `X-MCP-Service-Key`. `rpk_post` and `rpk_commit` are removed.

- [ ] **Step 1: Write failing tests** (rework `FakeIO` to the methods above; keep every existing test's intent):
  - `test_happy_path_body`: one `save` whose nodes include `hw/takaros` with `sync` property; then `git_sync` called once.
  - `test_fresh_read_before_save`: notes edited on the second `canvas` read are the ones saved.
  - `test_homelable_down_saves_nothing`: `canvas` raises: no `save`, no document call, exit 1.
  - `test_seed_from_rackpeek`: no design named `Homelab index` and `rpk_get` returns `fx.stored()`: design created, saved notes come from RackPeek, workload notes appended to parents.
  - `test_seed_rackpeek_down`: no design and `rpk_get` raises: no `create_design`, no `save`, exit 1.
  - `test_no_seed_without_rpk_env`: no design and no `RPK_URL`: design created, saved without notes.
  - `test_k8s_down_keeps_docs`: `k8s` raises: no document create or update for `app/*` parents; app nodes in the payload keep their stored labels; exit 1.
  - `test_doc_unchanged_not_written`: stored document body equals the new body: no `update_document`.
  - `test_git_sync_failure_exits_1`: `git_sync` raises: `save` happened, exit 1.
  - Keep: one host down, forgejo down still saves, gitleaks drops file, configmap masked, payload scrubbed, missing env exits 1.
- [ ] **Step 2: Run** `python3 -m unittest test_sync -v`. Expected: new tests fail.
- [ ] **Step 3: Implement** `run(io, hosts, today, log)` in this order: find design by name; if missing, seed source is `seed_items(io.rpk_get()["resources"])` when `RPK_URL` is set, else empty; else read `canvas` and `stored_items`. Any read failure returns 1 before any write. Clone, k8s, scans, gitleaks and `push_live` stay as they are. Build items, re-read the canvas (or reuse the seed), `carry_over`, `payload`, create the design only now if missing, `save`. Then documents: for each parent in `workload_docs` whose source succeeded, find the document by `node_id` and title `<parent> workloads` in `documents()`, create or update only when the body differs, with `revision_reason` set to whichever value in Homelable's `CLIENT_REVISION_REASONS` names a non-human edit (omit it if none fits). Then `git_sync`. Exit 1 if any source, push, document or git sync failed.
- [ ] **Step 4: Run** all tests in `images/homelab-sync`. Expected: all pass. The image itself is checked by CI's `test.sh` on the PR.
- [ ] **Step 5: Commit** `feat(homelab-sync): write the index to Homelable and seed notes from RackPeek`.

### Task 7: Move the sync to the homelable namespace

**Files:**
- Move from `2-k3s/37.rackpeek/` to `2-k3s/40.homelable/` with names `homelab-sync*`: `sync-cronjob.yaml`, `sync-rbac.yaml`, `sync-network-policy.yaml`, `sync-known-hosts.yaml`, `scan-key.pub`
- Modify: both `kustomization.yaml`s, both `secrets.enc.yaml`s, `2-k3s/37.rackpeek/network-policy.yaml`, `2-k3s/38.forgejo/network-policy.yaml`, `2-k3s/37.rackpeek/README.md`, `2-k3s/40.homelable/README.md`, `1-proxmox/homelab-scan/README.md`, `2-k3s/13.t3code/env/isolated-runtime.md`, `README.md`

- [ ] **Step 1: Manifests.** CronJob, ServiceAccount, labels and ClusterRoles renamed `homelab-sync*`; RoleBinding subjects point to namespace `homelable`; image `ghcr.io/spyrospsarras/homelab-sync@<digest from the PR B build>`; env `HL_URL=http://homelable.homelable.svc`, `HL_SERVICE_KEY` from `homelable-secrets/MCP_SERVICE_KEY`, `RPK_URL=http://rackpeek.rackpeek.svc:8080`, `RPK_API_KEY` and `HOMELAB_LIVE_TOKEN` from a new Secret `homelab-sync-secrets`, scan key Secret renamed `homelab-scan-key`. Egress: replace the RackPeek 8080 rule with `homelable` pods port 80, add `rackpeek` pods 8080 (seed). `rackpeek` ingress admits `app: homelab-sync` from namespace `homelable` instead of `rackpeek-sync`. Forgejo ingress admits namespace `homelable` next to `rackpeek`.
- [ ] **Step 2: Secrets.** Move `rackpeek-scan-key` into `2-k3s/40.homelable/secrets.enc.yaml` as `homelab-scan-key`, add `homelab-sync-secrets` (`RPK_API_KEY`, `HOMELAB_LIVE_TOKEN`), copying values decrypt-to-private-temp and re-encrypt, never printed. Leave `rackpeek-secrets` intact.
- [ ] **Step 3: Docs.** RackPeek README: read-only since the switch, points to Homelable. Homelable README gets the nightly sync section from the RackPeek README, adjusted. `isolated-runtime.md` Forgejo sentence: "Traefik, RackPeek, Homelable and the hubs' skills pods". Root `README.md` and `1-proxmox/homelab-scan/README.md`: sync now lives in `40.homelable`. Homelable README also says how the homelab agent writes notes: `kubectl -n homelable port-forward`, then `PATCH /api/v1/nodes/{id}` with `notes`, key read inside the command.
- [ ] **Step 4: Verify** both folders with the Task 1 Step 4 build and server dry-run. `rg -n 'rackpeek-sync' 2-k3s` returns nothing.
- [ ] **Step 5: Commit** `feat(homelable): move the nightly sync from RackPeek to Homelable`.
- [ ] **Step 6: After PR B merges:** before syncing, report how many RackPeek `Service` items have notes (read through RackPeek's API from inside the cluster, count only). Request the Argo sync of `homelable` and `rackpeek` at the merged SHA (dry-run first; include the removed CronJob for pruning). Run `kubectl -n homelable create job --from=cronjob/homelab-sync homelab-sync-first`. Check: log shows the seed, about 95 nodes saved, workload documents created, git sync pushed; node count plus workload rows equals the last RackPeek post count (225 on 2026-11-26, give or take real changes); three notes spot-checked against RackPeek.

### Task 7b: Rename the scanner on the hosts (after PR B merges)

- [ ] **Step 1:** For each `HOSTS` entry: copy `1-proxmox/homelab-scan/homelab-scan.py` to `/usr/local/sbin/homelab-scan` (0755 root; TrueNAS keeps its `/root` location, renamed `/root/homelab-scan`), rewrite the scan key's `authorized_keys` line to the new command and comment, rename the sudoers file and its path for non-root users (`visudo -cf` before moving it in place).
- [ ] **Step 2:** Run one scan over SSH with the scan key from inside the CronJob's pod spec (`kubectl -n homelable create job --from=cronjob/homelab-sync`), confirm every host reports ok, then delete the old script and sudoers file.

### Task 8: Remove RackPeek (PR C, gated)

- [ ] **Step 1:** Ask Spyros for a yes, stating that it deletes PVC `rackpeek-config-pithari`.
- [ ] **Step 2:** Delete `2-k3s/37.rackpeek` and `app-rackpeek.yaml`, the `rackpeek` lines in DNS files, Forgejo network policy and `.github/renovate.json`, the `RPK_*` env and `rackpeek` egress from the sync CronJob, and the seed code path plus its tests (`seed_items`, `rpk_get`). Update docs that still name RackPeek.
- [ ] **Step 3: Verify** tests pass, build and dry-run pass, `rg -n -i rackpeek 2-k3s images .github 1-proxmox README.md` returns nothing.
- [ ] **Step 4: Commit** `chore: remove RackPeek`. After merge: Argo sync with prune, Pi-hole copy (approval), and Spyros archives `homelab-bot/rackpeek-config` in Forgejo.
