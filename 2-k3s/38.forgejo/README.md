# Forgejo

Forgejo holds RackPeek's config history and live homelab config snapshots in private Git repositories. The `forgejo` database in `postgres-cluster` holds metadata. Repositories and attachments live on the `forgejo-data` PVC. Renovate updates the image pin in `kustomization.yaml`.

At https://git.epaflix.com, sign in through Authentik as a member of `authentik Admins`. Forgejo then logs you in as the same username. RackPeek uses the internal service at `http://forgejo.forgejo.svc:3000`.

## After deploy

1. With the owner's approval, copy the dnsmasq config to Pi-hole and restart `pihole-FTL`. Add the matching Unbound local zone to the live file, then check and reload Unbound. See the [Pi-hole README](../../1-proxmox/pihole/README.md) for paths and commands.
2. Login is Authentik only. Forgejo trusts the `X-authentik-username` header from Traefik and creates the user on first visit. Create your admin account before your first visit, from your own terminal, with your Authentik username. The printed random password is never used; do not share or log it.

   ```sh
   kubectl -n forgejo exec deploy/forgejo -- forgejo admin user create --admin --username <authentik username> --email <email> --random-password
   ```

3. From your own terminal, create the bot and its two `write:repository` tokens, one for RackPeek's Git sync and one for the live config snapshot job. Each token prints once; paste it only into the agent's hidden-input card. The bot's first push creates the private repositories `rackpeek-config` and `homelab-live`.

   ```sh
   kubectl -n forgejo exec deploy/forgejo -- forgejo admin user create --username rackpeek-bot --email <bot email> --random-password --must-change-password=false
   kubectl -n forgejo exec deploy/forgejo -- forgejo admin user generate-access-token --username rackpeek-bot --token-name rackpeek-config --scopes write:repository --raw
   kubectl -n forgejo exec deploy/forgejo -- forgejo admin user generate-access-token --username rackpeek-bot --token-name homelab-live --scopes write:repository --raw
   ```
