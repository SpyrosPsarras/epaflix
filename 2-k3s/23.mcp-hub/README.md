# mcp-hub

One long-lived streamable-HTTP MCP process that every agent client talks to,
instead of one stdio process per session per machine. Servers are mounted by
path in `files/mcp_hub.py`; one bearer token guards all of them.

| Path     | Server                | Backend                        |
|----------|-----------------------|--------------------------------|
| `/gmail` | `files/gmail_mcp.py`  | Gmail REST API, scope `gmail.modify` |

Reach it at `http://mcp-hub.mcp-hub.svc.cluster.local:8000` in-cluster and
`https://mcp.epaflix.com` on the LAN (Traefik `internal` entry point, split DNS
in `1-proxmox/pihole`). Away from home the laptop needs the VPN plus the LAN
resolver; without split DNS the name resolves to Cloudflare, which answers 404.
`GET /healthz` is the only unauthenticated route.

## Secrets

| Secret | Keys | Written by |
|---|---|---|
| `mcp-hub/mcp-hub-token` | `token` | `tools/rotate-token.sh` |
| `t3code/mcp-hub-client` | `token` (same value) | `tools/rotate-token.sh` |
| `mcp-hub/mcp-hub-gmail` | `client-id`, `client-secret`, `refresh-token` | `tools/bootstrap-gmail.py` |

The laptop copy of the token is `~/.config/opencode/mcp-hub.key` (mode 600).
Both writers need `sops` on PATH (encrypting uses only the age recipient in
`.sops.yaml`, no private key), and stamp a plaintext
`mcp-hub.epaflix.com/revision` annotation on each Secret. The kustomizations
copy it into the pod templates, so a new secret restarts the pod once synced:
mcp-hub syncs automatically on merge, t3code only when synced by hand in
ArgoCD. No Reloader watches these namespaces.

The committed `mcp-hub-gmail.enc.yaml` is a placeholder (`unset`) until the
bootstrap has run; the hub starts, and Gmail tools answer with the token
refresh error until then.

## First deploy

1. Split DNS on the Pi-hole (CT 1030), per `1-proxmox/pihole/README.md`
   (applied 2026-09-24): the `mcp.epaflix.com` address line plus its unbound
   local-zone, then `systemctl restart pihole-FTL`.
2. Merge; ArgoCD creates `mcp-hub` (automated). `t3code` syncs manually:
   sync it in ArgoCD to pick up `MCP_HUB_*` and the entrypoint change.
3. Gmail bootstrap below, merge again.

## Gmail bootstrap (once, on a machine with a browser)

Google Cloud console:

1. Create a project (any name), APIs & Services > Library > enable **Gmail API**.
2. APIs & Services > OAuth consent screen: user type **External**, add your
   address as a test user, then **Publish app** (status "In production").
   In Testing status Google expires refresh tokens after 7 days.
   `gmail.modify` is a restricted scope: unverified, the app still works for
   your own account behind a "Google hasn't verified this app" page you click
   through (Advanced > continue). Verification is only needed for other users.
3. Credentials > Create credentials > OAuth client ID > **Desktop app**.
   Download the JSON.

Then, from the repo:

```sh
python3 2-k3s/23.mcp-hub/tools/bootstrap-gmail.py --client-json ~/Downloads/client_secret_*.json
```

It runs the consent flow on a loopback port, prints the granted mailbox, and
rewrites `mcp-hub-gmail.enc.yaml` sops-encrypted with a new revision. Commit
and merge it; the hub restarts with the real credentials. Delete the
downloaded client JSON afterwards.

## Clients

**t3code pod**: automatic for OpenCode in `$HOME` (the `t3env-*` homes, Claude
and Codex are not wired). The StatefulSet gets `MCP_HUB_URL`/`MCP_HUB_TOKEN`
from `mcp-hub-client`, and `files/entrypoint.sh` registers `mcp.gmail` in
`~/.config/opencode/opencode.json` with `{env:MCP_HUB_TOKEN}` (no literal on
the PVC) and sets `gmail_gmail_send`, `gmail_gmail_send_draft`,
`gmail_gmail_trash` to `ask` unless already set.

**Laptop OpenCode** (`~/.config/opencode/opencode.json`):

```json
"mcp": {
  "gmail": {
    "type": "remote",
    "url": "https://mcp.epaflix.com/gmail",
    "enabled": true,
    "timeout": 30000,
    "headers": { "Authorization": "Bearer {file:~/.config/opencode/mcp-hub.key}" }
  }
},
"permission": {
  "gmail_gmail_send": "ask",
  "gmail_gmail_send_draft": "ask",
  "gmail_gmail_trash": "ask"
}
```

OpenCode names MCP tools `<server>_<tool>`, hence the doubled `gmail_`.
`gmail_modify` refuses to add `TRASH`/`SPAM`, so trashing always goes through
the approval-gated `gmail_trash`.

**Claude Code** (any home): `claude mcp add -s user --transport http gmail
https://mcp.epaflix.com/gmail --header "Authorization: Bearer $(cat ~/.config/opencode/mcp-hub.key)"`
(this stores the token literally in `.claude.json`).

## Verify

```sh
curl -sS https://mcp.epaflix.com/healthz                                          # ok
curl -sS -o /dev/null -w '%{http_code}\n' -X POST https://mcp.epaflix.com/gmail   # 401
kubectl --context epaflix -n mcp-hub logs deploy/mcp-hub | head                   # selftest OK, uvicorn
```

Then in OpenCode: "list my Gmail labels".

## Rotation

Hub token: `2-k3s/23.mcp-hub/tools/rotate-token.sh` (new token into both
Secrets, then the key file; `--reuse` re-encrypts the current key file).
Commit both Secrets, merge, then sync `t3code` in ArgoCD; T3 gets 401 until
then. The laptop gets 401 from the run until the hub has rolled.

Gmail refresh token: revoke at myaccount.google.com/permissions and re-run the
bootstrap.

## Adding a server

Write `files/<name>_mcp.py` exposing `server() -> MCPServer` (see
`gmail_mcp.py`; raise `ToolError` for expected failures, the SDK hides any
other exception text from the model), add it to `SERVERS` in `mcp_hub.py` and
to the `configMapGenerator`, add its env to `deployment.yaml` (plus a
revision replacement if it gets a Secret), register `mcp.<name>` in the
clients. Candidates: searxng (today a stdio script copied to every host),
notion (Notion's open-source server with an integration token), keepass (see
the note in `15.syncthing/keepass.yaml`).
