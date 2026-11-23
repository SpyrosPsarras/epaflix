# RackPeek

RackPeek is the homelab index: what runs where, where its config lives and whether that config is in Git. Its data is one YAML file on the `rackpeek-config-pithari` PVC at `/app/config`. Renovate updates the image pin in `kustomization.yaml`.

At https://rackpeek.epaflix.com, sign in through Authentik as a member of `authentik Admins`. The site is on Traefik's internal entrypoint only (LAN and Tailscale); from the internet it returns 404. RackPeek has no login of its own, so Authentik forward auth is the only gate for people. `/api` and `/mcp` also need the `X-Api-Key` header with `RPK_API_KEY` from Secret `rackpeek-secrets`. In-cluster callers use `http://rackpeek.rackpeek.svc:8080`.

The image runs as UID 1654 on port 8080 with a read-only root. `/app/tmp` (`TMPDIR`) and `/tmp` are emptyDirs.

## Git history

RackPeek saves its YAML to the private Forgejo repo `rackpeek-bot/rackpeek-config` with its built-in Git sync. Git sync is only switched on when `GIT_TOKEN` is set at startup, so the pod runs without it until the token exists.

RackPeek accepts only HTTPS remotes, and `git.epaflix.com` sits behind Authentik forward auth. RackPeek therefore uses `git-internal.epaflix.com`: Traefik's internal listener (LAN only, wildcard certificate) routes only Git smart-HTTP paths (`/<owner>/<repo>.git/...`) to Forgejo, without forward auth. Forgejo still requires the bot token there.

The PVC's root directory is created owned by root, and libgit2 refuses a repository whose directory belongs to another user (`repository path '/app/config' is not owned by current user`). The `config-owner` init container runs as root with only `CHOWN` and gives `/app/config` itself to UID 1654. Another init container adds `.dataprotection/` and `*.bak.*` to `/app/config/.gitignore`. RackPeek keeps its ASP.NET Data Protection keys in `.dataprotection/` and writes `config.yaml.bak.<timestamp>` copies, and Git sync stages every file that is not ignored. The repo's branch is `master`.

`rackpeek-secrets` also holds `HOMELAB_LIVE_TOKEN`, the bot's `homelab-live` token, for the nightly sync below.

1. With the owner's approval, copy the dnsmasq config to Pi-hole and restart `pihole-FTL`. Add the matching Unbound local zones to the live file, then check and reload Unbound. See the [Pi-hole README](../../1-proxmox/pihole/README.md).
2. The owner creates `rackpeek-bot` and its `rackpeek-config` token ([Forgejo README](../38.forgejo/README.md), step 3) and enters the token in the agent's hidden-input card. The agent adds it as `GIT_TOKEN` to `secrets.enc.yaml` in a PR (done 2026-11-23). After Argo syncs it, restart the pod so Git sync turns on: `kubectl -n rackpeek rollout restart deploy/rackpeek`.
3. Create the empty repo. RackPeek fetches as soon as a remote is added, so the repo must exist first:

   ```sh
   kubectl -n rackpeek get secret rackpeek-secrets -o go-template='{{.data.GIT_TOKEN|base64decode}}{{"\n"}}' \
     | kubectl -n forgejo exec -i deploy/forgejo -- sh -c 'read -r t; curl -fsS -o /dev/null -w "%{http_code}\n" -H "Authorization: token $t" -H "Content-Type: application/json" -d "{\"name\":\"rackpeek-config\",\"private\":true}" http://127.0.0.1:3000/api/v1/user/repos'
   ```

4. In the RackPeek UI, in this order: **Save** a first commit, then **Add Remote** `https://git-internal.epaflix.com/rackpeek-bot/rackpeek-config.git`, then **Push**. RackPeek's automatic first push on an empty remote fails on a repo without commits (it creates a branch before the first commit), so the first commit has to exist before the remote is added. Check in Forgejo that the commit has no `.dataprotection` files.

## Nightly sync

CronJob `rackpeek-sync` runs at 03:30 Europe/Athens. It reads the Kubernetes API (never Secrets), runs the read-only scanner on every allowlisted host over SSH ([rackpeek-scan](../../1-proxmox/rackpeek-scan/README.md)), and clones the public repo for Git status. Then it:

1. commits the masked host files, package lists and ConfigMaps to Forgejo `rackpeek-bot/homelab-live`, after gitleaks drops anything it flags;
2. posts all items to RackPeek with `mode: Replace`.

Every item the sync writes has tag `sync`. Notes and other tags on those items are carried over, and items without tag `sync` are never touched. An item that a successful source no longer reports gets label `gone-since: <date>` and stays until a person deletes it. If a source fails, its items keep their last facts and the Job exits 1, which fires `KubeJobFailedLastRun`.

The scan key is Secret `rackpeek-scan-key` (type `kubernetes.io/ssh-auth`); its public half is `scan-key.pub`. Host keys are pinned in ConfigMap `rackpeek-known-hosts`. The ServiceAccount reads through ClusterRole `rackpeek-reader`, bound per namespace in `sync-rbac.yaml`; a namespace without a binding is not indexed. `t3code-work` and `mcp-hub-work` are never bound and are skipped in code.

The scanner is installed on 17 hosts. On TrueNAS `/usr` is read-only, so it lives at `/root/rackpeek-scan` and the forced command is `sudo -n python3 /root/rackpeek-scan`. `jumpbox` is not scanned: its login user has no sudo. It still appears as a guest from Proxmox.

Run it now:

```sh
kubectl -n rackpeek create job --from=cronjob/rackpeek-sync rackpeek-sync-manual
kubectl -n rackpeek logs -f job/rackpeek-sync-manual
```

## Notes and rescans

Each item's notes say what it is, why it is set up that way and how it updates. The sync never writes notes; it copies them from the stored item on every run. The homelab agent writes them, and people can edit them in the UI.

To rescan:

1. One host: `ssh <host>` with the scan key prints the scan JSON. The forced command ignores the command you send.
2. Everything now: `kubectl -n rackpeek create job --from=cronjob/rackpeek-sync rackpeek-sync-manual`.
3. Agent pods: the sync does not read inside agent pods. The homelab agent compares its own live `AGENTS.md`, skills and MCP servers with Git when asked, and updates the notes of its `t3code-homelab` item.

To change notes from a script, read the full item with `GET /api/inventory`, set `notes`, and post it back as `{"yaml": "<document>", "mode": "Replace"}`. The JSON form of the same request clears every connection in RackPeek 2.3.0.

To scan a new host: copy `1-proxmox/rackpeek-scan/rackpeek-scan.py` to `/usr/local/sbin/rackpeek-scan`, add the scan key line from [rackpeek-scan](../../1-proxmox/rackpeek-scan/README.md) for the host's SSH user, add its host key to `sync-known-hosts.yaml`, its address to `sync-network-policy.yaml` and its entry to `HOSTS` in `sync-cronjob.yaml`.
