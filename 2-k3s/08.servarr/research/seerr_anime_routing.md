# Seerr anime routing: which Sonarr instance gets a TV request

Question: with Sonarr id 0 (`isDefault=true, is4k=false`) and Sonarr id 1 (anime, `isDefault=false, is4k=false`, `activeAnimeProfileId` set), why do anime TV requests land on id 0, and can settings alone send them to id 1?

Answer up front: the source selects the Sonarr instance with `isDefault && is4k === request.is4k` only. Anime never influences instance selection. It only changes what is sent inside whichever instance was selected (profile, root folder, series type, language profile, tags). With our settings every non-4k TV request lands on id 0, which matches the live FLCL test. No setting routes anime to id 1; the only supported path is a per-request `serverId` override.

## Sources read

Repo: `github.com/seerr-team/seerr` (this is the renamed `Fallenbagel/jellyseerr`; the old URL redirects there). Current develop head at read time: `2759058aeb01248beae841fd450f7e73ea8d95e3`.

- F0a. There is no `preview-OIDC` branch. The image tag tracks PR [seerr-team/seerr#2715](https://github.com/seerr-team/seerr/pull/2715), "feat: initial support for OpenID Connect authentication" (open, milestone v3.5.0, head `feat/oidc-login-basic`). Its 35 changed files are auth, docs, and UI only. `server/subscriber/MediaRequestSubscriber.ts`, `server/entity/MediaRequest.ts`, and `server/routes/request.ts` are untouched, so routing in the preview image equals develop and equals release v3.4.1 (blob verified identical logic; see F2 note).
- F0b. Caveat: I verified develop and the v3.4.1 tag. I did not diff older jellyseerr 2.x tags. The selection pattern below is the one in every version checked.

## F1. Where instance selection happens

Request creation does not pick an instance. `POST /api/v1/request` (`server/routes/request.ts`) calls `MediaRequest.request()`, which stores the caller's value verbatim (`server/entity/MediaRequest.ts`, develop):

```ts
serverId: requestBody.serverId,
```

The body field is `serverId` (`server/interfaces/api/requestInterfaces.ts`, `MediaRequestBody`). Omitted, it stays `null`. That is the "no serviceId override" case from the live test.

The actual selection runs when the request becomes APPROVED (manual approve, retry, or auto-approve on insert). The TypeORM subscriber `server/subscriber/MediaRequestSubscriber.ts` fires `afterInsert`/`afterUpdate` and calls `sendToSonarr()`:

```ts
let sonarrSettings = settings.sonarr.find(
  (sonarr) => sonarr.isDefault && sonarr.is4k === entity.is4k
);

if (
  entity.serverId !== null &&
  entity.serverId >= 0 &&
  sonarrSettings?.id !== entity.serverId
) {
  sonarrSettings = settings.sonarr.find(
    (sonarr) => sonarr.id === entity.serverId
  );
  logger.info(`Request has an override server: ${sonarrSettings?.name}`, ...);
}
```

That is the whole routing decision. If no instance matches, it logs "There is no default ... Sonarr server configured" and returns; the request stays approved but nothing is sent.

## F2. Roles of `isDefault` and `is4k`

- `isDefault` picks the one instance that receives requests with no explicit `serverId`. There is one default per 4k class.
- `is4k` partitions that lookup: the instance's `is4k` flag must equal the request's `is4k`. A non-4k request looks for `isDefault && is4k=false`; a 4k request for `isDefault && is4k=true`. This is why the old trick worked: marking the anime instance `is4k=true, isDefault=true` made it the target for 4k requests. It also flips `series4kEnabled` on globally (`server/lib/settings/index.ts`, `fullPublicSettings`), showing the 4K request button to everyone.
- A `serverId` override bypasses the default lookup entirely. It even works when no default exists, because `sonarrSettings?.id !== entity.serverId` is true when the default lookup found nothing.
- v3.4.1 has the identical selection lines (checked the `v3.4.1` blob of the same file). Only the anime-overlay trigger differs slightly: v3.4.1 applies overlays when `seriesType === 'anime'`; develop computes `const isAnime = series.keywords.results.some((keyword) => keyword.id === ANIME_KEYWORD_ID)` and gates overlays on `isAnime`, with a comment that seriesType "must not gate anime routing".

## F3. How a series counts as anime

Keyword, not genre. `server/api/themoviedb/constants.ts`:

```ts
export const ANIME_KEYWORD_ID = 210024;
```

In `sendToSonarr` (develop):

```ts
const isAnime = series.keywords.results.some(
  (keyword) => keyword.id === ANIME_KEYWORD_ID
);
```

TMDB keyword 210024 is "anime". Genre "Animation" (id 16) is not consulted in the request path, so FLCL classifies as anime through its keyword. The same keyword test appears in `MediaRequest.request()` for override-rule handling.

## F4. What anime then changes: overlays, not routing

Still in `sendToSonarr`, on the already-selected instance:

```ts
let rootFolder =
  isAnime && sonarrSettings.activeAnimeDirectory
    ? sonarrSettings.activeAnimeDirectory
    : sonarrSettings.activeDirectory;
let qualityProfile =
  isAnime && sonarrSettings.activeAnimeProfileId
    ? sonarrSettings.activeAnimeProfileId
    : sonarrSettings.activeProfileId;
```

Plus `seriesType` (`animeSeriesType`), language profile (`activeAnimeLanguageProfileId`), and tags (`animeTags`). `activeAnimeProfileId` and `activeAnimeDirectory` are per-instance overlays (`SonarrSettings` in `server/lib/settings/index.ts`) applied only when that instance was selected. They never cause selection.

Override Rules (`OverrideRule`) cannot substitute for routing either. They are fetched only for the default service, they can set `rootFolder`, `profileId`, and `tags` but have no server field, and anime TV requests skip them unless the rule explicitly includes keyword 210024 (`server/entity/MediaRequest.ts`). They also only apply to users without `MANAGE_REQUESTS`.

## D1. Prediction for our current settings

- id 0: `isDefault=true, is4k=false`. id 1: `isDefault=false, is4k=false`.
- Any non-4k TV request, anime or not, matches only id 0. id 1 is unreachable unless the request carries `serverId=1`.
- The source therefore predicts exactly the observed behavior: FLCL lands on id 0 with profile WEB-2160p and root `/media/tvshows`. The anime profile on id 1 is dead configuration as long as id 1 is not default and requests carry no `serverId`.

## D2. What would send anime to id 1

1. No settings-only change does it. The selection line has no anime term, and neither `activeAnime*` nor Override Rules can move a request between instances.
2. The only supported path today is a per-request `serverId: 1` in the request body (API) or picking the Anime instance in the request dialog's advanced options (UI). Manual, per request.
3. The `is4k=true, isDefault=true` hack on id 1 (requests submitted as 4k) is the only settings-level trick that redirects a whole class, and it is the one documented as broken for us: Delete Media cascades and availability tracking treat the 4k service as a separate lane, so orphans accumulate. Not recommended, which is why we removed it.
4. Upstream has an unmerged branch `fallenbagel/feat/routing-rules`, so content-based routing is on the roadmap. It is not in develop and not in PR 2715, so nothing to deploy yet.

## A1. Supported alternative that achieves the practical goal

Put the anime overlays on the default instance instead:

- On Sonarr id 0 set `activeAnimeProfileId` = "Remux-1080p - Anime" and `activeAnimeDirectory` = `/media/animes` (keep `activeProfileId` = WEB-2160p and `activeDirectory` = `/media/tvshows`).

Result: every request still goes to id 0, anime ones automatically get the Remux-1080p - Anime profile and `/media/animes` root folder (F4 code path), everything else keeps WEB-2160p and `/media/tvshows`. No `is4k` flag is touched on either instance. id 1 stops receiving requests and can be retired unless you keep it for a separate reason (different download clients, independent pruning).

Trade-off to know: one Sonarr instance then owns both libraries, so instance-level separation (per-instance settings, stats, maintenance windows) goes away. If that separation is the point of id 1, per-request `serverId` overrides are the only current answer.
