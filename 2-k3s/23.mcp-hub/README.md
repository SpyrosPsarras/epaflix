# MCP hubs

The homelab and work hubs run the same gateway with separate namespaces,
client grants, service allowlists and upstream credentials. The root directory
holds shared code and tools; deployable overlays live under `instances/`.

Homelab serves `searxng`, `gmail`, `drive` and `skills`. Work serves
`searxng`, `notion`, `skills` and `outlook`. Each hub mounts only its approved allowlist. Clients authenticate
with their own environment-prefixed bearer token; the hub replaces it with
an independent upstream credential. Neither hub exposes vault routes.
`GET /healthz` is the only unauthenticated route.

Both Argo CD Applications automatically sync merged revisions with pruning
and self-healing. Stage matching private inputs before merging revision
bumps. No account identity or credential belongs in this public repository.

See [instance configuration and rollout](instances/README.md) for service
inventories, Google consent, Notion refresh ownership, work Outlook, client generation,
input rotation and offline checks. Desktop setup is in
[local client provisioning](instances/local-clients.md).

## Skills

`skills` serves Agent Skills with the MCP Skills extension
(`io.modelcontextprotocol/skills`). Homelab serves the private Forgejo repo
`spypsarras/skills`, work serves `spypsarras/skills-work`. In each namespace,
the `skills-mcp` pod runs git-sync, which pulls `main` every 60 seconds
with the read-only bot token in Secret `skills-git` (`username`, `token`),
and a FastMCP server that rereads the synced folder on every request. A push
reaches clients within about a minute, without a restart. A root `AGENTS.md`
in these repos is not a skill. The T3 runtimes load it as agent instructions,
see [Forgejo agent instructions](../38.forgejo/README.md#agent-instructions). The server starts
only after git-sync's first successful pull, so a broken first checkout
fails startup instead of serving an empty set. A broken skill folder is
logged and the last valid set keeps serving from memory; a restart on a
broken commit fails until the commit is fixed or reverted. The hub reaches the
server with the token in Secret `mcp-hub-skills`, the same value as
`upstreams.skills.token` in `credentials.json`.

Clients need the Skills extension. Pi has none built in, so the laptop,
homePC and both T3 runtimes load the `@jangmanj/pi-mcp-ext` package, whose
`mcp_skills` tool lists, loads and reads skills and asks for approval before
the first load of each skill in a session. The T3 runtimes' Pi launcher sets
`PI_MCP_EXT_INCLUDE=skills`, so the package opens only that server, and
`PI_MCP_EXT_SKILL_AUTO_APPROVE=skills`, so the runtimes' fork pin
([#2507](https://github.com/SpyrosPsarras/epaflix/issues/2507)) loads hub skills
without the prompt and lists them in the system prompt.

Shared gateway modules live in `files/`. `tools/hub_clients.py` and
`tools/hub_services.py` have byte copies maintained by
`../13.t3code/one/tools/sync-shared.sh`; run it after changing either tool.
The [Notion ADR](docs/adr/0001-hosted-notion-behind-the-hub.md) records the
original hosted-Notion decision.
