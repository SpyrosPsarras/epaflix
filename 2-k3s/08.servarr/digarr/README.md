# Digarr

Digarr discovers music and sends owner-approved picks to Lidarr. It uses the `digarr` database on the shared CNPG cluster, not embedded PGlite. `DB_PASS_FILE` reads the bare password from `music-stack`, mounted read-only with mode 0440. `DIGARR_ENCRYPTION_KEY` comes from the same Secret. The pod runs as UID/GID 10001, with one replica and `Recreate` updates.

`https://digarr.epaflix.com` uses Authentik forward auth and permits `authentik Admins` or `Music users`. The `digarr-forwarded-user` scope mapping supplies `X-Forwarded-User`. Only `digarr-https` uses the `digarr-forwardauth` middleware, which replaces client-supplied identity headers with the authenticated response. The outpost route has higher priority and no forward-auth middleware.

`digarr-ingress` permits TCP 3000 only from Traefik pods in `traefik-system`. Proxy auth trusts `10.42.0.0/16`. Kube-router still permits a pod's own node, including host-network pods on that node. This accepted cluster limit also applies here.

Startup and readiness use `/health`, which checks the database and returns `dbBackend: postgres`. Liveness checks TCP 3000 so a database outage does not restart the pod. `DigarrDown` warns after 15 minutes without an available replica or Deployment. There is no metrics endpoint or ServiceMonitor.

`/app/backups` is a writable `emptyDir` for automatic pre-migration backups. Pod replacement loses these files; they are not durable recovery copies. Retain the shared PostgreSQL backups and the matching encryption key before upgrades. Version 2.2.1 adds no database migrations. Its new media-server address guard still allows Navidrome's Kubernetes service address without `DIGARR_MEDIA_SERVER_ALLOW_LOCAL`.

## Owner-first login

Digarr makes the first account admin, including accounts provisioned by proxy auth. Keep other users out until the owner has logged in through Authentik and verified the admin role. Do not add household members to `Music users` before this check. Existing usernames retain their permissions when proxy auth matches them.

## Live checks after owner-approved sync

Run these checks only after the owner approves the live work. Record results below without passwords, keys, tokens or session cookies.

| Check | Request or action | Expected result |
| --- | --- | --- |
| C1 | `curl -s -o /dev/null -w '%{http_code}' -H 'X-Forwarded-User: <owner>' https://digarr.epaflix.com/api/v1/auth/me`, with no session | 302 to Authentik, not an owner identity |
| C2 | While signed in as a test member, send `X-Forwarded-User: <owner>` to `/api/v1/auth/me` | Response names the member, not the owner |
| C3 | From a disposable scratch pod in another namespace, send the same header directly to `http://digarr.servarr:3000/api/v1/auth/me` | Connection times out or is refused by NetworkPolicy; no HTTP identity response |
| C4 | Request `/health` from an allowed path | `status: ok`, `dbBackend: postgres`, version 2.2.1 |
| C5 | Check application bindings | Policy engine `any`, `authentik Admins` order 0, `Music users` order 1; non-members denied |

C3 needs an approved disposable pod. This PR does not create one or perform any live checks.

## Owner connections and D1 checks

Configure the following in the UI after owner-first login. Read credentials privately; never paste them into Git, a report or chat.

| Setting | Value |
| --- | --- |
| Owner listening connection | ListenBrainz, using the owner's token |
| Library connection | Navidrome as Subsonic, `http://navidrome:4533` |
| Acquisition target | Lidarr, `http://lidarr:8686`, using `music-stack` key `lidarr-api-key` |
| Lidarr target assigned user | Owner only |
| Lidarr target Monitor new albums | None, `monitorNewItems: None` |
| Lidarr quality / metadata profiles | `MP3-AAC` / `Studio` |
| Settings > Recommendations | Auto-approve off; auto-approve monitoring `None` |
| Every other member's policy | Request only |
| Every other member's targets | None |
| Scheduled modes | Similar Users (Deep) and Release Radar weekly on Tuesday |
| Optional connection | TIDAL, only while the subscription is active |

Members are Request only and have no targets. Approving a member's request uses that member's targets, not the reviewing admin's, so it cannot download anything. Only picks the owner approves through the owner's own target reach Lidarr. Release Radar results wait in the review queue.

| Check | Action | Expected result |
| --- | --- | --- |
| D1a | Owner approves one artist pick | Lidarr has the artist with 0 monitored albums, no queued search, and `qualityProfileId` matching `MP3-AAC` |
| D1b | Owner approves one album pick | Exactly 1 monitored album |
| D1c | Test member submits an approval, then the owner reviews that request | Lidarr's artist list and queue remain unchanged |
| D1d | Let one scheduled run finish | Lidarr's artist count and queue remain unchanged |

## Fallback if header mapping fails

Do not widen network access or trust a client header. In a reviewed follow-up change, remove the custom scope mapping from the provider and blueprint, set `PROXY_AUTH_ENABLED=false`, and replace `digarr-forwardauth` on `digarr-https` with the standard `authentik-forwardauth` in `traefik-system`. Remove the unused custom middleware. Keep the Authentik group gate, security headers, NetworkPolicy and owner-only target settings. Users then sign in locally to Digarr behind Authentik. Record this fallback in the PR and repeat the identity and D1 checks after the owner-approved sync.

## Live changes
