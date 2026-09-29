# KeePass vault on DeltaSync

Status: draft for review, 2026-09-29.

## Goal

Move the KeePass vault (`Passwords.kdbx`) off Syncthing and onto a
self-hosted [DeltaSync](https://github.com/HBBSoftware/Keepass-DeltaSync)
server, for two reasons:

1. Edits made on several devices at the same moment merge per entry, where
   today they produce `.sync-conflict` copies.
2. The Android phone joins the vault.

Devices: t3code (through the keepass MCP), the home PC and the laptop (both
Linux with KeePassXC), and an Android phone.

Out of scope: the Syncthing folders that are not the vault. They stay on
Syncthing.

## Decisions

- Q1. We fix both conflicts and phone access.
- Q2. The server is reachable only on the LAN and over Tailscale. The
  Tailscale subnet router already routes `192.168.10.0/24`, and Pi-hole
  resolves the names, so the Traefik `internal` entrypoint is enough.
- Q3. We rewrite the MCP to talk to the DeltaSync API directly. There is no
  local `.kdbx` on the t3code side.
- Q6. Hard cutover. A dated `.kdbx` backup is kept for rollback.
- F1. We fix the attachment gap upstream before cutover (see below).
- The image uses a pinned tag that Renovate updates. The `:latest` tag
  would never trigger an Argo CD rollout, because the manifest never
  changes.
- The database lives in the existing epaflix CNPG cluster. We add no
  Postgres of our own.

## F1: attachments are dropped by the desktop client

`keepassxc-cli export` (checked on 2.7.10) writes attachments as pool
references:

```xml
<Meta><Binaries><Binary ID="0" Compressed="True">H4sI...</Binary></Binaries></Meta>
...
<Binary><Key>id_ed25519</Key><Value Ref="0"/></Binary>
```

The client's parser skips every `Ref` binary
(`client/internal/kdbx/canonical/parse.go:219`, "Skip for v1"). 30 of the 337
vault entries have attachments: every `/SSH/*` key with its
`KeeAgent.settings`, the `carbone-test` pem files, and every Secret Service
item (`FDO_SECRETS_DATA`). Without a fix these never leave the device that
created them, and an edit synced from another device can remove them.

The canonical wire format already carries inline binaries, so only the
desktop parser needs to change.

## Components

### D1. Server: `2-k3s/25.deltasync`

- Namespace `deltasync`. One `Deployment` running
  `registry.gitlab.com/star95/keepass-deltasync/server`. The tag is set in
  `kustomization.yaml` `images:`, where the existing kustomize manager in
  `.github/renovate.json` updates it. Server minor and major updates need
  review, like the servarr rule.
- Database: a `DatabaseRole` and a `Database` named `deltasync` on
  `postgres-cluster` in `postgres-system`, copied from
  `24.deal-finder/database.yaml`. The role password lives in a sops secret.
  The app gets `DATABASE_URL`/`DATABASE_USER`/`DATABASE_PASSWORD` and the
  `PG*` variables for the entrypoint's migrations, pointed at
  `postgres-cluster-rw.postgres-system`.
- Admin: `ADMIN_USERNAME`, `ADMIN_PASSWORD_FILE` and `ADMIN_TOKEN_FILE` come
  from a sops secret mounted as files. The same values are stored in KeePass
  as `/Homelab/deltasync-admin`.
- `Service` on port 80. A Traefik `IngressRoute` on entrypoint `internal`
  for `deltasync.epaflix.com`, with a cert-manager certificate. Set
  `TRUSTED_PROXIES` to the Traefik pod CIDR so the session cookie gets
  `Secure` and the audit log records real client IPs.
- An Argo CD application in `11.argocd/apps/`, like the other apps.
- Backups come from the CNPG cluster's existing backups. The server stores
  only client-encrypted blobs.

### D2. Upstream fix for F1

This is a patch to `client/internal/kdbx/canonical/parse.go` (and to the
code that calls it with the export XML):

1. Read `Meta/Binaries/Binary` into a map from `ID` to bytes. When
   `Compressed="True"`, gunzip it.
2. When an entry binary has `Ref`, resolve it from that map. If the ID is
   missing, fail the sync. Do not skip it silently.
3. Add a test that exports a real `keepassxc-cli` database with one
   attachment, parses it, emits it, imports it again, and compares the
   bytes.

The patch is written in a scratch clone. Spyros submits the MR to
`gitlab.com/Star95/keepass-deltasync`. Cutover waits for a `client/v*`
release that contains the fix.

### D3. New keepass MCP (Go)

- Lives in `2-k3s/25.deltasync` next to the server. The vault leaves
  Syncthing, so `15.syncthing` is the wrong home. It replaces
  `15.syncthing/files/keepass_mcp.py` and the pykeepass Deployment, which
  are removed at cutover step 6.
- Crypto comes from the upstream public package
  `gitlab.com/Star95/keepass-deltasync/client/mobile` (`NewSession`,
  `EncryptEntry`, `DecryptEntry`, `EncryptGroup`, `DecryptGroup`). We port no
  crypto. We write only the HTTP client for `/api/v1/databases/{id}/changes`,
  `/entries/{uuid}`, `/groups/{uuid}` and `/devices/enroll`, following the
  upstream server routes.
- The tools keep the same names and argument shapes as today:
  `vault_list`, `vault_get`, `vault_add`, `vault_update`, `vault_trash`,
  `vault_attach` and `vault_attachment`. Paths still look like
  `/Group/Title`. The hub path `/keepass`, the `X-Hub-Secret` check and the
  hub-only NetworkPolicy stay as they are, so the `keepassxc-secrets` skill
  and the MCP hub need no changes.
- State: an in-memory index of the decrypted entries and groups, built on
  start from `changes?since=0` and refreshed from `changes?since=<seq>`
  before each call, at most every few seconds. Writes push one entry and
  update the index from the response.
- Identity: its own enrolled device, "t3code". The device token, the
  X25519 private key and the vault master password come from sops secrets.
  The master password reuses `t3code-vault-passphrase`.
- Untitled entries: the current `k._writing()` workaround goes away. The
  plan adds a `uuid` argument to `vault_get`, `vault_update` and
  `vault_trash`.

### D4. Desktops (home PC, laptop)

Both machines are on the LAN. The laptop also runs Tailscale, so it
reaches the same hostname when it is away. No per-device server URL.

- Install the `client/v*` Linux release that contains the D2 fix.
- Run `keepass-deltasync enroll --server https://deltasync.epaflix.com
  <token>`, then `init passwords <path to Passwords.kdbx>`.
- Run `daemon --store-keyring` as a systemd user unit.
- The home PC pushes first and is the source of truth for the initial
  upload.

### D5. Phone (Android)

- DeltaSync from F-Droid, with KeePassDX as the editor.
- Enroll with the QR code from `admin.html`. Turn on "remember password"
  and background sync.
- The phone gets connected to the home PC later, and this step runs then,
  over adb from the home PC.
- Test before cutover: an `/SSH/*` entry, edited on the phone, keeps its
  attachment on the desktop, and the other way round. Android uses kotpass,
  not the desktop parser, so D2 does not cover it.

### D6. Cutover

Preconditions: the D2 release exists, D1 and D3 run against a test copy of
the vault, and the D5 attachment test passes.

1. Announce the freeze. Stop KeePassXC on every desktop and scale the old
   keepass MCP to 0.
2. Copy `Passwords.kdbx` to `Passwords-2026-MM-DD-pre-deltasync.kdbx` on
   the home PC and in KeePass-independent storage (TrueNAS).
3. Home PC: `init` and the first `sync`.
4. Enroll the laptop and run `init` on it, pointed at its existing file, so
   it merges.
5. Deploy D3 and check `vault_list` against it.
6. Remove the vault folder from Syncthing on every device and in
   `2-k3s/15.syncthing`.
7. Enroll the phone (D5) once it is attached to the home PC.
8. Update the `keepassxc-secrets` skill and `23.mcp-hub/README.md`.

Rollback: put the dated backup back into the Syncthing folder and scale the
old MCP back up. The DeltaSync server can keep running unused.

### D7. Observability

- A Prometheus probe on the server's `/api/v1/health`.
- The server's audit log (who, device, IP) through `admin.html` and
  `/api/v1/admin/log`.
- The MCP logs one line per write (tool, entry UUID, device, result). It
  never logs values. Its `/healthz` fails when the last successful
  `/changes` call is older than 5 minutes.

## Risks

- R1. DeltaSync is young and has mostly one maintainer, with its own crypto
  scheme. Mitigation: the dated backup, CNPG backups, and upstream's
  per-entry history (3 versions).
- R2. Android syncs by polling every 15 to 30 minutes.
- R3. The MCP depends on the upstream `client/mobile` API. Pin the Go
  module version, and let Renovate handle `gomod` updates, each reviewed.

## Verification

- D1: `curl https://deltasync.epaflix.com/api/v1/health` returns 200 over
  both LAN and Tailscale.
- D2: the upstream test passes. On a test vault, an attachment added on one
  desktop shows up byte-identical on the other.
- D3: every tool works against a test database. Two parallel `vault_update`
  calls plus a desktop edit on other entries merge with no loss.
- D6: after cutover, `vault_get` on `/SSH/spyros` returns the `id_ed25519`
  attachment, and an SSH login with it to Pi-hole works.
