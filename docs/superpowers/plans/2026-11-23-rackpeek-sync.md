# RackPeek nightly sync (PR 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A nightly CronJob fills RackPeek with every host, guest, Argo app and workload, keeps notes written by people, and commits masked live config to Forgejo `homelab-live`.

**Architecture:** A read-only scanner runs on each host as a forced SSH command and prints one JSON document, masked on the host. A stdlib Python sync in namespace `rackpeek` reads the Kubernetes API, runs the scans, clones the public repo, builds one RackPeek payload, carries notes and tags over, marks gone items, commits masked files to Forgejo after gitleaks, and posts the payload with `mode: Replace`.

**Tech Stack:** Python 3.12+ standard library, OpenSSH, git, gitleaks, Kubernetes RBAC and NetworkPolicy, SOPS/ksops, GitHub Actions + GHCR.

**Spec:** `docs/superpowers/specs/2026-11-19-rackpeek-homelab-index-design.md` (sections Nightly sync, Host scan, Host allowlist, Git status, Data model, Secrets, Boundaries).

## Global Constraints

- Schedule `30 3 * * *`, `timeZone: Europe/Athens`, `concurrencyPolicy: Forbid`.
- Writes to RackPeek: one `POST /api/inventory` with `{"json": {...}, "mode": "Replace"}`. Read first with `GET /api/inventory`. Header `X-Api-Key`. In-cluster URL `http://rackpeek.rackpeek.svc:8080`.
- RackPeek Replace replaces only the named items and keeps all others (`ResourceCollectionMerger.Merge`, tag RackPeek-2.3.0). Names are unique case-insensitively; a duplicate name fails the whole POST.
- Every sync-written item has tag `sync`. Notes and tags other than `sync` are copied from the stored item.
- An item tagged `sync` that a source that succeeded no longer reports gets label `gone-since: <YYYY-MM-DD>` (kept if already set) and is posted again unchanged otherwise. Items from a failed source are not touched.
- Any failed source: the Job exits 1 after writing what succeeded. Log one line per source with counts and the error.
- Names: `hw/<host>`, `pve/<host>`, `vm/<name>`, `lxc/<name>`, `k3s/epaflix`, `app/<argo app>`, `<ns>/<kind>/<name>` (kind `deploy`, `sts`, `ds`, `cronjob`), `truenas/app/<name>`.
- Never read Secrets. Never read, list or name anything in `t3code-work` or `mcp-hub-work`; skip Argo apps whose destination namespace is one of them, and skip PVs whose `claimRef.namespace` is one of them.
- Host allowlist and SSH users: takaros, evanthoulaki, patroklos, shypowder, pihole, proxmox-backup (`root`); pegaprox (`root`); jumpbox (`spy`); nick, k3s-master-51..53, k3s-worker-61..65 (`ubuntu`); truenas (`truenas_admin`). Addresses 192.168.10.10–13, .30, .31, .21, .40, .41, .51–53, .61–65, .200. Never homepc, laptop, laptop-ts, t3code.
- Masking on the host: name-only list `/etc/ssh`, `/etc/pve/priv`, `/etc/rancher`, `/etc/proxmox-backup`, `/etc/pihole`, `/etc/wireguard`, `/etc/ssl/private`, `/etc/shadow*`, `/etc/gshadow*`, `*.key`, `*.pem`, `*.pw`, `id_*`, every dotfile and dotdir under `/root`; binaries send name + sha256; text values whose key matches `pass|pwhash|secret|token|key|credential|auth` (case-insensitive) and the password part of `scheme://user:pass@host` become `***`; content only for text files under 64 KiB.
- Excluded paths: `/var/lib` entirely.
- The scan key's `authorized_keys` line: `restrict,from="192.168.10.51,192.168.10.52,192.168.10.53,192.168.10.61,192.168.10.62,192.168.10.63,192.168.10.64,192.168.10.65,10.0.0.0/8",command="<cmd>" <pubkey> rackpeek-scan`, where `<cmd>` is `/usr/local/sbin/rackpeek-scan` for root and `sudo -n /usr/local/sbin/rackpeek-scan` otherwise.
- No comments in code. Commit titles conventional, no co-author.

## Decisions taken in this plan

- D11. The scanner is Python (`rackpeek-scan.py`), not bash. Python 3.12+ is on every sampled host (takaros, pihole, k3s-master-51, truenas, jumpbox, 2026-11-23); Task 6 checks the rest before install. Masking in Python is testable on fixtures.
- D12. SSH host keys are pinned: Task 4 builds ConfigMap `rackpeek-known-hosts` from this pod's inventory `known_hosts` entries for the allowlist. The sync uses `StrictHostKeyChecking=yes`.
- D13. Forgejo `homelab-live` is pushed over `https://git-internal.epaflix.com/rackpeek-bot/homelab-live.git` with `HOMELAB_LIVE_TOKEN`; the first push creates it (`ENABLE_PUSH_CREATE_USER`).
- D14. RBAC RoleBindings are a static list in Git: every namespace except `t3code-work`, `mcp-hub-work`, `kube-node-lease`, `kube-public`. A namespace with no binding is reported as item label `indexed: no` on its `app/` item.

## Review Focus

1. One host unreachable or its scan times out (600 s): the other hosts are still scanned and posted; that host's items keep their last facts; exit 1. Test in Task 3.
2. A secret in an unusual shape (`export DB_PASSWORD=x`, `"apiKey": "x"`, `postgres://u:p@h/db`, `Authorization: Bearer x`) is masked on the host. Test in Task 1.
3. A person edits notes or adds a tag on a sync item, or creates an item without tag `sync`: the next run keeps the notes and tags and never posts the non-sync item. Test in Task 2.
4. Excluded namespaces never appear in the payload, also not via an Argo app destination, a PV claim or a namespace list. Test in Task 2.
5. RackPeek or Forgejo unreachable (for example during a pithari disk move): nothing partial is posted to RackPeek, the run exits 1, and the next run recovers. Test in Task 3.

---

### Task 1: Host scanner

**Files:**
- Create: `1-proxmox/rackpeek-scan/rackpeek-scan.py`
- Create: `1-proxmox/rackpeek-scan/test_rackpeek_scan.py`
- Create: `1-proxmox/rackpeek-scan/README.md`

**Interfaces:**
- Produces: `rackpeek-scan` prints one JSON object: `{"schema":1,"host":str,"os":{...},"hardware":{...},"storage":{...},"packages":[str],"files":[File],"truenas":{...}|null,"pve":{...}|null,"errors":[str]}`. `File = {"path","size","mtime","sha256"|null,"content"|null,"masked":bool,"kind":"text"|"binary"|"name-only"}`.
- Functions tested directly: `mask_text(text: str) -> str`, `classify(path: str) -> str` (`"name-only"|"skip"|"read"`), `file_entry(path: str, root: str) -> dict`, `collect_paths(root: str, pve: bool) -> list[str]`.

- [ ] **Step 1: Write failing tests** in `test_rackpeek_scan.py` (unittest, stdlib): `test_mask_key_value` (`DB_PASSWORD=x`, `export TOKEN="y"`, `"apiKey": "z"`, `pwhash: q` all become `***` and keep the key), `test_mask_url_password` (`postgres://u:p@h/db` → `postgres://u:***@h/db`), `test_mask_bearer` (`Authorization: Bearer abc` → `Authorization: ***`), `test_mask_keeps_plain` (`hostname=takaros` unchanged), `test_name_only` (`/etc/ssh/sshd_config`, `/root/.bashrc`, `/etc/x/a.pem`, `/home/u/id_ed25519` → `name-only`, entry has no `sha256` and no `content`), `test_skip_var_lib` (`/var/lib/rancher/x` → `skip`), `test_binary_hash_only` (file with a NUL byte → `kind binary`, sha256 set, content null), `test_large_text_no_content` (65 537 bytes → sha256 set, content null), `test_collect_uses_root` (fixture tree under a temp root with `/usr/local/bin/x`, `/root/notes.txt`, `/root/.ssh/id_rsa`, `/etc/systemd/system/a.service`, `/var/lib/y` → collected paths include the first four, exclude `/var/lib/y`).
- [ ] **Step 2: Run** `python3 -m unittest 1-proxmox/rackpeek-scan/test_rackpeek_scan.py -v`. Expected: fails on import.
- [ ] **Step 3: Implement** the functions and `main()`. `main` ignores argv and `SSH_ORIGINAL_COMMAND`, reads nothing from stdin, runs each probe with a 20 s timeout, and records a failed probe in `errors` instead of exiting. Probes: `/etc/os-release`, `uname -r`, `/proc/uptime`; `dmidecode -t system -t processor -t memory` (parsed to vendor, product, serial, cpu model, sockets, memory total); `lsblk -J -o NAME,SIZE,TYPE,MODEL,SERIAL,MOUNTPOINT`; `ip -j addr`; packages via `dpkg-query -W -f '${Package} ${Version}\n'`; modified conffiles via `dpkg --verify`; unowned files under `/etc` via `dpkg -S` on candidates. Proxmox (when `/etc/pve` exists): `pvs --reportformat json`, `lvs --reportformat json`, `zpool status -P`, `pvesh get /cluster/resources --output-format json` and per guest `pvesh get /nodes/<node>/<type>/<vmid>/config --output-format json` (only on the host whose name sorts first among online nodes, so it runs once). TrueNAS (when `midclt` exists): `pool.query`, `pool.dataset.query` (name, type, used, avail, encryption), `sharing.nfs.query`, `iscsi.extent.query`, `app.query`. Files under the masking rules from Global Constraints. The test root comes from env `RACKPEEK_SCAN_ROOT` (default `/`), only for tests.
- [ ] **Step 4: Run tests.** Expected: all pass. Then `python3 1-proxmox/rackpeek-scan/rackpeek-scan.py | python3 -m json.tool >/dev/null` on this pod exits 0 (it runs without root and reports probe errors).
- [ ] **Step 5: README** lists what the script reads, the masking rules, the forced-command line format, and how to rescan by hand: `ssh <host>` with the scan key prints the JSON.
- [ ] **Step 6: Commit** `feat(rackpeek): read-only host scanner`.

### Task 2: Payload builder

**Files:**
- Create: `images/rackpeek-sync/build.py`
- Create: `images/rackpeek-sync/test_build.py`
- Create: `images/rackpeek-sync/fixtures/` (`k8s.json`, `scans/takaros.json`, `scans/truenas.json`, `stored.json`, `repo/` minimal tree with `.github/renovate.json` and one app dir)

**Interfaces:**
- Consumes: scan JSON from Task 1; Kubernetes object lists as `{"<kind>": [obj, ...]}`; repo path.
- Produces: `build_items(k8s: dict, scans: dict[str, dict], repo: str, failed: set[str]) -> list[dict]` (RackPeek resources in JSON form: `{"kind": "Server"|"Desktop"|"System"|"Service", "name", "tags", "labels", "notes"?, "runsOn", ...}`), `carry_over(new: list[dict], stored: list[dict], ok_sources: set[str], today: str) -> list[dict]`, `git_status(path: str, sha256: str, repo_index: dict[str, str]) -> str`, `excluded(ns: str) -> bool`. A source name is `k8s`, `repo` or a host alias; each item has label `source`.

- [ ] **Step 1: Write failing tests:** `test_names_and_runs_on` (fixture yields `hw/takaros` Server, `pve/takaros` System type `hypervisor` runsOn `hw/takaros`, `vm/truenas` System type `vm` runsOn `pve/<node>`, `lxc/pihole` type `container`, `k3s/epaflix` type `cluster`, `app/forgejo` type `other` label `kind: argo-app`, `forgejo/deploy/forgejo` Service runsOn `app/forgejo`, `truenas/app/silo` Service runsOn `vm/truenas`); `test_workload_labels` (`forgejo/deploy/forgejo` has labels `images`, `nodes`, `updates`, `configmaps`, `secrets` (names and key names from the SOPS file in the repo fixture, never values), `ingress`, `auth`, `git`, `disk-chain`); `test_no_duplicate_names` (two objects differing only in case produce one item and a logged warning, not two); `test_excluded_namespaces` (fixture objects, an Argo app and a PV in `t3code-work` and `mcp-hub-work` → no item name, label value or note contains either string); `test_carry_notes_and_tags` (stored `app/forgejo` with notes `"why"` and tags `["sync","critical"]` → new item keeps both); `test_non_sync_untouched` (stored item without tag `sync` is not in the output); `test_gone_marked` (stored sync item from source `k8s`, absent now, `k8s` ok → present with `gone-since: today`; already gone keeps its old date); `test_failed_source_untouched` (stored sync item from host `nick`, `nick` failed → not in the output); `test_git_status` (file whose sha256 is in the repo index → `in Git: <path>`, else `not in Git`; k8s object with Argo tracking → `managed by app/<name>`, else `live only`).
- [ ] **Step 2: Run** `cd images/rackpeek-sync && python3 -m unittest test_build -v`. Expected: fails on import.
- [ ] **Step 3: Implement.** `updates` label: `renovate` when a `kustomization.yaml` `images:` entry or a Helm chart under the app path is matched by Renovate's managers and not disabled by a `packageRules` entry for that package; `ci: <workflow>` when a workflow under `.github/workflows/build-*.yml` publishes the image; else `manual`. `uses`: hostnames and `*.svc` names found in env values, ConfigMap data and NetworkPolicy egress namespace selectors. `disk-chain`: PVC → PV → zvol path from the TrueNAS scan (match on PV `volumeHandle`) or local-path dir → Proxmox VM disk from the `pvesh` config of the node VM → storage → device.
- [ ] **Step 4: Run tests.** Expected: all pass.
- [ ] **Step 5: Commit** `feat(rackpeek): build the RackPeek payload from scans`.

### Task 3: Sync runner and image

**Files:**
- Create: `images/rackpeek-sync/sync.py`, `images/rackpeek-sync/test_sync.py`, `images/rackpeek-sync/Dockerfile`, `images/rackpeek-sync/test.sh`
- Create: `.github/workflows/build-rackpeek-sync.yml` (copy of `build-vpn-picker.yml` with `vpn-picker` → `rackpeek-sync`)

**Interfaces:**
- Consumes: `build_items`, `carry_over` from Task 2; scan JSON from Task 1.
- Produces: `main() -> int`; env `RPK_URL`, `RPK_API_KEY`, `HOMELAB_LIVE_TOKEN`, `HOMELAB_LIVE_URL`, `REPO_URL` (`https://github.com/SpyrosPsarras/epaflix.git`), `SSH_KEY_FILE` (`/ssh/id_ed25519`), `KNOWN_HOSTS_FILE` (`/ssh-hosts/known_hosts`), `HOSTS` (space-separated `alias=user@ip`).

- [ ] **Step 1: Write failing tests** with fakes for the network calls (inject callables): `test_one_host_down` (one fake host raises timeout → others posted, exit 1, log line names the host); `test_rackpeek_down_posts_nothing` (GET fails → no POST, no Forgejo push, exit 1); `test_forgejo_down_still_posts` (push fails → RackPeek still posted, exit 1); `test_post_body` (POST body is `{"json": {"version": 5, "resources": [...]}, "mode": "Replace"}` and every resource has tag `sync`); `test_gitleaks_drops_file` (fake gitleaks reports a path → that file's content is removed from the Forgejo tree and from labels, path listed in the log).
- [ ] **Step 2: Run** `python3 -m unittest test_sync -v`. Expected: fails on import.
- [ ] **Step 3: Implement.** Kubernetes via `urllib` and the ServiceAccount token at `/var/run/secrets/kubernetes.io/serviceaccount/`. SSH via `ssh -i $SSH_KEY_FILE -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$KNOWN_HOSTS_FILE -o ConnectTimeout=10 user@ip`, 600 s timeout, hosts in parallel (max 6). Repo: `git clone --depth 1`. Forgejo tree: one dir per host with masked files plus `packages.txt`, and `k8s/<ns>/configmaps/<name>.yaml`; commit message `sync <date>`; push with the token as HTTP Basic password (username `rackpeek-bot`) through `git -c http.extraHeader` is not used, the URL carries no token, a `GIT_ASKPASS` script prints the token from env. gitleaks: `gitleaks dir <tree> --report-format json --report-path -  --no-banner`.
- [ ] **Step 4: Dockerfile** from `python:3.15-alpine` with label `org.opencontainers.image.source`, `apk add --no-cache openssh-client git`, gitleaks from its GitHub release pinned by version and sha256 with a `# renovate:` marker like `images/airvpn-bluetit/Dockerfile`; user 1000; entrypoint `python3 -u /app/sync.py`.
- [ ] **Step 5: test.sh** runs the unit tests in the built image, checks `ssh -V`, `git --version`, `gitleaks version`, and checks that the image runs with no env and exits 1 without a traceback.
- [ ] **Step 6: Run** `python3 -m unittest discover -s images/rackpeek-sync -v`. Expected: all pass. The image build itself is proven by the workflow on the PR.
- [ ] **Step 7: Commit** `feat(rackpeek): nightly sync runner and image`.

### Task 4: Kubernetes objects

**Files:**
- Create: `2-k3s/37.rackpeek/sync-cronjob.yaml`, `2-k3s/37.rackpeek/sync-rbac.yaml`, `2-k3s/37.rackpeek/sync-network-policy.yaml`, `2-k3s/37.rackpeek/sync-known-hosts.yaml`
- Modify: `2-k3s/37.rackpeek/kustomization.yaml` (resources, image pin for `ghcr.io/spyrospsarras/rackpeek-sync`), `2-k3s/37.rackpeek/network-policy.yaml` (RackPeek ingress also from pods `app: rackpeek-sync` in its namespace), `2-k3s/37.rackpeek/secrets.enc.yaml` (new key `SCAN_SSH_KEY`)
- Create: `2-k3s/37.rackpeek/scan-key.pub`

**Interfaces:**
- Consumes: image from Task 3; env names from Task 3.
- Produces: ServiceAccount `rackpeek-sync`; ClusterRoles `rackpeek-reader`, `rackpeek-cluster-reader`; Secret key `SCAN_SSH_KEY`; public key file for Task 6.

- [ ] **Step 1: Generate the scan key** inside `vault run --name sops-age-k3s-cluster`: `ssh-keygen -t ed25519 -N '' -C rackpeek-scan` in a mode-700 dir, `sops set` the private key into `secrets.enc.yaml` as `SCAN_SSH_KEY`, copy the `.pub` to `scan-key.pub`, delete the dir. Verify: `sops decrypt` lists 4 keys (names only).
- [ ] **Step 2: RBAC.** `rackpeek-reader`: get/list on `pods`, `services`, `configmaps`, `persistentvolumeclaims`, `serviceaccounts`, `apps/*` (deployments, statefulsets, daemonsets, replicasets), `batch/*` (jobs, cronjobs), `networking.k8s.io/networkpolicies`, `traefik.io/ingressroutes`, `traefik.io/middlewares`, `argoproj.io/applications`, `rbac.authorization.k8s.io/roles,rolebindings`. No `secrets`, no `pods/exec`, no `pods/log`. RoleBinding per namespace from D14. `rackpeek-cluster-reader`: get/list `nodes`, `persistentvolumes`, `storageclasses`, `namespaces`. Verify: `kubectl auth can-i get secrets -n forgejo --as=system:serviceaccount:rackpeek:rackpeek-sync` → `no`; `kubectl auth can-i list deployments -n t3code-work --as=…` → `no`; `kubectl auth can-i list deployments -n forgejo --as=…` → `yes` (server dry-run applies first).
- [ ] **Step 3: NetworkPolicy** for `app: rackpeek-sync`: egress DNS; Kubernetes API 6443 to `10.0.0.51/32`, `10.0.0.52/32`, `10.0.0.53/32` (as the `reloader` policy in `2-k3s/36.home-assistant/network-policy.yaml`); TCP 22 to each allowlist address as `/32`; TCP 8080 to pods `app: rackpeek`; TCP 8443 to Traefik pods (git-internal); TCP 443 to `0.0.0.0/0` except RFC1918 (GitHub clone, as `2-k3s/34.windmill/network-policy.yaml`).
- [ ] **Step 4: known_hosts ConfigMap** from this pod's inventory known_hosts entries for the 18 allowlist addresses (D12). Verify each line parses with `ssh-keygen -l -f`.
- [ ] **Step 5: CronJob** per Global Constraints, `successfulJobsHistoryLimit: 3`, `failedJobsHistoryLimit: 3`, `activeDeadlineSeconds: 1800`, pod label `app: rackpeek-sync`, read-only root, non-root 1000, Secret mounted at `/ssh` with `SCAN_SSH_KEY` as `id_ed25519` mode 0400, ConfigMap at `/ssh-hosts`, emptyDir `/tmp`, `HOSTS` env with the allowlist.
- [ ] **Step 6: Verify** `kustomize build --enable-alpha-plugins --enable-exec 2-k3s/37.rackpeek` renders, `kubectl apply --dry-run=server` accepts every new object, pre-commit passes.
- [ ] **Step 7: Commit** `feat(rackpeek): CronJob, RBAC and network policy for the sync`.

### Task 5: RackPeek README and gitignore follow-ups

**Files:**
- Modify: `2-k3s/37.rackpeek/README.md`, `2-k3s/37.rackpeek/deployment.yaml`

- [ ] **Step 1:** Init container also ignores `*.bak.*` (F4). The command first adds a trailing newline when `.gitignore` does not end with one, then appends each missing pattern on its own line. Test locally on a scratch file without a trailing newline: the result has `.dataprotection/` and `*.bak.*` on separate lines; a second run changes nothing.
- [ ] **Step 2:** README: branch is `master` (F5); new section "Nightly sync" with schedule, sources, the `sync` tag and `gone-since` rules, how to rescan one host by hand, and how to run the Job now: `kubectl -n rackpeek create job --from=cronjob/rackpeek-sync rackpeek-sync-manual`.
- [ ] **Step 3: Commit** `docs(rackpeek): nightly sync and gitignore for backups`.

### Task 6: Host install (live, after merge and owner confirmation)

- [ ] **Step 1: Ask the owner** with the question tool to approve installing on the 18 allowlist hosts. Show the exact `authorized_keys` line and sudoers line.
- [ ] **Step 2: Check python3** on each host: `ssh <host> python3 -c 'import sys; assert sys.version_info >= (3, 11)'`. A host that fails is reported and skipped.
- [ ] **Step 3: Install per host** (one at a time, stop on the first error): copy the script to `/usr/local/sbin/rackpeek-scan` mode 0755 owner root; append the `authorized_keys` line from Global Constraints for the host's SSH user unless the key is already present; for `ubuntu` and `spy` add `/etc/sudoers.d/rackpeek-scan` with `<user> ALL=(root) NOPASSWD: /usr/local/sbin/rackpeek-scan`, checked by `visudo -cf`.
- [ ] **Step 4: TrueNAS:** add the public key with its options to `truenas_admin` via `midclt call user.update` (sshpubkey) and a sudo command allowlist via `sudo_commands_nopasswd`. Verify the `restrict,from=…,command=…` prefix survives in `/home/truenas_admin/.ssh/authorized_keys` after a `midclt call ssh.update '{}'`. If it does not, stop and ask the owner (spec fallback: read-only API key).
- [ ] **Step 5: Verify per host** from this pod with the scan key: the output parses as JSON with `"schema": 1`; `ssh … 'id'` with the scan key returns the JSON, not `id` output (forced command).
- [ ] **Step 6: First run:** read the pithari thread; no RackPeek or Forgejo move in progress. Then `kubectl -n rackpeek create job --from=cronjob/rackpeek-sync rackpeek-sync-first`, wait, check the log counts, the RackPeek totals, and the first `homelab-live` commit in Forgejo contains no unmasked value (run gitleaks on a clone from this pod).
