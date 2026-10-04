# KeePass vault on DeltaSync Implementation Plan

> **Superseded** by `2026-10-03-keepass-sunset` (KeePass retired in favour of Vaultwarden).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The vault syncs per entry through a self-hosted DeltaSync server to t3code (new Go MCP), home PC, laptop and later the phone, and Syncthing no longer carries it.

**Architecture:** The DeltaSync server runs in `2-k3s/25.deltasync` on the shared CNPG cluster, and only the Traefik `internal` entrypoint reaches it. The keepass MCP is rewritten in Go. It imports upstream `client/mobile` for all crypto, keeps an in-memory index built from `/changes`, and serves the same seven tools behind the MCP hub. The desktops run the upstream client daemon after an upstream fix for pool-referenced attachments.

**Tech Stack:** DeltaSync server (PHP image), CNPG, Traefik, sops/ksops, Argo CD, Go 1.26, `github.com/modelcontextprotocol/go-sdk`, `gitlab.com/Star95/keepass-deltasync/client/mobile`.

**Spec:** `docs/superpowers/specs/2026-09-29-keepass-deltasync-design.md`

## Global Constraints

- Server image `registry.gitlab.com/star95/keepass-deltasync/server`, tag pinned in `kustomization.yaml` `images:`, updated by Renovate. Minor and major updates need review.
- Database: `DatabaseRole` + `Database` named `deltasync` on `postgres-cluster` in `postgres-system`. No other Postgres.
- Hostname `deltasync.epaflix.com`, entrypoint `internal` only, `tls.certResolver: cloudflare` with the `*.epaflix.com` domain block from `23.mcp-hub/ingress.yaml`. (The spec says cert-manager. The cluster uses Traefik's resolver, so follow the cluster.)
- MCP tool names and argument shapes match `2-k3s/15.syncthing/files/keepass_mcp.py` exactly: `vault_list(prefix="")`, `vault_get(path, include_password=true)`, `vault_add(path, username="", password="", url="", notes="", props=null)`, `vault_update(path, title?, username?, password?, url?, notes?, props?)` (a null prop value deletes it), `vault_trash(path)`, `vault_attach(path, filename, content_b64)`, `vault_attachment(path, filename)`. `vault_get`, `vault_update` and `vault_trash` also take an optional `uuid` that overrides `path`.
- Entry summary keys: `path`, `title`, `expired`, `username`, `url`, `notes`, `attachments` (sorted names). `vault_get` adds `custom_properties`, plus `password`, or `password: null` with the same expired note as today.
- MCP HTTP: `POST /keepass` requires header `X-Hub-Secret` equal to env `KEEPASS_HUB_SECRET`, else 401. `GET /healthz` is open and returns 503 when the last successful `/changes` call is older than 5 minutes.
- Never log secret values. One log line per write: tool, entry UUID, result.
- Canonical UUIDs are lowercase hyphenated. The root group is the sentinel `parent_group: ""`. Root and the Recycle Bin are never synced objects. Trash means a `DELETE` tombstone.
- Commit style `type(scope): summary`, no co-author, rebase on `origin/main` before every commit.

## Review Focus

1. Round-trip loss: updating one field must keep every canonical field the MCP does not model (`autotype`, `history`, `custom_data`, `tags`, times). Tested in Task 3 (`TestUpdateKeepsUnknownFields`).
2. Two entries with the same path: `vault_get` by path errors and lists their UUIDs instead of picking one silently. Tested in Task 3 (`TestDuplicatePathIsAmbiguous`).
3. A stale write: another device changed the entry since the last refresh. The MCP refreshes before every write and bumps `times.modified` to now. Tested in Task 3 (`TestWriteRefreshesFirst`).
4. An entry whose `parent_group` points to a group that is unknown or deleted shows under `/` (root), as upstream does, and does not vanish. Tested in Task 3 (`TestOrphanFallsBackToRoot`).
5. An expired entry never returns its password. Tested in Task 4 (`TestExpiredWithholdsPassword`).

---

### Task 1: Upstream fix for pool-referenced attachments (F1)

Work in the scratch clone `/tmp/opencode/ds` (already cloned from `https://github.com/HBBSoftware/Keepass-DeltaSync`). Nothing is committed to epaflix. The output is a patch file.

**Files:**
- Modify: `client/internal/kdbx/canonical/parse.go` (binary loop near line 218) and whatever passes the export XML into it (find with `grep -rn 'ParseExport\|func Parse' client/internal/kdbx`).
- Test: `client/internal/kdbx/canonical/pool_test.go`
- Output: `/tmp/opencode/deltasync-attachment-pool.patch`

**Interfaces:**
- Produces: the parser resolves `<Value Ref="N"/>` against `<Meta><Binaries><Binary ID="N" Compressed="True|False">base64</Binary>`, gunzipping when `Compressed="True"`. An unknown `Ref` returns an error `binary %q: pool ref %s not found`.

- [ ] **Step 1: Write `TestPoolRefAttachment`.** Use an inline export fixture, copied from a real `keepassxc-cli 2.7.10 export` (captured in the spec's F1 section): one entry `e1`, one binary `id_ed25519` with `Ref="0"`, pool `ID="0" Compressed="True"`, gzip of `secretkeydata\n`. Assert that the parsed entry has `Binaries == []Binary{{Name: "id_ed25519", Data: []byte("secretkeydata\n")}}`. Add `TestPoolRefMissing`: `Ref="7"` with no pool entry returns an error that contains `pool ref 7 not found`.
- [ ] **Step 2: Run** `cd client && go test ./internal/kdbx/canonical/ -run Pool -v`. Expected: FAIL (binaries empty). Install Go 1.26 into `~/go-sdk` from go.dev/dl if `go` is missing.
- [ ] **Step 3: Implement.** Parse `Meta/Binaries` once per export into `map[string][]byte`, pass it to the entry parser, and resolve `Ref` there. History entries use the same map.
- [ ] **Step 4: Run** `go test ./...` in `client/`. Expected: all PASS.
- [ ] **Step 5: Export the patch.** `git -C /tmp/opencode/ds diff > /tmp/opencode/deltasync-attachment-pool.patch`. Report the path to Spyros, who submits the MR upstream. Cutover (Task 6) waits for a `client/v*` release that contains it.

### Task 2: DeltaSync server in `2-k3s/25.deltasync`

**Files:**
- Create: `2-k3s/25.deltasync/{namespace.yaml,database.yaml,server.yaml,ingress.yaml,kustomization.yaml,ksops-generator.yaml,deltasync-secrets.enc.yaml,README.md}`
- Create: `2-k3s/11.argocd/apps/app-deltasync.yaml`, a copy of `app-deal-finder.yaml` with path `2-k3s/25.deltasync` and namespace `deltasync`.
- Modify: `.github/renovate.json`. Add a rule for `matchFileNames: ["2-k3s/25.deltasync/kustomization.yaml"]` with `matchUpdateTypes: ["minor","major"]` and `automerge: false`, like the servarr rule.
- Modify: `2-k3s/10.observability/blackbox-values.yaml`. Add a target named `deltasync` with url `https://deltasync.epaflix.com/api/v1/health`.

**Interfaces:**
- Produces: in-cluster URL `http://deltasync.deltasync.svc` (Service port 80) for the MCP, and `https://deltasync.epaflix.com` for devices.
- Produces: the sops secret `deltasync-secrets` in namespace `deltasync`, with keys `db-password`, `admin-password` and `admin-token`. The role Secret `deltasync-db-role` goes in `postgres-system` (`username: deltasync`, `password` matching `db-password`), like `24.deal-finder`.

- [ ] **Step 1: Write the manifests.**
  - `database.yaml` copies `24.deal-finder/database.yaml` with `deltasync` names.
  - `kustomization.yaml` has no top-level namespace, same as deal-finder.
  - `server.yaml`: a Deployment with 1 replica, `Recreate` strategy and container port 80. Env: `DATABASE_URL=pgsql:host=postgres-cluster-rw.postgres-system.svc;port=5432;dbname=deltasync`, `DATABASE_USER=deltasync`, `DATABASE_PASSWORD` and `PGPASSWORD` from the secret, `PGHOST`/`PGPORT`/`PGDATABASE`/`PGUSER` to match, `LOG_LEVEL=INFO`, `ADMIN_USERNAME=admin`, `ADMIN_PASSWORD_FILE=/run/secrets/deltasync/admin-password`, `ADMIN_TOKEN_FILE=/run/secrets/deltasync/admin-token`, and `TRUSTED_PROXIES` set to the k3s pod CIDR (check it with `kubectl get nodes -o jsonpath='{.items[*].spec.podCIDR}'`, expect `10.42.0.0/16`).
  - Readiness and liveness probes use `httpGet /api/v1/health`.
  - The pod has resource requests and runs non-root if the image allows it. Test by rendering, not by guessing.
  - Plus a Service.
- [ ] **Step 2: Generate the secrets.** Generate `db-password` and `admin-password` (32 characters) and `admin-token` (`openssl rand -base64 32 | tr '+/' '-_' | tr -d '='`). Encrypt them with sops using the repo's `.sops.yaml`. Store `admin` plus the password, and the token as the custom property `admin_token`, in KeePass at `/Homelab/deltasync-admin` with `vault_add`. Nothing goes in shell history. Pipe it from `openssl` into the files.
- [ ] **Step 3: Verify the render.** Run `kustomize build --enable-alpha-plugins --enable-exec 2-k3s/25.deltasync`. Expected: it renders the Namespace, DatabaseRole, Database, 2 Secrets, Deployment, Service and IngressRoute, and exits 0. Also run `jq . .github/renovate.json >/dev/null`.
- [ ] **Step 4: Commit and open a PR.** `feat(deltasync): self-hosted DeltaSync server on the shared CNPG cluster`. Link the PR with `link_pull_request`. After merge and the Argo CD sync, `curl -fsS https://deltasync.epaflix.com/api/v1/health` returns 200 from the LAN, and `kubectl -n deltasync logs deploy/deltasync | grep 'admin account'` shows the password length.
- [ ] **Step 5: Create the user.** Run `kubectl -n deltasync exec deploy/deltasync -- php bin/admin user:create spyros`. Record in the README that enrollment tokens come from `admin.html`.

### Task 3: MCP vault core (Go)

**Files:**
- Create: `images/keepass-mcp/go.mod` (module `github.com/SpyrosPsarras/epaflix/images/keepass-mcp`, `go 1.26`, requires `gitlab.com/Star95/keepass-deltasync/client` pinned to the latest tagged `client/v*` pseudo-version and `github.com/modelcontextprotocol/go-sdk`)
- Create: `images/keepass-mcp/api.go`, the HTTP client
- Create: `images/keepass-mcp/vault.go`, the index, paths and entry edits
- Test: `images/keepass-mcp/vault_test.go`, which runs against an `httptest.Server` fake of the four endpoints below and uses real `mobile.NewSession` crypto

**Interfaces:**
- `type API struct{ Base, Token string; HTTP *http.Client }`
- `func (a *API) Changes(ctx, dbID string, since int64) (Changes, error)` calls `GET /api/v1/databases/{dbID}/changes?since=N&include=groups`, and `Changes{CurrentSeq int64; Objects []Change}`, `Change{UUID, Blob, ModifiedAt string; Deleted bool; Seq int64; Kind int}`. Kind 2 means group; 0 or 1 means entry.
- `func (a *API) Put(ctx, dbID, kind, uuid string, blob []byte, modified time.Time) (seq int64, err error)` and `Delete(...)` call `PUT|DELETE /api/v1/databases/{dbID}/{entries|groups}/{uuid}` with body `{"modified_at":"2006-01-02T15:04:05Z","blob":"<std base64>"}`. The response is `{"entry":{...}}` or `{"group":{...}}`. `Authorization: Bearer <device token>`.
- `func (a *API) DatabaseID(ctx, name string) (string, error)` calls `GET /api/v1/databases` and matches on name.
- `type Vault` holds `*mobile.Session`, `map[uuid]entry` (raw decrypted JSON as `map[string]any`, so unknown fields round-trip), `map[uuid]group`, `seq`, `lastSync time.Time` and a mutex.
- `func Open(ctx, api *API, dbID string, password []byte) (*Vault, error)` does a full load from `since=0`.
- `func (v *Vault) Refresh(ctx) error` pulls `since=seq`, applies it (a deleted object drops out of the index), and sets `lastSync`.
- `func (v *Vault) Entries() []Entry`, where `Entry{UUID, Path, Title, Username, URL, Notes, Password string; Expired bool; Props map[string]string; Attachments []Attachment}`. The standard KeePass string keys are `Title`, `UserName`, `Password`, `URL` and `Notes`. Every other string key is a custom property. `Path` is the group names from root, then the title, joined by `/` with a leading `/`. `Expired` means `times.expires` is true and `expiry_time` is in the past (check the field names in the upstream `Times` struct).
- `func (v *Vault) Find(path, uuid string) (Entry, error)` returns an ambiguity error that lists the UUIDs when a path matches more than one entry.
- `func (v *Vault) Write(ctx, uuid string, edit func(raw map[string]any) error) error` refreshes, applies the edit, sets `times.modified` to now in UTC, then encrypts and PUTs.
- `func (v *Vault) Add(ctx, path string, fields map[string]String) (Entry, error)` creates any missing groups (PUT a group with a new UUID and `parent_group`), then the entry, with `v:1` and all four times set to now.
- `func (v *Vault) Trash(ctx, uuid string) error` sends a DELETE.

- [ ] **Step 1: Write the failing tests.**
  - `TestPathsFromGroups`: groups SSH (root) with a child `keys`, and entry `k` in `keys`. Its path is `/SSH/keys/k`.
  - `TestUpdateKeepsUnknownFields`: an entry carrying `autotype`, `history`, `custom_data` and `tags`. Change `UserName`, capture the PUT blob, decrypt it, and assert that all four are byte-equal to the input and `times.modified` moved forward.
  - `TestDuplicatePathIsAmbiguous`.
  - `TestWriteRefreshesFirst`: the fake server bumps an entry between `Open` and `Write`. The written blob starts from the bumped version.
  - `TestOrphanFallsBackToRoot`.
  - `TestAttachmentRoundTrip`: `binaries[{name,data}]` with the data base64 in JSON returns the same bytes.
  - `TestDeletedDropsOut`.
- [ ] **Step 2: Run** `cd images/keepass-mcp && go test ./...`. Expected: FAIL (undefined).
- [ ] **Step 3: Implement** `api.go` and `vault.go` to the interfaces above.
- [ ] **Step 4: Run** `go test ./... && go vet ./...`. Expected: PASS.
- [ ] **Step 5: Commit.** `feat(keepass-mcp): DeltaSync vault core on upstream client/mobile crypto`.

### Task 4: MCP tools, HTTP server, enroll command, image

**Files:**
- Create: `images/keepass-mcp/main.go` with the subcommands `serve` (default) and `enroll`
- Create: `images/keepass-mcp/tools.go`, the seven tools on the go-sdk
- Test: `images/keepass-mcp/tools_test.go`
- Create: `images/keepass-mcp/Dockerfile`. A multi-stage build: `golang:1.26` runs `go test ./...` and then `CGO_ENABLED=0 go build`. The final image is `gcr.io/distroless/static:nonroot`.
- Create: `.github/workflows/build-keepass-mcp.yml`, copied from `build-vpn-picker.yml` with `images/keepass-mcp` paths. It tags `:${sha}` and `:latest` and prints the digest.

**Interfaces:**
- Consumes: Task 3 `Vault`.
- Env: `DELTASYNC_URL` (default `http://deltasync.deltasync.svc`), `DELTASYNC_DATABASE` (name, default `passwords`), `DELTASYNC_DEVICE_TOKEN`, `KEEPASS_PASSPHRASE`, `KEEPASS_HUB_SECRET`, `LISTEN` (default `:8000`).
- `enroll --server URL --name t3code <enrollment-token>` generates a keypair (`mobile.GenerateDeviceKeypair`), POSTs `/api/v1/devices/enroll` (body as in upstream `client/internal/api/client.go` `Enroll`), and prints JSON `{device_token, private_key_b64}` to stdout only. It never writes files.
- Tools refresh at most every 3 s before a read. `/healthz` follows the Global Constraints.

- [ ] **Step 1: Write the failing tests.** Against the Task 3 fake server:
  - `TestHubSecret`: 401 without the header or with a wrong one, 200 on `initialize` with the right one.
  - `TestHealthzStale`: `lastSync` 6 minutes old returns 503. Fresh returns 200.
  - `TestToolContract`: `tools/list` returns exactly the seven names. `vault_list` returns the summary keys from the Global Constraints.
  - `TestExpiredWithholdsPassword`.
  - `TestUpdateNullPropDeletes`.
  - `TestGetByUUIDForUntitled`.
- [ ] **Step 2: Run** `go test ./...`. Expected: FAIL.
- [ ] **Step 3: Implement** `tools.go` and `main.go`. Mount the streamable HTTP handler at `/keepass`, wrapped in the secret check.
- [ ] **Step 4: Run** `go test ./... && docker build images/keepass-mcp` (or rely on CI if there's no docker here). Expected: PASS.
- [ ] **Step 5: Commit and open a PR.** `feat(keepass-mcp): Go MCP server and image build`. Link it. After merge, note the printed digest for Task 5.

### Task 5: MCP deployment next to the server (not yet on the hub)

**Files:**
- Create: `2-k3s/25.deltasync/keepass-mcp.yaml`. A Deployment with 1 replica, an image pinned by the Task 4 digest, `automountServiceAccountToken: false`, non-root, a read-only root filesystem, `drop: [ALL]`, and exec probes on `/healthz` as in `15.syncthing/keepass.yaml`. Plus a Service `keepass:8000` and a NetworkPolicy that admits only `mcp-hub` pods, copied from `15.syncthing/keepass.yaml`.
- Create: `2-k3s/25.deltasync/keepass-mcp.enc.yaml`, the sops Secret `keepass-mcp` with `device-token`, `private-key` and `passphrase`. The passphrase is the value in `t3code-vault-passphrase`, copied through a pipe and never printed.
- Modify: `2-k3s/25.deltasync/kustomization.yaml` adds the new files and the same hub-secret revision replacement as `15.syncthing/kustomization.yaml`.
- Modify: `2-k3s/23.mcp-hub/tools/sops_secret.py` only if it hardcodes the `syncthing` namespace for `--keepass`. Make the namespace `deltasync`.

- [ ] **Step 1: Enroll the MCP.** Get an enrollment token for `spyros` from `admin.html`. Run `go run . enroll --server https://deltasync.epaflix.com --name t3code <token> | sops --encrypt ...` into `keepass-mcp.enc.yaml`, so nothing reaches the terminal. Point it at a test database named `passwords-test`, created by the home PC in Task 6 step 1.
- [ ] **Step 2: Verify the render** with `kustomize build ... 2-k3s/25.deltasync`. Expected: the MCP Deployment, Service, NetworkPolicy and Secret are present.
- [ ] **Step 3: Commit and open a PR.** `feat(deltasync): deploy the Go keepass MCP against the test database`. After the sync, `kubectl -n deltasync exec deploy/keepass -- /keepass-mcp healthcheck` (or wget `/healthz` if the binary has no subcommand) returns ok.

### Task 6: Test run, then cutover (runbook, with Spyros)

These steps are manual. Do each only with Spyros present. Step 6 onward waits for the Task 1 fix in a release.

**Files:**
- Modify: `2-k3s/25.deltasync/README.md`. Record each step as done, with the date.
- Modify at cutover: `2-k3s/23.mcp-hub` (the `/keepass` upstream becomes `keepass.deltasync.svc:8000`), `2-k3s/15.syncthing` (remove `keepass.yaml`, `files/keepass_mcp.py`, the configMapGenerator and the replacement), `/home/spyros/.agents/skills/keepassxc-secrets/SKILL.md` (the t3code section describes DeltaSync, and the `k._writing()` recipe is replaced by the `uuid` argument), and `23.mcp-hub/README.md`.

1. [ ] Home PC: install `keepass-deltasync` (the latest Linux release), enroll, then `init passwords-test ~/tmp/passwords-test.kdbx` with a copy of the vault. The laptop does the same on its own copy. Point the MCP at `passwords-test`. Check: an edit on the PC, another on the laptop and a `vault_update` from t3code, on three different entries within one minute, all show up everywhere. `vault_attachment /SSH/spyros id_ed25519` fails before the Task 1 fix and works after it.
2. [ ] Phone, once it is attached to the home PC over adb: DeltaSync from F-Droid plus KeePassDX, enrolled by QR code against `passwords-test`. Check: an `/SSH/*` entry edited on the phone keeps its attachment on the PC, and the other way round. If this fails, stop and report. The desktops can still cut over.
3. [ ] Freeze: KeePassXC closed on both desktops, and the old keepass MCP scaled to 0 (`kubectl -n syncthing scale deploy/keepass --replicas 0`).
4. [ ] Backup: `Passwords-<date>-pre-deltasync.kdbx`, copied to the home PC and to TrueNAS.
5. [ ] Home PC: `init passwords <real vault path>` and `sync`. Laptop: `init passwords <its path>` and `sync`. Both run `daemon --store-keyring` as a systemd user unit (`~/.config/systemd/user/keepass-deltasync.service`, `Restart=on-failure`).
6. [ ] MCP: set `DELTASYNC_DATABASE=passwords`. Switch the hub's `/keepass` upstream. From t3code, `vault_list` count equals 337 minus the Recycle Bin entries, and the `/SSH/spyros` key logs in to Pi-hole.
7. [ ] Remove the vault folder from Syncthing on every device and from the cluster Syncthing. Open the PR that removes the old keepass pieces from `15.syncthing`, and update the skill and the hub README.
8. [ ] Phone on `passwords`, as in step 2.

Rollback before step 7: scale the old MCP back up and restore the backup into the Syncthing folder.
