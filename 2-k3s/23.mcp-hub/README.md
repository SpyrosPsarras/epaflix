# mcp-hub

The one MCP endpoint every agent client in the homelab talks to. Clients know
one URL and their own hub token; everything behind a path is the hub's
business.

## Vocabulary

- **Hub**: this gateway, `https://mcp.epaflix.com` on the LAN and
  `http://mcp-hub.mcp-hub.svc.cluster.local:8000` in-cluster.
- **Client**: one machine or pod whose agents use the hub: each LAN PC
  (laptop, homepc, ...) and the t3code pod.
- **Hub token**: one client's bearer secret. Every client has its own; any
  hub token opens every path.
- **Path**: one MCP server as clients see it (`/gmail`, `/vaultwarden`, ...).
- **Module**: a path served in-process from our Python code.
- **Upstream**: a path the hub forwards to an MCP server it does not run
  in-process; the hub adds an **upstream credential** clients never see.
- **Notion grant**: the hub's OAuth authorization with hosted Notion MCP.

| Path          | Kind     | Serves                                   | Client name          |
|---------------|----------|------------------------------------------|----------------------|
| `/gmail`      | module   | `files/gmail_mcp.py`, Gmail API          | `gmail`              |
| `/searxng`    | module   | `files/searxng_mcp.py`, in-cluster SearXNG | `searxng`          |
| `/jev`        | upstream | `jev-mcp.yaml`, published [`@jkudish/jev-mcp`](https://github.com/jkudish/jev-mcp) (11 Jev judgment tools) via OpenRouter, Secret `mcp-hub-jev`. OpenCode clients only | `jev` |
| `/vaultwarden` | upstream | `vaultwarden-mcp.yaml`, Spyros's Vaultwarden vault via `bw serve` | `vaultwarden` |
| `/vault-secret` | upstream | the same server's `/secret`, plain JSON for `vault-run.py` (see Vaultwarden) | none |
| `/kubernetes` | upstream | `kubernetes-mcp.yaml`, cluster-admin on this cluster | `kubernetes-epaflix` |
| `/notion`     | upstream | hosted `https://mcp.notion.com/mcp`      | `notion`             |
| `/drive`      | upstream | `workspace-mcp.yaml`, community [`google_workspace_mcp`](https://github.com/taylorwilsdon/google_workspace_mcp) (Drive tools only), same Google account and Secret as `/gmail` | `drive` |

`GET /healthz` is the only unauthenticated route. Traefik routes
`mcp.epaflix.com` on the `internal` entry point only (LAN LB, split DNS in
`1-proxmox/pihole`); from the internet the name answers 404. Away from home a
PC needs the VPN plus the LAN resolver.

Upstream failures (unreachable, credential rejected, Notion grant expired)
come back as a JSON-RPC error carrying the reason, never as the upstream's
own 401, so clients do not start an OAuth flow against the hub.

## Clients

`files/clients.json` maps client name to the SHA-256 of its hub token (safe
in git: the tokens are 256-bit random). `tools/add-client.py` writes it;
merging rolls the hub. Revoke a client by deleting its line and merging.

**A LAN PC** (run on that PC, from a checkout, then commit and merge):

```sh
2-k3s/23.mcp-hub/tools/add-client.py homepc          # new token
2-k3s/23.mcp-hub/tools/add-client.py laptop --reuse  # keep the token in the key file
```

The token goes to `~/.config/opencode/mcp-hub.key` (mode 600). Every path is
registered in `~/.config/opencode/opencode.json` (header
`Bearer {file:~/.config/opencode/mcp-hub.key}`) and in Claude Code's user
config (a `headersHelper` that reads the key file), so neither config holds
the token. The PC keeps its own stdio `kubernetes` server for its other
kubeconfig contexts; the hub's is `kubernetes-epaflix`.

**The t3code pod**: `add-client.py t3code` writes a new token into
`13.t3code/one/mcp-hub-client.enc.yaml`; merge, and ArgoCD syncs `t3code`.
The pod gets `MCP_HUB_URL`/`MCP_HUB_TOKEN`, and
`13.t3code/one/files/entrypoint.sh` registers the shared paths for Pi,
Claude and Codex in `$HOME`, with the token as an
env reference.

Both use `tools/hub_clients.py` (the pod has a byte copy,
`13.t3code/one/tools/sync-shared.sh` keeps it in sync; CI checks). It also
sets these OpenCode tools to `ask` (OpenCode names tools `<server>_<tool>`):
`gmail_send`, `gmail_send_draft`, `gmail_trash`; every Drive tool that
writes (create, copy, import, `update_drive_file` which also edits and
trashes, and the sharing tools); the vault writes
`vault_add`, `vault_update`, `vault_trash`, `vault_attach`; and the mutating
Kubernetes tools (`pods_delete`, `pods_exec`, `pods_run`,
`resources_create_or_update`, `resources_delete`, `resources_scale`,
`helm_install`, `helm_uninstall`). Claude and Codex prompt for MCP tools by
default.

Claude Code expands `${MCP_HUB_TOKEN}` in the `headers` of user-scope servers
at connect time (verified on 2.1.281; the stored config keeps the literal).

### Rollout of the gateway change (2026-09)

mcp-hub and syncthing synced on merge; t3code was then synced by hand.
Between the merge and that sync the running pod kept its old MCP setup (stdio
searxng, hosted Notion; it had never had hub paths, since t3code was last
synced before the hub existed). **Its keepass tools failed**: the merge removed the
`keepass-exec` RBAC and the pod's `kubectl exec` bridge with it. t3code now
syncs on merge too. Then, on each PC:
`add-client.py <pc> --reuse` (switches OpenCode's notion/searxng to the hub,
adds keepass and kubernetes-epaflix, wires Claude Code), then
`tools/bootstrap-notion.py` once, and remove `~/.config/opencode/mcp/searxng-mcp.py`.
Done on homepc (registered first as `laptop`, renamed); the laptop itself is
not a client yet.

## Secrets

| Secret | Keys | Written by |
|---|---|---|
| `t3code/mcp-hub-client` | `token` | `tools/add-client.py t3code` |
| `mcp-hub/mcp-hub-gmail` | `client-id`, `client-secret`, `refresh-token`, `email` (hub `/gmail`, `workspace-mcp`) | `tools/bootstrap-gmail.py` |
| `mcp-hub/mcp-hub-vaultwarden` | `client-id`, `client-secret`, `password` (bw container only), `hub-secret` (hub and server) | `tools/sops_secret.py --vaultwarden` |
| `mcp-hub/mcp-hub-notion-grant` (not in git) | see `files/notion_grant.py` | `tools/bootstrap-notion.py`, then the hub |

The sops writers need only `sops` and the age recipient in `.sops.yaml`.
They stamp a plaintext `mcp-hub.epaflix.com/revision` annotation that the
kustomizations copy into the pod templates, so a new secret rolls the pod
on merge (no Reloader watches these namespaces).

## Vaultwarden

`vaultwarden-mcp.yaml` serves 7 `vault_*` tools over Spyros's own Vaultwarden account. Its `bw` container logs in with his
personal API key, unlocks with his master password and runs `bw serve` on
127.0.0.1; `files/vaultwarden_mcp.py` turns that REST API into MCP. Paths are
`/<folder>/<item name>`. Reads run `bw sync` at most once a minute, so edits
in the Bitwarden apps show up within a minute. Only hub pods may connect, and
the server checks the hub's `X-Hub-Secret`, so the vault is readable and writable by any client with a hub token, from the LAN.
`bw` stays at 2026.8.0 until Vaultwarden is upgraded (`.github/renovate.json`).
Write or rotate the login and hub secret with `tools/sops_secret.py
--vaultwarden` (prompts; values never echo), commit, merge.

`vault_get` never returns a password or a custom field value (it lists field
names), because a tool result lands in the model's context and in session
files. They leave only through
`POST /vault-secret {"path": ...}`, a plain JSON route (not MCP) that the hub
forwards to the server's `/secret`. `13.t3code/one/files/vault-run.py` is its
only caller: it runs a command with the password in `$VAULT_PASSWORD` and each
custom field in `$VAULT_FIELD_<NAME>`. This
stops accidental leaks, not a determined agent: any hub token opens the route.

Vaultwarden replaced the KeePass vault and its `/keepass` path on 2026-10-04
(`docs/superpowers/specs/2026-10-03-keepass-sunset-design.md`).
`hub_clients.py` drops `keepass` entries from existing client configs.

## Kubernetes

`kubernetes-mcp.yaml` runs containers/kubernetes-mcp-server with a
cluster-admin ServiceAccount (toolsets core, config, helm). It has no
static-token auth, so the NetworkPolicy (hub pods only on the MCP port) is
the gate; the hub token is what stands between the LAN and cluster-admin.

## Notion

Hosted Notion MCP, forwarded with a grant the hub holds, because its tools
(semantic search, meeting notes, views, ...) are the ones in daily use and
Notion maintains them. The open-source server is unmaintained, and an own
REST module would have far fewer tools. Reasons and consequences:
`docs/adr/0001-hosted-notion-behind-the-hub.md`.

Hosted Notion is OAuth-only. The refresh token rotates on every refresh and
replaying a rotated one revokes the grant, so the hub is the only refresher
and writes each new pair back to its Secret (`Role mcp-hub-notion-grant`:
get/patch on that one Secret). **The grant lapses 180 days after consent, or
after 30 days unused**: Notion tools then answer "Notion grant expired".
Renew, on a PC with a browser and cluster kubectl:

```sh
2-k3s/23.mcp-hub/tools/bootstrap-notion.py   # prints the next hard expiry
```

It logs in as your personal Notion account (every client acts with its
permissions) and writes the Secret with kubectl. No commit, no restart.

## Drive

`/drive` gives agents read-write Google Drive on the same account as `/gmail`:
search, read, create, edit, share and trash files (17 tools minus
`start_google_auth`, which is disabled). Google's own hosted Drive MCP server
(`drivemcp.googleapis.com`) is a Developer Preview that needs a Google
Workspace account, which a personal account cannot get, so the hub runs the
MIT-licensed community server in single-user mode instead.

`workspace-mcp.yaml` writes the credential file at start from Secret
`mcp-hub-gmail`, so Gmail and Drive share one refresh token. The OAuth scope is
full `drive`: sharing and trashing need it, and no Drive scope is narrower and
still lets an agent edit files it did not create. The server has no auth of its
own here, so the NetworkPolicy (hub pods only) is the gate, as for `/kubernetes`.
Mind prompt injection: a document an agent reads can carry instructions, and
the tools above can then share or trash files.

Rolling it out, on a PC with a browser, sops and cluster kubectl:

1. In the Google Cloud project that already holds the Gmail client, enable the
   **Google Drive API** (APIs & Services > Library).
2. `python3 2-k3s/23.mcp-hub/tools/bootstrap-gmail.py --reuse`: it reads the
   existing OAuth client from Secret `mcp-hub-gmail` with kubectl, so no new
   client or download is needed. On the consent page, tick both Gmail and Drive.
3. Commit `mcp-hub-gmail.enc.yaml` to the branch and merge.
4. `2-k3s/23.mcp-hub/tools/add-client.py <pc> --reuse` on each PC; the t3code
   pod picks `drive` up on its next start.

Merging before step 2 leaves `workspace-mcp` in `CreateContainerConfigError`
(the old Secret has no `email` key and no Drive scope); the hub and `/gmail`
keep working.

## Gmail bootstrap (once, on a machine with a browser)

Google Cloud console:

1. Create a project (any name), APIs & Services > Library > enable **Gmail API**
   and **Google Drive API**.
2. APIs & Services > OAuth consent screen: user type **External**, add your
   address as a test user, then **Publish app** (status "In production").
   In Testing status Google expires refresh tokens after 7 days.
   `gmail.modify` and `drive` are restricted scopes: unverified, the app still works for
   your own account behind a "Google hasn't verified this app" page you click
   through (Advanced > continue).
3. Credentials > Create credentials > OAuth client ID > **Desktop app**.
   Download the JSON.

Then `python3 2-k3s/23.mcp-hub/tools/bootstrap-gmail.py --client-json
~/Downloads/client_secret_*.json`, commit `mcp-hub-gmail.enc.yaml`, merge,
and delete the downloaded JSON. Revoke at myaccount.google.com/permissions
and re-run to rotate.

## Verify

```sh
curl -sS https://mcp.epaflix.com/healthz                                          # ok
curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://mcp.epaflix.com/gmail   # 401
kubectl --context epaflix -n mcp-hub logs deploy/mcp-hub | head                   # selftest OK, uvicorn
OPENCODE_CONFIG=~/.config/opencode/opencode.json opencode mcp list
```

## Adding a server

A module: write `files/<name>_mcp.py` exposing `server() -> MCPServer` (see
`gmail_mcp.py`; raise `ToolError` for expected failures, the SDK hides any
other exception text from the model), add it to `MODULES` in `mcp_hub.py`
and to the `configMapGenerator`, add its env to `deployment.yaml` (plus a
revision replacement if it gets a Secret). An upstream: add an `Upstream` to
`upstreams()` with its credential. Then add the name to `SERVERS` (and any
approval-gated tools to `ASK`) in `tools/hub_clients.py`, run
`13.t3code/one/tools/sync-shared.sh`, extend the selftest, and re-run
`add-client.py <pc> --reuse` on each PC.
