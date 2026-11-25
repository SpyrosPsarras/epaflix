# Forgejo

Forgejo holds RackPeek's config history, live homelab config snapshots and the agent skills the MCP hubs serve, in private Git repositories. The `forgejo` database in `postgres-cluster` holds metadata. Repositories and attachments live on the `forgejo-data-pithari` PVC. Renovate updates the image pin in `kustomization.yaml`.

At https://git.epaflix.com, sign in through Authentik as a member of `authentik Admins`. The site is on Traefik's internal entrypoint only (LAN and Tailscale); from the internet it returns 404. Traefik forward auth gates the site, then Forgejo signs you in through the Authentik OIDC application `forgejo-sso`. Forgejo creates the account on first login from the part of your email before `@`. The `forgejo-role` scope sends `forgejo: admin` for `authentik Admins`, which makes the account a Forgejo admin. Password and basic-auth login are off; the bot uses tokens. RackPeek uses the internal service at `http://forgejo.forgejo.svc:3000`.

The hubs' `skills-mcp` pods read `spypsarras/skills` (homelab) and `spypsarras/skills-work` (work) through the internal service. Bots `skills-homelab-bot` and `skills-work-bot` each have read access to one repo; their `read:repository` tokens live in Secret `skills-git` in `mcp-hub-homelab` and `mcp-hub-work`. The network policy admits only those pods from the hub namespaces. See the [MCP hub README](../23.mcp-hub/README.md#skills).

Token clients skip Authentik. Route `forgejo-token-https` sends Git smart-HTTP requests (`/<owner>/<repo>/info/refs`, `git-upload-pack`, `git-receive-pack`) and `/api/v1/` requests that carry `Authorization: token …` straight to Forgejo, which then requires a valid token. Browsers and API calls without a token still go through forward auth.

T3 Code reaches Forgejo through `tea` as one bot per machine: `t3-laptop`, `t3-homelab` and `t3-work`. A bot sees only repositories that list it as a collaborator. The pod tokens come from `13.t3code/env/tools/forgejo-grant.py`, see the [Forgejo grant](../13.t3code/env/isolated-runtime.md#forgejo-grant).

## After deploy

1. With the owner's approval, copy the dnsmasq config to Pi-hole and restart `pihole-FTL`. Add the matching Unbound local zone to the live file, then check and reload Unbound. See the [Pi-hole README](../../1-proxmox/pihole/README.md) for paths and commands.
2. Add the Authentik login source once. The client id and secret go from Secret `forgejo-secrets` straight into the pod and are not printed. They also live in provider `forgejo-sso` of `2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml`; rotate both files in the same PR. Then find the source id with `forgejo admin auth list`, run the same pipe with `forgejo admin auth update-oauth --id <id> --key "$id" --secret "$secret"`, and restart Forgejo with `kubectl -n forgejo rollout restart deploy/forgejo`, because the running server keeps the old credentials.

   ```sh
   kubectl -n forgejo get secret forgejo-secrets -o go-template='{{.data.OIDC_CLIENT_ID|base64decode}}{{"\n"}}{{.data.OIDC_CLIENT_SECRET|base64decode}}{{"\n"}}' \
     | kubectl -n forgejo exec -i deploy/forgejo -- sh -c 'read -r id; read -r secret; forgejo admin auth add-oauth --name authentik --provider openidConnect --key "$id" --secret "$secret" --auto-discover-url https://auth.epaflix.com/application/o/forgejo-sso/.well-known/openid-configuration --scopes email --scopes profile --scopes forgejo --required-claim-name forgejo --group-claim-name forgejo --admin-group admin --skip-local-2fa'
   ```

3. From your own terminal, create the bot and its two `write:repository` tokens, one for RackPeek's Git sync and one for the live config snapshot job. Each token prints once; paste it only into the agent's hidden-input card. The `rackpeek-config` token also has `write:user`, because Forgejo needs it to create the bot's repo through `POST /user/repos` (RackPeek README, step 3). The bot's first push creates the private repo `homelab-live`; `rackpeek-config` is created empty beforehand.

   ```sh
   kubectl -n forgejo exec deploy/forgejo -- forgejo admin user create --username rackpeek-bot --email <bot email> --random-password --must-change-password=false
   kubectl -n forgejo exec deploy/forgejo -- forgejo admin user generate-access-token --username rackpeek-bot --token-name rackpeek-config --scopes write:repository,write:user --raw
   kubectl -n forgejo exec deploy/forgejo -- forgejo admin user generate-access-token --username rackpeek-bot --token-name homelab-live --scopes write:repository --raw
   ```
