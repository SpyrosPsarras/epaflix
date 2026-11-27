# Forgejo

Forgejo holds the Homelable index history, live homelab config snapshots and the agent skills the MCP hubs serve, in private Git repositories. The `forgejo` database in `postgres-cluster` holds metadata. Repositories and attachments live on the `forgejo-data-pithari` PVC. Renovate updates the image pin in `kustomization.yaml`.

At https://git.epaflix.com, sign in through Authentik as a member of `authentik Admins`. The site is on Traefik's internal entrypoint only (LAN and Tailscale); from the internet it returns 404. Traefik forward auth gates the site, then Forgejo signs you in through the Authentik OIDC application `forgejo-sso`. Forgejo creates the account on first login from the part of your email before `@`. The `forgejo-role` scope sends `forgejo: admin` for `authentik Admins`, which makes the account a Forgejo admin. Password and basic-auth login are off; the bot uses tokens. RackPeek uses the internal service at `http://forgejo.forgejo.svc:3000`.

The hubs' `skills-mcp` pods read `spypsarras/skills` (homelab) and `spypsarras/skills-work` (work) through the internal service. Bots `skills-homelab-bot` and `skills-work-bot` each have write access to one repo. Their hub tokens are `read:repository` and live in Secret `skills-git` in `mcp-hub-homelab` and `mcp-hub-work`. The network policy admits only those pods from the hub namespaces. See the [MCP hub README](../23.mcp-hub/README.md#skills).

Token clients skip Authentik. Route `forgejo-token-https` sends Git smart-HTTP requests (`/<owner>/<repo>/info/refs`, `git-upload-pack`, `git-receive-pack`) and `/api/v1/` requests that carry `Authorization: token …` straight to Forgejo, which then requires a valid token. Browsers and API calls without a token still go through forward auth.

T3 Code reaches Forgejo through `tea`. The homelab and work pods sign in as `skills-homelab-bot` and `skills-work-bot` with a second token each, and the laptop signs in as `spypsarras`. The pod tokens come from `13.t3code/env/tools/forgejo-grant.py`, see the [Forgejo grant](../13.t3code/env/isolated-runtime.md#forgejo-grant).

## Agent instructions

`AGENTS.md` at the root of `spypsarras/skills` and `spypsarras/skills-work` holds the T3 runtimes' agent rules and the owner's preferences. The pods load a push within about a minute, see [Instructions from Forgejo](../13.t3code/env/isolated-runtime.md#instructions-from-forgejo). Each bot pushes `AGENTS.md` changes to its own repo directly. That is the owner's choice. The agent can change its own rules, and Forgejo's commit history shows who changed what. To require owner review of `AGENTS.md` while bots keep pushing skills, give `main` this branch protection rule:

- Push restricted to an allowlist with only `spypsarras`, deploy keys off.
- Merge restricted to an allowlist with only `spypsarras`.
- Unprotected file patterns `*/**`.
- No protected file patterns.

Forgejo then accepts a push from a user outside the allowlist only when every changed file matches an unprotected pattern (`routers/private/hook_pre_receive.go` in v17.0.2). Patterns are lowercase globs with `/` and `.` as separators, so `*/**` matches every file inside a top-level folder whose name has no dot. That covers every skill and no root file. The bots keep pushing skill changes to `main`, and a bot change to `AGENTS.md` has to be a pull request that only the owner can merge. Do not list `AGENTS.md` as a protected file pattern. Forgejo also refuses to merge pull requests that touch protected files.

After saving the rule, check both directions. A bot push that adds a root file fails with `Not allowed to push to protected branch main`. A bot push that changes only a skill folder succeeds.

## After deploy

1. With the owner's approval, copy the dnsmasq config to Pi-hole and restart `pihole-FTL`. Add the matching Unbound local zone to the live file, then check and reload Unbound. See the [Pi-hole README](../../1-proxmox/pihole/README.md) for paths and commands.
2. Add the Authentik login source once. The client id and secret go from Secret `forgejo-secrets` straight into the pod and are not printed. They also live in provider `forgejo-sso` of `2-k3s/07.authentik-deployment/authentik-iac-blueprint.enc.yaml`; rotate both files in the same PR. Then find the source id with `forgejo admin auth list`, run the same pipe with `forgejo admin auth update-oauth --id <id> --key "$id" --secret "$secret"`, and restart Forgejo with `kubectl -n forgejo rollout restart deploy/forgejo`, because the running server keeps the old credentials.

   ```sh
   kubectl -n forgejo get secret forgejo-secrets -o go-template='{{.data.OIDC_CLIENT_ID|base64decode}}{{"\n"}}{{.data.OIDC_CLIENT_SECRET|base64decode}}{{"\n"}}' \
     | kubectl -n forgejo exec -i deploy/forgejo -- sh -c 'read -r id; read -r secret; forgejo admin auth add-oauth --name authentik --provider openidConnect --key "$id" --secret "$secret" --auto-discover-url https://auth.epaflix.com/application/o/forgejo-sso/.well-known/openid-configuration --scopes email --scopes profile --scopes forgejo --required-claim-name forgejo --group-claim-name forgejo --admin-group admin --skip-local-2fa'
   ```

3. From your own terminal, create the bot and its two `write:repository` tokens, one for Homelable's Git sync and one for the live config snapshot job. Each token prints once; paste it only into the agent's hidden-input card. The first token also has `write:user`, because Forgejo needs it to create the bot's repo through `POST /user/repos`. The bot's first push creates the private repo `homelab-live`; `homelable-config` is created empty beforehand ([Homelable README](../40.homelable/README.md#git-history)). The bot was first created as `rackpeek-bot`. Renaming it to `homelab-bot` in Site administration keeps its repos and tokens.

   ```sh
   kubectl -n forgejo exec deploy/forgejo -- forgejo admin user create --username homelab-bot --email <bot email> --random-password --must-change-password=false
   kubectl -n forgejo exec deploy/forgejo -- forgejo admin user generate-access-token --username homelab-bot --token-name homelable-config --scopes write:repository,write:user --raw
   kubectl -n forgejo exec deploy/forgejo -- forgejo admin user generate-access-token --username homelab-bot --token-name homelab-live --scopes write:repository --raw
   ```
