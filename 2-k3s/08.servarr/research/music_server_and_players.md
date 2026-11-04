# Music server and player apps for the servarr namespace

Question: which self-hosted music server and which client apps fit this cluster best as of late 2026? Music arrives through Lidarr into a library folder. The owner wants phone, desktop and web players and good discovery through scrobbling and recommendations.

Answer up front: run **Navidrome 0.64.x** as a separate music server next to Jellyfin. Navidrome speaks the Subsonic/OpenSubsonic API, the API most music apps use. Since 0.64.0 it also speaks a subset of the Jellyfin API, so Jellyfin music apps (Finamp, Jellify) work with it too. It has built-in Last.fm and ListenBrainz scrobbling, similar-artist and similar-song lookups from Last.fm, ListenBrainz and Deezer, smart playlists, a WebAssembly plugin system, reverse-proxy header auth that works with the existing Authentik forward-auth middleware, and a Prometheus endpoint. For discovery, use ListenBrainz as the hub. A Navidrome plugin imports the ListenBrainz Daily Jams, Weekly Jams and Weekly Exploration playlists. Explo can fetch the recommended tracks you do not own yet through Lidarr. Clients: **Symfonium** (paid) or **Tempus** (free) on Android, **Amperfy** or **Arpeggi** (both free) on iOS, **Feishin** on desktop and web. All of them work in the car through Android Auto or CarPlay.

Jellyfin alone is the fallback if you want zero extra services. On Jellyfin 12, scrobbling and OIDC depend on third-party plugins that target the 10.11 plugin ABI or are archived (F5).

## Method and limits

- Read on 2026-11-04 (UTC). Star counts, releases and commit counts come from the GitHub REST API on that date. "Commits 90d" means commits on the default branch since 2026-08-04, capped at 100 by the query.
- SearXNG was down for this research. Every query returned no results or unrelated pages: brave, duckduckgo, google cse, startpage and wikipedia were suspended or blocked by CAPTCHA. Following the rules, I did not use another search engine. I read known URLs directly: GitHub repos and API, official docs, project sites, Apple's iTunes lookup API by app ID, App Store pages, and GitHub topic pages linked from official docs. To find newer projects I used GitHub topic pages (`music-server`, `subsonic-server`, `opensubsonic`, `self-hosted-music`, `music-streaming-server`, `navidrome-plugin`), not a web search. This may have missed projects without those topics.
- Anything I could not confirm in a primary source is marked "unverified".

## Findings

### F1. Navidrome is the most active and most widely supported server

- 24,514 stars, GPL-3.0, Go. Latest release v0.64.2 on 2026-09-24. 100+ commits in the last 90 days. ([repo](https://github.com/navidrome/navidrome), [releases](https://github.com/navidrome/navidrome/releases))
- Multi-user with per-user play counts, playlists and favourites. Multi-library with per-user access. Transcoding, sharing, jukebox. Navidrome's docs say it runs on a Raspberry Pi Zero. ([overview](https://www.navidrome.org/docs/overview/))
- Database is SQLite. Search runs on SQLite FTS5 and a scheduled SQLite `ANALYZE` exists. I found no Postgres option. ([v0.61.0 notes](https://github.com/navidrome/navidrome/releases/tag/v0.61.0), [options](https://www.navidrome.org/docs/usage/configuration/options/)) The built-in backup copies only the database. ([backup](https://www.navidrome.org/docs/usage/admin/backup/))
- Smart playlists (beta) are JSON rules, either `.nsp` files picked up by the scan or created from a client. The web UI has no rule editor yet, but Feishin has one. ([smart playlists](https://www.navidrome.org/docs/usage/features/smart-playlists/))
- Scrobbling to Last.fm and ListenBrainz is built in and set per user. `ListenBrainz.BaseURL` can point to Maloja or another ListenBrainz-compatible server. Since 0.64.0 a per-user scrobble filter can keep some songs out of scrobbles while still counting them locally. Scrobble history has been stored locally since 0.59.0. ([scrobbling](https://www.navidrome.org/docs/usage/features/scrobbling/))
- Metadata agents, tried in this order by default: `deezer,lastfm,listenbrainz`. Last.fm gives similar artists, top songs and similar songs, and needs an API key and secret. ListenBrainz gives similar artists, top songs and similar songs with no key, and works best when files carry MusicBrainz IDs. Deezer gives similar artists and top songs. ([external services](https://www.navidrome.org/docs/usage/integration/external-services/))
- Plugins: experimental support arrived in v0.57.0 (2025-07-01). v0.60.0 (2026-02-03) rewrote it as WebAssembly plugins in an Extism sandbox, with Go, Rust, Python or JavaScript PDKs. Plugins can be metadata agents, scrobblers, scheduled tasks and event handlers, each with declared HTTP hosts and permissions. 0.64.0 removed Extism's built-in HTTP and blocks private addresses unless the manifest lists them, so older plugins may need updates. ([v0.57.0](https://github.com/navidrome/navidrome/releases/tag/v0.57.0), [v0.60.0](https://github.com/navidrome/navidrome/releases/tag/v0.60.0), [plugins](https://www.navidrome.org/docs/usage/features/plugins/), [v0.64.0](https://github.com/navidrome/navidrome/releases/tag/v0.64.0))
- Auth: there is no native OIDC. External auth reads a user header (default `Remote-User`, set with `ExtAuth.UserHeader`) from requests whose source IP is in `ExtAuth.TrustedSources`. Unknown users are created automatically. The Subsonic endpoint `/rest/*` accepts the header too, but most mobile clients cannot log in through a forward-auth proxy, so the docs suggest bypassing auth on `/rest/*` and letting Navidrome check the password. The docs give an Authentik example and warn that the proxy must strip a client-supplied header. ([external auth](https://www.navidrome.org/docs/usage/integration/authentication/))
- Prometheus: `ND_PROMETHEUS_ENABLED`, with a custom path and optional basic auth. There are Grafana dashboards 24397 and 18038. ([monitoring](https://www.navidrome.org/docs/usage/integration/monitoring/), [options](https://www.navidrome.org/docs/usage/configuration/options/))
- Container image: `navidrome/navidrome`. ([docker docs](https://www.navidrome.org/docs/installation/docker/))

### F2. Navidrome 0.64 added an experimental Jellyfin API

`Jellyfin.Enabled = true` exposes a subset of the Jellyfin API under `/jellyfin`. The team tests it with Finamp, Jellify and Feishin. It supports browsing, search, favourites, playlists, streaming with transcoding and ReplayGain, lyrics, Instant Mix, Quick Connect, and scrobbles to Last.fm, ListenBrainz and scrobbler plugins. Known limits: no server push events, and the genre list ignores per-user library access. 0.64.0 also re-encoded all IDs, so clients with offline caches may need to re-sync. ([Jellyfin API docs](https://www.navidrome.org/docs/usage/features/jellyfin/), [v0.64.0 notes](https://github.com/navidrome/navidrome/releases/tag/v0.64.0), [Substreamer note](https://github.com/ghenry22/substreamer))

### F3. Navidrome plugins for discovery exist and are maintained

- `kgarner7/navidrome-listenbrainz-daily-playlist` (294 stars, v6.0.0 2026-07-08) imports ListenBrainz playlists such as `daily-jams`, `weekly-jams` and `weekly-exploration` into Navidrome per user. It matches tracks only by MBID, so it only includes tracks already in the library. Needs Navidrome ≥ 0.63.0. ([repo](https://github.com/kgarner7/navidrome-listenbrainz-daily-playlist))
- `NeptuneHub/AudioMuse-AI-NV-plugin` (552 stars) adds sonic similarity for Instant Mix and similar songs, over both the Subsonic and the Jellyfin API. ([repo](https://github.com/NeptuneHub/AudioMuse-AI-NV-plugin), [Navidrome AudioMuse docs](https://www.navidrome.org/docs/usage/integration/audiomuse/))
- Smaller ones: a ListenBrainz loves sync plugin (6 stars), a Last.fm popularity rating plugin (2 stars), and the official Apple Music metadata plugin (301 stars). ([topic page](https://github.com/topics/navidrome-plugin), [apple-music-plugin](https://github.com/navidrome/apple-music-plugin), [egecelikci/navidrome-listenbrainz-plugin](https://github.com/egecelikci/navidrome-listenbrainz-plugin))

### F4. Discovery tools that sit next to the server

- **ListenBrainz** lists users with similar taste based on listen history (`GET /1/user/{user}/similar-users`). ([API docs](https://listenbrainz.readthedocs.io/en/latest/users/api/core.html)) It generates Weekly Exploration, Weekly Jams, Daily Jams and Fresh Releases playlists, and has LB Radio. Daily Jams requires following the `troi-bot` user. ([Explo wiki](https://github.com/LumePart/Explo/wiki), [kgarner7 plugin](https://github.com/kgarner7/navidrome-listenbrainz-daily-playlist))
- **Explo** (2,066 stars, MIT, v1.2.0 2026-09-09) fetches those ListenBrainz playlists, downloads missing tracks through Lidarr, slskd or YouTube, and creates the playlist on Navidrome or any Subsonic server, Jellyfin, Emby, Plex or MPD. It also imports Spotify, Apple Music and ListenBrainz playlists. It has a Helm chart. ([repo](https://github.com/LumePart/Explo), [wiki](https://github.com/LumePart/Explo/wiki))
- **AudioMuse-AI** (2,900 stars, AGPL-3.0, v3.7.0 2026-10-29) does sonic analysis of your own files and builds playlists. Supported servers: Navidrome, Jellyfin, LMS, Lyrion, Emby, Plex. It needs PostgreSQL ("designed to work with PostgreSQL v15"), and the README lists minimum hardware as a 4-core CPU with AVX2 and 8 GB RAM. It has a Helm chart. ([repo](https://github.com/NeptuneHub/AudioMuse-AI))
- **multi-scrobbler** (1,299 stars, MIT) scrobbles from many sources, Jellyfin among them, to several targets. ([repo](https://github.com/FoxxMD/multi-scrobbler))

### F5. Jellyfin 12 for music

- Cluster runs `jellyfin/jellyfin:12.4` (`2-k3s/08.servarr/jellyfin/jellyfin.yaml`). Upstream: 58,992 stars, GPL-2.0, v12.4 on 2026-10-26. ([repo](https://github.com/jellyfin/jellyfin))
- 12.0 bundles a ListenBrainz provider for similar artists, adds per-library similarity and recommendation providers, and gives plugins new search and similarity hooks. 12.0 also asks users to remove repository plugins before migrating, because plugins need time to adapt. ([v12.0 notes](https://github.com/jellyfin/jellyfin/releases/tag/v12.0))
- No Subsonic API, so the Subsonic app ecosystem does not apply. Jellyfin apps for music include Finamp, Jellify, Feishin, Supersonic and Symfonium. ([jellyfin.org clients data](https://github.com/jellyfin/jellyfin.org/blob/master/src/data/clients.ts), [Symfonium](https://symfonium.app/))
- Scrobbling needs plugins. The ListenBrainz plugin (222 stars, 6.5.3.4 on 2026-08-27) sends listens, syncs loves, and syncs ListenBrainz-generated playlists, but its `build.yaml` targets ABI 10.11.0.0. The original Last.fm plugin is archived. The active fork (`danielfariati`, 153 stars) targets ABI 10.11.9.0. I did not verify that either works on 12.x. ([LB plugin](https://github.com/lyarenei/jellyfin-plugin-listenbrainz), [archived Last.fm](https://github.com/jesseward/jellyfin-plugin-lastfm), [fork](https://github.com/danielfariati/jellyfin-plugin-lastfm))
- Smart playlists need the SmartLists plugin (399 stars, v12.1.0.0, "Requires Jellyfin 12"). It can fill lists from ListenBrainz. ([repo](https://github.com/jyourstone/jellyfin-smartplaylist-plugin))
- OIDC: the SSO plugin is archived ("Project archived because I'm tired of working on this") and targets ABI 10.11.0.0. The official LDAP plugin is active (v24, 2026-09-08). ([SSO plugin](https://github.com/9p4/jellyfin-plugin-sso), [LDAP plugin](https://github.com/jellyfin/jellyfin-plugin-ldapauth))
- Prometheus `/metrics` is built in but off by default (`EnableMetrics`). ([monitoring docs](https://jellyfin.org/docs/general/post-install/networking/advanced/monitoring))
- Observed in this cluster: the Jellyfin pod used 557 MiB and 3m CPU while idle (`kubectl top pod -n servarr`, 2026-11-04).

### F6. Plex + Plexamp depends on a plex.tv account, and prices went up in 2025

- A Plex server must be claimed to a plex.tv account. The Docker image takes a claim token from `plex.tv/claim`. ([pms-docker](https://github.com/plexinc/pms-docker)) Plexamp needs a Plex Media Server for personal music. ([plexamp page](https://www.plex.tv/plexamp/))
- Plex Pass from 2025-04-29: $6.99/month, $69.99/year, $249.99 lifetime. Remote playback of personal media is no longer free, but the FAQ says the change covers only movies, TV and video, not music in Plexamp. ([Plex blog](https://www.plex.tv/blog/important-2025-plex-updates/)) Remote Watch Pass renews at $2.99/month or $29.99/year after 2026-06-01. ([plans](https://www.plex.tv/plans/))
- Plexamp is free with gapless, loudness levelling, smart playlists and stations, and Android Auto and CarPlay. Plex Pass unlocks downloads, sonically similar artists, tracks and albums, Sonic Sage, Guest DJ, and mix builders. ([plexamp page](https://www.plex.tv/plexamp/))
- Proprietary. I found no ListenBrainz support, OIDC or Prometheus endpoint in the pages I read (unverified).

### F7. Other servers

- **gonic** (2,647 stars, GPL-3.0, v0.23.0 2026-10-22, 25 commits/90d). Subsonic server, listed as an OpenSubsonic participant. Multi-user, Last.fm and ListenBrainz scrobbling, Last.fm similar artists, folder browsing, podcasts, jukebox. SQLite file. Its web UI only handles configuration, not playback. No smart playlists, no plugins, no proxy-header auth, no Prometheus (only an expvar `/debug/vars`). Image `sentriz/gonic`. ([README](https://github.com/sentriz/gonic), [OpenSubsonic](https://opensubsonic.netlify.app/docs/))
- **Ampache** (3,843 stars, AGPL-3.0, 8.3.0 2026-10-20). PHP. Ampache API plus Subsonic/OpenSubsonic. Smartlists. Plugins include Last.fm, Libre.fm, ListenBrainz and AudioMuse. Auth methods `mysql,ldap,http,pam,external,oidc`, with OIDC in the 8.3.0 config. MySQL/MariaDB only. No Prometheus found. Image `ampache/ampache`. Substreamer notes that Ampache needs legacy auth for Subsonic clients. ([README](https://github.com/ampache/ampache), [config 8.3.0](https://github.com/ampache/ampache/blob/8.3.0/config/ampache.cfg.php.dist), [plugins](https://github.com/ampache/ampache/tree/8.3.0/src/Plugin), [Substreamer](https://github.com/ghenry22/substreamer))
- **Funkwhale** (GitLab, 12 GitLab stars, 2.0.13 2026-11-01, 100+ commits/90d). Federated (ActivityPub) audio publishing, compatible with Subsonic clients. Last.fm, ListenBrainz and Maloja scrobbling as contrib plugins. Radios, follows. LDAP only, no OIDC found. Stack: Postgres, Redis, Celery worker and beat, API, front, Typesense. ([README](https://dev.funkwhale.audio/funkwhale/funkwhale), [compose](https://dev.funkwhale.audio/funkwhale/funkwhale/-/blob/develop/deploy/docker-compose.yml), [settings](https://dev.funkwhale.audio/funkwhale/funkwhale/-/blob/develop/api/config/settings/common.py))
- **Lyrion Music Server** (1,814 stars, Perl, mostly GPL-2.0). A server for Squeezebox hardware and software players. Stable feed shows 9.1.2 (build 2026-10-24) while the getting-started page shows v9.1.1. SQLite by default, MySQL possible. Plugin system with a built-in AudioScrobbler (Last.fm) plugin. No Subsonic API. Image `lmscommunity/lyrionmusicserver`. Per-user accounts not found in the docs I read (unverified). ([site](https://lyrion.org/), [DB](https://lyrion.org/reference/database-structure/), [plugins dir](https://github.com/LMS-Community/slimserver/tree/public/9.1/Slim/Plugin), [License](https://github.com/LMS-Community/slimserver/blob/public/9.2/License.txt))
- **Koel** (17,332 stars, MIT, v9.15.0 2026-09-28). Laravel/PHP web player. Subsonic/OpenSubsonic API since v9.5.0 (2026-05-29). Smart playlists, Last.fm and ListenBrainz scrobbling. MySQL, MariaDB, PostgreSQL or SQLite. OIDC SSO (Authentik named) and proxy-header auth are **Koel Plus only**, pay-what-you-want from €19.99 once. Image `phanan/koel`. ([Subsonic docs](https://github.com/koel/koel/blob/master/docs/usage/subsonic.md), [v9.5.0](https://github.com/koel/koel/releases/tag/v9.5.0), [Plus](https://github.com/koel/koel/blob/master/docs/plus/what-is-koel-plus.md), [SSO](https://github.com/koel/koel/blob/master/docs/plus/sso.md), [price](https://docs.koel.dev/plus/purchase-activation), [integrations](https://github.com/koel/koel/blob/master/docs/service-integrations.md))
- **Polaris** (2,735 stars, MIT, Rust, 0.16.1 2026-05-08, 8 commits/90d). Own API and apps, no Subsonic. Multi-user. Since 0.15 it stores config and playlists in files, not a database. 0.15 removed Last.fm. ([README](https://github.com/agersant/polaris), [CHANGELOG](https://github.com/agersant/polaris/blob/master/CHANGELOG.md))
- **LMS, Lightweight Music Server** (`epoupon/lms`, 1,697 stars, GPL-3.0, C++, v3.81.0 2026-09-18, 54 commits/90d). Subsonic/OpenSubsonic. ListenBrainz scrobbling plus listen and love sync, Last.fm scrobbling. Its own recommendation engine (audio-similarity by default, or tag-based) and a radio mode. Auth backends include `http-headers` for SSO proxies. SQLite. Image `epoupon/lms`. ([README](https://github.com/epoupon/lms), [INSTALL](https://github.com/epoupon/lms/blob/master/INSTALL.md))
- **Newer 2025-2026 names found through topic pages:** Swing Music (2,088 stars, AGPL-3.0, v3.0.0 2026-06-12) has its own API, Daily Mixes, Last.fm scrobbling, multi-user and its own Android app. ([repo](https://github.com/swingmx/swingmusic)) Songloft (2,109 stars, created 2026-02, Apache-2.0) is Chinese-first, uses JS plugins and its own Flutter clients, with no Subsonic or scrobbling in the English README. ([repo](https://github.com/songloft-org/songloft)) AudioMuse-AI-MusicServer (57 stars) and MiniMediaSonicServer (7 stars) are small. Music Assistant (3,278 stars) plays to home speakers rather than serving phone apps. ([repo](https://github.com/music-assistant/server))

### F8. Is a dedicated server worth it when Jellyfin exists?

Pros of adding Navidrome:

1. Client choice. Navidrome's app directory lists about 100 apps ([catalog source](https://github.com/navidrome/website/tree/master/assets/apps)). The Subsonic/OpenSubsonic family covers every platform, and with the Jellyfin API Finamp and Jellify still work (F2).
2. Scrobbling and similar-song lookups are built in and per user. On Jellyfin 12 they depend on plugins built for 10.11 (F5).
3. Smart playlists, ListenBrainz playlist import and sonic similarity work today (F1, F3).
4. Isolation. A Jellyfin 12 upgrade that asks you to remove plugins does not touch music. Music scans do not load the video server.
5. Small. One Go binary with SQLite. Navidrome's docs claim it runs on a Pi Zero (F1). I have no measured numbers for this cluster.
6. Fits the existing stack: Traefik forward-auth with Authentik for the web UI, and a Prometheus endpoint for the existing Grafana.

Cons:

1. One more service to patch, back up and monitor.
2. Two user stores. There is no OIDC. Header auth covers the web UI, but mobile apps on `/rest` and `/jellyfin` use Navidrome passwords (F1).
3. SQLite only, so it cannot use the shared CloudNativePG cluster. It needs its own PV and backup.
4. If music is also added to Jellyfin, the library is scanned twice and play counts are split.
5. Jellyfin 12 is improving for music (bundled ListenBrainz similar artists, new similarity hooks), so the gap may shrink.

On balance it is worth it, given how much the owner cares about scrobbling and discovery.

## Comparison: servers

Features. "Ext auth" means reverse-proxy header login.

| Server | Subsonic / OpenSubsonic | Smart playlists | Multi-user | ListenBrainz scrobble | Last.fm scrobble | Plugins | Ext auth / OIDC | DB | Prometheus |
|---|---|---|---|---|---|---|---|---|---|
| Navidrome 0.64 | Yes / yes, plus Jellyfin API (exp.) | Yes (beta) | Yes | Built in | Built in | Yes, WASM | Header / no | SQLite | Yes |
| Jellyfin 12 | No (Jellyfin API) | Plugin | Yes | Plugin (ABI 10.11) | Plugin fork (ABI 10.11) | Yes | LDAP plugin; SSO plugin archived | SQLite (unverified for 12) | Yes |
| Plex + Plexamp | No | Yes (Plexamp) | Yes, plex.tv accounts | Not found | Not checked | No | plex.tv only | Not checked | Not found |
| gonic | Yes / yes | No | Yes | Built in | Built in | No | No | SQLite | No (expvar) |
| Ampache 8.3 | Yes / yes | Yes | Yes | Plugin, bundled | Plugin, bundled | Yes | http, LDAP, PAM, OIDC | MySQL/MariaDB | Not found |
| Funkwhale 2.0 | Subsonic yes | Radios with filters | Yes | Contrib plugin | Contrib plugin | Yes | LDAP / no | Postgres + Redis | Not found |
| Lyrion 9.1 | No | Not checked | Unverified | Third-party (unverified) | Built-in plugin | Yes | No | SQLite (MySQL opt.) | Not found |
| Koel 9.15 | Yes / yes (since 9.5) | Yes | Yes | Built in | Built in | No | Plus only (€19.99+) | MySQL/MariaDB/Postgres/SQLite | Not found |
| Polaris 0.16 | No | No | Yes | No | Removed in 0.15 | No | Not found | Files | Not found |
| LMS (epoupon) 3.81 | Yes / yes | Tag filters | Yes | Built in, with sync | Built in | No | `http-headers`, PAM | SQLite | Not found |
| Swing Music 3.0 | No | Not checked | Yes | Not found | Built in | No | Not found | Not checked | Not found |

Project health on 2026-11-04 (UTC):

| Server | License | Stars | Latest release | Commits 90d | Image |
|---|---|---|---|---|---|
| Navidrome | GPL-3.0 | 24,514 | v0.64.2, 2026-09-24 | 100+ | `navidrome/navidrome` |
| Jellyfin | GPL-2.0 | 58,992 | v12.4, 2026-10-26 | 100+ | `jellyfin/jellyfin` |
| Plex | Proprietary | n/a (pms-docker 4,046) | Not checked | n/a | `plexinc/pms-docker` |
| gonic | GPL-3.0 | 2,647 | v0.23.0, 2026-10-22 | 25 | `sentriz/gonic` |
| Ampache | AGPL-3.0 | 3,843 | 8.3.0, 2026-10-20 | 100+ | `ampache/ampache` |
| Funkwhale | AGPL-3.0 ([LICENSE](https://dev.funkwhale.audio/funkwhale/funkwhale/-/blob/develop/LICENSE)) | 12 on GitLab | 2.0.13, 2026-11-01 | 100+ | `funkwhale/api`, `funkwhale/front` |
| Lyrion | GPL-2.0, mixed | 1,814 | 9.1.2 feed, 2026-10-24 | 100+ | `lmscommunity/lyrionmusicserver` |
| Koel | MIT | 17,332 | v9.15.0, 2026-09-28 | 100+ | `phanan/koel` |
| Polaris | MIT | 2,735 | 0.16.1, 2026-05-08 | 8 | via `agersant/polaris-docker` |
| LMS (epoupon) | GPL-3.0 | 1,697 | v3.81.0, 2026-09-18 | 54 | `epoupon/lms` |
| Swing Music | AGPL-3.0 | 2,088 | v3.0.0, 2026-06-12 | 8 | `ghcr.io/swingmx/swingmusic` |

Resource use: only Jellyfin is measured here (557 MiB idle, F5). Navidrome and gonic only claim to run on Raspberry Pi class hardware (F1, F7). Funkwhale needs at least 7 containers (F7). AudioMuse-AI asks for 8 GB RAM (F4).

## Comparison: clients

"API" is the server API the app uses. "?" means not stated in the sources I read.

Android:

| App | API | Price | Offline | Gapless | ReplayGain | Car | Discovery notes | Source |
|---|---|---|---|---|---|---|---|---|
| Symfonium | OpenSubsonic, Jellyfin, Plex, Emby, Kodi, Audiobookshelf, cloud | Free trial, one-time purchase (amount unverified), closed source | Yes | Yes | Yes | Android Auto, Wear OS | Personal and instant mixes, smart playlists, connects to Navidrome AudioMuse endpoints | [site](https://symfonium.app/), [ND catalog](https://github.com/navidrome/website/blob/master/assets/apps/symfonium/index.yaml) |
| Tempus | OpenSubsonic | Free, GPL-3.0 (1,408 stars, v4.31.0 2026-10-25) | Yes, "may have limitations" | Yes | Yes | Android Auto, GitHub build only | Instant Mix via `getSimilarSongs`; LB/Last.fm via server | [README](https://github.com/eddyizm/tempus) |
| Tempo | OpenSubsonic | Free | Yes | Yes | ? | Android Auto | Unmaintained: last release 3.9.0, 2024-12-31; Tempus is the fork | [repo](https://github.com/CappielloAntonio/tempo) |
| Ultrasonic | Subsonic | Free (GitLab, 4.9.0 2026-03-10) | Yes | ? | ? | Android Auto | None noted | [README](https://gitlab.com/ultrasonic/ultrasonic), [ND catalog](https://github.com/navidrome/website/blob/master/assets/apps/ultrasonic/index.yaml) |
| Substreamer | OpenSubsonic | Free, open source (393 stars, v8.1.9) | Yes | ? | Server transcode preset only | ? | AudioMuse "play more like this"; local listening stats | [README](https://github.com/ghenry22/substreamer) |
| Finamp | Jellyfin (Navidrome 0.64 works, not actively supported) | Free, MPL-2.0 (4,491 stars, 1.0.2) | Yes | Yes | Yes | Android Auto (Play Store build), CarPlay | Radio of related tracks | [README](https://github.com/finamp-app/finamp) |
| Jellify | Jellyfin | Free, MIT | Yes | Yes | ? | Android Auto, CarPlay | Instant Mix, similar artists | [README](https://github.com/Jellify-Music/App) |
| Plexamp | Plex | Free, Plex Pass for downloads and sonic features | Plex Pass | Yes | Loudness levelling | Android Auto, CarPlay | Sonic similarity, Guest DJ (Pass) | [plexamp](https://www.plex.tv/plexamp/) |

iOS. App Store price checked through the iTunes lookup API and store pages on 2026-11-04.

| App | API | Price | Offline | Gapless | ReplayGain | CarPlay | Discovery notes | Source |
|---|---|---|---|---|---|---|---|---|
| Amperfy | Ampache, Subsonic/OpenSubsonic | Free, GPL-3.0 (v2.1.1) | Yes | Yes | Yes | Yes | Scrobbling | [README](https://github.com/BLeeEZ/amperfy), [App Store](https://apps.apple.com/app/amperfy-music/id1530145038) |
| Arpeggi | OpenSubsonic | Free, closed source (1.6.0) | Yes | Yes | Yes | Yes | Song and Artist Station modes; offline scrobble queue | [App Store](https://apps.apple.com/us/app/arpeggi/id6503619183) |
| play:Sub | Subsonic API ≥ 1.8 | $4.99 | Yes | ? | ? | Yes | Artist radio and similar artists (server dependent); Last.fm via server | [App Store](https://apps.apple.com/us/app/play-sub-subsonic-music-streamer/id955329386) |
| Narjo | Navidrome/Subsonic, Jellyfin, Emby, Plex | Free + IAP: Pro $1.99/month, Lifetime $29.99 | Yes | Yes | Yes | Yes | Smart playlist downloads; which features need Pro is unverified | [App Store](https://apps.apple.com/us/app/narjo/id6748359006) |
| Nautiline | OpenSubsonic | Free + IAP $10.00, closed source | Yes | Yes | Yes | Yes | None noted | [App Store](https://apps.apple.com/app/id6755918140) |
| Substreamer | OpenSubsonic | Free | Yes | ? | ? | ? | AudioMuse, listening stats | [App Store](https://apps.apple.com/us/app/substreamer/id1012991665) |
| Finamp | Jellyfin | Free | Yes | Yes | Yes | Yes | Radio | [App Store](https://apps.apple.com/us/app/finamp/id1574922594) |
| Jellify | Jellyfin | Free | Yes | Yes | ? | Yes | Instant Mix, similar artists | [App Store](https://apps.apple.com/us/app/jellify/id6736884612) |
| Manet | Jellyfin (assumed) | Unverified | Unverified | Unverified | Unverified | Unverified | Not in Jellyfin's client list | Unverified |

Desktop and web:

| App | API | Platforms | Price | Gapless | ReplayGain | Notes | Source |
|---|---|---|---|---|---|---|---|
| Feishin (Sonixd rewrite) | Navidrome, Jellyfin, OpenSubsonic | Windows, macOS, Linux, web, Docker `ghcr.io/jeffvli/feishin` | Free, GPL-3.0 (10,327 stars, v1.17.0) | Yes, mpv | Yes | Navidrome smart playlist editor, lyrics, scrobble to server | [README](https://github.com/jeffvli/feishin), [i18n strings](https://github.com/jeffvli/feishin/blob/development/src/i18n/locales/en.json) |
| Supersonic | Subsonic/OpenSubsonic, Jellyfin | Windows, macOS, Linux | Free, GPL-3.0 (2,414 stars, v0.22.1) | Yes, mpv | Yes | Artist radio; no offline mode yet | [README](https://github.com/supersonic-app/supersonic) |
| Aonsoku | OpenSubsonic | Desktop, web, Docker | Free, MIT (979 stars, v0.15.0) | ? | ? | Podcasts, LRCLIB lyrics | [ND catalog](https://github.com/navidrome/website/blob/master/assets/apps/aonsoku/index.yaml) |
| ampcast | Navidrome and others | Desktop, web, Docker | Free | Yes | Yes | Scrobbles to Last.fm and ListenBrainz itself | [ND catalog](https://github.com/navidrome/website/blob/master/assets/apps/ampcast/index.yaml) |
| Navidrome web UI | Built in | Browser | Free | ? | ? | Smart playlists listed; no rule editor | [overview](https://www.navidrome.org/docs/overview/) |
| Airsonic Refix | OpenSubsonic | Web, Docker | Free | ? | ? | Light web player | [ND catalog](https://github.com/navidrome/website/blob/master/assets/apps/airsonic-refix/index.yaml) |

## Discovery features

| Feature | Where |
|---|---|
| Scrobble to ListenBrainz and Last.fm per user, built in | Navidrome, gonic, LMS, Koel; Ampache and Funkwhale through bundled plugins (F1, F7) |
| Similar artists and similar or top songs from Last.fm, ListenBrainz, Deezer | Navidrome agents (F1); gonic from Last.fm (F7); Jellyfin 12 from ListenBrainz, artists only (F5) |
| Import ListenBrainz Daily Jams, Weekly Jams, Weekly Exploration | Navidrome plugin, owned tracks only (F3); Jellyfin LB plugin and SmartLists (F5) |
| Download recommended tracks you do not own | Explo through Lidarr (F4) |
| Find users with similar taste | ListenBrainz similar-users (F4) |
| Sonic similarity, offline | AudioMuse-AI with Navidrome or Jellyfin plugins (F3, F4); LMS built-in engine (F7); Plexamp with Plex Pass (F6) |
| Client radio and mixes from server similarity | Tempus Instant Mix, Supersonic artist radio, Arpeggi stations, play:Sub artist radio, Symfonium mixes, Finamp radio, Jellify Instant Mix |

## Recommendation

Ranked:

1. **Navidrome 0.64.x, separate from Jellyfin.** It ranks best on client choice, built-in scrobbling, discovery plugins, auth fit and metrics (F1 to F3, F8). Suggested setup:
   - A1. Deployment in `servarr` with a local PV for `/data` and the Lidarr library mounted read-only at `/music`. Turn on `Backup.*` to a second path.
   - A2. `ND_PROMETHEUS_ENABLED=true` and a ServiceMonitor. Import Grafana dashboard 24397.
   - A3. Web UI behind the existing `authentik-forwardauth` middleware with `ND_EXTAUTH_TRUSTEDSOURCES` set to the Traefik pod CIDR and `ND_EXTAUTH_USERHEADER=X-authentik-username`. Separate routes for `/rest/` and `/jellyfin/` without forward-auth, but with a `headers` middleware that removes `X-authentik-username` (R2).
   - A4. Each user connects ListenBrainz, and Last.fm if wanted. The Last.fm API key and secret come from the secret store, not from manifests.
   - A5. Install the ListenBrainz daily playlist plugin. Add Explo with its Lidarr downloader later, after checking Lidarr writes MBID tags (R6).
   - A6. Optional later: AudioMuse-AI with the Navidrome plugin, if a node has AVX2 and 8 GB to spare (R5).
   - A7. Leave music out of Jellyfin, or add it only for TV clients, to avoid double scans and split play counts.
2. **LMS (epoupon)** is the runner-up. It has ListenBrainz scrobble, listen sync and love sync, plus its own recommendation engine and header auth, all built in. It loses on ecosystem size, plugins and metrics (F7).
3. **Jellyfin only.** Choose this if one service matters more than discovery. Install SmartLists and the ListenBrainz plugin once they are confirmed working on 12.x (F5).

Not recommended here: Plex (plex.tv account, paid features, F6), Funkwhale (federation focus, 7 containers), Ampache (MySQL only), Koel (SSO is paid, Subsonic only since May 2026), Polaris (no scrobbling), gonic (no smart playlists, plugins or metrics), Lyrion (built for Squeezebox players).

Clients:

- Android: **Symfonium** if a one-time purchase is fine, since it has the widest server support, Android Auto and mixes. **Tempus** if free and FOSS matter; take the GitHub build for Android Auto.
- iOS: **Amperfy** (free, FOSS, CarPlay). **Arpeggi** (free) for its stations. **Narjo** ($29.99 lifetime) if one app should cover Navidrome, Jellyfin and Plex.
- Desktop and web: **Feishin** on desktop, plus its Docker web build if a second web UI is wanted. It also edits Navidrome smart playlists. **Supersonic** if you want a lighter native app.
- Car: Symfonium, Tempus, Ultrasonic, Finamp and Jellify on Android Auto. Amperfy, Arpeggi, play:Sub, Narjo, Nautiline, Finamp and Jellify on CarPlay.

## Risks

- R1. Navidrome is SQLite only. It cannot use the shared Postgres. The database needs a backup job and a PV that survives node loss (F1).
- R2. Header spoofing. If `/rest/` or `/jellyfin/` skip forward-auth while `ExtAuth.TrustedSources` trusts Traefik, a client could send `X-authentik-username` itself and log in as anyone. Traefik's `authResponseHeaders` replaces the header only on routes that run forward-auth ([Traefik forwardauth](https://github.com/traefik/traefik/blob/master/docs/content/reference/routing-configuration/http/middlewares/forwardauth.md)). Remove it on bypass routes with a `headers` middleware set to `""` ([Traefik headers](https://github.com/traefik/traefik/blob/master/docs/content/reference/routing-configuration/http/middlewares/headers.md)). Navidrome ignores an empty header (F1).
- R3. Experimental parts. The Jellyfin API and smart playlists are marked experimental or beta. 0.64.0 re-encoded every ID, and future migrations may force client re-syncs (F2).
- R4. Third-party plugins. 0.64.0 broke plugins that used Extism HTTP. Pin plugin versions and recheck them after each Navidrome upgrade (F1).
- R5. AudioMuse-AI asks for Postgres 15, but the shared CNPG cluster runs 16 (`2-k3s/06.postgres/cluster/postgres-cluster.yaml`). It also needs AVX2 and 8 GB RAM. Run it on its own database or not at all (F4).
- R6. The ListenBrainz playlist plugin matches only by MBID. If Lidarr does not write MusicBrainz IDs into the tags, the imported playlists come out empty. I did not verify Lidarr's tag writing.
- R7. Symfonium, Arpeggi and Nautiline are closed source. Narjo's free tier limits are unverified.
- R8. Jellyfin 12 scrobbling plugins target ABI 10.11 and the SSO plugin is archived. Treat Jellyfin music scrobbling as unproven on 12.x (F5).

## Not verified

- Symfonium price. Narjo free versus Pro features. Manet entirely.
- Gapless and ReplayGain for Ultrasonic, Substreamer, play:Sub, Aonsoku and the Navidrome web UI.
- Jellyfin 12 compatibility of the ListenBrainz and Last.fm plugins. Jellyfin 12 DB backend.
- Plex ListenBrainz or Last.fm scrobbling, Plex DB, Plex metrics.
- Lyrion multi-user and ListenBrainz plugin.
- Measured RAM for Navidrome, gonic or LMS on this cluster.
- Newer projects that lack the GitHub topics I scanned, because SearXNG was down.
