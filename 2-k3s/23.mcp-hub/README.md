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
- **Path**: one MCP server as clients see it (`/gmail`, `/keepass`, ...).
- **Module**: a path served in-process from our Python code.
- **Upstream**: a path the hub forwards to an MCP server it does not run
  in-process; the hub adds an **upstream credential** clients never see.
- **Notion grant**: the hub's OAuth authorization with hosted Notion MCP.

| Path          | Kind     | Serves                                   | Client name          |
|---------------|----------|------------------------------------------|----------------------|
| `/gmail`      | module   | `files/gmail_mcp.py`, Gmail API          | `gmail`              |
| `/searxng`    | module   | `files/searxng_mcp.py`, in-cluster SearXNG | `searxng`          |
| `/keepass`    | upstream | `15.syncthing/keepass.yaml`, the Syncthing vault | `keepass`    |
| `/kubernetes` | upstream | `kubernetes-mcp.yaml`, cluster-admin on this cluster | `kubernetes-epaflix` |
| `/notion`     | upstream | hosted `https://mcp.notion.com/mcp`      | `notion`             |

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
`13.t3code/one/mcp-hub-client.enc.yaml`; merge, then sync `t3code` in ArgoCD
(manual). The pod gets `MCP_HUB_URL`/`MCP_HUB_TOKEN`, and
`13.t3code/one/files/entrypoint.sh` registers every path for OpenCode,
Claude and Codex in `$HOME` and every `/home/t3env-*`, with the token as an
env reference.

Both use `tools/hub_clients.py` (the pod has a byte copy,
`13.t3code/one/tools/sync-shared.sh` keeps it in sync; CI checks). It also
sets these OpenCode tools to `ask` (OpenCode names tools `<server>_<tool>`):
`gmail_send`, `gmail_send_draft`, `gmail_trash`; the vault writes
`vault_add`, `vault_update`, `vault_trash`, `vault_attach`; and the mutating
Kubernetes tools (`pods_delete`, `pods_exec`, `pods_run`,
`resources_create_or_update`, `resources_delete`, `resources_scale`,
`helm_install`, `helm_uninstall`). Claude and Codex prompt for MCP tools by
default.

Claude Code expands `${MCP_HUB_TOKEN}` in the `headers` of user-scope servers
at connect time (verified on 2.1.281; the stored config keeps the literal).

### Rollout of the gateway change (2026-09)

mcp-hub and syncthing sync on merge; t3code does not. Between the merge and
the manual t3code sync the running pod keeps its old MCP setup (stdio
searxng, hosted Notion; it has never had hub paths, since t3code was last
synced before the hub existed). **Its keepass tools fail**: the merge removes the
`keepass-exec` RBAC and the pod's `kubectl exec` bridge with it. Sync t3code
soon after the merge. Then, on each PC:
`add-client.py <pc> --reuse` (switches OpenCode's notion/searxng to the hub,
adds keepass and kubernetes-epaflix, wires Claude Code), then
`tools/bootstrap-notion.py` once, and remove `~/.config/opencode/mcp/searxng-mcp.py`.
Done on homepc (registered first as `laptop`, renamed); the laptop itself is
not a client yet.

## Secrets

| Secret | Keys | Written by |
|---|---|---|
| `t3code/mcp-hub-client` | `token` | `tools/add-client.py t3code` |
| `mcp-hub/mcp-hub-gmail` | `client-id`, `client-secret`, `refresh-token` | `tools/bootstrap-gmail.py` |
| `mcp-hub/mcp-hub-keepass` + `syncthing/keepass-hub-secret` | `secret` (same value) | `tools/sops_secret.py --keepass` |
| `mcp-hub/mcp-hub-notion-grant` (not in git) | see `files/notion_grant.py` | `tools/bootstrap-notion.py`, then the hub |

The sops writers need only `sops` and the age recipient in `.sops.yaml`.
They stamp a plaintext `mcp-hub.epaflix.com/revision` annotation that the
kustomizations copy into the pod templates, so a new secret rolls the pod
once synced (no Reloader watches these namespaces). mcp-hub and syncthing sync
automatically on merge, t3code only by hand.

## Keepass

The vault stays where Syncthing keeps it (local PV on `k3s-worker-63`); the
keepass pod serves it over streamable HTTP next to it. Only hub pods may
connect (NetworkPolicy), and the pod checks the hub's `X-Hub-Secret`. So the
vault is readable and writable by any client with a hub token, from the LAN.
Writes are serialized in the pod; Syncthing carries them to the other devices.
Editing the same vault in KeePassXC at the same moment can leave a
`.sync-conflict` copy. Rotate the shared secret with `tools/sops_secret.py
--keepass`, commit both files, merge.

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

## Gmail bootstrap (once, on a machine with a browser)

Google Cloud console:

1. Create a project (any name), APIs & Services > Library > enable **Gmail API**.
2. APIs & Services > OAuth consent screen: user type **External**, add your
   address as a test user, then **Publish app** (status "In production").
   In Testing status Google expires refresh tokens after 7 days.
   `gmail.modify` is a restricted scope: unverified, the app still works for
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
