# Homelab and work hubs

`homelab/` and `work/` run the same maintained gateway from `../files/`.
They have separate namespaces, Services, Deployments, health probes,
ServiceAccounts, client grants and upstream credentials. The legacy hub
remains available with its existing clients and routes. No consumer is
switched by this change.

The active approved homelab inventory is `searxng`, `gmail` and `drive`.
The owner stages Gmail and Drive with fresh consent through
`bootstrap-gmail.py --instance homelab --context <approved-context>`.
The work inventory is `searxng` and `notion`. The owner stages Notion with
fresh work-account consent through `bootstrap-notion.py --instance work --context <approved-context>`;
the work hub and runtime
counters roll it out.
Both include the shared authenticated backend in `searxng/`, with an independent upstream
Secret in each namespace. Provisioning, the dedicated work client and owner enrollment
are documented in [the work backend](../../13.t3code-work/README.md#first-deployment-for-1837).
Other work services remain owner-added later. No work data source is configured.

Both hub Applications automatically sync merged revisions with pruning and
self-healing. Their Deployments can roll immediately after merge; unlike
the runtimes, the hubs have no manual stop procedure. Stage approved private
hub inventories, runtime allowlists and matching inputs before merging their
own revision bumps. The Google backend can merge before its Secret staging;
until the Secrets exist it stays not-ready with `CreateContainerConfigError`,
while the hub and searxng remain unaffected. Their public
overlays contain no Secret, approved service inventory or account identity.
Each has an internal-only Traefik route with a generic homelab/work hostname.
Without private configuration their pods cannot start. Rendering an overlay
is not approval to deploy it.

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
connection or extra connection prevents startup. Both instances reject
`vaultwarden`, `vault-secret`, `vault-attachment` and the retired reader
marker. Both T3 pods read secrets through their own stock `vault` client
and UID-1001 sidecar, not through a hub route. The legacy mixed hub retains
its own `vaultwarden-mcp` and `vault-run.py` until #1848.

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

## Homelab Gmail and Drive

The active private inventory is
`{"environment":"homelab","services":["searxng","gmail","drive"]}`
in both hub and runtime, staged by the owner with fresh consent through
`bootstrap-gmail.py --instance homelab --context <approved-context>`.
Work stays `{"environment":"work","services":["searxng","notion"]}`.
Preserve the existing searxng connection, all ordinary client digests and
the homelab/work boundary.

Only homelab includes `../google`. Both added connections use
`http://workspace-mcp.mcp-hub-homelab.svc.cluster.local:8000/mcp` and the same
fresh, independently minted Workspace bearer. `/gmail` and `/drive` are
aliases for one combined upstream, not per-service authorization boundaries.
Agents register exactly `searxng`, `gmail` and `drive`, with token environment
references. There is no separate send-draft registration. The legacy
`files/gmail_mcp.py` endpoint and Drive-only `workspace-mcp.yaml` remain
unchanged; reusing the send-draft helper does not reuse their grant.

The exact served union is 31 tools, 15 Gmail and 16 Drive, including our
`gmail_send_draft` extension. Both aliases return this same list. The
pinned image's complete tier supplies the other 30 names:

```text
search_gmail_messages, get_gmail_message_content, get_gmail_messages_content_batch,
send_gmail_message, get_gmail_attachment_content, get_gmail_thread_content,
modify_gmail_message_labels, list_gmail_labels, manage_gmail_label, draft_gmail_message,
list_gmail_filters, manage_gmail_filter, get_gmail_threads_content_batch,
batch_modify_gmail_message_labels, gmail_send_draft,
search_drive_files, get_drive_file_content, get_drive_file_download_url,
create_drive_file, create_drive_folder, import_to_google_doc, import_to_google_slides,
import_to_google_sheets, get_drive_shareable_link, list_drive_items, copy_drive_file,
update_drive_file, manage_drive_access, set_drive_file_permissions,
get_drive_file_permissions, check_drive_file_public_access
```

Consent and seed require exactly this complete scope set, including entries
implied by broader scopes:

```text
https://www.googleapis.com/auth/drive
https://www.googleapis.com/auth/drive.file
https://www.googleapis.com/auth/drive.readonly
https://www.googleapis.com/auth/gmail.compose
https://www.googleapis.com/auth/gmail.labels
https://www.googleapis.com/auth/gmail.modify
https://www.googleapis.com/auth/gmail.readonly
https://www.googleapis.com/auth/gmail.send
https://www.googleapis.com/auth/gmail.settings.basic
https://www.googleapis.com/auth/userinfo.email
https://www.googleapis.com/auth/userinfo.profile
openid
```

`start_google_auth` stays disabled. CI's required pinned-image check verifies
the exact union and the startup extension. An unknown tool or incompatible
image blocks rollout rather than expanding the approved inventory.

The raw Workspace server listens only on `127.0.0.1:8001`. Only the
authenticated gateway listens on port 8000, and the `workspace-mcp` Service
exposes only that gateway. Every MCP request needs the independent bearer
from `mcp-hub-google-upstream/token`; ordinary client bearers are rejected.
NetworkPolicy alone is not the boundary. The hub replaces the client's
bearer with the Workspace bearer; the gateway strips it before forwarding
to loopback. No Google grant or upstream bearer belongs in client config.

Only the raw container consumes `mcp-hub-gmail`. It seeds a writable,
memory-backed temporary credential file under `/creds`. After seeding, the
Workspace process is the only credentials-file writer. Its send-draft
helper refreshes access tokens in memory only, never writes the shared file
and never retries a send. A restart seeds again from the private grant;
startup never enrolls an account. A revoked grant requires fresh owner
consent, not a restart loop. Notion refresh ownership stays unchanged.
Workspace and FastMCP log at `CRITICAL` because upstream INFO, WARNING and
ERROR messages can include the account email. File logs use
`/tmp/.google_workspace_mcp/logs`; CI checks them and container output for
synthetic identity and credential leaks after failed HTTP tool calls.

Writes run directly by owner decision, including send-draft, trash through
label modification with `TRASH`, and sharing. There is no hub-side gate,
no gate in the homelab pod's clients, and desktop OpenCode keeps the legacy
ask table until #1946. The legacy `ASK` table stays unchanged and is not
extended. The homelab pod generates Pi, Claude and Codex config, not OpenCode.

### Owner enrollment and stopped rollout

1. G1: Stop homelab consumers through the approved input-rotation procedure.
   The owner obtains a fresh OAuth client and runs
   `tools/bootstrap-gmail.py --instance homelab --context <approved-context> --client-json <private-client-file>`.
   The owner signs in through the browser, verifies the account before
   consent and approves the exact scope set above. Copy no legacy token or
   refresh token. Keep identities, grant material and evidence outside this
   public repo. No public Secret manifest is created.
2. G2: Stage the grant in `mcp-hub-homelab/mcp-hub-gmail` through the bootstrap
   tool. Separately mint the upstream bearer inside the private provisioning
   process and stage it in `mcp-hub-google-upstream/token` and both Google
   connections in `mcp-hub-instance/credentials.json`. Preserve clients and
   searxng. Update both private service inventories. Recompute only
   `runtime.json.sha256["services.json"]` over its exact stored bytes; preserve
   other inputs, approvals and grant digests. Use resource-version checks and
   suppress Secret command output. Do not enroll during ordinary staging.
3. G3: The owner selects reviewed rollout revisions after staging. Hub inventory
   or connection changes need `mcp-hub.epaflix.com/inputs-revision` on the hub;
   Google grant or gateway-bearer rotations separately need that annotation
   on the homelab `workspace-mcp` Deployment. A bearer rotation changes both
   backend and hub inputs, so roll both. Use homelab-only patches, never the
   shared base or work. Runtime input changes separately need
   `t3code.epaflix.com/inputs-grants-revision` through the
   [runtime input rotation procedure](../../13.t3code/env/isolated-runtime.md#input-rotation).
   The backend may merge before its Secrets are staged. Until they exist,
   it stays not-ready with `CreateContainerConfigError`, without affecting
   the hub or searxng. Stage the private inventory, runtime
   allowlist and matching inputs before their own revision commit merges
   and enables `gmail`/`drive`. Do not resume stopped consumers before
   verification.
4. G4: After owner approval and rollout, verify the exact tool union, both
   aliases, synthetic reads, direct-backend rejection of ordinary credentials,
   wrong-environment and legacy-token rejection, unchanged work services and
   legacy availability. Regenerate client configs and remove stale managed
   entries. Resume homelab consumers only after these checks pass. Live
   send-draft, trash and sharing checks remain owner-run.

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

For homePC and laptop access to both hubs, follow
[local client provisioning and owner steps](local-clients.md). The local
wrapper uses prefixed Pi server names without changing isolated T3 clients.

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
reading a legacy key file or making a request. Neither hub configuration nor
its legacy runner replaces organizational agent policies.

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
   provisioner uses a stable Secret name instead, it must update a tracked
   pod-template revision as part of the approved rollout. Both overlays
   provide `mcp-hub.epaflix.com/inputs-revision` in their own
   `kustomization.yaml` for the stable `mcp-hub-instance` Secret. Work also
   uses that annotation on its search Deployment when its upstream credential
   changes. Use an environment-only patch for search bearer rotations.
   Stage approved Secret updates and arrange downtime before merging tracked
   revisions for automatic sync.
   Homelab input changes follow the coordinated procedure below.
   Mounted files alone do not reload this process's in-memory configuration.
   Do not assume Reloader watches these namespaces.
4. A4: Approve the ArgoCD source for the private composition before merge.
   Wait for automatic sync and rollout, then verify direct backend rejection
   without credentials, wrong-hub and legacy-client rejection, one accepted
   end-to-end request per hub, and
   continued legacy availability before any client cutover.

### Coordinated homelab input changes

Stage the approved private inputs a revision covers before changing it.
Use a new opaque operator revision for each approved change, never a
password/token hash, private identifier or secret-derived value. A revision
is a rollout marker, not approval or evidence of valid grants. It changes
only the homelab hub pod template, not the namespace, stable Secret name or
auth rules.

1. R1: Obtain approval for the exact private change, downtime and merge.
   Stop old homelab consumers through the approved procedure before changing
   authority. The hub keeps running and automatically rolls on merged changes.
   Do not force a running runtime restart.
   Workload startup must not create accounts or log in. Secret staging/rotation
   must not implicitly enroll accounts or log in. Owner API enrollment requires
   separate explicit authorization and is outside Secret staging/rotation.
2. R2: Stage only approved `mcp-hub-instance` changes in
   `mcp-hub-homelab` through the private secret workflow. The hub retains
   its old in-memory configuration until rollout. Treat this interval as downtime.
3. R3: For hub `services.json` or `credentials.json` changes, update
   `mcp-hub.epaflix.com/inputs-revision` in `homelab/kustomization.yaml`.
   Review the rendered pod template and stable Secret reference before merge.
4. R4: Merge the approved revision after private inputs are staged. Wait for
   `mcp-hub-homelab` to sync automatically and finish its single-replica
   `Recreate` rollout. Verify authorized requests and auth rejection before resuming
   consumers. Keep work and legacy Applications and Secrets unchanged.

Runtime allowlist or client-token changes require a separate reviewed input
revision through the [runtime input rotation procedure](../../13.t3code/env/isolated-runtime.md#input-rotation).
Regenerate client metadata and private service snapshots. Automatic hub sync
does not authorize restarting a runtime.

## Restricted reader retirement after merge

The approved replacement homelab inventory is `searxng` only. Both hubs use
`../searxng/`, with separate namespace-local upstream bearer Secrets. The
live homelab inventory checked for #1845 still contained only `vaultwarden`,
`vault-secret` and `vault-attachment`. Stage compatible private inputs before
merging new hub code, because automatic sync can roll it immediately. The
following steps require approved maintenance; none were performed by this
implementation.

1. P1: Pause homelab consumers first. Close homePC/laptop homelab sessions and
   stop new requests. Stop the homelab T3 backend through its approved maintenance
   procedure before replacing its reviewed inputs. Keep it stopped until P4.
   Do not restart pods, scale workloads or sync Applications without approval.
   Keep the reader available until all consumers have moved. Do not change work
   or the mixed hub, and do not rerun account enrollment or first provisioning.
2. P2: In an approved in-cluster administrative process, mint a fresh independent
   `search_token = secrets.token_urlsafe(48)`. Create the Opaque Secret
   `mcp-hub-homelab/mcp-hub-searxng` with exactly the key `token`.
   Keep the value in memory, never print it, pass it on argv, log it, put it in
   a public manifest or reuse the work bearer. If the Secret already exists,
   verify the approved private source rather than silently rotating it.
   In the same private process, read `mcp-hub-homelab/mcp-hub-instance`
   without displaying its data. Require all three existing client digests,
   preserve them byte-for-byte, then construct the replacements below:

   ```python
   services = {"environment": "homelab", "services": ["searxng"]}
   credentials["clients"] = {
       name: credentials["clients"][name]
       for name in ("homepc", "laptop", "t3code-homelab")
   }
   credentials["upstreams"] = {"searxng": {
       "url": "http://searxng-mcp.mcp-hub-homelab.svc.cluster.local:8000/mcp",
       "token": search_token,
   }}
   ```

   Serialize `services` into `data["services.json"]` and `credentials`
   into `data["credentials.json"]` using Kubernetes base64 encoding. Replace
   the Secret with its current `resourceVersion` so a concurrent update fails
   instead of being overwritten. Keep only those two data keys. This removes
   `vault_reader`, all vault upstreams and every canary client digest while
   retaining the three ordinary clients. Store the same approved changes in
   the private provisioning source. Suppress Secret output and API error bodies.
3. P3: Once P2 is staged, change the homelab hub's
   `mcp-hub.epaflix.com/inputs-revision` from `r2` to a new opaque revision
   such as `r3` in `homelab/kustomization.yaml`. Review and merge that
   revision only after P2 is staged. Automatic sync creates the authenticated
   search backend and rolls the hub with the new allowlist.
   Wait for both Deployments to become ready. Verify direct `/mcp` requests
   without a bearer or with an ordinary hub token return 401; verify a request
   through the hub's `/searxng` succeeds using an existing homelab client.
   Require authorized requests to all three removed hub routes to return 404.
   No query or credential belongs in public evidence. No hub NetworkPolicy was
   present in the tracked overlay or live namespace when this change was checked.
4. P4: While the homelab T3 backend is still stopped, replace only
   `t3code-homelab/t3-runtime-inputs`'s `services.json` with the exact
   `services` object above. Recompute
   `runtime.json.sha256["services.json"]` over the exact serialized bytes
   stored in that key, and record the approved #1845 hub change in
   `runtime.json.approvals.hub`. Preserve every other input, digest, approval
   and `grantSha256` value. Preserve `t3-runtime-grants` and both
   `vaultwarden-agent` Secrets unchanged. Update the private source and replace
   the runtime-input Secret with its current `resourceVersion`, without output.
   Bump `t3code.epaflix.com/inputs-grants-revision` from `r3` to a new
   opaque revision such as `r4` in
   `2-k3s/13.t3code-homelab/statefulset.yaml`. After the reviewed revision and
   runtime image are published, resume parent and runtime automation through
   the shared [input rotation procedure](../../13.t3code/env/isolated-runtime.md#input-rotation).
   Startup regenerates its private service snapshot and Pi/Claude/Codex registrations.
   Verify they advertise `searxng` and none of the retired vault routes.
   Update the paused desktop clients' private homelab allowlists and regenerate
   their registrations before resuming their sessions. Keep ordinary tokens.
5. P5: Verify both T3 pods consume synthetic secrets through `vault run` in
   their own organization and collection, including cross-environment rejection.
   Permanently delete every synthetic probe in a `finally` cleanup and verify
   absence from active items and trash. Do not read real items or restart work.
6. P6: Only after consumers are migrated and P3-P5 pass, explicitly delete the
   manual Argo CD Application `vault-reader-homelab` and namespace
   `vault-reader-homelab`, including `vault-reader-inputs` and remaining
   reader/canary resources. Remove the obsolete `vault-canary-client` Secret
   from `mcp-hub-homelab` and reader bearers from private provisioning sources.
   Preserve `mcp-hub-instance`, `mcp-hub-searxng`, `t3-runtime-grants`
   and both `vaultwarden-agent` Secrets. Manually prune the obsolete namespace's
   read RoleBinding through `t3code-homelab-access` if it still exists.
   Obtain separate owner approval to delete the reader's Vaultwarden account and
   revoke its access. Do not delete stock-client accounts, organizations or
   collections. Manifest removal alone does not authorize live deletion.

The tracked hub and runtime revisions remain unchanged here because the new
private inputs have not been staged. Future homelab bearer rotations must also
bump the search Deployment's input annotation through a homelab-only patch,
not by changing the shared base and rolling work. Moving the work backend to
the shared base preserves its immutable selector but changes its runner
ConfigMap reference. Both hubs sync automatically with pruning and self-healing.
Both runtimes sync automatically without self-healing. For this stopped cutover,
pause parent and homelab runtime automation through the shared input rotation
procedure; the hubs' own automation remains active. Leave the work hub and
runtime manifests unchanged.

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
merging and automated ArgoCD sync policies. Hub tests require pruning and
self-healing; runtime tests require pruning without self-healing. No fixture
prints credentials.
