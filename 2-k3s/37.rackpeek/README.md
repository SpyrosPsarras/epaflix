# RackPeek

RackPeek is the homelab index: what runs where, where its config lives and whether that config is in Git. Its data is one YAML file on the `rackpeek-config` PVC at `/app/config`. Renovate updates the image pin in `kustomization.yaml`.

At https://rackpeek.epaflix.com, sign in through Authentik as a member of `authentik Admins`. RackPeek has no login of its own, so Authentik forward auth is the only gate for people. `/api` and `/mcp` also need the `X-Api-Key` header with `RPK_API_KEY` from Secret `rackpeek-secrets`. In-cluster callers use `http://rackpeek.rackpeek.svc:8080`.

The image runs as UID 1654 on port 8080 with a read-only root. `/app/tmp` (`TMPDIR`) and `/tmp` are emptyDirs.

## Git history

RackPeek saves its YAML to the private Forgejo repo `rackpeek-bot/rackpeek-config` with its built-in Git sync. Git sync is only switched on when `GIT_TOKEN` is set at startup, so the pod runs without it until the token exists.

1. With the owner's approval, copy the dnsmasq config to Pi-hole and restart `pihole-FTL`. Add the matching Unbound local zone to the live file, then check and reload Unbound. See the [Pi-hole README](../../1-proxmox/pihole/README.md).
2. The owner creates `rackpeek-bot` and its `rackpeek-config` token ([Forgejo README](../38.forgejo/README.md), step 3) and enters the token in the agent's hidden-input card. The agent adds it as `GIT_TOKEN` to `secrets.enc.yaml` in a PR. After sync, restart the pod: `kubectl -n rackpeek rollout restart deploy/rackpeek`.
3. In the RackPeek UI, add the remote `http://forgejo.forgejo.svc:3000/rackpeek-bot/rackpeek-config.git`. The first Git action must be a push: Forgejo creates the repo on the bot's first push, so a fetch against the empty remote fails.
