# Music discovery: finding music from people with similar taste

Question: as of late 2026, which existing services and self-hosted tools find music and playlists from people with similar taste, and how do they feed a self-hosted Lidarr + music server stack automatically?

Answer up front: ListenBrainz is the only service that still offers taste matching between people through an open API. It has similar users, a user-to-user similarity score, and Weekly Exploration, a playlist built by collaborative filtering (recommendations taken from people who listen like you). Last.fm and Spotify no longer give this to new hobby apps. Last.fm has no similar-user API method, and Spotify cut recommendations in 2024, then cut access to other users' playlists in 2026. The smallest working stack for this repo is: Jellyfin (already running) + the Jellyfin ListenBrainz plugin for scrobbling and playlist sync + Explo to download each week's recommendations through Lidarr and create the playlist. Use digarr instead of Explo if you want to review picks before anything downloads, or want an explicit "artists from similar users" list. The main risk is ListenBrainz itself. It is overloaded in 2026, and on 2026-11-04 the newest weekly playlists I could find were from the week of 2026-10-19, so the last two weekly runs were missing (F2).

## Sources read

Read on 2026-11-04 (UTC). Star counts, latest release and commit counts come from the GitHub REST API on that date. "Commits 6m" means commits on the default branch since 2026-05-04, bots included. Web search ran through SearXNG. Most results were off-topic, so nearly every claim below comes from a project's own docs, code or blog, fetched directly.

## Taste-matching services

### F1. ListenBrainz has the features and the open API

- Similar users: `GET /1/user/{user}/similar-users` returns users with a similarity score "based on their listen history". `GET /1/user/{user}/similar-to/{other}` returns one user-to-user score, which is the compatibility number. Source: [LB core API](https://listenbrainz.readthedocs.io/en/latest/users/api/core.html). Live check: the similar-users endpoint answered HTTP 200 without a token.
- Similar users' listens as a feed: `GET /1/user/{user}/feed/events/listens/similar` ([social API](https://listenbrainz.readthedocs.io/en/latest/users/api/social.html)). Follow and unfollow endpoints exist on the same page.
- Recommendations: "ListenBrainz uses collaborative filtering to generate recording recommendations". Raw output is at `GET /1/cf/recommendation/user/{user}/recording`, marked experimental ([recommendation API](https://listenbrainz.readthedocs.io/en/latest/users/api/recommendation.html)).
- Playlists: Weekly Jams and Weekly Exploration every Monday. Daily Jams every morning, but only if you follow the `troi-bot` user. Artist and track similarity is rebuilt every Sunday ([data update intervals](https://listenbrainz.readthedocs.io/en/latest/general/data-update-intervals.html)). `GET /1/user/{user}/playlists/createdfor` lists them, and "Createdfor playlists are all public, so no Authorization is needed" ([core API](https://listenbrainz.readthedocs.io/en/latest/users/api/core.html)). So you can also read the recommendation playlists of a similar user.
- Troi, the engine behind these playlists and LB Radio, is at [metabrainz/troi-recommendation-playground](https://github.com/metabrainz/troi-recommendation-playground). Latest release v2026.09.01.0 (2026-09-01).
- LB Radio: `GET /1/explore/lb-radio?prompt=...&mode=easy|medium|hard` ([misc API](https://listenbrainz.readthedocs.io/en/latest/users/api/misc.html)). Without a token it returned HTTP 401 in my test. The docs do not say a token is needed, so treat anonymous access as unverified.
- Fresh releases: `GET /1/user/{user}/fresh_releases` and `GET /1/explore/fresh-releases/` (same page).
- Export: `POST /1/export/` builds a ZIP of your full history ([export API](https://listenbrainz.readthedocs.io/en/latest/users/api/export.html)). Public data dumps of all listens come out twice a month, with incremental dumps between them ([dumps](https://listenbrainz.readthedocs.io/en/latest/users/listenbrainz-dumps.html)).
- Since August 2026, ListenBrainz blocks clients that send no meaningful User-Agent. Navidrome and multi-scrobbler were hit and shipped fixes ([blog 2026-08-24](https://blog.metabrainz.org/2026/08/24/blocked-listenbrainz-services-user-agent-strings/)).

### F2. ListenBrainz reliability is weak in 2026

- The project wrote that "every other week your statistics, weekly playlists, and other features fail to generate for everyone". The Spark cluster crashes weekly, which breaks "stats, playlists, user similarity, unlinked listens, fresh releases". They are moving stats to ClickHouse. Listens themselves are not lost. The dev team is three people, and the project lost its founder in late February 2026 ([blog 2026-08-19](https://blog.metabrainz.org/2026/08/19/growing-pains-an-update-on-the-listenbrainz-service-status/)).
- In July 2026, LB Radio and the popularity endpoints were disabled to cut load. A wave of Last.fm imports caused queues of up to 484K listens ([blog 2026-07-06](https://blog.metabrainz.org/2026/07/06/current-listenbrainz-server-issues-high-load-part-2/)). I could not verify whether LB Radio is fully back.
- Live check on 2026-11-04: I looked at the public `createdfor` lists of four accounts that had listened in the last three days. The newest Weekly Jams and Weekly Exploration were for the week of 2026-10-19 (some duplicated). Nothing existed for 2026-10-26 or 2026-11-02. Daily Jams were current. `GET /1/status/service-status` showed weekly stats about 11 days old.

### F3. Last.fm: similar artists yes, similar users no

- The [API method list](https://www.last.fm/api) has `artist.getSimilar`, `track.getSimilar`, `tag.getSimilar` and `user.getTopArtists`/`getLovedTracks`/`getFriends`. It has no neighbours, compatibility (tasteometer) or personal-recommendation method.
- The website's neighbours and compatibility features: unverified. Last.fm served a JavaScript bot challenge to my fetch.
- [API terms](https://www.last.fm/api/tos): non-commercial use only, rate limits at Last.fm's discretion, attribution required. A free API key is enough for a homelab.
- Last.fm Pro costs $4.99 per month ([last.fm/pro](https://www.last.fm/pro)). It is not needed for the API.
- Lidarr's built-in Last.fm import lists only pull a user's top artists or albums, or a tag's top artists ([Lidarr source, LastFm](https://github.com/Lidarr/Lidarr/tree/develop/src/NzbDrone.Core/ImportLists/LastFm)). They do not pull recommendations.

### F4. Spotify: closed for this use case

- 2024-11-27: new apps lost Related Artists, Recommendations, Audio Features, featured and category playlists, and "Algorithmic and Spotify-owned editorial playlists" ([Spotify blog](https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api)).
- February and March 2026: Development Mode apps need an owner with Premium, and each app is limited to 5 users. `GET /playlists/{id}/items` now works only "for playlists the user owns or collaborates on". `GET /users/{id}/playlists`, artist top tracks and new releases were removed ([migration guide](https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide)). Extended Quota apps are not affected, but hobby apps rarely get that status.
- July 2026: a developer can now have 25 client IDs, and quota is counted per developer account ([changelog July 2026](https://developer.spotify.com/documentation/web-api/references/changes/july-2026)). From 2026-07-20, connections to Spotify expire every 6 months, so ListenBrainz users must reconnect ([MetaBrainz blog](https://blog.metabrainz.org/2026/07/19/spotify-users-will-need-to-reconnect-every-6-months-to-listenbrainz/)).
- Effect: Spotify still works as a source of your own history (export ZIP, F12). It cannot feed recommendations or other people's playlists into a new self-hosted tool. Lidify dropped Spotify for this reason ([Lidify README](https://github.com/TheWicklowWolf/Lidify)).

### F5. Deezer: similar artists without a key

Navidrome uses "Deezer's public API", which "doesn't require API keys", for similar artists and top songs ([Navidrome external services](https://www.navidrome.org/docs/usage/integration/external-services/)). Kima Hub and Aurral use it the same way. Personal feeds such as Flow need your own Deezer app ([digarr auth docs](https://github.com/iuliandita/digarr/blob/develop/docs/AUTHENTICATION.md)). Whether Deezer accepts new app registrations today: unverified. The portal requires a login.

### F6. MusicBrainz and Libre.fm

- MusicBrainz offers no taste matching. It is the ID system everything else depends on. ListenBrainz features "work best when your music files have MusicBrainz IDs" ([Navidrome docs](https://www.navidrome.org/docs/usage/integration/external-services/)). Lidarr writes MusicBrainz recording, release and artist IDs into tags (`AudioTagService.cs` in [Lidarr](https://github.com/Lidarr/Lidarr/blob/develop/src/NzbDrone.Core/MediaFiles/AudioTagService.cs)). Accounts are moving from musicbrainz.org to metabrainz.org ([blog 2026-06-25](https://blog.metabrainz.org/2026/06/25/upcoming-changes-to-user-accounts-and-authentication/)).
- Libre.fm is online. Its server software [gnu-fm](https://github.com/libre-fm/gnu-fm) had its last release in 2022 and 7 commits in 6 months. ListenBrainz can import Libre.fm history (F12). Libre.fm recommendation features: unverified.

### F7. Soulseek: interest-based similar users, manual only

- The protocol has `SimilarUsers` (code 110), `ItemSimilarUsers` (112), `Recommendations` (54) and `WishlistSearch` (103), none marked obsolete ([Nicotine+ protocol doc](https://github.com/nicotine-plus/nicotine-plus/blob/master/doc/SLSKPROTOCOL.md)). Similarity comes from "things I like" text entries that you type in, not from listen history ([Nicotine+ interests.py](https://github.com/nicotine-plus/nicotine-plus/blob/master/pynicotine/interests.py)).
- [Nicotine+](https://github.com/nicotine-plus/nicotine-plus) (GPL-3.0, 3,219 stars, 3.3.11 on 2026-09-16) shows interests and similar users. [slskd](https://github.com/slskd/slskd) can browse a user's shares. I found no interests feature in its README or config docs (unverified).
- Browsing the shares of someone who has a rare album you love is a real discovery method, but no tool automates it.

### F8. Funkwhale and the fediverse

[Funkwhale](https://dev.funkwhale.audio/funkwhale/funkwhale) 2.0.13 came out on 2026-11-01. It is active, hosted on GitLab, and has 12 GitLab stars. Its ListenBrainz plugin submits listens and syncs listens and favorites. It has no recommendation or similar-user logic ([plugin source](https://dev.funkwhale.audio/funkwhale/funkwhale/-/tree/develop/api/funkwhale_api/contrib/listenbrainz)). I did not verify the federation features for following other people's libraries.

### F9. Rate Your Music, Musicboard and others

rateyourmusic.com returned HTTP 403 to my fetch. I found no public API for RYM/Sonemic (unverified). Musicboard is a ratings and lists site ([musicboard.app](https://www.musicboard.app/)), API unverified. Neither has a self-hosted integration in any tool checked here. The real 2025-2026 change is on the self-hosted side: Explo, Aurral, digarr, Kima Hub, mixarr and AudioMuse-AI all grew or appeared in this period (table below), plus a large 2026 move of users from Last.fm to ListenBrainz (F2).

## Scrobbling and history

### F10. Server-side scrobbling covers phone apps automatically

- Jellyfin: [jellyfin-plugin-listenbrainz](https://github.com/lyarenei/jellyfin-plugin-listenbrainz) "sends the music activity on your Jellyfin server". Any Jellyfin client counts. It also syncs loves both ways, caches listens while ListenBrainz is down, backs listens up to local files, and "Sync[s] playlists generated by ListenBrainz into Jellyfin". Plugin 6.x supports Jellyfin 10.11 and 12. This repo runs `jellyfin/jellyfin:12.4` (`2-k3s/08.servarr/jellyfin/jellyfin.yaml`). The Jellyfin Last.fm plugin ([jesseward/jellyfin-plugin-lastfm](https://github.com/jesseward/jellyfin-plugin-lastfm)) is archived.
- Navidrome: built-in per-user scrobbling to ListenBrainz and Last.fm. `ListenBrainz.BaseURL` can point at Maloja or Koito ([Navidrome options](https://www.navidrome.org/docs/usage/configuration/options/)). Subsonic apps report plays through the `scrobble` call, and the server forwards them.
- Client-side scrobbling is only needed for players that bypass your server, such as Spotify on the go. [multi-scrobbler](https://github.com/FoxxMD/multi-scrobbler) handles that. It reads Jellyfin, Plex, Subsonic/Navidrome, Spotify, Deezer, Last.fm and ListenBrainz and writes to ListenBrainz, Last.fm, Koito and Maloja ([README](https://github.com/FoxxMD/multi-scrobbler)).

### F11. Local copy of your history

[Koito](https://github.com/gabehf/Koito) and [Maloja](https://github.com/krateng/maloja) store scrobbles on your own server. Both accept the ListenBrainz API. Koito can relay to ListenBrainz and calls itself "unstable" in its README. Maloja has "no social networking, radios, recommendations". Neither recommends anything, so both are optional backups.

### F12. Seeding history fast

ListenBrainz accepts uploads of a Spotify extended streaming history ZIP, a ListenBrainz ZIP, Libre.fm CSV, PanoScrobbler, Maloja, Audioscrobbler/Rockbox and Spinitron files. Last.fm and Libre.fm history is imported by connecting the account under Connect Services ([ImportListens.tsx](https://github.com/metabrainz/listenbrainz-server/blob/master/frontend/js/src/settings/import/ImportListens.tsx)). The live Spotify connection only sees the last 50 listens, hence the ZIP (same file). Imported old listens reach your statistics only after the next full rebuild: "allow up to two weeks". Listen counts update at once ([update intervals](https://listenbrainz.readthedocs.io/en/latest/general/data-update-intervals.html)).

## Self-hosted glue

### F13. Tools that turn ListenBrainz taste data into files and playlists

- [Explo](https://github.com/LumePart/Explo) fetches Weekly Exploration, Weekly Jams, Daily Jams and Fresh Releases. It also imports any ListenBrainz, Spotify or Apple Music playlist. It downloads through yt-dlp, slskd, or Lidarr (whole albums), then creates the playlist in Jellyfin, Emby, Plex, MPD or Subsonic servers ([wiki](https://github.com/LumePart/Explo/wiki)). Run it on Tuesdays, after Monday's generation ([FAQ](https://github.com/LumePart/Explo/wiki/8.-FAQ)). Lidarr mode skips an album after 20 minutes with no progress or 2 hours total, and can use a dedicated root folder and quality profile ([system notes](https://github.com/LumePart/Explo/wiki/6.-System-Notes)). Jellyfin needs real-time monitoring on. Explo is single-user per instance. It sends a User-Agent (`src/util/http.go`).
- [digarr](https://github.com/iuliandita/digarr) is the closest match to "people with the same likes". It has "Similar Users (Quick)" and "Similar Users (Deep): Discover from top artists of ListenBrainz users with similar taste" (`src/core/discovery-modes/modes/listenbrainz.ts`), plus LB weekly feeds and radio. Picks go to a review queue, then to Lidarr or slskd. Playlists export to Navidrome, Jellyfin, Emby, Plex or Spotify. Taste sources also include Last.fm, Spotify, Plex, Jellyfin, Subsonic and Discogs.
- [mixarr](https://github.com/aquantumofdonuts/mixarr) has scheduled subscriptions, including "ListenBrainz Similar Users' Artists" (`subscription-presets.ts`), Last.fm, Deezer, Spotify and Tidal. It sends to Lidarr only.
- [Aurral](https://github.com/lklynet/aurral) builds recommendations from similar artists (ListenBrainz, or Last.fm when a key is set), seeded by your history and library. "Flows" are scheduled playlists that download themselves. Lidarr is optional ([discover docs](https://github.com/lklynet/aurral/blob/main/docs/src/content/docs/using/discover.mdx)). It is artist-to-artist, not people-to-people. Its docs say ListenBrainz similar artists "lean toward well-known names".
- [Sonobarr](https://github.com/Dodelidoo-Labs/sonobarr) and [Lidify](https://github.com/TheWicklowWolf/Lidify) suggest Last.fm similar artists for artists you have in Lidarr. Sonobarr also reads LB Weekly Exploration.
- Playlist-only sync, no downloads: the [Navidrome LB playlist plugin](https://github.com/kgarner7/navidrome-listenbrainz-daily-playlist) imports Daily, Weekly and any LB playlist by ID, matching by MBID. The Jellyfin LB plugin syncs only your own LB-generated playlists, not user-created ones (F10). [octo-sync](https://github.com/m8tec/octo-sync) does it for Navidrome with low activity. listenbrainz-content-resolver is archived. [listenbrainz-local](https://github.com/metabrainz/listenbrainz-local) has had no commit since 2025-09-18.
- Library-only similarity, which finds nothing new: [AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) analyses the sound of your own files for Instant Mix and similar songs, with plugins for Navidrome and Jellyfin. [Kima Hub](https://github.com/Chevron7Locked/kima-hub) is a whole music server (OpenSubsonic) with sound similarity, LB/Deezer similar artists and weekly discovery through Lidarr or Soulseek. It is a music server itself, so it would sit beside or replace Jellyfin for music rather than plug into it.
- Download side: [Soularr](https://github.com/mrusse/soularr) searches slskd for Lidarr's wanted albums. [Tubifarry](https://github.com/TypNull/Tubifarry) is a Lidarr plugin that adds slskd and YouTube as indexer and download client. Lidarr plugins need the plugins/nightly branch ([Brainarr README](https://github.com/RicherTunes/Brainarr)).
- Lidarr itself has no ListenBrainz import list. The develop branch has Custom, HeadphonesImport, LastFm, Lidarr, LidarrLists, MusicBrainzSeries and Spotify only ([source tree](https://github.com/Lidarr/Lidarr/tree/develop/src/NzbDrone.Core/ImportLists)). Lidarr's metadata API had a long outage. Issue [#5498](https://github.com/Lidarr/Lidarr/issues/5498) (opened 2025-04-09) was still open on the read date.

### F14. Observability

- Prometheus endpoints: multi-scrobbler `/api/metrics`, plus `/api/health` that returns 500 when a source or client fails, plus a community Grafana dashboard ([config docs](https://docs.multi-scrobbler.app/configuration)). Also Navidrome (`ND_PROMETHEUS_ENABLED`), Jellyfin (`EnableMetrics`, [Startup.cs](https://github.com/jellyfin/jellyfin/blob/master/Jellyfin.Server/Startup.cs)), slskd (`/metrics`, off by default, [config](https://github.com/slskd/slskd/blob/master/docs/config.md)) and Kima Hub (`/api/metrics`, auth). For Lidarr, use [exportarr](https://github.com/onedr0p/exportarr).
- Health endpoint only: digarr (`/health`), Aurral, AudioMuse-AI (`/api/health`), Koito (`/health`).
- Neither: Explo has none (it sends notifications to Discord or an HTTP webhook through `nikoksr/notify`). I found nothing in Soularr or Sonobarr (code grep).

## Comparison

| Tool | Role | Connects to | License | Stars | Latest release | Commits 6m | Main maintainer | Image | Observability |
|---|---|---|---|---|---|---|---|---|---|
| ListenBrainz | taste matching, CF playlists | open API, dumps | GPL-2.0 server | 1,051 (server repo) | v-2026-10-28.0 | 500 | MetaBrainz, 3 devs (F2) | n/a (service) | Grafana queue dashboard, free account (F2) |
| [Explo](https://github.com/LumePart/Explo) | LB playlists → download → playlist | LB; yt-dlp, slskd, Lidarr; Jellyfin, Emby, Plex, Subsonic, MPD | MIT | 2,066 | v1.2.0, 2026-09-09 | 352 | LumePart | `ghcr.io/lumepart/explo`, Helm chart | notifications only |
| [digarr](https://github.com/iuliandita/digarr) | similar-user artists, review queue | LB, Last.fm, Spotify, Jellyfin, Plex, Subsonic; Lidarr, slskd; playlist export | MIT | 326 | v2.2.0, 2026-11-03 | 624 | iuliandita | `docker.io/iuliandita/digarr`, Helm chart | `/health` |
| [mixarr](https://github.com/aquantumofdonuts/mixarr) | subscriptions → Lidarr | LB, Last.fm, Deezer, Spotify, Tidal, Plex, Jellyfin | GPL-3.0 | 161 | v3.0.0, 2026-10-30 | 12 | aquantumofdonuts | `ghcr.io/aquantumofdonuts/mixarr` | compose healthcheck |
| [Aurral](https://github.com/lklynet/aurral) | similar-artist discovery, own library | LB or Last.fm; Lidarr optional; yt-dlp, slskd, Usenet | MIT | 1,882 | v2.13.0, 2026-11-03 | 1,523 | lklynet | `ghcr.io/lklynet/aurral` | health route |
| [Sonobarr](https://github.com/Dodelidoo-Labs/sonobarr) | Last.fm similar → Lidarr | Last.fm, LB weekly, Lidarr | MIT | 382 | 0.13.0, 2026-09-09 | 8 | smileBeda | `ghcr.io/dodelidoo-labs/sonobarr` | none found |
| [Lidify](https://github.com/TheWicklowWolf/Lidify) | Last.fm similar → Lidarr | Last.fm, Lidarr | MIT | 610 | v0.2.11, 2026-08-21 | 2 | TheWicklowWolf | `thewicklowwolf/lidify` | not checked |
| [Jellyfin LB plugin](https://github.com/lyarenei/jellyfin-plugin-listenbrainz) | scrobble + LB playlist sync | Jellyfin 10.11/12, LB | MIT | 222 | 6.5.3.4, 2026-08-27 | 17 | lyarenei | Jellyfin plugin repo | Jellyfin logs |
| [Navidrome LB playlists](https://github.com/kgarner7/navidrome-listenbrainz-daily-playlist) | LB playlist sync | Navidrome ≥ 0.63, LB | MIT | 294 | v6.0.0, 2026-07-08 | 5 | kgarner7 | `.ndp` plugin | Navidrome logs |
| [multi-scrobbler](https://github.com/FoxxMD/multi-scrobbler) | scrobble router | many sources → LB, Last.fm, Koito, Maloja | MIT | 1,299 | 0.20.3, 2026-10-27 | 1,314 | FoxxMD | `foxxmd/multi-scrobbler` | Prometheus, health, webhooks |
| [Koito](https://github.com/gabehf/Koito) | local scrobble store | LB API in, relay out | MIT | 1,124 | v0.3.2, 2026-06-01 | 47 | gabehf | `gabehf/koito` | `/health` |
| [Maloja](https://github.com/krateng/maloja) | local scrobble store | LB/GNU FM API in | GPL-3.0 | 1,856 | tag v3.2.9, 2026-10-16 (no GitHub release) | 20 | krateng | `krateng/maloja` | not checked |
| [AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) | sound similarity in own library | Navidrome, Jellyfin, Plex, Emby, LMS, Lyrion | AGPL-3.0 | 2,900 | v3.7.0, 2026-10-29 | 1,361 | NeptuneHub | `ghcr.io/neptunehub/audiomuse-ai`, Helm chart | `/api/health` |
| [Kima Hub](https://github.com/Chevron7Locked/kima-hub) | full music server with discovery | LB, Deezer, Lidarr, slskd, Subsonic apps | GPL-3.0 | 1,293 | v1.12.2, 2026-11-04 | 1,667 | Chevron7Locked | `chevron7locked/kima` | Prometheus, health |
| [Soularr](https://github.com/mrusse/soularr) | Lidarr wanted → slskd | Lidarr, slskd | GPL-3.0 | 1,008 | v1.2.3, 2026-10-22 | 25 | mrusse | `mrusse08/soularr` | none found |
| [Tubifarry](https://github.com/TypNull/Tubifarry) | Lidarr plugin: slskd, YouTube | Lidarr plugins branch | MIT | 1,080 | v2.3.1, 2026-11-04 | 71 | TypNull | Lidarr plugin | via Lidarr |
| [slskd](https://github.com/slskd/slskd) | Soulseek client | Soulseek | AGPL-3.0 | 4,246 | 0.26.2, 2026-10-23 | 596 | jpdillingham | `slskd/slskd` | Prometheus (opt-in) |
| [Lidarr](https://github.com/Lidarr/Lidarr) | music downloads | indexers, MusicBrainz | GPL-3.0 | 5,800 | v3.1.6.5079, 2026-10-26 | 66 | Lidarr team | several | exportarr (v2.3.0, 2025-08-12) |
| [Navidrome](https://github.com/navidrome/navidrome) | music server | Subsonic apps, LB, Last.fm, Deezer | GPL-3.0 | 24,514 | v0.64.2, 2026-09-24 | 398 | deluan | `deluan/navidrome` | Prometheus (opt-in) |

Left out on purpose: [Cmdarr](https://github.com/DeviantEng/Cmdarr) and [navispot](https://github.com/betsha1830/navispot) have no license file, so reuse rights are unclear. [Brainarr](https://github.com/RicherTunes/Brainarr) recommends through an LLM, not through people. [Trackhop](https://github.com/xlollx/Trackhop) is a playlist syncer, and an account named `claude` authored 443 of its commits. [spotify-to-plex](https://github.com/jjdenhertog/spotify-to-plex) is Plex only.

## Recommendation

### D1. Rank

1. **Jellyfin + Jellyfin LB plugin + Explo (Lidarr mode).** It is the smallest set and uses the server you already run. Explo has the most users (2,066 stars), a Helm chart, active development, and does the whole weekly loop. Weekly Exploration is ListenBrainz's collaborative-filtering output, which is "music from people who listen like you" in practice.
2. **Swap Explo for digarr** if automatic album grabs fill the library with noise, or if you want the explicit "top artists of similar users" list. digarr has a review queue, a Helm chart and a `/health` endpoint. The cost is a younger project with one main maintainer.
3. **Add Navidrome next to Jellyfin** only if you want Subsonic phone apps and AudioMuse-style mixes. Navidrome scrobbles natively, and the kgarner7 plugin syncs LB playlists. Skip it otherwise.

### D2. Full pipeline for option 1

1. Listen: Jellyfin with any Jellyfin client on phone and desktop. A client that skips the server, such as Spotify, goes through multi-scrobbler.
2. Scrobble: the Jellyfin LB plugin sends listens from the server, so client choice does not matter. Add multi-scrobbler later only for outside sources or a second target (Last.fm, Koito).
3. Seed: create a pseudonymous ListenBrainz account. Upload the Spotify extended streaming history ZIP and/or connect Last.fm. Follow `troi-bot` for Daily Jams. Expect stats and playlists after the next weekly run, or up to two weeks for old imports.
4. Recommend: ListenBrainz builds Weekly Exploration and Weekly Jams on Monday. `similar-users` gives the people. Their public `createdfor` and own playlists can be imported by ID through Explo's ListenBrainz playlist import.
5. Download: Explo runs Tuesday and asks Lidarr for the albums. Use a separate Lidarr root folder and quality profile for discovery, so you can prune it. Lidarr fetches through the existing Prowlarr and qBittorrent setup. slskd + Soularr or Tubifarry are optional extra sources.
6. Playlist: Explo creates the weekly playlist in Jellyfin. The LB plugin also syncs LB-generated playlists for tracks already in the library.
7. Observe: Jellyfin `EnableMetrics` and exportarr for Lidarr go into the existing Prometheus in `2-k3s/10.observability`. Send Explo notifications to the existing ntfy through its HTTP webhook. If you add multi-scrobbler, scrape its `/api/metrics` and alert on `/api/health`.

### D3. Why

- It reuses Jellyfin and the *arr layout already in `2-k3s/08.servarr/`. That means one new pod (Explo), one Jellyfin plugin and Lidarr.
- ListenBrainz is the only taste-matching source with an open API, free use and data you can export (F1, F3, F4).
- Server-side scrobbling means phone apps need no setup (F10).

### Risks

- R1. ListenBrainz misses weekly runs (F2). Explo then finds no new playlist and does nothing. Mitigation: digarr's similar-users mode reads other data and gives a second path. Watch Explo notifications.
- R2. Public listening data. ListenBrainz says listens are "made available to the public". It offers no private mode, only account deletion ([MetaBrainz GDPR page](https://metabrainz.org/gdpr)). Listens appear in public dumps (F1), and createdfor playlists are public. Mitigation: a username unrelated to your real name or other accounts, a separate email address, and delete single listens with `POST /1/delete-listen` when needed. Last.fm profiles are public too. Its private-listening setting is unverified.
- R3. Library growth. Lidarr mode downloads whole albums for single recommended tracks (Explo system notes). Mitigation: a separate root folder and profile, and periodic pruning.
- R4. MBID matching. Playlist sync matches by MusicBrainz ID. Lidarr writes MBIDs into tags, but files from yt-dlp or Soulseek may lack them, so those tracks drop out of synced playlists.
- R5. Lidarr metadata outages (issue #5498) block adding new artists. Every Lidarr-based tool here fails while it lasts.
- R6. Maintainer concentration. Explo, digarr and the Jellyfin LB plugin each depend mostly on one person (commit counts in the table).
- R7. Jellyfin real-time monitoring, which Explo needs, may not see changes on network storage. I did not test this on this cluster.
- R8. Soulseek and YouTube downloads carry legal risk that depends on the country. Soulseek also shows your shares to other users. Keep slskd behind the existing VPN pattern, or skip it.
- R9. Spotify-based tools break when Spotify's policy changes. This happened in 2024 and again in 2026 (F4). Use Spotify only as a history export.

## Not verified

- Last.fm website neighbours, compatibility and private-listening settings (the bot challenge blocked my fetch).
- Whether anonymous LB Radio API access is back, and whether the October-November 2026 weekly playlist gap is site-wide. I checked four accounts.
- RYM/Sonemic and Musicboard API status. Deezer new-app availability. Libre.fm recommendation features. Funkwhale library-following features.
- slskd support for Soulseek interests and similar users.
- Lidify and Maloja observability (not inspected).
- Exact Lidarr setting names for writing tags. I verified the code path only.
