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
connection or extra connection prevents startup. `vaultwarden` and
`vault-secret` are rejected even if explicitly listed. Vault access waits
for the restricted connector tickets.

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
never advertises the command-time secret route. That route remains tested
and available only on the legacy hub while vault is disabled on both new
hubs. `vault-run.py` also refuses instance settings before reading a legacy
key file or making a request. This is not a replacement for organizational
agent policies.

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
/tmp/hub-fixture-venv/bin/python 2-k3s/23.mcp-hub/tests/test_instances.py -v
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
