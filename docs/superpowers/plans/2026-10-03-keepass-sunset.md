# Sunset KeePass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace KeePass (the k3s MCP pod and KeePassXC on homePC and the laptop) with Vaultwarden plus gnome-keyring, then remove KeePass everywhere and keep one archive on homePC.

**Architecture:** A two-container pod `vaultwarden-mcp` in `mcp-hub`. The `bw` container runs `bw serve` on 127.0.0.1:8087, and the `server` container runs `vaultwarden_mcp.py`. That file keeps the 7 `vault_*` tools from `keepass_mcp.py` but calls `bw serve` over HTTP. mcp-hub routes `/vaultwarden` to it. On the desktops, gnome-keyring takes over the Secret Service, so app tokens move without re-login. Two PRs: PR 1 adds Vaultwarden next to KeePass. PR 2 removes KeePass after the compare and the desktop switch.

**Tech Stack:** Python 3.13 with `mcp==2.2.0` (stdlib `urllib` for `bw serve`), `@bitwarden/cli` 2026.8.0 on `node:22-alpine`, kustomize and ksops on ArgoCD, gnome-keyring and libsecret on Arch.

**Spec:** `docs/superpowers/specs/2026-10-03-keepass-sunset-design.md`

## Global Constraints

- `@bitwarden/cli` stays pinned at exactly `2026.8.0` (2026.9.x crashes against Vaultwarden 1.37.3).
- Vaultwarden URL: `https://vaultwarden.epaflix.com`.
- Tool names, arguments and output keys match `keepass_mcp.py` exactly: `vault_list(prefix)`, `vault_get(path, include_password)`, `vault_add(path, username, password, url, notes, props)`, `vault_update(path, title, username, password, url, notes, props)`, `vault_trash(path)`, `vault_attach(path, filename, content_b64)`, `vault_attachment(path, filename)`.
- Writes keep `ask` in OpenCode: `vault_add`, `vault_update`, `vault_trash`, `vault_attach`.
- One Secret, `mcp-hub-vaultwarden`, in `mcp-hub` with keys `hub-secret`, `client-id`, `client-secret`, `password`. Only the `bw` container gets the login keys. Only the hub and the `server` container get `hub-secret`.
- KeePass keeps working until Task 6. Nothing gets deleted before Spyros confirms the Task 4 compare.
- Never cordon, drain or reboot a node. Spyros reboots the desktops himself.
- Never send secret values to Jev, logs or chat. Desktop scripts print attribute names and counts, never secrets.
- Every task that changes tracked files goes through the review gate (`review-gate` skill) before its commit lands on a PR.
- Before every commit: the branch is rebased on `origin/main`.

## Review Focus

1. **Two items with the same `/Folder/Name`.** The import holds duplicates (for example 9 copies of `/Adminkit/adminkit.us.auth0.com`). Spyros expects reads to return the first match, like KeePass did, and writes to hit only that one. Test: `test_duplicate_path_reads_first` in Task 1.
2. **Nested folders.** Bitwarden stores `Personal/Git` as a folder name containing `/`. `vault_get("/Personal/Git/github.com")` must resolve folder `Personal/Git` plus name `github.com`, and `vault_add` into a missing nested folder must create the folder `Personal/Git`. Test: `test_nested_folder_path` in Task 1.
3. **Items with no folder.** These use `/Name` paths, as KeePass root entries did. Test: covered in `test_list_get_roundtrip`.
4. **`bw serve` down or vault locked.** Every tool returns a clear error naming `bw serve`, not a stack trace, and the pod's readiness probe fails. Test: `test_bw_unreachable_error` in Task 1.
5. **Stale reads after an edit in Bitwarden Desktop.** Reads call `POST /sync` at most once every 60 s. Test: `test_sync_rate_limited` in Task 1.

---

### Task 1: `vaultwarden_mcp.py` with selftest

**Files:**
- Create: `2-k3s/23.mcp-hub/files/vaultwarden_mcp.py`
- Reference (do not modify): `2-k3s/15.syncthing/files/keepass_mcp.py` (copy its HTTP wrapper, `X-Hub-Secret` check, FastMCP tool registration and `--selftest`/`--http` CLI shape)

**Interfaces:**
- Produces: CLI `python3 vaultwarden_mcp.py [--http | --selftest]`. Env: `BW_SERVE_URL` (default `http://127.0.0.1:8087`), `VAULTWARDEN_HUB_SECRET`, `PORT` (default 8000). HTTP route `/vaultwarden`. Tool outputs: `_summary(item) -> {"path", "title", "expired": False, "username", "url", "notes", "attachments": [sorted filenames]}`. `vault_get` adds `custom_properties` (dict of custom field name to value) and `password`.
- Path rule: `"/" + folder.name + "/" + item.name` when the item has a folder, `"/" + item.name` otherwise. `_split(path) -> (folder_name, title)` splits on the LAST `/`, and folder names may contain `/`.
- `bw serve` endpoints used: `GET /list/object/items`, `GET /list/object/folders`, `POST /object/folder`, `POST /object/item`, `PUT /object/item/{id}`, `DELETE /object/item/{id}`, `POST /attachment?itemid={id}` (multipart field `file`), `DELETE /object/attachment/{aid}?itemid={id}`, `GET /object/attachment/{aid}?itemid={id}`, `POST /sync`. Responses are `{"success": bool, "data": ...}`, where list data sits in `data.data`.

- [ ] **Step 1: Write the selftest first.** `_selftest()` starts a fake `bw serve` (`http.server` in a thread, in-memory items and folders, same response envelope) on a free port, points `BW_SERVE_URL` at it, and asserts:
  - `test_list_get_roundtrip`: seed `/Personal/github.com` (login, password `p1`, custom field `k1=v1`) and root item `/rootitem`. `vault_list()` has 2 entries. `vault_get("/Personal/github.com")["password"] == "p1"` and `custom_properties == {"k1": "v1"}`. `vault_list("Personal")` has 1 entry.
  - `test_nested_folder_path`: `vault_add("/Personal/Git/new", username="u", password="x")` creates folder `Personal/Git`, and `vault_get("/Personal/Git/new")["username"] == "u"`.
  - `test_duplicate_path_reads_first`: two items both at `/Dup/x` with passwords `a` and `b`. `vault_get` returns `a`, and `vault_update("/Dup/x", password="c")` changes only the first one.
  - `vault_update` with `props={"k1": None, "k2": "v2"}` removes `k1` and sets `k2`. `props={"absent": None}` is a no-op.
  - `vault_trash` calls `DELETE`, and a later `vault_get` raises `ValueError`.
  - `vault_attach` followed by `vault_attachment` round-trips the bytes, re-attaching the same filename replaces the old attachment, and empty `content_b64` raises `ValueError`.
  - `vault_get("/nope")` raises `ValueError` containing "list entries to see valid paths".
  - `test_bw_unreachable_error`: with `BW_SERVE_URL` pointing at a closed port, `vault_list()` raises `RuntimeError` whose message contains `bw serve`.
  - `test_sync_rate_limited`: two `vault_list()` calls within 60 s hit `POST /sync` exactly once.
  - HTTP wrapper: a request to `/vaultwarden` without the right `X-Hub-Secret` gets 401, and with it the MCP handshake succeeds (same check as keepass_mcp.py's selftest).
- [ ] **Step 2: Run it and watch it fail.** `python3 2-k3s/23.mcp-hub/files/vaultwarden_mcp.py --selftest` fails because the tools don't exist yet.
- [ ] **Step 3: Implement** the 7 tools plus `_bw(method, path, body=None, files=None) -> data`, `_items() -> list` (sync-gated), `_folders() -> {id: name}`, `_find(path) -> item` (first match) and `_folder_id(name, create: bool) -> str | None`. Field mapping: `title` is `name`, `username`/`password`/`url` are `login.username`, `login.password` and `login.uris[0].uri`, `notes` is `notes`, and `props` are `fields` of type 0 (text). `vault_add` creates `type: 1` (login). Use stdlib `urllib.request` only; build multipart by hand (about 10 lines) instead of adding a dependency.
- [ ] **Step 4: Run** `python3 -m venv /tmp/vw && /tmp/vw/bin/pip install -q mcp==2.2.0 && /tmp/vw/bin/python 2-k3s/23.mcp-hub/files/vaultwarden_mcp.py --selftest`. Expected: `selftest ok`, exit 0.
- [ ] **Step 5: Commit** `feat(mcp-hub): vaultwarden MCP server over bw serve`.

### Task 2: Deploy `vaultwarden-mcp` and route it through the hub (PR 1)

**Files:**
- Create: `2-k3s/23.mcp-hub/vaultwarden-mcp.yaml` (Deployment, Service, NetworkPolicy, modelled on `jev-mcp.yaml`)
- Create: `2-k3s/23.mcp-hub/files/vaultwarden-mcp/package.json` and `package-lock.json` (only `@bitwarden/cli` `2026.8.0`)
- Modify: `2-k3s/23.mcp-hub/kustomization.yaml` (resource, configMaps `vaultwarden-mcp-app` with `vaultwarden_mcp.py`, and `vaultwarden-mcp-bw` with the package files, plus a revision replacement onto both `mcp-hub` and `vaultwarden-mcp` like `mcp-hub-jev`)
- Modify: `2-k3s/23.mcp-hub/ksops-generator.yaml` (add `mcp-hub-vaultwarden.enc.yaml`)
- Modify: `2-k3s/23.mcp-hub/files/mcp_hub.py` (add `/vaultwarden` upstream; keep `/keepass`)
- Modify: `2-k3s/23.mcp-hub/deployment.yaml` (env `VAULTWARDEN_HUB_SECRET` from `mcp-hub-vaultwarden` key `hub-secret`)
- Modify: `2-k3s/23.mcp-hub/tools/sops_secret.py` (add `--vaultwarden`)
- Modify: `2-k3s/23.mcp-hub/tools/hub_clients.py`, then copy it with `2-k3s/13.t3code/one/tools/sync-shared.sh` (add `"vaultwarden": "/vaultwarden"` to `SERVERS` and the 4 write tools to `ASK`; keep keepass)
- Modify: `2-k3s/13.t3code/one/files/jev-guard.js`, `jev-guard.test.mjs` (exempt `vaultwarden_` exactly like `keepass_`)
- Modify: `.github/workflows/ci.yml` (MCP hub selftest step runs `vaultwarden_mcp.py --selftest`)
- Modify: `2-k3s/23.mcp-hub/README.md` (route, secret table row and a "Vaultwarden" section)

**Interfaces:**
- Consumes: Task 1 CLI and env names.
- Produces: in-cluster URL `http://vaultwarden-mcp.mcp-hub.svc.cluster.local:8000/vaultwarden` and hub path `/vaultwarden`. Agent tool names are `vaultwarden_vault_*`.

- [ ] **Step 1: Write the failing tests.**
  - In `mcp_hub.py --selftest`: assert `/vaultwarden` is in `upstreams()` and sends `x-hub-secret` from `VAULTWARDEN_HUB_SECRET`.
  - In `hub_clients.py --selftest`: assert `out["permission"]["vaultwarden_vault_trash"] == "ask"`.
  - In `jev-guard.test.mjs`: copy the `keepass_` exemption cases with `vaultwarden_`.
- [ ] **Step 2: Run** the CI selftest commands from `ci.yml` plus `node --test 2-k3s/13.t3code/one/files/jev-guard.test.mjs`. Expected: the new asserts fail.
- [ ] **Step 3: Implement.**
  - `bw` container (`node:22-alpine`, uid 1000, read-only root, `HOME=/tmp`, `BITWARDENCLI_APPDATA_DIR=/tmp/bw`, `BW_NOINTERACTION=true`) runs:
    1. `npm ci` from the configMap (same pattern as jev-mcp).
    2. `bw config server https://vaultwarden.epaflix.com`.
    3. `bw login --apikey` (env `BW_CLIENTID` and `BW_CLIENTSECRET` from Secret keys `client-id` and `client-secret`).
    4. `export BW_SESSION=$(bw unlock --passwordenv BW_PASSWORD --raw)` (env `BW_PASSWORD` from key `password`).
    5. `exec bw serve --hostname 127.0.0.1 --port 8087`.
  - Readiness for both containers: exec `wget -q -O- http://127.0.0.1:8087/status | grep -q '"status":"unlocked"'`.
  - `server` container (`python:3.13-alpine`, `pip install mcp==2.2.0` at start like the hub, `vaultwarden_mcp.py --http`, `VAULTWARDEN_HUB_SECRET` from key `hub-secret`).
  - NetworkPolicy: ingress on 8000 from `app: mcp-hub` only.
  - `sops_secret.py --vaultwarden`: prompt `client_id`, `client_secret` and master password with `getpass` (never echoed or printed), generate `hub-secret` with `secrets.token_urlsafe(32)`, write `2-k3s/23.mcp-hub/mcp-hub-vaultwarden.enc.yaml` through the existing `encrypt()` and `publish()`. Also support `--vaultwarden --rotate-hub` to rotate only `hub-secret`, keeping the rest (decrypt the old file with `sops -d`; this needs the age key, so run it on the laptop).
- [ ] **Step 4: Run** the same commands as Step 2, plus `kustomize build --enable-alpha-plugins --enable-exec 2-k3s/23.mcp-hub >/dev/null` (or the repo's usual render check, if ksops can't decrypt in the pod, render with the generator removed). Expected: all pass, and the render shows both containers and the right secret keys per container.
- [ ] **Step 5: Review gate, rebase on `origin/main`, commit** `feat(mcp-hub): route /vaultwarden to a bw serve backed MCP`.

### Task 3: Spyros creates the secret, merge PR 1, verify

Human-only parts get a `wizard` script; the agent does the rest.

- [ ] **Step 1:** Generate a wizard (`wizard` skill) that walks Spyros through:
  1. Vaultwarden web vault → Account settings → Security → Keys → View API key.
  2. Run `python3 2-k3s/23.mcp-hub/tools/sops_secret.py --vaultwarden` in his own T3 terminal and paste the values when prompted.
  3. Check that `git status` shows `mcp-hub-vaultwarden.enc.yaml`.
- [ ] **Step 2:** Agent commits the encrypted file, pushes, opens PR 1, calls `link_pull_request`, waits for CI green, and asks Spyros to merge.
- [ ] **Step 3: Verify** after ArgoCD syncs: the pod `vaultwarden-mcp` is 2/2 Ready, and through the hub `vaultwarden_vault_list` returns more than 300 entries. `vaultwarden_vault_get` on `/Personal/syncthing` with `include_password=false` returns username and url.
- [ ] **Step 4:** Re-run `tools/add-client.py` for `homepc` so its configs gain `vaultwarden` (the t3code pod picks it up on the next restart through `entrypoint.sh`).

### Task 4: Exact compare (D2)

- [ ] **Step 1:** Call `keepass_vault_list()` and `vaultwarden_vault_list()` and diff by `path`, counting duplicates, then compare `attachments` per path. Write the result to `/tmp/opencode/kp-vs-vw.md` with no secrets: paths only, grouped as "missing in Vaultwarden", "app tokens (go to gnome-keyring)", "passkeys (re-register by hand)" and "attachment mismatches".
- [ ] **Step 2:** Show Spyros the summary and ask through the ask UI whether to import anything missing (the agent can `vaultwarden_vault_add` it from `keepass_vault_get`, item by item, with his OK). **Stop until he confirms. Tasks 5 to 7 are blocked on this.**

### Task 5: Desktops switch to gnome-keyring (homepc first, then laptop)

Run over `ssh <host>`. Root steps need sudo. If `sudo -n true` fails, put the root steps into a wizard for Spyros instead.

- [ ] **Step 1: Root setup.** `pacman -S --needed gnome-keyring`. Find the PAM file of the login path (`greetd` or the DMS greeter or `login`; check `/etc/pam.d/` and `systemctl status display-manager`). Add `auth optional pam_gnome_keyring.so` and `session optional pam_gnome_keyring.so auto_start`. Verify with `grep pam_gnome_keyring /etc/pam.d/<file>`.
- [ ] **Step 2: Export app tokens from KeePassXC (still running).** In a throwaway script `/tmp/kp-export.py` (not committed; it uses `busctl`/D-Bus `GetSecrets` the way the discovery script did), dump every item that has a `service`, `application` or `xdg:schema` attribute to `/run/user/$UID/kp-items.json` (mode 0600, tmpfs). Also dump the 3 script secrets (cliproxy API key, jira api token, sops age key) under fixed names. Print only the item count.
- [ ] **Step 3: Switch providers.** Quit KeePassXC, remove `~/.config/autostart/*keepassxc*.desktop`, `systemctl --user enable --now gnome-keyring-daemon.socket`, and run `busctl --user status org.freedesktop.secrets` to confirm the owner is `gnome-keyring-daemon`. The first unlock asks Spyros to set the login keyring password: tell him to use his login password.
- [ ] **Step 4: Import.** `/tmp/kp-import.py` stores each item with its original attributes and label (`secret-tool store --label=... attr val ...`, secret on stdin). Store the script secrets as `service cliproxy key api`, `service jira key api` and (homePC) write the age key to `~/.config/sops/age/k3s-cluster.txt` with mode 0600. Then `shred -u /run/user/$UID/kp-items.json`. Verify with `secret-tool search --all service gh:github.com | grep -c ^label` ≥ 1, and `secret-tool lookup service cliproxy key api | wc -c` > 0.
- [ ] **Step 5: Repoint consumers.**
  - In the three `pi-cliproxyapi/config.json` files, replace `secret-tool lookup Uuid <id>` with `secret-tool lookup service cliproxy key api`. They sync through Syncthing, so edit once on homepc.
  - Make the same change in laptop `~/.local/bin/zed-launcher`.
  - In `dh-jira/scripts/jira`, replace the `kpx.sh` call with `secret-tool lookup service jira key api`.
  - Delete `~/.pi/profiles/epaflix/bin/sops-kpx` and its `sops` symlink.
  - Run `git config --global credential.helper /usr/lib/git-core/git-credential-libsecret`.
  - Verify: `sops -d 2-k3s/23.mcp-hub/mcp-hub-jev.enc.yaml >/dev/null` works on homepc, `gh auth status` is OK, and `ssh -T git@github.com` authenticates.
- [ ] **Step 6:** Ask Spyros to toggle Bitwarden's "Unlock with system authentication" off and on, then reboot. After the reboot, check:
  - `pgrep keepassxc` is empty.
  - The `org.freedesktop.secrets` owner is `gnome-keyring-daemon`, not kwallet.
  - Bitwarden unlocked without waiting.
  - The Step 5 checks still pass.
  - `ssh homepc true` from the pod works.
- [ ] **Step 7:** Repeat Steps 1 to 6 on the laptop. Skip the pi config edits (already synced) and the age key (already present).

### Task 6: Remove KeePass from the repo and agents (PR 2)

**Files:**
- Delete:
  - `2-k3s/15.syncthing/keepass.yaml`
  - `2-k3s/15.syncthing/files/keepass_mcp.py`
  - `2-k3s/15.syncthing/vault-passphrase.enc.yaml`
  - `2-k3s/15.syncthing/keepass-hub-secret.enc.yaml`
  - `2-k3s/23.mcp-hub/mcp-hub-keepass.enc.yaml`
- Modify:
  - `2-k3s/15.syncthing/kustomization.yaml`, `ksops-generator.yaml`
  - `2-k3s/23.mcp-hub/files/mcp_hub.py`, `deployment.yaml`, `kustomization.yaml`, `ksops-generator.yaml`, `tools/sops_secret.py` (drop `--keepass`), `tools/hub_clients.py` + synced copy (drop keepass from `SERVERS`/`ASK`; add `"keepass"` to a removal list so existing configs lose the entry), `README.md`
  - `.github/workflows/ci.yml` (drop pykeepass and the keepass selftest)
  - `2-k3s/13.t3code/one/files/jev-guard.js` + tests + `jev-guard.live.mjs` + `one/jev-guard.md`, `one/files/ssh-policy.test.py:36`, `one/jev-auto.md:166`, `env/README.md:17`, `v2/readiness.md:12`
  - `2-k3s/10.observability/deploy.sh:84`, `2-k3s/07.authentik-deployment/files/check_authentik_blueprint.py:13`, `.github/hooks/test-check-authentik-blueprint.sh:601-603` (the age key is now `~/.config/sops/age/k3s-cluster.txt` on both PCs)
  - `1-proxmox/ssh/homelab-ssh.md:10` and `2-k3s/13.t3code/one/files/homelab-ssh.md:10` ("never look in Vaultwarden for SSH keys")
  - The DeltaSync spec and plan get a one-line "Superseded by 2026-10-03-keepass-sunset" header.
- Skill: replace `keepassxc-secrets` with `vaultwarden-secrets` (SKILL.md only, no scripts). The pod and homepc paths use the hub tools `vaultwarden_vault_*`, and app tokens on a desktop use `secret-tool lookup service <s> key <k>`. Keep the "entry missing → ask Spyros to add it in Bitwarden, then continue" recipe. Update the skill source in the private-config bundle (`2-k3s/13.t3code/one/files/private-config.py` shows where), rebuild `one/private-agent-config.enc.yaml`, and replace `~/.agents/skills/keepassxc-secrets` on homepc (it syncs to the laptop and `dot-claude`).

**Interfaces:**
- Consumes: Task 2 names.
- Produces: no `keepass` route, client entry, pod or secret.

- [ ] **Step 1: Write the failing test.** `hub_clients.py --selftest` asserts that a config holding a `keepass` hub entry loses it, and that `keepass_vault_trash` disappears from the permissions.
- [ ] **Step 2: Run** the selftests and watch the new assert fail.
- [ ] **Step 3: Make the deletions and edits listed above.**
- [ ] **Step 4: Verify.**
  - The CI selftest commands and `node --test` suites pass.
  - `rg -i 'keepass|kdbx|kpx' --glob '!docs/superpowers/**' --glob '!**/package-lock.json'` returns only intended history lines (the migration README).
  - The kustomize render of `15.syncthing` has no keepass objects.
- [ ] **Step 5: Review gate, rebase, commit, PR 2, `link_pull_request`, CI green, ask Spyros to merge.**
- [ ] **Step 6: After merge.**
  - `kubectl get deploy,svc,netpol,secret -n syncthing | grep -i -E 'keepass|vault-passphrase'` is empty, because ArgoCD pruned them.
  - The hub `/keepass` route returns 404.
  - Restart the t3code pod only when Spyros agrees (it ends his session), then check that `opencode.json` has no `keepass`.

### Task 7: Backup and cleanup (D5)

- [ ] **Step 1:**
  - On homepc, create `~/Documents/keepass-archive-2026-10-03/` (mode 0700, outside every Syncthing folder; check with the Syncthing folder list).
  - Copy `~/Documents/Secrets/Passwords.kdbx`, `~/Documents/Passwords.kdbx`, `~/Documents/Passwords_backup.kdbx`, `~/Documents/kdewallet.kdbx` and the sync-conflict file into it.
  - Verify with `sha256sum` against the sources. Ask Spyros to open the main one once with `keepassxc-cli ls`, before Step 3 uninstalls keepassxc, because only he has the password.
- [ ] **Step 2:**
  - Remove folder `secrets-vault` from Syncthing on all 3 devices (REST API `DELETE /rest/config/folders/<id>` on each, or the web UI).
  - Then delete `~/Documents/Secrets` on homepc and the laptop, and `data/secrets-vault` on the `syncthing-data` PVC, through `kubectl exec` into the syncthing pod.
  - Delete the laptop copies in `~/Downloads/Passwords_*.kdbx` and `~/Documents/{Passwords,Passwords_backup,kdewallet}.kdbx`.
  - On homepc, delete only the originals of the files now in the archive.
- [ ] **Step 3:**
  - Run `pacman -Rns keepassxc git-credential-keepassxc` on both desktops (sudo, or a wizard).
  - Remove `~/.config/keepassxc` and `~/.cache/keepassxc`.
  - Verify: `find ~ -iname '*.kdbx' -not -path '*/keepass-archive-*'` is empty on both machines.
- [ ] **Step 4:** Report to Spyros what changed, then run `jev_jev_gate` on the evidence before calling it done.
