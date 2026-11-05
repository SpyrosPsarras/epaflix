# Navidrome

Navidrome serves Lidarr's music library from `/media/music` on `servarr-media`, mounted read-only at `/music`. It runs as UID/GID 568 with a 5Gi `truenas-iscsi` data PVC. Backups go to `/media/backups/navidrome` through the `/backups` mount, daily at 04:00 Europe/Oslo, retaining seven copies.

`https://navidrome.epaflix.com` uses Authentik forward auth and permits `authentik Admins` or `Music users`. The `/rest/` and `/jellyfin/` routes use native app authentication instead. Their middleware removes `X-authentik-username`, `True-Client-IP` and `X-Real-IP` from incoming requests.

`navidrome-ingress` permits TCP 4533 only from Traefik, Prometheus and same-namespace digarr pods. External auth trusts the pod CIDR `10.42.0.0/16`. Kube-router still permits a pod's own node, including host-network pods on that node. This accepted cluster limit also applies here. The ServiceMonitor sets `jobLabel: app`, yielding `job="navidrome"` for `NavidromeDown`.

## Owner-first login

After merge and Argo CD sync, the owner must open Navidrome through Authentik before anyone joins `Music users`. Confirm the owner's Navidrome user has `isAdmin: true`, then add household members. The new group starts with no members.

## Live checks still to do

- L1: Confirm the PVC binds, the pod becomes ready, Prometheus scrapes `/metrics` and HTTP redirects to HTTPS. Apply the Pi-hole repo changes separately after owner confirmation.
- L2: Confirm `/` without a session redirects to Authentik and an account outside the allowed groups is denied. The owner creates and removes any test user.
- L3: Send a forged `X-authentik-username` header to `/rest/ping.view?f=json&v=1.16.1&c=probe`. Expect `status: failed` and error code 10. On `/jellyfin/Users/Me` expect 401, or test another authenticated Jellyfin path if it returns 404. Never accept 200 with user data.
- L4: After owner approval, test direct `navidrome.servarr:4533/api/user` access with a forged identity from a scratch pod in another namespace. Expect timeout or connection refused.
- L5: Confirm the next Authentik blueprint-check CronJob has no 403, a phone app play appears on ListenBrainz within five minutes once that integration is configured, and a backup `.db` exists after 04:00.

## Live changes
