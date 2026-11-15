# Discord media: subtitle source

## Intent

Family members in `#media` want to know whether a subtitle, usually the Greek one, was translated by Lingarr or downloaded from a subtitle provider.

## Goals and non-goals

- Goal: the `#media` agent answers "was this subtitle Lingarr translated?" for a movie or an episode.
- Non-goal: reading media or subtitle files, judging translation quality, calling Lingarr.
- Anti-goal: new network access, new keys or a new image for this.

## Constraints

- The agent already calls Bazarr with `$BAZARR_API_KEY`. No new egress or secret.
- Bazarr history records every subtitle event. Verified live on 2026-11-14 from the `discord-media` pod:
  - A Lingarr translation is `action` 6 with description "English subtitles translated to Greek using Lingarr.", plus `language` and `subtitles_path`.
  - A download is `action` 1, an upgrade `action` 3, both with `provider`. A manual upload is `action` 4.
  - `GET /movies/history?radarrid=<id>` and `GET /episodes/history?episodeid=<sonarrEpisodeId>` return only that item's records, newest first.

## Approaches

1. Bazarr history API (picked). Already reachable, already records the source per subtitle.
2. Read-only media mount. Rejected: a Lingarr `.el.srt` and a downloaded `.el.srt` look the same on disk.
3. Lingarr API. Rejected: needs a new NetworkPolicy rule and another key in the pod for the same answer.

## Design

- Add the two history endpoints and the `action` meanings to the Bazarr section of `2-k3s/35.discord/media/AGENTS.md`. The agent reads the newest record for the asked language and names the source. For an action code not listed above, it reads `description` and checks older records. With no record, it says the source is unknown.
- Pin the endpoints in `2-k3s/35.discord/tools/manifests.test.py`.

## Left to the builder

Exact wording in `AGENTS.md`.
