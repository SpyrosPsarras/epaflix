# Forgejo

Forgejo holds RackPeek's config history and live homelab config snapshots in private Git repositories. The `forgejo` database in `postgres-cluster` holds metadata. Repositories and attachments live on the `forgejo-data` PVC. Renovate updates the image pin in `kustomization.yaml`.

At https://git.epaflix.com, sign in through Authentik as a member of `authentik Admins`, then sign in to Forgejo. RackPeek uses the internal service at `http://forgejo.forgejo.svc:3000`.

## After deploy

1. With the owner's approval, copy the dnsmasq config to Pi-hole and restart `pihole-FTL`. Add the matching Unbound local zone to the live file, then check and reload Unbound. See the [Pi-hole README](../../1-proxmox/pihole/README.md) for paths and commands.
2. The owner creates the first Forgejo admin from their own terminal. Replace `<name>` and `<email>` before running the command. It prints the random password to that terminal. Do not share or log it.

   ```sh
   kubectl -n forgejo exec deploy/forgejo -- forgejo admin user create --admin --username <name> --email <email> --random-password
   ```

3. In the Forgejo UI, create user `rackpeek-bot`. As that user, create private repositories `rackpeek-config` and `homelab-live`. Create two `write:repository` tokens, one for RackPeek's Git sync and one for the live config snapshot job.
