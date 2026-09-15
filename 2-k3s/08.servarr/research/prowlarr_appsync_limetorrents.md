# Prowlarr app-sync AddIndexer 400 loop: LimeTorrents

Question (#1032): which indexer ids do the `AddIndexer` 400s belong to, and per indexer, should the category set be widened in Prowlarr or should the indexer be excluded from the app?

Answer up front: every 400 belongs to LimeTorrents (Prowlarr indexer id 14). The limetorrents Cardigann definition parses the latest-releases page (an empty-term search) with no standard Torznab categories, so the app-side add test finds zero releases in any app category and Radarr/Sonarr refuse the save with 400. Widening categories cannot fix rows that parse with no category at all, so the decision is exclusion. Radarr has been excluded in effect since 2026-09-12, when the Radarr app got the `movies` tag that LimeTorrents lacks; the 400s stopped that hour. Sonarr keeps its LimeTorrents row because term searches through it return categorized results; only its RSS is dead.

## F1. The 400s are all LimeTorrents

In Prowlarr's `AddIndexer` flow, the app tests the new indexer with an empty-term search before saving. The `ReleaseSearchService` line naming the indexer and category set lands immediately before each 400:

- sonarr2 era (Aug 26-27, `prowlarr.11.txt` to `prowlarr.15.txt`): 11 of 11 `sonarr2:8989/api/v3/indexer?forceSave=true` 400s are preceded by `Searching indexer(s): [LimeTorrents] for Term: []`.
- radarr (until 2026-09-12): same pairing with `Categories: [2000]`; last one at 21:17:31 search, 21:17:34 400 in `prowlarr.0.txt`.
- Zero radarr 400s since (`prowlarr.txt`, Sep 13-15: 0 matches). The only LimeTorrents `[2000]` searches after Sep 12 are manual API probes made during this investigation (`Limit: 5`; sync tests use `Limit: 100`).

The #873 suspects (ids 10, 15) are unrelated. #873 itself ties those ids to `429 TooManyRequests` from `indexer testall`, a different failure. Current state (queried 2026-09-15): id 10 no longer exists in Prowlarr, and id 15 (TorrentDay) is enabled and mapped to both Sonarr and Radarr.

## F2. Why the add always fails

Prowlarr v2.5.2.5491 `RadarrV3Proxy.AddIndexer` POSTs the definition to the app. Radarr and Sonarr validate a newznab indexer by test-querying it, get zero releases, and answer 400 with "Query successful, but no results in the configured categories were returned from your indexer." The proxy retries once with `?forceSave=true`; the app still validates and answers 400 again. `ApplicationService.ExecuteAction` catches the exception, records a failure, and the next sync cycle (about 15 min) starts over. That is the loop. It raises no health warning: `/api/v1/health`, checked once during this investigation, returned `[]`.

## F3. Why the test can never pass: the latest page parses categoryless

Direct probes against Prowlarr's search API for indexer 14:

| query | results |
|---|---|
| no category filter, no term | 150 |
| `categories=2000`, no term | 0 |
| `categories=5000`, no term | 0 |
| `categories=6000`, no term | 0 |
| `categories=2000`, term "blue trail" | 1 |
| `categories=5000`, term "black mirror" | results |

Term searches parse and filter categories correctly. The latest page, which is what the add test and RSS hit, returns rows that match no standard category, so every app-category filter drops them. No `syncCategories` widening can match rows that carry no category.

## F4. Per-app decision

- D1 radarr: excluded, already in force. The Radarr app carries tag 3 (`movies`), LimeTorrents carries no tags, and `ShouldHandleIndexer` (ApplicationService.cs, v2.5.2.5491) handles an indexer for a tagged app only when tags intersect. Verified in `ApplicationStatus`: last failure 2026-09-12 19:17 UTC, `EscalationLevel` 0, nothing since. Do not tag LimeTorrents `movies`; that restarts the loop.
- D2 sonarr: keep the mapped row. Sonarr's app is untagged, so it handles all indexers. Its LimeTorrents row (mapping id 23, remote id 9) has a dead RSS: one uncategorized fetch per 15 min, zero usable rows, no errors in the logs. Term searches work, so interactive and automatic searches still return LimeTorrents results to sonarr users.

## F5. Root cause sits upstream

The limetorrents definition's latest-page category mapping is broken in the definitions bundled with Prowlarr v2.5.2.5491. If a future Prowlarr update fixes it, the radarr add starts succeeding for any tag-matched indexer; until then LimeTorrents stays untagged and out of the apps by design.

## A1. Guard: prowlarr-appsync-drift

`2-k3s/maintenance/prowlarr-appsync-drift-cronjob.yaml` runs daily and enforces the convergence invariant directly against Prowlarr's Postgres: every enabled indexer that an enabled app would handle (app untagged, or tags intersect) must have an `ApplicationIndexerMapping` row. A missing row is the pre-condition of this loop: app-sync will retry `AddIndexer` for that pair until it converges or fails forever, as LimeTorrents did for weeks without a single health warning. Alert `ProwlarrAppSyncDrift` in `2-k3s/10.observability/alertmanager-config/custom-alerts.yaml` pages on it.

## Sources

- Prowlarr pod logs: stdout plus `/config/logs/prowlarr*.txt` (pod `prowlarr-678c9bccdc-dmw62`, namespace `servarr`).
- Prowlarr Postgres (credentials in the `servarr-postgres` secret): tables `Indexers`, `Applications`, `ApplicationIndexerMapping`, `ApplicationStatus`, `Tags`.
- Prowlarr v2.5.2.5491 source: `src/NzbDrone.Core/Applications/ApplicationService.cs` (`SyncIndexers`, `ShouldHandleIndexer`), `src/NzbDrone.Core/Applications/Radarr/RadarrV3Proxy.cs` (`AddIndexer`, `ExecuteIndexerRequest`), `src/NzbDrone.Core/Applications/ApplicationDefinition.cs` (`Enable` = `SyncLevel` is AddOnly or FullSync).
- Issues #1032, #873, PR #845.
