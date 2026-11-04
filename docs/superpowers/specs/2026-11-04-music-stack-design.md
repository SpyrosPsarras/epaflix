# Self-hosted music stack

## Intent

Replace Tidal with music the owner keeps himself, and find new music
through people with similar taste. Reuse existing apps and write no new app.
The owner leaves Tidal right away, so the new stack is his only player from
day one.

Research behind every choice:
`2-k3s/08.servarr/research/music_acquisition.md`,
`music_server_and_players.md` and `music_discovery.md` in the same folder.

## Goals

- G1. Lidarr downloads music into a library folder through the existing
  Prowlarr and qBittorrent.
- G2. Navidrome serves the library to phone, desktop and web apps, inside
  and outside the home.
- G3. Every play reaches ListenBrainz, so it can match the owner with
  similar listeners.
- G4. digarr suggests artists that similar listeners play. The owner
  approves each one before Lidarr downloads it.
- G5. ListenBrainz playlists (Daily Jams, Weekly Jams, Weekly Exploration)
  appear in Navidrome.
- G6. The Tidal favourite artists move into the library once.
- G7. Each new app has metrics or a health probe, logs in Loki, and an
  alert when it is down.

## Non-goals

- Soulseek (slskd and Soularr). Add it later only if torrents find too
  little music.
- Streaming rippers (Tidal, Qobuz or Deezer plugins, Tidarr, streamrip).
  These carry terms-of-use and legal risk.
- AudioMuse-AI and digarr's AI suggestions. The first is heavy, and the
  second adds a paid or GPU-bound dependency.
- Music in Jellyfin. Navidrome is the only music server.
- Seerr for music, because Seerr has no music support.
- Explo, Aurral, mixarr and Tubifarry.

## Anti-goals

- Music downloads without the owner's approval.
- A login path that lets a client pick its own identity with a header.
- Credentials in Git or chat. Tokens live in SOPS Secrets or in the app's
  encrypted database.
- Listen history linked to the owner's real name.

## Constraints

- Argo CD application `servarr` owns `2-k3s/08.servarr/`. Changes reach
  the cluster only through a merged PR.
- Images use a version tag plus digest in
  `2-k3s/08.servarr/kustomization.yaml`, so Renovate can update them. See
  `docs/servarr-image-pins.md`.
- The media share `servarr-media` (`/media`) has 2.7 TB free of 20 TB,
  87% used on 2026-11-04.
- The shared CNPG Postgres runs 16. The `servarr-postgres` Secret exists,
  so new databases and users are added by hand to the encrypted source.
  `postgres-setup-job.yaml` refuses to run again.
- The Authentik middleware `traefik-system/authentik-forwardauth` returns
  `X-authentik-*` headers only.
- All nodes are amd64.

## Approaches

The owner picked O2.

- O1. Lidarr, Navidrome and Explo. Explo downloads ListenBrainz's weekly
  picks automatically. This was rejected because the owner wants to
  approve each pick.
- O2, chosen. Lidarr, Navidrome and digarr. digarr has an explicit
  "Similar Users (Deep)" mode, which takes the top artists of ListenBrainz
  users with similar taste. Picks wait in a review queue. The trade-off is
  that digarr is young, with 326 stars and one main maintainer.
- O3. Lidarr, Jellyfin and Explo. This was rejected because the Jellyfin
  ListenBrainz plugin targets Jellyfin 10.11, and the cluster runs 12.4.

## Design

### Data flow

1. The owner plays music in a Navidrome app.
2. Navidrome scrobbles each play to ListenBrainz on the server side, so
   every app counts.
3. ListenBrainz finds similar users and builds the weekly playlists.
4. On a schedule, digarr reads ListenBrainz and fills its review queue.
5. The owner approves artists or albums. digarr sends them to Lidarr.
6. Lidarr searches Prowlarr, qBittorrent downloads, and Lidarr imports
   into the music folder with MusicBrainz IDs in the tags.
7. Navidrome scans the folder. Its ListenBrainz playlist plugin imports
   the weekly playlists, matching tracks by MusicBrainz ID.

### Lidarr

- Image `ghcr.io/linuxserver/lidarr`, stable 3.1.6.5079 or newer, pinned.
- Copy Sonarr's Postgres pattern: the `LIDARR__POSTGRES__*` variables, a
  passfile, and the databases `lidarr-main` and `lidarr-log`. Create the
  databases and roles with the CNPG `Database` resource and managed roles
  in Git, so Argo owns them. The installed operator has both CRDs. If
  the cluster spec rules that out, prepare a SQL script and run it only
  after the owner confirms. (Builder's call.)
- Config PVC `lidarr-config-iscsi` on `truenas-iscsi`, like Sonarr's.
- Root folder `/media/music`. Downloads go to a qBittorrent category
  `music`, in a folder next to the existing download folders.
- Prowlarr syncs to Lidarr the same indexers it syncs to Sonarr and
  Radarr, with their music categories. The `music` category gets the same
  seeding rules as the existing categories. Cleanuparr covers Lidarr,
  and its private-tracker seeding guard
  (`cleanuparr/files/enforce-private-seeding.py`) covers the `music`
  category. Unpackerr does not, because music releases are rarely
  archived. (Builder's call.)
- MusicBrainz ID tag writing is on.
- Authentik forward auth uses the same IngressRoute pair as Sonarr, with
  the auth method set to External.
- Configarr manages one quality profile and one metadata profile. Lidarr
  support in Configarr is experimental and has no presets, so we write
  our own template. It ships in its own PR after a dry-run against the
  live Lidarr, because a bad template would also stop the Sonarr and
  Radarr sync.
- exportarr runs as a sidecar with a ServiceMonitor.
- A NetworkPolicy lets only Traefik, Prometheus and the apps that call
  Lidarr reach it. With External auth, `/initialize.json` returns the
  API key to any caller, so an open Lidarr would let any pod add
  downloads.

### Navidrome

- Image `navidrome/navidrome` 0.64.x, pinned.
- SQLite on a new `truenas-iscsi` PVC. Navidrome's built-in backup writes
  a nightly copy to `/media/backups/navidrome` and keeps 7.
- `/media/music` mounted read-only.
- Hostname `navidrome.epaflix.com`, exposed the same way as Jellyfin.
- Web UI behind `authentik-forwardauth`. Navidrome trusts
  `X-authentik-username` only from the pod network
  (`ExtAuth.TrustedSources`). A NetworkPolicy lets only Traefik,
  Prometheus and digarr reach the pod, so no other pod can send the
  header.
- `/rest/` (Subsonic) and `/jellyfin/` (experimental Jellyfin API) skip
  Authentik, because phone apps cannot do a browser login. Those routes
  carry a `headers` middleware that sets `X-authentik-username` to `""`.
  Apps log in with Navidrome's own password.
- ListenBrainz scrobbling is on. Each user pastes their own token in the
  Navidrome UI (D3).
- The plugin `kgarner7/navidrome-listenbrainz-daily-playlist` is pinned by
  version and imports Daily Jams, Weekly Jams and Weekly Exploration.
- Prometheus metrics are on, with a ServiceMonitor.

### digarr

- Image `ghcr.io/iuliandita/digarr` 2.2.0 or newer, pinned. One replica.
- Database `digarr` (created the same way as Lidarr's) on the shared
  Postgres 16, since digarr needs 15 or
  newer. This keeps it inside CNPG backups instead of the embedded PGlite.
- `DIGARR_ENCRYPTION_KEY` comes from a SOPS Secret. The owner keeps a copy
  of the key in the vault, because losing it means re-entering every
  connection.
- Authentik objects for the three hostnames go where the existing apps'
  objects live, following the Sonarr pattern in
  `2-k3s/07.authentik-deployment/`. The digarr header mapping must apply
  to digarr only. (Builder's call.)
- Login goes through Authentik forward auth. digarr's proxy auth trusts
  `X-Forwarded-User` from `PROXY_AUTH_TRUSTED_PROXIES` (the pod network). A
  NetworkPolicy lets only Traefik reach the pod. Traefik must overwrite that header on every request. This
  needs a digarr-specific forwardauth middleware whose `authResponseHeaders`
  includes `X-Forwarded-User`, plus an Authentik property mapping that
  returns the username in that header. If that does not work, fall back
  to digarr's local login behind the standard middleware (two logins).
  digarr's own OIDC is out, because it rejects identity servers that
  resolve to private addresses.
- Connections: ListenBrainz (taste source), Lidarr (target), and Navidrome
  as a Subsonic server (taste source and playlist export). AI is off.
- Schedule: "Similar Users (Deep)" weekly, plus Release Radar. The weekly
  run happens on Tuesday, after ListenBrainz generates its playlists on
  Monday.
- Health: readiness probe on `/health`, plus an alert when the Deployment
  has no ready replica. A NetworkPolicy lets only Traefik reach the pod.

### Leaving Tidal

1. The owner creates a ListenBrainz account under a name that cannot be
   linked to him, and makes it private wherever settings allow.
2. The owner uses Soundiiz once to copy his Tidal playlists and liked
   songs into ListenBrainz
   ([tutorial](https://soundiiz.com/tutorial/tidal-to-listenbrainz)).
   The free plan moves one playlist at a time, up to 200 tracks each.
   Batch transfers need a paid plan. Soundiiz lists every track it could
   not match.
3. In digarr, the owner turns on the experimental TIDAL connection and
   imports his favourite artists into the review queue. digarr marks this
   connection as untested against a live account. If it fails, the
   owner adds the artists from the Soundiiz results by hand.
4. The owner approves the import. Lidarr adds each artist with nothing
   monitored (D1), and the owner picks the albums to download.
5. The Navidrome ListenBrainz plugin imports the copied playlists by ID.
   Each one fills in as Lidarr downloads its tracks.
6. Old Tidal play history is not imported. ListenBrainz has no Tidal
   importer, and Soundiiz moves playlists and liked songs, not plays.
   ListenBrainz computes similar users from listen history. Similar-user matches start empty and improve over a
   few weeks of listening. Artist-based digarr modes work from day one.

### Observability

- Metrics: Lidarr through exportarr, Navidrome natively. digarr has no
  metrics endpoint, so its health comes from its readiness probe.
- Logs: Alloy already ships pod logs to Loki.
- Alerts in `2-k3s/10.observability/alertmanager-config/custom-alerts.yaml`,
  with tests: each target down for 15 minutes, and Lidarr health checks
  failing (exportarr exposes them).

### Rollout order

0. Before Tidal is cancelled: the Soundiiz transfer, and digarr's Tidal
   connection if digarr is already running. Otherwise the owner keeps the
   Soundiiz results and adds favourite artists by hand later.
1. Lidarr, Postgres databases, qBittorrent category, Prowlarr sync,
   exportarr. Verify one album import end to end, and that the files have
   MusicBrainz IDs.
2. Navidrome with the routes and header stripping. Verify:
   - The web UI logs in through Authentik.
   - A spoofed `X-authentik-username` on `/rest/` is ignored.
   - A phone app plays music and a scrobble reaches ListenBrainz.
3. digarr with proxy login, then the Tidal import.
4. The Navidrome ListenBrainz playlist plugin, after the first weekly
   playlists exist.

## Owner's calls

- D1. Approving an album pick downloads only that album. Approving an
  artist pick adds the artist with nothing monitored, and the owner
  picks albums by hand. Lidarr never monitors new albums, nothing is
  auto-approved, and Release Radar results wait in the review queue.
- D2. Quality targets MP3 320 or AAC 320, with no FLAC. MP3 V0 and
  256 kbps MP3 or AAC are accepted at first, and Lidarr upgrades them
  to 320 later. That is roughly 80 to
  120 MB per album, so disk space is no longer a design limit.
- D3. The owner and his household use the stack. An Authentik group
  limits who reaches Navidrome and digarr. Each person gets their own
  Navidrome user, ListenBrainz account and token, and Weekly playlist
  import. digarr follows only the owner's taste at first, and only the
  owner approves downloads.
- D4. The owner has fewer than 100 favourite artists on Tidal, so the
  free Soundiiz plan is enough.

## Risks

- R1. ListenBrainz is overloaded. On 2026-11-04 the newest weekly
  playlists on four checked accounts were from the week of 2026-10-19.
- R2. ListenBrainz publishes all listens. Use a pseudonymous account.
- R3. Lidarr depends on one central metadata server, which was broken
  from May to October 2025.
- R4. Header spoofing on Navidrome's app routes. Covered by header
  stripping and the rollout check.
- R5. Disk: 2.7 TB free. At MP3 sizes (D2) and with album-only
  downloads (D1) this is small. The existing TrueNAS free-space alerts
  cover it.
- R6. digarr's Tidal connection and proxy header wiring are unproven here.
- R7. Navidrome plugins can break on upgrade. Pin them and check after
  each upgrade.

## Left to the builder

- Exact folder names under `/media`, matching the existing layout.
- Resource requests and limits, sized from Sonarr and Seerr.
- Whether exportarr runs as a sidecar or a separate Deployment, following
  existing repo patterns.
- Which backup already covers the `truenas-iscsi` SQLite PVCs, and
  attaching Navidrome to it.

## Owner steps

- Create the ListenBrainz account and paste its token into Navidrome and
  digarr.
- Run the Soundiiz transfer from Tidal to ListenBrainz before cancelling
  Tidal.
- Create a Navidrome user and app password for each household member.
- Install the apps: Symfonium or Tempus (Android), Amperfy or Arpeggi
  (iOS), Feishin (desktop).
- Approve the first digarr review queue.
