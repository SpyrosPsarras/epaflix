# Homelable

Homelable is the homelab index: a drawn network and rack canvas with live up/down checks, Markdown pages, and a network scanner. It replaces RackPeek. Design: [docs/superpowers/specs/2026-11-24-homelable-design.md](../../docs/superpowers/specs/2026-11-24-homelable-design.md).

At https://homelable.epaflix.com, sign in through Authentik as a member of `authentik Admins`. The site is on Traefik's internal entrypoint only (LAN and Tailscale). Login is OpenID only (`AUTH_MODE=oidc`): Homelable turns its local login off, and it has no group check of its own, so the policy binding on Authentik application `homelable` is the only gate. The provider and application live in `2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml`; the client id and secret exist there and in `secrets.enc.yaml`, so change them in both in the same commit.

## Pod

One pod with two containers. `backend` is the API on 8000 and runs as root, as the upstream image requires, with only `NET_RAW` added for ping and nmap SYN scans. `frontend` is nginx on 80 and proxies `/api` to `127.0.0.1:8000`. Both have a read-only root; nginx writes only to emptyDirs. The Service exposes port 80 only.

The pod sits on the normal pod network, so scans see hosts and open ports on `192.168.10.0/24` but no MAC addresses or mDNS names. The egress policy allows DNS, Traefik (Authentik and `git-internal.epaflix.com`) and the two homelab subnets, nothing else.

## Data

SQLite and uploads are on PVC `homelable-data-pithari` at `/app/data`. On a version change the backend copies the database to `homelab.db.back-<version>` once. Renovate opens Homelable updates for review only, because an upgrade migrates the database one way.

Settings saved in the UI go to `/app/data/scan_config.json` and would override the environment. The backend command deletes that file every time the container starts, so the manifests stay the source of truth. Change settings here, not in the UI.

## Keys

Secret `homelable-secrets` holds `SECRET_KEY`, `MCP_SERVICE_KEY`, the OIDC client and `GIT_SYNC_TOKEN`. `MCP_SERVICE_KEY` opens every API endpoint and never expires. Middleware `strip-service-key` removes the `X-MCP-Service-Key` header from every request through Traefik, so the key only works inside the cluster. `LIVEVIEW_KEY`, `DOCS_VIEW_KEY` and `HOMEPAGE_API_KEY` stay unset on purpose: any value turns on pages that need no login.

Scripts call `http://homelable.homelable.svc` with `X-MCP-Service-Key`. The ingress policy admits only Traefik and pods labelled `app: homelab-sync`. The homelab agent uses `kubectl -n homelable port-forward svc/homelable 8080:80` and reads the key inside the command that sends it.

## Git history

`GIT_SYNC_*` pushes documents and diagrams, including node notes and properties, to the private Forgejo repo `homelab-bot/homelable-config` over `https://git-internal.epaflix.com`, when someone presses Sync or the nightly sync calls it. The token is the bot's `write:repository` token. Homelable runs `git ls-remote` before its first push, and Forgejo answers 404 for a repo that does not exist, so the repo was created empty beforehand with the bot's `write:user` token (`POST /api/v1/user/repos`, `private: true`), the same way as RackPeek's `rackpeek-config`.

## Homelab index

The nightly sync owns the design `Homelab index` and rewrites it every night. Draw your own maps on other designs. The UI saves a whole design at once, so a tab opened on `Homelab index` before the 03:30 sync and saved after it deletes the nodes that run added; the next run puts them back.

## Nightly sync

CronJob `homelab-sync` runs at 03:30 Europe/Athens. It reads the Kubernetes API (never Secrets), runs the read-only scanner on every allowlisted host over SSH ([homelab-scan](../../1-proxmox/homelab-scan/README.md)), and clones the public repo for Git status. Then it:

1. commits the masked host files, package lists and ConfigMaps to Forgejo `homelab-bot/homelab-live`, after gitleaks drops anything it flags;
2. reads `Homelab index` again and saves it with every host, Proxmox host, guest, Argo app and namespace as a node;
3. writes each Argo app's workloads (and TrueNAS's apps) to that node's page, only when the text changed;
4. presses Git sync, so `homelab-bot/homelable-config` gets the night's state.

Sync nodes carry property `managed-by: homelab-sync`. Their notes, position, size, colours, icon and edges are carried over; their name, type, IP, ping check and other properties come from the sync. Nodes without that property are sent back unchanged. A node that a successful source no longer reports gets property `gone-since: <date>` and stays until a person deletes it. If a source fails, its nodes keep their last facts, its pages are not rewritten, and the Job exits 1, which fires `KubeJobFailedLastRun`. If Homelable cannot be read, nothing is saved.

Hosts and guests with a known address (the `HOSTS` list, or a static `ip=` in the Proxmox guest config) get a ping check. Guests on DHCP get none. Guests sit inside their Proxmox host's box.

When `Homelab index` does not exist yet, the first run creates it and copies notes from RackPeek (`RPK_URL`), appending each workload's notes to its app's notes under a `### <workload>` heading. If RackPeek cannot be read, nothing is created and the next run tries again.

The scan key is Secret `homelab-scan-key` (type `kubernetes.io/ssh-auth`); its public half is `homelab-scan-key.pub`. Host keys are pinned in ConfigMap `homelab-known-hosts`. The ServiceAccount reads through ClusterRole `homelab-sync-reader`, bound per namespace in `homelab-sync-rbac.yaml`; a namespace without a binding is not indexed. `t3code-work` and `mcp-hub-work` are never bound and are skipped in code. `jumpbox` is not scanned: its login user has no sudo. It still appears as a guest from Proxmox.

Run it now:

```sh
kubectl -n homelable create job --from=cronjob/homelab-sync homelab-sync-manual
kubectl -n homelable logs -f job/homelab-sync-manual
```

## Notes and rescans

Each node's notes say what it is, why it is set up that way and how it updates. The homelab agent writes them, and people can edit them in the UI. Workload notes go in the app's notes. Do not edit the workload pages; the next run overwrites them.

To change a node's notes from a script, `PATCH /api/v1/nodes/<id>` with `{"notes": "..."}`.

To scan a new host: copy `1-proxmox/homelab-scan/homelab-scan.py` to `/usr/local/sbin/homelab-scan`, add the scan key line from [homelab-scan](../../1-proxmox/homelab-scan/README.md) for the host's SSH user, add its host key to `homelab-known-hosts.yaml`, its address to `homelab-sync-network-policy.yaml` and its entry to `HOSTS` in `homelab-sync-cronjob.yaml`.
