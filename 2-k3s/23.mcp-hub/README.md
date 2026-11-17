# MCP hubs

The homelab and work hubs run the same gateway with separate namespaces,
client grants, service allowlists and upstream credentials. The root directory
holds shared code and tools; deployable overlays live under `instances/`.

Homelab serves `searxng`, `gmail` and `drive`. Work serves `searxng` and
`notion`. Each hub mounts only its approved allowlist. Clients authenticate
with their own environment-prefixed bearer token; the hub replaces it with
an independent upstream credential. Neither hub exposes vault routes.
`GET /healthz` is the only unauthenticated route.

Both Argo CD Applications automatically sync merged revisions with pruning
and self-healing. Stage matching private inputs before merging revision
bumps. No account identity or credential belongs in this public repository.

See [instance configuration and rollout](instances/README.md) for service
inventories, Google consent, Notion refresh ownership, client generation,
input rotation and offline checks. Desktop setup is in
[local client provisioning](instances/local-clients.md).

Shared gateway modules live in `files/`. `tools/hub_clients.py` and
`tools/hub_services.py` have byte copies maintained by
`../13.t3code/one/tools/sync-shared.sh`; run it after changing either tool.
The [Notion ADR](docs/adr/0001-hosted-notion-behind-the-hub.md) records the
original hosted-Notion decision.
