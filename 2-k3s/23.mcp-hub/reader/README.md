# Restricted homelab reader package

This is a separate manual-sync Application, `vault-reader-homelab`, in
namespace `vault-reader-homelab`. Its ClusterIP is
`http://vault-reader.vault-reader-homelab.svc.cluster.local:8000`.
The ordinary hub, mixed connector and work tools do not depend on it.
Work reader routes remain disabled. The owner approved the fixture-only
scope in #1778. Follow the provisioner, manual sync and canary procedure
below for activation. Keep #1778 open until the live checks certify G1
through G3.

## Private inputs and configuration

The external Secret `vault-reader-inputs` has exactly these mounted keys:
`config.json`, `client-id`, `client-secret`, `password`, `metadata-token`,
`broker-token`. Provision through the approved private secret workflow,
never chat, T3, model configuration, public manifests or logs. These are
new independent credentials, not the owner's personal CLI/cache/session.
No fixture Secret or approval flag is supplied by this package.

`config.json` has the exact following shape. Angle-bracket values are
invalid placeholders, not defaults or approved identities:

```json
{
  "environment": "homelab",
  "reader": {
    "server_url": "https://vault.homelab.example.test",
    "user_id": "<approved-user-uuid>",
    "organization_id": "<approved-organization-uuid>",
    "collection_ids": ["<approved-collection-uuid>"],
    "credentials_dir": "/run/vault-reader/private/credentials",
    "scratch_dir": "/run/vault-reader/private/scratch",
    "executable": "/tools/reader_bw_bridge.cjs"
  },
  "metadata_token_file": "/run/vault-reader/private/metadata-token",
  "broker_token_file": "/run/vault-reader/private/broker-token"
}
```

IDs are canonical UUIDs. Collections are a unique nonempty list of at most
128 IDs. The explicit HTTPS server and immutable user/organization IDs
bind authority, never display names or folder prefixes. Config is capped
at 64 KiB and duplicate keys refuse. Package paths are fixed as above.

Login files contain exact nonempty UTF-8 bytes, at most 16 KiB each, with
no NUL. No newline is appended or trimmed. Password spaces, tabs and
newlines are significant. `client-id` must be `user.<approved-user-uuid>`
before any login. Locked status must match that account and server before
unlock. Credentials reach only the appropriate subprocess environment,
not argv. The CLI's private cache can hold authentication state.

Metadata and broker tokens are separate random values, 32 to 4096 visible
ASCII characters, without whitespace/newline. They cannot begin with
`homelab.` or `work.` and must differ. Generate and store them privately.
These hub-to-reader bearer credentials are distinct from ordinary prefixed
hub client grants. The helper receives only its own `homelab.` client token.
The hub's `mcp-hub-instance` Secret holds the reader upstream bearers, not
the account API key or master password.

Every selected vault upstream in hub `credentials.json` has exactly
`url`, `token`, `protocol`. Use `protocol: restricted-v1`, one reader origin,
and these route mappings:

| Hub route | Reader path | Bearer |
| --- | --- | --- |
| `/vaultwarden` | `/vaultwarden` | metadata-token |
| `/vault-secret` | `/secret` | broker-token |
| `/vault-attachment` | `/attachment` | broker-token |

Opt in privately with the allowlist shape in
[the hub/helper contract](../instances/README.md#restricted-homelab-command-time-helper).
Only `vault_list` and `vault_get` are MCP tools. Broker secret and attachment
bodies are not model tools. References contain `collection_id` and exactly
one `item_id` or exact `title`. Known out-of-scope IDs, ambiguous titles,
personal items and observed unapproved collection assignments refuse.
An item in several observed approved collections requires an explicitly
assigned approved collection. There are no writes, uploads, raw CLI,
`bw serve`, SSH loading, migration or retirement routes.

## Projected ownership and state

Kubernetes projects root-owned group-readable files. The non-root startup
reads one pinned `..data` generation, bounds every regular file and copies
exact bytes into owned 0600 files under a real owned 0700 directory.
The core's private-file checks are unchanged. Files owned by root or
readable by group/other users are not accepted by the core/token/config
readers directly. There is no login at startup, health or schema discovery.

A private startup lock excludes a second process using the same scratch.
The single replica uses `Recreate`, not rolling overlap. `/run/vault-reader`
is a dedicated 128 MiB memory-backed emptyDir. Startup removes only its
owned private subtree before staging inputs. This clears cached session
and orphan state after container SIGKILL/restart, even when emptyDir survives.
It never cleans a legacy home or unrelated files. Graceful shutdown stops
the bounded worker/CLI process group and removes private state. Node failure
or volume loss is not secure erasure. Memory scratch counts toward the
1 GiB cgroup budget. CPU is capped at one core. Startup sets the inherited
core-dump limit to zero before starting the server or CLI.

The root filesystem is read-only, UID/GID are 1000, all capabilities are
dropped, privilege escalation is disabled and no service-account token is
mounted. Ingress permits only `app=mcp-hub` in `mcp-hub-homelab` on TCP 8000.
The policy does not restrict egress or protect against infrastructure
administrators. The privately configured server needs HTTPS and DNS
reachability. Authentication still rejects direct unauthorized requests.
Application separation is not infrastructure-admin or cross-cluster isolation.

## Freshness, rotation and recovery

One connector owns one lazy CLI home/session and one bounded reader worker.
Every data operation forces fresh sync and checks identity, ordinary enabled
confirmed User membership, exact visible collections and item scope before
returning data. Invalid references do not cause relogin. Runtime/login/sync/
grant errors discard state and latch further data reads closed. Health can
remain green while reads are latched; it is not grant or canary approval.
There is no login retry loop or cached-data/legacy fallback.

The reader refuses accounts that need automatic encrypted maintenance. In the
pinned CLI this includes a required minimum-PBKDF2 KDF update. Login and unlock
check the CLI's actual migration requirements but never execute a migration.
A failed maintenance check also refuses. Neither case returns a session or
vault data, and the existing command-failure latch remains closed. Ordinary
flag-off/current-KDF accounts still work. The default-disabled server flag is
not a read-only guarantee. The owner must arrange any account repair, KDF
change or key rotation outside this reader, then approve deliberate recovery.
Do not use repeated reader restarts to perform or bypass maintenance.

The package stages inputs once per process. Before each non-health HTTP
request it compares one current bounded projection generation with those exact
staged bytes. Any input change, missing file or unsafe projection closes the
reader and latches routes at 503, even if the old bytes are restored. It never
logs in with the changed projection automatically. Health stays live so an
input change cannot trigger a liveness restart loop. In-flight operations
remain bounded; this is not a transaction against simultaneous grant/input
changes. Only deliberate restart stages new authority. For every approved
input rotation, update `vault-reader.epaflix.com/inputs-revision` in
`homelab/deployment.yaml`. Hub `services.json` or `credentials.json` changes
require `mcp-hub.epaflix.com/inputs-revision` in
`../instances/homelab/kustomization.yaml`. Shared bearer rotation requires
both revisions and matching private Secrets. Follow the
[coordinated homelab input procedure](../instances/README.md#coordinated-homelab-input-changes)
for opaque revisions and ordered manual sync. Coordinate runtime
allowlist/token digest changes with the existing
`t3code.epaflix.com/inputs-grants-revision` and OnDelete/manual rollout. Stop
old consumers before changing authority; do not run two reader Pods during
credential/session changes. Removing vault services must also regenerate
client metadata and the private helper snapshot. Homelab startup exports
`MCP_HUB_SERVICES_FILE=$HOME/.config/t3-runtime/services.json`, derived only
from digest-validated private inputs, owned 0600. Failed validation removes
that managed snapshot instead of retaining a usable stale broker selection.
Work keeps its existing selection path and cannot select a restricted broker.

For a failure latch, investigate the owner-approved private configuration,
grants and backend condition first, then deliberately restart the reader
with the approved revision. Restart retries authentication only on the next
data operation. Restarting repeatedly is not a rate-limit workaround. Already
delivered secrets and deliberately retained downloads cannot be revoked.
Delete retained helper downloads explicitly after use; unlink is not secure
erasure. Quiet helper execution always discards child stdout/stderr, including
normal diagnostics. Validate consumers with synthetic values first.

## Pinned runtime and updates

The dedicated image uses Node 22.22.3 and Python 3.12.13, each digest-pinned,
CLI 2026.8.0 with npm lock integrity, and a hash-locked MCP 2.2.0/Python stack.
Dependencies are baked at build time. Startup does not download packages.
The bridge checks CLI package name/version and the entire bw.js SHA-256
`688a23275876477fa5eddd42a1a61a25f7168b3cd164bd634235531904cc87f6`
on every invocation. Two unique checked sites change in memory: the forced-sync
call avoids redundant identity renewal, and `DefaultEncryptedMigrator` refuses
any result other than `noMigrationNeeded`, or a detection error, before running
maintenance. This shared guard covers API-key login and passwordenv unlock.
The exact `restricted-sync-v2` capability distinguishes this contract from the
old sync-only bridge, which the core rejects before login. Missing, duplicated
or changed sites also refuse before login. Normal expiry renewal, forced network
fetch, throwing on sync failure and the original installed bundle remain
unchanged. Wrong artifacts or stock bw refuse with no fallback.
The ordinary legacy CLI/connector is unchanged.

Update base digests and both public lock files deliberately. A CLI update
requires renewed source/compiled-client review, bridge capability/hash/call
verification and the unchanged differential proof before changing the pin.
Never merely accept a new bundle hash. Regenerate references with:

```sh
bash 2-k3s/23.mcp-hub/reader/sync-image.sh
bash 2-k3s/13.t3code/one/tools/sync-shared.sh --check
bash 2-k3s/13.t3code/env/tools/sync-runtime-image.sh
bash 2-k3s/23.mcp-hub/reader/smoke.sh
```

`image-tag.sh` hashes every reader image source/lock input. CI builds and
runs the image on pull requests, verifies generated references, then publishes
the tested source-derived `inputs-<hash>` reference only off PRs. The runtime
workflow retains all three variants and tests real Node/kubectl helper startup
in homelab. No image publication or live sync is authorized by local tests.

The native proof executes actual compiled client methods with synthetic
constructor collaborators, controlled backend fetch and injected exports.
It does not log in, enroll or decrypt a real vault, nor prove full real
client/server interoperability. It verifies stock/adapted renewal differences,
fresh fetches, expiry, failed refresh and revoked-store behavior. Actual compiled
login/unlock and migration handlers prove stock attempts `change_kdf`, the reader
refuses without that SDK call, ordinary accounts work, and failed detection
refuses. Key Connector conversion and domain-confirmation paths already refuse
under `BW_NOINTERACTION=true`; the native test exercises those refusals too.
The common asymmetric-key regeneration login-success handler is present in the
bundle but is not constructed by the CLI container. Biometric migration is
desktop-only and V2 rotation is excluded for CLI. These checks do not prove all
SDK/WASM or server internals, crypto interoperability, or live feature flags.
Fixture composition tests core/HTTP failure latching with synthetic failed CLI
commands, not real account maintenance.

<a id="first-sync-gate-still-missing"></a>

## Provisioner, manual sync and canary gates

1. G1: Owner approves the dedicated non-administrator account, complete item
   and collection inventory, disjoint homelab-only grants, readOnly=true,
   manage=false, no all-collections/group override and no unrelated/work/
   personal access. Visible CLI exports cannot prove hidden assignments or
   effective readOnly/manage/group/all-collections authority.
2. G2: Owner supplies reviewed private input/account/bearer values through the
   secret workflow and approves the independent Application/manual first sync.
   Public revision annotations are not approval.
3. G3: An explicitly authorized live canary uses dummy items only on the
   existing server. Verify metadata, password/custom fields, attachment reads,
   denied writes/out-of-scope access, actual grants, token/burst behavior,
   absence of automatic account-maintenance writes and continued legacy service. No real-item migration or account mutation is
   part of these offline tests.

For authorized activation, follow the
[coordinated homelab input procedure](../instances/README.md#coordinated-homelab-input-changes)
in this order:

1. P1: The operator first manually syncs `vault-reader-homelab` and
   `mcp-hub-homelab` to create the namespaces. With the required Secrets
   missing, pods wait for their mounts and containers cannot start or log in.
   The reader also validates and stages inputs before serving requests;
   startup, health and schema discovery do not log in.
2. P2: Run `bash 2-k3s/23.mcp-hub/reader/provision-homelab.sh` on the
   owner's desktop, outside T3. The wizard stages `vault-reader-inputs`,
   `mcp-hub-instance` and `vault-canary-client`. After staging, kubelet retries
   the mounts and both pods start on their own at the tracked revision in no
   set order without logging in, then the R4 sync recreates them with the new
   revision.
3. P3: Apply R3: one tracked change sets both inputs revisions to the same
   new revision.
4. P4: Apply R4.
5. P5: Apply the separate bounded canary Job, which no Application manages:

```sh
kubectl --context epaflix apply -k 2-k3s/23.mcp-hub/reader/canary
kubectl --context epaflix -n mcp-hub-homelab logs job/vault-reader-canary
```

Each check prints only `PASS C1` through `PASS C11`, or
`FAIL C<number>: <non-secret reason>`. Any failure exits nonzero. Inspect
the Job status as well as all eleven lines; a deadline or killed pod is
not acceptance. Delete the finished Job before a deliberate rerun. The
wizard then compares the reader account's before/after KDF settings.
This comparison is not proof that no other account maintenance occurred.
These offline artifacts do not certify G1 through G3.
