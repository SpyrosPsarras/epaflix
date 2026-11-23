# RackPeek

RackPeek is the homelab index: what runs where, where its config lives and whether that config is in Git. Its data is one YAML file on the `rackpeek-config` PVC at `/app/config`. Renovate updates the image pin in `kustomization.yaml`.

At https://rackpeek.epaflix.com, sign in through Authentik as a member of `authentik Admins`. RackPeek has no login of its own, so Authentik forward auth is the only gate for people. `/api` and `/mcp` also need the `X-Api-Key` header with `RPK_API_KEY` from Secret `rackpeek-secrets`. In-cluster callers use `http://rackpeek.rackpeek.svc:8080`.

The image runs as UID 1654 on port 8080 with a read-only root. `/app/tmp` (`TMPDIR`) and `/tmp` are emptyDirs.

## Git history

RackPeek saves its YAML to the private Forgejo repo `rackpeek-bot/rackpeek-config` with its built-in Git sync. Git sync is only switched on when `GIT_TOKEN` is set at startup, so the pod runs without it until the token exists.

RackPeek accepts only HTTPS remotes, and `git.epaflix.com` sits behind Authentik forward auth. RackPeek therefore uses `git-internal.epaflix.com`: Traefik's internal listener (LAN only, wildcard certificate) routes only Git smart-HTTP paths (`/<owner>/<repo>.git/...`) to Forgejo, without forward auth. Forgejo still requires the bot token there.

An init container adds `.dataprotection/` to `/app/config/.gitignore`. RackPeek keeps its ASP.NET Data Protection keys in that directory, and Git sync stages every file that is not ignored.

1. With the owner's approval, copy the dnsmasq config to Pi-hole and restart `pihole-FTL`. Add the matching Unbound local zones to the live file, then check and reload Unbound. See the [Pi-hole README](../../1-proxmox/pihole/README.md).
2. The owner creates `rackpeek-bot` and its `rackpeek-config` token ([Forgejo README](../38.forgejo/README.md), step 3) and enters the token in the agent's hidden-input card. The agent adds it as `GIT_TOKEN` to `secrets.enc.yaml` in a PR. After Argo syncs it, restart the pod so Git sync turns on: `kubectl -n rackpeek rollout restart deploy/rackpeek`.
3. Create the empty repo. RackPeek fetches as soon as a remote is added, so the repo must exist first:

   ```sh
   kubectl -n rackpeek get secret rackpeek-secrets -o go-template='{{.data.GIT_TOKEN|base64decode}}{{"\n"}}' \
     | kubectl -n forgejo exec -i deploy/forgejo -- sh -c 'read -r t; curl -fsS -o /dev/null -w "%{http_code}\n" -H "Authorization: token $t" -H "Content-Type: application/json" -d "{\"name\":\"rackpeek-config\",\"private\":true}" http://127.0.0.1:3000/api/v1/user/repos'
   ```

4. In the RackPeek UI, in this order: **Save** a first commit, then **Add Remote** `https://git-internal.epaflix.com/rackpeek-bot/rackpeek-config.git`, then **Push**. RackPeek's automatic first push on an empty remote fails on a repo without commits (it creates a branch before the first commit), so the first commit has to exist before the remote is added. Check in Forgejo that the commit has no `.dataprotection` files.
