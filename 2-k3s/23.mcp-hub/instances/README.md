# Homelab and work hubs

`homelab/` and `work/` run the same maintained gateway from `../files/`.
They have separate namespaces, Services, Deployments, health probes,
ServiceAccounts, client grants and upstream credentials. The legacy hub
remains available with its existing clients and routes. No consumer is
switched by this change.

The two new ArgoCD Applications require manual sync. Their public overlays
contain no Secret, approved service inventory, ingress hostname or account
identity. Without private configuration their pods cannot start. Rendering
an overlay is not approval to sync it.

## Configuration

Each namespace requires its own `mcp-hub-instance` Secret with two keys.

`services.json` is the explicit allowlist, safe to distribute to that
instance's clients. This synthetic example is not the approved inventory:

```json
{"environment": "homelab", "services": ["searxng"]}
```

`credentials.json` stays on the hub. Its shape is illustrated below with
invalid client digests, so this example cannot start a deployment:

```json
{
  "clients": {"agent": "<lowercase-sha256-of-homelab-prefixed-token>"},
  "upstreams": {
    "searxng": {
      "url": "https://search.homelab.example.test/mcp",
      "token": "<independent-upstream-bearer>"
    }
  }
}
```

For work, `environment` must be `work`. Service names are `gmail`,
`searxng`, `drive`, `kubernetes` and `notion`. An empty list, unknown or
duplicate name, environment mismatch, malformed client digest, missing
connection or extra connection prevents startup. Work rejects `vaultwarden`,
`vault-secret` and `vault-attachment`. Homelab source can select these routes
only with the explicit `vault_reader: restricted-v1` marker below. The
dedicated reader has its own manual Application. The narrow helper is
installed only in the homelab runtime. Neither is live-approved.

The new hubs expose exactly the listed routes, plus unauthenticated
`GET /healthz`. They forward to explicitly configured MCP endpoints rather
than instantiate the legacy modules or use their default upstreams. Every
non-Notion connection needs its own bearer token, distinct from ordinary
client credentials. URLs cannot contain
credentials, query parameters or fragments. The client's bearer never
leaves the hub.

These endpoints must enforce application authentication on direct requests,
not just rely on tool discovery or NetworkPolicy. The legacy unauthenticated
Drive and Kubernetes backends are not suitable new connections. Approval
must identify separately authenticated endpoints with separate upstream
identities. If a backend does not support authentication, its approved
connection needs an authenticated gateway and ordinary clients must not
reach its raw port. No legacy upstream credentials or personal vault login
may be copied into either new namespace.

A newly minted ordinary client token is `homelab.` or `work.` followed by
`secrets.token_urlsafe(32)`. Store only its SHA-256 digest under `clients`.
Use a fresh random value for each client and environment. Tokens must never
be printed or committed. The other new hub rejects the prefix, and the
legacy hub reserves both prefixes and rejects them even if their digest
was accidentally registered there. Existing legacy tokens keep working.

## Notion refresh ownership

A `notion` connection has only its URL:

```json
{"url": "https://mcp.notion.com/mcp"}
```

It does not accept a static token or a configurable grant namespace. Each
new hub owns `mcp-hub-notion-grant` in its own namespace. Its Role permits
only get/patch on that one Secret. It never reads the legacy grant. Startup
requires a usable independent grant when Notion is listed. The existing
rotation, locking and write-back code remains the sole refresher.

The single replica and `Recreate` rollout are intentional. Do not scale a
hub or run two refresh owners against one Notion grant. Never copy the
legacy refresh token. After account and live-operation approval, the
bootstrap tool's `--instance homelab|work` option selects only that
namespace and requires an explicit `--context`. Without `--instance` its
legacy behavior remains unchanged. The browser must consent as the approved
account, not silently reuse a personal browser session. Account verification
and consent are human checkpoints.

## Client configuration

Distribute only `services.json`, the chosen hub URL and the client's own
token reference to an isolated agent home. Do not distribute
`credentials.json`. For example, with synthetic paths and URLs:

```sh
python3 tools/hub_clients.py pi /tmp/agent-home/mcp.json \
  https://homelab.example.test HOMELAB_MCP_TOKEN /tmp/approved-services.json
```

The same final argument works for OpenCode, Claude Code and Codex. The
existing call shape is unchanged for legacy clients. Alternatively set
`MCP_HUB_SERVICES_FILE` and optionally `MCP_HUB_INSTANCE` in the agent
process; the latter requires a matching allowlist file. The existing T3
entrypoints inherit these options. Instance registration failures, or
missing instance URL/token settings, stop startup rather than retain a
stale broad configuration. Legacy registration warnings retain their old
behavior. No live consumer is changed until its deliberate cutover.

Generation removes stale maintained hub entries outside the allowlist. It
preserves unrelated servers and existing OpenCode approval choices. It
never advertises command-time secret or attachment routes as model tools.
`vault-run.py` remains the legacy runner and refuses instance settings before
reading a legacy key file or making a request. The separate restricted
helper below does not change its behavior. Neither helper replaces
organizational agent policies.

## Restricted homelab command-time helper

`vault-restricted` is installed only in the homelab runtime image from
`2-k3s/13.t3code/one/files/vault-restricted.py`. No real account or grants
are implied by these synthetic examples. Owner approval, private provisioning,
manual first sync and the dummy-item live canary are still required before
activation. See [reader package and private inputs](../reader/README.md).
The laptop selects the remote backend through the existing connection path.
Commands execute there. No laptop helper, vault CLI, connector credential or
SSH-agent forwarding is added.

The helper requires `MCP_HUB_INSTANCE=homelab`, explicit `MCP_HUB_URL`, an
independent `homelab.` client token in `MCP_HUB_TOKEN`, and an absolute
`MCP_HUB_SERVICES_FILE`. The service file must be an owned regular file with
no group/other permissions, contain at most 16 KiB and select each requested
route with this marker:

```json
{"environment":"homelab","services":["vaultwarden","vault-secret","vault-attachment"],"vault_reader":"restricted-v1"}
```

A declared `T3_RUNTIME_VARIANT` or existing private home environment marker
must also be homelab. Work, mixed, missing configuration and legacy tokens
refuse before HTTP. There is no legacy URL, key-file, path-reference or
connector fallback. Use HTTPS outside loopback. The explicit in-cluster
homelab Service hostname also permits HTTP, as required by the existing
isolated runtime contract. Proxy environment settings are ignored.

On the hub, each selected vault upstream requires `protocol: restricted-v1`
and its exact connector path. `/vault-secret` maps to `/secret` and
`/vault-attachment` to `/attachment`. These two routes share the connector's
broker bearer, separate from metadata's bearer on `/vaultwarden`. All three
must select the same explicit connector origin. Only the hub holds those
connector bearers. The helper holds its ordinary homelab client token, never
the account's login credentials or CLI session. No raw vault CLI is needed
in T3.

References contain a canonical collection UUID and exactly one canonical
item UUID or exact title. A folder or path never authorizes access. Duplicate
titles require an item UUID. The connector refreshes and checks approved
visible grants for each request. Failed refresh or missing/revoked grants
fail closed rather than use an older password. Actual effective permissions
and hidden assignments still require owner review and the live canary.
Already delivered values cannot be revoked.

After private configuration is supplied, these examples use synthetic IDs
and names only. A command gets only explicitly mapped values:

```sh
python3 2-k3s/13.t3code/one/files/vault-restricted.py run \
  --collection-id 00000000-0000-4000-8000-000000000003 \
  --item-id 00000000-0000-4000-8000-000000000005 \
  --password-env VAULT_PASSWORD --field 'api token=VAULT_API_TOKEN' \
  -- python3 /path/to/approved-consumer.py
```

Use `--username-env VAULT_USER` when needed. Targets must match
`VAULT_[A-Z][A-Z0-9_]{0,63}`. Duplicate targets, including collisions with an
attachment target, refuse. Field names are exact, with the final `=`
separating the source name from the target. Empty strings remain empty;
missing or null requested values refuse. Outer `VAULT_*`, `BW_*` and
`MCP_HUB_*` variables are removed before the child starts. No automatic
field-name normalization or broker-auth inheritance occurs.

A command-scoped attachment is a private file whose path, not content,
enters the requested environment variable:

```sh
python3 2-k3s/13.t3code/one/files/vault-restricted.py run \
  --collection-id 00000000-0000-4000-8000-000000000003 --title demo \
  --attachment demo.txt --attachment-env VAULT_INPUT_FILE \
  -- python3 /path/to/approved-file-consumer.py
```

Secret mappings and one attachment can be combined. The helper creates a
0700 directory under `/tmp` and a 0600 file, then removes them when the
command completes, times out or receives SIGINT/SIGTERM. Attachment IDs or
filenames must be exact and unambiguous. The helper accepts only bounded
ASCII basenames containing letters, digits, dot, underscore or hyphen,
starting with a letter or digit and not ending with a dot.

To retain a download, explicitly choose a new absolute destination in an
owned private directory. The helper does not create destination directories:

```sh
python3 2-k3s/13.t3code/one/files/vault-restricted.py download \
  --collection-id 00000000-0000-4000-8000-000000000003 --title demo \
  --attachment demo.txt --destination /private/downloads/demo.txt
```

Existing files, symlinks at any path component, traversal segments and
public destination directories refuse. Creation is exclusive, with mode
0600. Download output contains only status, destination and byte count.
The caller owns cleanup of retained downloads. File removal is not secure
erasure.

### Quiet execution and bounds

The helper runs argv directly, without an implicit shell. Put `--` before
the command. Child stdin is closed; stdout and stderr go to `/dev/null`.
The helper returns only a fixed completion status and the child's exit code.
Timeout returns 124; SIGINT/SIGTERM returns 130; refusal returns 1 with a
fixed message. There is no raw-output switch. Normal diagnostics are lost
as well as secrets. Validate a consumer with synthetic values before using
it here. Exact-string redaction cannot safely handle encoded, split or
partial output, so the helper deliberately does not try it.

Each request has a 130-second total deadline, a 12 MiB response cap, strict
status/JSON/schema validation and no redirects or compressed responses.
Attachments are capped at 8 MiB. Individual secret strings are capped at
16 KiB and mapped values total at most 64 KiB. `--timeout` can reduce the
120-second child lifetime, never increase it. The helper disables core dumps
and sets a CPU limit tied to the timeout. It leaves address-space, file-size
and descriptor budgets inherited. Node and Go reserve large virtual address
ranges; RLIMIT_AS is not a resident-memory budget. The runtime's cgroup memory
limit remains the aggregate budget. Process-group descendants
are killed on timeout/cancellation and after the direct child exits,
including when the child closes its output descriptors early.

These are per-command safeguards, not a sandbox or aggregate cgroup quota.
A deliberate consumer can write secret files, send data elsewhere or escape
its process group. A caller that edits or bypasses its runtime can also
bypass this policy. SIGKILL, host failure and escaped descendants are not
covered by graceful cleanup. The reader package has its own bounded memory
scratch and startup orphan cleanup; this does not erase files a deliberate
consumer retains elsewhere. Its pinned Node 22/native checks and the three
runtime smokes run in CI. Private provisioning and live acceptance remain
blocked. The removed
pi-permission-system stays removed; cc-safety-net does not replace its old
environment-dump or infrastructure deny rules. Keep the remaining guards
and organizational policy checks.

## Approval-dependent provisioning and rollout

1. A1: Approve each private inventory, backend authentication boundary,
   account identity, ordinary client list and routing configuration. Keep
   actual endpoints, account identifiers and vault data out of this repo's
   public examples and evidence.
2. A2: Provision independent Secrets through approved private configuration.
   Mount only the two required keys. Do not reuse the legacy `add-client.py`
   workflow, which deliberately writes legacy grants and consumers.
3. A3: For a private Kustomize overlay, generate `mcp-hub-instance` with a
   content-hashed name. Kustomize rewrites the mounted Secret reference,
   causing an independent rollout on configuration or credential changes.
   CI exercises this composition with synthetic Secret data. If an external
   provisioner uses a stable Secret name instead, it must update a revision
   annotation on that Deployment's pod template as part of the approved
   rollout. Mounted files alone do not reload this process's in-memory
   configuration. Do not assume Reloader watches these namespaces.
4. A4: Approve the ArgoCD source for the private composition and manual sync.
   Verify direct backend rejection without credentials, wrong-hub and
   legacy-client rejection, one accepted end-to-end request per hub, and
   continued legacy availability before any client cutover.

Infrastructure-administrator and cross-cluster isolation remain deferred.
These deployments separate application configuration and credentials; they
do not claim protection from an infrastructure administrator.

## Offline verification

Run from the repository root. These commands use fixtures only, do not
contact infrastructure, and do not decrypt existing Secrets:

```sh
python3 -m venv /tmp/hub-fixture-venv
/tmp/hub-fixture-venv/bin/pip install mcp==2.2.0 -r .github/hooks/requirements.txt
/tmp/hub-fixture-venv/bin/python -m unittest discover -s 2-k3s/23.mcp-hub/tests -v
/tmp/hub-fixture-venv/bin/python 2-k3s/23.mcp-hub/files/mcp_hub.py --selftest
python3 2-k3s/23.mcp-hub/tools/hub_clients.py --selftest
bash 2-k3s/13.t3code/one/tools/sync-shared.sh --check
```

CI runs these fixture and render checks. The requests use the real gateway,
forwarder, SDK HTTP servers and backend authentication middleware. The
legacy vault fixture uses its real HTTP app with only the vault data lookup
replaced by synthetic data. Render fixtures cover both new overlays,
content-hashed secret rollouts, independent Notion RBAC, legacy ConfigMap
merging and manual-sync ArgoCD Applications. No fixture prints credentials.
