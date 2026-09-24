# Hosted Notion MCP behind the hub, with a hub-held rotating grant

Clients reach Notion only through the hub's `/notion` path, which forwards to
Notion's hosted MCP (`https://mcp.notion.com/mcp`) using an OAuth grant the hub
holds. We chose this over Notion's open-source server (officially unmaintained)
and over our own REST-API module (never expires, but keyword search only, far
fewer tools, and only pages shared with an integration), because the hosted
server's tools are the ones in daily use and Notion maintains them.

## Consequences

- The goal "no third-party-hosted MCP" became "no client talks to anything but
  the hub".
- Hosted Notion is OAuth-only. The refresh token rotates on every refresh,
  replaying a rotated one revokes the whole grant, and the grant lapses after
  180 days or 30 idle days. So the hub is the single refresher, persists each
  rotated token atomically in a Kubernetes Secret it may update (not in git),
  and someone re-runs the browser bootstrap at least twice a year.
- The grant is the owner's personal Notion account, so every client acts with
  the owner's full Notion permissions.
