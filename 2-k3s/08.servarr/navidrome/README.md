# Navidrome

Navidrome serves Lidarr's music library from `/media/music` on `servarr-media`, mounted read-only at `/music`. It runs as UID/GID 568 with a 5Gi `truenas-iscsi` data PVC. Backups go to `/media/backups/navidrome` through the `/backups` mount, daily at 04:00 UTC, retaining seven copies. The image has no time zone data, so `TZ=Europe/Oslo` has no effect. No volume-level backup covers the `truenas-iscsi` app PVCs, so these files are the only copy of users, playlists and play counts.

`https://navidrome.epaflix.com` and `https://music.epaflix.com` serve the same Navidrome. Both use Authentik forward auth, each through its own proxy provider (`navidrome`, `navidrome-music`), and permit `authentik Admins` or `Music users`. The `/rest/` and `/jellyfin/` routes use native app authentication instead. Their middleware removes `X-authentik-username`, `True-Client-IP` and `X-Real-IP` from incoming requests.

`navidrome-ingress` permits TCP 4533 only from Traefik, Prometheus and same-namespace digarr pods. External auth trusts the pod CIDR `10.42.0.0/16`. Kube-router still permits a pod's own node, including host-network pods on that node. This accepted cluster limit also applies here. The ServiceMonitor sets `jobLabel: app`, yielding `job="navidrome"` for `NavidromeDown`.

## Owner-first login

The first account that logs in through Authentik becomes the Navidrome admin. Before anyone joins `Music users`, confirm that account is the owner and has `is_admin = 1`. Household members get their own Navidrome user on first login through Authentik, as the owner did.

## Remaining check

- The owner's ListenBrainz token is set in Navidrome, but no scrobble had been recorded by 2026-11-08 00:33 UTC. Confirm that a phone app play appears on the owner's ListenBrainz listens.

## Live changes

All done 2026-11-06 to 2026-11-08 after owner confirmation, at merge `d3084221` (#2207).

Argo CD: `servarr` (the 11 Navidrome objects), `authentik` (the blueprint Secret) and `observability` (`custom-alerts`) synced as dry run, then for real. Navidrome 0.64.2 became ready; its first full scan imported the 14 tracks already in `/media/music`. Authentik applied the blueprint at 07:39 UTC; after that `/` returned 302 to Authentik. Prometheus reports `up{job="navidrome"} = 1`. The 2026-11-07 Authentik blueprint-check Job completed.

The controller created `/media/backups/navidrome` (568:568) before the sync so the subPath mount is writable.

Pi-hole on CT 1030: backups `10-epaflix.conf.bak-20261106-navidrome` and `/root/no-aaaa-leak.conf.bak-20261106-navidrome`. The live file matched the pre-merge repo file; copied the new one (live equals repo), inserted the `navidrome.epaflix.com.` local-zone after `lidarr`, `unbound-checkconf` clean, `unbound-control reload`, `systemctl restart pihole-FTL`. `navidrome.epaflix.com` A is `192.168.10.101`, AAAA empty.

Owner-first login on 2026-11-08: the user table holds one user, the owner, with `is_admin = 1`, and a ListenBrainz session key. `Music users` has no members yet.

| Check | Request | Result |
| --- | --- | --- |
| Forged header, Subsonic | `GET /rest/ping.view?f=json&v=1.16.1&c=probe` with `X-authentik-username: <owner>` | `failed`, code 10 |
| Forged header with username, no password | same, plus `u=<owner>` | `failed`, code 40 |
| Forged header, Jellyfin API | `GET /jellyfin/Users/Me` with the header | 401 |
| Unauthenticated Jellyfin sanity | `GET /jellyfin/System/Info/Public` | 200 |
| Forged header on the web API | `GET /api/user` with the header, no session | 302 to Authentik |
| Case variant | `GET /REST/ping.view` with the header | 302 to Authentik |
| Direct from another namespace | `curlimages/curl` pod in `default` to `navidrome.servarr:4533/api/user` with the header | connection refused |
| Direct from Lidarr | `lidarr` pod to `navidrome.servarr:4533` | connection refused |
| Group gate | `ak shell` on application `navidrome` | engine `any`; bindings `authentik Admins` (order 0), `Music users` (order 1). Non-member denial verified from this config; no test user created |
| Backup | `/backups` after 04:00 | `navidrome_backup_2026.11.07_04.00.00.db`, log "Backup complete" at 04:00:00Z |

After all checks the user table still held one user.
