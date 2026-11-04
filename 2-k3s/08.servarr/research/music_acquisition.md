# Music acquisition: how to find and download music automatically (late 2026)

Question: what is the best way, as of November 2026, to find and download music automatically in this homelab, feeding a music server? The `servarr` namespace already has Prowlarr, qBittorrent behind a VPN, Jellyfin, Seerr, Configarr, Authentik and a shared Postgres. There is no music stack yet.

Answer up front: run Lidarr. It is still the only maintained Servarr app for music, it uses the Prowlarr and qBittorrent we already run, and it can store its data in our shared Postgres. Lidarr's weak point is its closed, central metadata server, which was broken or degraded from May to October 2025 (F2). It answered correctly when I tested it on 2026-11-04. Torrents alone may not find much music, so plan a second source: slskd (Soulseek), connected to Lidarr with Soularr, a separate container that fits GitOps. The Tubifarry plugin does the same job with more features, but you install it from Lidarr's UI. Seerr still has no music support (F11). If you want a request and discovery UI on top, Aurral is the best fit: it works with Lidarr, is very active, and documents both native OIDC and Authentik forward auth (F13). Jellyfin can play the music, so a new player is optional.

## How I gathered this

- Numbers like stars, releases and commits come from the GitHub REST API. I read them on 2026-11-04 between 12:50 and 13:15 UTC, by GitHub's clock. "Commits 6m" means commits on the default branch since 2026-05-04. "Active maintainers" means non-bot authors with 3 or more commits in that window. That is my definition, not GitHub's.
- Image architectures come from the registry manifest lists (ghcr.io and Docker Hub), read on the same day.
- SearXNG did not work. Every engine (brave, duckduckgo, google cse, startpage) returned rate-limit or CAPTCHA errors on all queries, including retries about 20 minutes apart. So this note has no web search results and no forum or Reddit opinion. To find projects I used GitHub repository search instead. A project that is popular but not on GitHub could be missing.
- I checked the cluster (read-only) and found that all nodes are amd64. arm64 support is listed for completeness.

## F1. Lidarr release state

- The v3 line is current. v3.0.1.4866 (stable, 2025-10-30) moved to .NET 8 and removed Basic auth. The only login choices are now Forms and External, the reverse-proxy method ([release v3.0.1](https://github.com/Lidarr/Lidarr/releases/tag/v3.0.1.4866), [settings wiki](https://wiki.servarr.com/lidarr/settings)).
- Latest stable: v3.1.6.5079, 2026-10-26. Latest pre-release: v3.2.0.5144, also 2026-10-26 ([releases](https://github.com/Lidarr/Lidarr/releases)). The update API for the master branch lists 3.1.6.5079 as current ([update feed](https://lidarr.servarr.com/v1/update/master/changes?os=linux&runtime=netcore&arch=x64)).
- Activity: 66 commits in 6 months, from mynameisbogdan (32), markus101 (14) and augustuen (5). The repo has 5,800 stars and 1,063 open issues ([repo](https://github.com/Lidarr/Lidarr)). Development continues, but with a small team.
- License GPL-3.0. Images: `ghcr.io/linuxserver/lidarr` tags `latest`, `develop` and `nightly`, plus `ghcr.io/hotio/lidarr:release`, all published for amd64 and arm64.
- Observability: GitHub code search finds no Prometheus code in Lidarr. exportarr (MIT, v2.3.0, 2025-08-12, last push 2026-06-11) exports Lidarr metrics ([exportarr](https://github.com/onedr0p/exportarr)).

## F2. The metadata server outage, 2025 to 2026

Lidarr does not query MusicBrainz directly. It calls `api.lidarr.audio`, a central server run by the Lidarr team. Its code is open ([LidarrAPI.Metadata](https://github.com/Lidarr/LidarrAPI.Metadata), last push 2025-07-09), but the running cache is not, because it holds API keys ([issue #5498 comments](https://github.com/Lidarr/Lidarr/issues/5498)). The timeline below comes from maintainer comments in issue #5498:

- 2025-04-09: searches return HTTP 500. Fixed 2025-04-17.
- 2025-05-21: a MusicBrainz schema change breaks all searches. The team first tries an in-place fix, then rebuilds the server.
- 2025-07-25: alpha test of the new server, closed to users.
- 2025-08-17: some traffic moves to the new server. Multi-CD albums break for a while.
- 2025-09-15: 100% of traffic goes to the new server, still rate limited (HTTP 503).
- 2025-10-05: "MOSTLY back". Spotify lists are disabled. Issue closed.
- 2026-01-05: issue reopened. Text search is "functionally broken", ID search works better, images are only partly fixed, and Spotify lists work only for artists that have a Spotify link in MusicBrainz. The issue is still open and locked, with no update since that day.

State now:
- On 2026-11-04 at 13:02 UTC I ran three text searches against `https://api.lidarr.audio/api/v0.4/search?type=artist`. "radiohead", "pink floyd" and "daft punk" each returned the correct artist first, in about 0.2 s. That is a spot check, not a health history.
- I searched Lidarr issues opened from May to November 2026 for metadata or LidarrAPI. None reports an outage. In [#5733](https://github.com/Lidarr/Lidarr/issues/5733) (2026-05-25) a maintainer says the API is fine and the missing album is a Blu-ray release type that Lidarr does not support.
- During the outage, the community built replacement metadata servers: [hearring-aid](https://github.com/blampe/hearring-aid) (496 stars, no releases, 8 commits in 6 months, points to the hosted `api.musicinfo.pro`), [nuLMD](https://github.com/ofernander/nuLMD) (20 stars, v5.0.0, 2026-10-21) and BrainzMash ([statichum/brainzmash-hearring-aid](https://github.com/statichum/brainzmash-hearring-aid), which Aurral uses). Tubifarry can point Lidarr at a custom metadata server.

## F3. Plugins, and the `plugins` branch

- Plugin support is now in Lidarr's main code. The commit "New: Plugin support" (`fcfc60a`) first shipped in v3.1.2.4938 (pre-release, 2026-03-30) ([release notes](https://github.com/Lidarr/Lidarr/releases/tag/v3.1.2.4938)).
- The GitHub compare API shows that the stable `master` tag v3.1.6.5079 includes `fcfc60a`, and its sidebar has a "Plugins" entry (`frontend/src/Components/Page/Sidebar/PageSidebar.js` at that tag). The [plugins wiki page](https://wiki.servarr.com/lidarr/plugins) still says "the master branch doesn't support plugins yet" and tells users to run develop or nightly. That page was last edited 2026-10-10, 16 days before 3.1.6.5079 reached master, so it is probably out of date. I did not run a master build to confirm that the Plugins page works there: unverified.
- The old `plugins` branch was last changed on 2026-01-18 and is 79 commits behind `develop`. Do not use it.
- You install a plugin by pasting a GitHub URL into System > Plugins. Lidarr checks for plugin updates only when you open that page, and you must restart after an install or update ([wiki](https://wiki.servarr.com/lidarr/plugins)). The plugin files live in Lidarr's `/config`, not in Git.

## F4. Postgres

- Supported since Lidarr v1.1.2.2890. The wiki says Npgsql 9.x supports PostgreSQL 14 to 18. Lidarr's built-in backup does not include Postgres data ([postgres wiki](https://wiki.servarr.com/lidarr/postgres-setup)).
- The wiki's advice: SQLite is fine for most libraries. Postgres helps when long tasks and imports block each other ("database is locked", "Failed writing to database"). Postgres does not speed up metadata lookups or file imports.
- You configure it with `PostgresHost`, `PostgresPort`, `PostgresUser`, `PostgresPassword`, `PostgresMainDb` and `PostgresLogDb` in `config.xml`. Create both databases first. v3.1.3 added a Postgres connection string option ([v3.1.6 notes](https://github.com/Lidarr/Lidarr/releases/tag/v3.1.6.5079)). Our Sonarr manifest sets the same keys through `SONARR__POSTGRES__*` environment variables. I expect `LIDARR__POSTGRES__*` to work the same way, but I did not find it on the Lidarr wiki: unverified.

## F5. Prowlarr and qBittorrent

- Prowlarr syncs indexers to Lidarr by category. An indexer that claims only TV categories is not synced to Lidarr ([Prowlarr settings wiki](https://wiki.servarr.com/prowlarr/settings)).
- Lidarr's own indexer types include Torznab, Newznab, Gazelle API, Redacted, FileList, Nyaa and others ([supported wiki](https://wiki.servarr.com/lidarr/supported)).
- qBittorrent fixes since v3.1.3: login on qBittorrent 5.2, API key support for qBittorrent, and auth cookie reuse ([v3.1.6 notes](https://github.com/Lidarr/Lidarr/releases/tag/v3.1.6.5079)). We run qBittorrent 5.3.0.
- Unverified: whether our current Prowlarr indexers carry music categories. I did not query Prowlarr. How much music Lidarr finds on torrents depends on this.

## F6. Import lists

Built-in lists, from the [supported wiki](https://wiki.servarr.com/lidarr/supported): Custom List, Headphones, Last.fm Tag, Last.fm User, Lidarr, Lidarr Lists, MusicBrainz Series, Spotify Followed Artists, Spotify Playlists and Spotify Saved Albums.

- There is no built-in ListenBrainz list. ListenBrainz comes in through outside tools: Explo, Sonobarr, Aurral, Digarr, DroppedNeedle (F13, F14) and the small [recommendarr](https://github.com/Shadowalker125/recommendarr) (last push 2025-03-11).
- Spotify lists go through Lidarr's metadata and Spotify proxy. They were switched off during the 2025 outage and only partly work since 2026-01-05 (F2).

## F7. Profiles and config as code

- Lidarr has quality profiles, metadata profiles (primary types, secondary types, release statuses) and custom formats. The old "community guide" now lives in the wiki pages on naming, custom formats and metadata profiles ([community guide](https://wiki.servarr.com/lidarr/community-guide)).
- Configarr supports Lidarr as "experimental" since v1.8.0, with metadata profiles since v1.19.0. There are no TRaSH presets for Lidarr, so you write your own templates and custom formats ([Configarr experimental support](https://configarr.de/docs/configuration/experimental-support)). Configarr latest is v1.39.0 (2026-11-03). We pin 1.38.0.
- Recyclarr supports only Sonarr and Radarr ([README](https://github.com/recyclarr/recyclarr)).

## F8. Soulseek through slskd

- [slskd](https://github.com/slskd/slskd): AGPL-3.0, 4,246 stars, 0.26.2 (2026-10-23), 596 commits in 6 months. One person writes almost all of it (jpdillingham, 553 commits). Images for amd64, arm and arm64.
- It stores data in SQLite. It has an optional Prometheus `/metrics` endpoint, off by default and protected with basic auth by default ([config docs](https://github.com/slskd/slskd/blob/master/docs/config.md)). It has local login and API keys, but no OIDC.
- VPN: slskd does not route its own traffic. Its VPN feature only polls Gluetun for status and the forwarded port. Without a listen port, searches return fewer results ([config docs](https://github.com/slskd/slskd/blob/master/docs/config.md)).
- You must share files. The DroppedNeedle and SoulSync READMEs both warn that the network bans clients that share nothing. Sharing means uploading to strangers.
- Three ways to connect slskd to Lidarr:
  1. [Soularr](https://github.com/mrusse/soularr): a Python container that reads Lidarr's wanted list, searches slskd and tells Lidarr to import. GPL-3.0, 1,008 stars, v1.2.3 (2026-10-22), 25 commits in 6 months, 2 active maintainers. amd64 and arm64. Needs no Lidarr plugin. The config file holds both API keys.
  2. [Tubifarry](https://github.com/TypNull/Tubifarry) plugin with its slskd indexer and download client (F9).
  3. [allquiet-hub/Lidarr.Plugin.Slskd](https://github.com/allquiet-hub/Lidarr.Plugin.Slskd): 64 stars, v1.1.7.0 (2026-10-27), one author. The release notes say it requires Lidarr nightly and slskd 0.26.0 or newer. The repo has no license file.

## F9. Tubifarry

- MIT, 1,080 stars. v2.3.1 (2026-11-04) needs Lidarr 3.2.0.0 or newer, which today exists only as a pre-release. v2.3.0.1 (2026-10-12) needs 3.1.6.0, so it runs on current stable ([releases](https://github.com/TypNull/Tubifarry/releases)).
- 71 commits in 6 months, 64 of them by one author (Meyn).
- Features, per the [README](https://github.com/TypNull/Tubifarry): YouTube downloads, slskd indexer and client, "web clients" (Lucida, DABmusic, T2Tunes, Subsonic), Streamrip and Invidious clients (added in 2026), a Sonarr and Radarr soundtrack import list, a queue cleaner, format conversion, lyrics, a missing-album "Search Sniper", and extra metadata sources (Discogs, Deezer, Last.fm).
- Legal: YouTube downloads and the "web clients" take audio from services that do not license it for download. The README itself says YouTube "actively detects and blocks automated downloaders" and asks for cookies to get around this.

## F10. Streaming-service rippers

These tools save audio from paid streaming accounts as files. A subscription pays for streaming, not for keeping files, so this most likely breaks the services' terms of use. I did not read each service's terms for this note.

- [TrevTV Tidal](https://github.com/TrevTV/Lidarr.Plugin.Tidal) (last release 2026-01-17) and [Qobuz](https://github.com/TrevTV/Lidarr.Plugin.Qobuz) (last release 2025-11-22) plugins: 0 commits in 6 months, no license file. The wiki points to a [Deezer fork](https://github.com/jasonpatrickellykrause/Lidarr.Plugin.Deezer) (0 stars, 10.2.0.57, 2026-10-12).
- [streamrip](https://github.com/nathom/streamrip): GPL-3.0, 5,115 stars, v2.2.0 (2026-03-12), 0 commits on `dev` in 6 months.
- [deemix (bambanah fork)](https://github.com/bambanah/deemix): GPL-3.0, 1,279 stars, active. [lidarr-on-steroids](https://github.com/youegraillot/lidarr-on-steroids): last push 2024-11-04, dead.
- [Tidarr](https://github.com/cstaelen/tidarr): MIT, 724 stars, 1.2.9 (2026-10-27), 100 commits in 6 months, native OIDC, amd64 and arm64.
- [Octo-Fiesta](https://github.com/V1ck3s/octo-fiesta): GPL-3.0, 897 stars, v0.11 (2026-09-02), 98 commits in 6 months. A Subsonic proxy in front of Navidrome. When a song is not in the library, it downloads it from Deezer, Qobuz, Tidal or Yandex. Its free SquidWTF backend is deprecated.

## F11. Seerr and music

- Seerr still has no music support. The feature request [#96](https://github.com/seerr-team/seerr/issues/96) has been open since 2022. A maintainer wrote on 2026-08-01 that it will not get a milestone until the work is close to ready.
- Maintainers asked for the music work to land in pieces on a separate `music` branch. One PR is merged there ([#3109](https://github.com/seerr-team/seerr/pull/3109), Lidarr settings, 2026-06-08). That branch has had no commits since and is 157 commits behind `develop`. MusicBrainz, ListenBrainz and Cover Art Archive provider PRs (#3110, #3123, #3116) are still open. Release v3.5.1 (2026-10-21) has no music.
- Forks that add music: [Allseerr](https://github.com/DanCue44/Allseerr) (12 stars, amd64 image only) and [seerrng](https://github.com/snapetech-org/seerrng) (92 stars, 2,738 commits in 6 months, with commit authors "Codex" and "codex"). Both are young and depend on one person each.

## F12. DroppedNeedle, the former MusicSeerr

- `github.com/HabiRabbu/musicseerr` redirects to [DroppedNeedle/DroppedNeedle](https://github.com/DroppedNeedle/DroppedNeedle), so this is the "Musicseerr" app under a new name. Repo created 2025-10-10. AGPL-3.0, 1,542 stars, v2.15.0 (2026-09-20), 455 commits in 6 months, 424 of them by one author.
- It is a request, discovery and player app with its own download engine. It does not use Lidarr. Its sources are slskd and Usenet (SABnzbd, optionally through Prowlarr). There is no torrent support in the core; a third-party [torrent plugin](https://github.com/alphyriver/droppedneedle-torrent-plugin) has 2 stars.
- SQLite. Login with OIDC, Jellyfin or Plex. Image `droppedneedle/droppedneedle` for amd64 and arm64. No Prometheus code found by GitHub code search.

## F13. Aurral

- [lklynet/aurral](https://github.com/lklynet/aurral): MIT, 1,882 stars, v2.13.0 (2026-11-03), 1,523 commits in 6 months. lklynet wrote 1,300 of them; 10 other authors wrote 3 or more each.
- Discovery, request and library app. Lidarr is optional: when connected, Lidarr owns the artists Aurral adds. Download sources: yt-dlp, slskd, Usenet, Tidarr and deemix. It can apply the quality profile, custom formats and naming from "Davo's Community Lidarr Guide" to Lidarr ([Lidarr integration docs](https://docs.aurral.org/integrations/lidarr/)).
- Login: native OIDC (`OIDC_*` variables) and reverse-proxy auth with `AUTH_PROXY_TRUSTED_IPS`. It has a written Authentik forward-auth setup, including the `/outpost.goauthentik.io/sign_out` logout ([users docs](https://docs.aurral.org/admin/users/), [environment docs](https://docs.aurral.org/admin/environment/)).
- SQLite (`better-sqlite3` in `backend/package.json`). Image `ghcr.io/lklynet/aurral` for amd64 and arm64. No Prometheus code found.
- Artist and album metadata come from BrainzMash, a hosted service that the docs say "runs on the maintainer's own infrastructure and can be unavailable at times" ([metadata docs](https://docs.aurral.org/integrations/metadata/)). That is a second central metadata dependency, next to Lidarr's.

## F14. Discovery add-ons for Lidarr

- [Explo](https://github.com/LumePart/Explo): MIT, 2,066 stars, v1.2.0 (2026-09-09), 352 commits in 6 months, 1 main maintainer plus 7 others with 3 or more commits. A ListenBrainz "Discover Weekly": it downloads the tracks through Lidarr, slskd or YouTube and makes playlists in Jellyfin, Emby, Plex, MPD or Subsonic. It has an official Helm chart. I found no login settings in `sample.env`, so put it behind forward auth.
- [Sonobarr](https://github.com/Dodelidoo-Labs/sonobarr): MIT, 382 stars, 0.13.0 (2026-09-09), 8 commits in 6 months, one maintainer. Last.fm and ListenBrainz suggestions sent to Lidarr, with request approval. SQLite, native OIDC.
- [Lidify](https://github.com/TheWicklowWolf/Lidify): MIT, 610 stars, v0.2.11 (2026-08-21), 2 commits in 6 months. Its Spotify mode has been broken since November 2024, so it runs on Last.fm only. No login found in the README.
- [Digarr](https://github.com/iuliandita/digarr): MIT, 326 stars, v2.2.0 (2026-11-03), one maintainer. AI suggestions sent to Lidarr or slskd. Embedded PGlite or external PostgreSQL 15+, OIDC.
- [Mixarr](https://github.com/aquantumofdonuts/mixarr): GPL-3.0, 161 stars, v3.0.0 (2026-10-30).

## F15. All-in-one replacements

- [SoulSync](https://github.com/Nezreka/SoulSync): MIT, 2,347 stars, 3.6.2 (2026-11-04). 7,542 commits in 6 months; the authors include "claude" and "Claude". It aims to replace the *arr apps for music, movies, TV and YouTube. Sources: slskd, Tidal, Qobuz, Deezer, YouTube, Lidarr, and torrents or Usenet through Prowlarr. SQLite. OIDC plus forward auth from Authentik. amd64 and arm64.
- [Soulbeet](https://github.com/terry90/soulbeet): MIT, 666 stars, v0.8.0 (2026-10-28), one maintainer. slskd plus beets, built for Navidrome. SQLite.

## F16. Taggers and legacy tools

- [beets](https://github.com/beetbox/beets): MIT, 15,891 stars, v2.14.1 (2026-09-17), 1,271 commits in 6 months and many contributors. A command-line tagger and library organizer, not a downloader. Soulbeet and Tubifarry (since v2.3.0.1) can run it.
- [MusicBrainz Picard](https://github.com/metabrainz/picard): GPL-2.0, 5,351 stars, release-3.0.4 (2026-11-01). A desktop GUI tagger; it does not fit a headless pipeline.
- [Headphones](https://github.com/rembo10/headphones): GPL-3.0, 3,762 stars, v0.6.4 (2025-07-01), last push 2025-08-08, 0 commits in 6 months. Dormant.

## F17. The player

- Lidarr can tell Jellyfin to rescan its music library after an import (Emby / Jellyfin notification, [supported wiki](https://wiki.servarr.com/lidarr/supported)). So our Jellyfin can serve music without a new app.
- [Navidrome](https://github.com/navidrome/navidrome) (GPL-3.0, 24,514 stars, v0.64.2, 2026-09-24) is the usual Subsonic server. It has a Prometheus endpoint (`ND_PROMETHEUS_ENABLED`) and reverse-proxy auth (`ND_EXTAUTH_TRUSTEDSOURCES`) ([options](https://www.navidrome.org/docs/usage/configuration/options/)). You need it only for Subsonic apps, Octo-Fiesta or Soulbeet.

## Comparison

Read 2026-11-04 UTC. Stars and commits from the GitHub API. "Maint." uses my definition above. "Metrics: none found" means GitHub code search found no Prometheus code.

| Tool | Role | License | Stars | Latest release | Commits 6m | Maint. | amd64/arm64 | DB | Metrics | Authentik fit |
|-|-|-|-|-|-|-|-|-|-|-|
| Lidarr | Music *arr | GPL-3.0 | 5,800 | v3.1.6.5079, 2026-10-26 | 66 | 3 | yes/yes | SQLite or Postgres | via exportarr | forward auth (External) |
| slskd | Soulseek client | AGPL-3.0 | 4,246 | 0.26.2, 2026-10-23 | 596 | 1 (+2 small) | yes/yes | SQLite | native `/metrics` | forward auth; API key for apps |
| Soularr | Lidarr to slskd bridge | GPL-3.0 | 1,008 | v1.2.3, 2026-10-22 | 25 | 2 | yes/yes | none (config file) | none found | forward auth on optional web UI |
| Tubifarry | Lidarr plugin | MIT | 1,080 | v2.3.1, 2026-11-04 | 71 | 1 | runs inside Lidarr | Lidarr's | none found | n/a |
| Lidarr.Plugin.Slskd | Lidarr plugin | none | 64 | v1.1.7.0, 2026-10-27 | 76 | 1 | runs inside Lidarr | Lidarr's | unverified | n/a |
| Aurral | Discovery, requests, library | MIT | 1,882 | v2.13.0, 2026-11-03 | 1,523 | 1 main + 10 | yes/yes | SQLite | none found | native OIDC or forward auth (documented) |
| DroppedNeedle | Requests and downloads, no Lidarr | AGPL-3.0 | 1,542 | v2.15.0, 2026-09-20 | 455 | 1 (+2 small) | yes/yes | SQLite | none found | native OIDC |
| Explo | ListenBrainz weekly playlists | MIT | 2,066 | v1.2.0, 2026-09-09 | 352 | 1 main + 7 | yes/yes | none found | none found | forward auth |
| Sonobarr | Discovery to Lidarr | MIT | 382 | 0.13.0, 2026-09-09 | 8 | 1 | yes/yes | SQLite | none found | native OIDC |
| Lidify | Discovery to Lidarr | MIT | 610 | v0.2.11, 2026-08-21 | 2 | 0 | yes/yes | unverified | none found | forward auth |
| Digarr | AI discovery | MIT | 326 | v2.2.0, 2026-11-03 | 624 | 1 | yes/yes | PGlite or Postgres 15+ | unverified | native OIDC |
| SoulSync | All-in-one replacement | MIT | 2,347 | 3.6.2, 2026-11-04 | 7,542 | many, AI-assisted | yes/yes | SQLite | unverified | native OIDC or forward auth |
| Soulbeet | slskd + beets | MIT | 666 | v0.8.0, 2026-10-28 | 58 | 1 | yes/yes | SQLite | unverified | unverified |
| Octo-Fiesta | Subsonic proxy ripper | GPL-3.0 | 897 | v0.11, 2026-09-02 | 98 | 1 (+4 small) | yes/yes | unverified | none found | Subsonic auth |
| Tidarr | Tidal ripper | MIT | 724 | 1.2.9, 2026-10-27 | 100 | 1 (+2 small) | yes/yes | unverified | unverified | native OIDC |
| streamrip | CLI ripper | GPL-3.0 | 5,115 | v2.2.0, 2026-03-12 | 0 | 0 | no official image checked | n/a | n/a | n/a |
| Headphones | Legacy music manager | GPL-3.0 | 3,762 | v0.6.4, 2025-07-01 | 0 | 0 | yes/yes (linuxserver) | SQLite (unverified) | none found | forward auth |
| beets | Tagger | MIT | 15,891 | v2.14.1, 2026-09-17 | 1,271 | many | yes/yes (linuxserver) | SQLite | n/a | n/a |
| Seerr upstream | Requests | MIT | 13,104 | v3.5.1, 2026-10-21 | 202 | 6 | n/a | SQLite or Postgres | n/a | no music support |

## Recommendation, ranked

1. **Lidarr on stable v3.1.6, in `servarr`.** Use the linuxserver image like Sonarr and Radarr, store its data in the shared Postgres (main and log databases), add it as an app in Prowlarr, and use qBittorrent with its own `music` category as the download client. Add the Jellyfin notification. Put the web UI behind the same Traefik `authentik-forwardauth` middleware as Sonarr, with Lidarr's authentication method set to External in `config.xml` (the UI dropdown does not offer it, per the [settings wiki](https://wiki.servarr.com/lidarr/settings)). Manage profiles with the Configarr we already run, using our own templates, since there are no presets. Use exportarr for metrics. Why: it reuses Prowlarr, qBittorrent, Postgres, Configarr and Authentik as they are, and nothing else in the Servarr family does music.
2. **slskd plus Soularr, as a second source.** Deploy slskd with a VPN sidecar and a forwarded listen port, like qBittorrent. Share one read-only music folder, and let Soularr feed Lidarr's wanted list to slskd. Why Soularr over Tubifarry: it is a normal container with a config file, so it lives in Git and runs on stable Lidarr. Tubifarry is the stronger choice if you also want soundtracks from Sonarr and Radarr, the missing-album search scheduler or lyrics, but you install it from the UI and its newest release follows Lidarr pre-releases (F9). Do this step only if torrent results turn out thin; check our Prowlarr music indexers first (R8).
3. **Aurral as the music request and discovery UI, optional.** Connect it to Lidarr, so Lidarr stays the only downloader, and log in through Authentik with native OIDC or the documented forward auth. Why: it is the most active option that works with Lidarr and Authentik, and it imports ListenBrainz, Last.fm and Spotify playlists. Explo is a smaller choice if all you want is a weekly ListenBrainz playlist in Jellyfin.

Not recommended now:
- Seerr music: it has not shipped (F11).
- DroppedNeedle: it skips Lidarr and torrents, so it would not use our Prowlarr and qBittorrent. Pick it only if you decide to drop Lidarr and run slskd or Usenet instead.
- SoulSync: it duplicates the whole *arr stack we already run, and 7,542 commits in 6 months is a lot of churn to run against.
- Streaming rippers (Tidal, Qobuz, Deezer plugins, Tidarr, streamrip, Octo-Fiesta): terms-of-use and legal risk (F10), and the TrevTV plugins are stale.
- Headphones, Lidify and lidarr-on-steroids: dormant or broken.

## Risks

- **R1. Central metadata.** Lidarr cannot add artists or albums when `api.lidarr.audio` is down. In 2025 it was broken or degraded for about five months (F2). The only fallbacks are third-party servers (F2) or running your own MusicBrainz mirror, which is heavy. Aurral adds a second single point of failure (BrainzMash).
- **R2. Legal.** Most music on public torrents and on Soulseek is shared without a license. On Soulseek you must share files, which means you upload too. Ripping from streaming services most likely breaks their terms. Legal sources: your own rips, and Bandcamp purchases through the [Bandcamp plugin](https://wiki.servarr.com/lidarr/plugins), which downloads only albums you bought.
- **R3. Soulseek over VPN.** slskd does not route through a VPN by itself, and its VPN feature knows only Gluetun. It needs the same sidecar setup as qBittorrent plus a forwarded port. I have not checked how much work that is with our VPN image.
- **R4. One-person projects.** slskd, Tubifarry, Aurral, DroppedNeedle and most add-ons depend on one main developer. Lidarr itself has about three active committers and 1,063 open issues.
- **R5. Plugins outside Git.** Lidarr plugins install at runtime into `/config`, update only by hand from the UI, and need a restart. The wiki and git history disagree on whether master supports plugins (F3).
- **R6. Configarr support for Lidarr is experimental.** There are no presets, quality definitions are not evaluated, and support "could be dropped in the future" ([docs](https://configarr.de/docs/configuration/experimental-support)).
- **R7. Observability gaps.** Lidarr needs exportarr for metrics. Soularr, Aurral, Explo and the plugins have no Prometheus endpoint I could find, so we can only watch them through logs and health checks.
- **R8. Indexer coverage unknown.** I did not check which of our Prowlarr indexers carry music categories. If none do, Lidarr will find little, and step 2 (slskd) is where the music actually comes from.

## Not verified

- No web search results, because SearXNG failed on every query. Community opinion and non-GitHub projects are missing.
- Whether Lidarr master 3.1.6.5079 shows a working Plugins page (F3).
- `LIDARR__POSTGRES__*` environment variable names (F4).
- Music categories on our Prowlarr indexers (F5, R8).
- Database engine, metrics and login for the rows marked "unverified" in the table.
- The exact terms of use of each streaming service (F10).
