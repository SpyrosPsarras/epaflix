# Sonarr v5 upgrade: evaluation, 2026-08-24

Question from #834: Sonarr commit `2f9e12a` fixes the stale tracked-download cache
that blinded our queue on 2026-08-07. A maintainer called it "already fixed in
v5". What does moving to v5 cost us, and does it fix what we actually have?

The question is moot today. There is no Sonarr v5 to move to. When there is, it
arrives through a Renovate rule that will auto-merge it unattended, which is the
finding worth acting on and is now tracked in #1129.

## v5 does not exist as a shippable artifact

| check | result |
|---|---|
| `gh api repos/Sonarr/Sonarr/releases` | newest tag `v4.0.19.3001` (pre-release, 2026-08-11). No v5 tag at all. |
| `gh api repos/Sonarr/Sonarr/git/matching-refs/tags/v5` | length 0. No v5 tag of any kind. |
| `gh api repos/Sonarr/Sonarr/branches` | v5 work lives on `v5-develop`, head `e27c1f47` (2026-08-19). `develop` is stale since 2025-02-22. |
| `gh api repos/Sonarr/Sonarr/compare/main...2f9e12a` | `diverged`, 789 ahead. Not in `main`. |
| `gh api repos/Sonarr/Sonarr/compare/v5-develop...2f9e12a` | `behind`, 0 ahead. Contained in `v5-develop`. Do not use `/branches-where-head` for this: it answered `v5-develop` on one run and empty on the next. |
| `curl "services.sonarr.tv/v1/update/$br?version=4.0.19.2979&os=linux&arch=x64&runtime=netcore"` | `develop` and `nightly` both serve 4.0.19.3001. `main` and `v5-develop` return `{"available":false}`, but so does a deliberately bogus branch name, so read those two cells as "not a channel that offers us an update" and nothing stronger. |
| `curl "hub.docker.com/v2/repositories/linuxserver/sonarr/tags?page_size=100"` | 8398 tags exist; the newest 100 hold no tag starting with 5 or containing a non-4 `5.`. Newest-first, so this shows no v5 yet rather than enumerating everything. |

So no update channel, official build or LinuxServer image carries v5. The only
way to run `2f9e12a` today is to build `v5-develop` ourselves, which means owning
a Sonarr build for one 18-line event handler. Not worth it.

How close is it? `gh api repos/Sonarr/Sonarr/milestones` reports the `v5.0`
milestone at 142 closed issues against 3 open, `due_on: null`, last updated
2026-08-04, and `sonarr.tv/docs/api/?api=v5` already returns 200. Treat a v5 GA
as plausible within months, not years.

## What v5 would and would not fix

#834 turned up two separate populations behind our blind queue reads:

| population | pinned state | survives restart | fixed by `2f9e12a` |
|---|---|---|---|
| the 16 re-grabs of 2026-08-07 | `Imported`, in memory | no | yes |
| 648 rows, incl. the 5 stalled torrents in #1029 | `Failed`, re-seeded from `DownloadHistory` | yes | no |

`2f9e12a` adds an `EpisodeGrabbedEvent` handler that evicts cache entries in
`Imported`, `Failed` or `Ignored`. That clears the in-memory population on the
next grab. It does not help the second population, and that verdict is read from
v5 source, not from ours: on `v5-develop` at `e27c1f47`,
`TrackedDownloadService.cs:122-127` still seeds state from
`GetLatestDownloadHistoryItem()`, `:262-270` still maps `DownloadFailed` to
`Failed`, and `DownloadMonitoringService.cs:139-145` still excludes `Failed` from
trackable. So v5 re-pins `Failed` from `DownloadHistory` on every process start
exactly as v4 does.

The first population also self-healed within 70 minutes on 2026-08-07 (all 16
imported at 08:12Z after a 07:01Z grab). So v5 buys us a shorter blind window on
a fault that already resolves itself, and buys nothing for the fault that does
not. The census guard change tracked in #834 remains the only work that unblocks
#618.

## The real cost is that we do not control when it lands

`2-k3s/08.servarr/kustomization.yaml` pins `lscr.io/linuxserver/sonarr` by digest
with no tag, so Renovate resolves the moving `:latest`. The servarr digest rule in
`.github/renovate.json` matches that file with `matchUpdateTypes: ["digest"]` and
`automerge: true`.

This is exactly the trap already documented in that file's homarr rule, quoted
verbatim:

> on a digest-only entry Renovate resolves :latest and every release, major
> included, arrives as an untyped digest bump that no updateType rule can catch
> (#540, follow-up to #284).

#540 fixed homarr by pinning it to a `vX.Y.Z` tag. Nobody did that for the *arrs,
and #540's
closing claim that "every other image in the same automerge rule is either
genuinely tag-pinned to a version stream or stateless/easy to roll back" does not
hold for it. Sonarr is stateful on PostgreSQL and its schema migrations are
one-way, with no downgrade path once v5 has migrated `sonarr-main`.

On the day LinuxServer flips `:latest` to v5, we take a major version with a
one-way database migration through an auto-merged PR at 2am. Four Deployments are
exposed, `sonarr`, `sonarr2`, `radarr` and `prowlarr`, from three `images:`
entries, because `sonarr` and `sonarr2` run the same image.

Those four also pull on every restart, which is a live-only fact worth flagging:
`kubectl --context epaflix -n servarr get deploy -o custom-columns=NAME,IMAGE,POLICY`
reports `Always` for all four while none of the four manifests sets
`imagePullPolicy` at all. I did not establish why they drift, and the obvious
explanation is suspect, since a digest-pinned reference should default to
`IfNotPresent`. Treat the live value as the fact and the cause as unknown.

LinuxServer does publish usable version tags. `curl
"hub.docker.com/v2/repositories/linuxserver/sonarr/tags?page_size=100"` returns
`4.0.19`, `4.0.19.2979-ls322` and `version-4.0.19.2979` among the newest 100, so
the homarr fix transfers directly.

## Recommendation

1. Do not pursue v5 now. Nothing to install, and it fixes the lesser half of the
   problem.
2. Pin the `lscr.io/linuxserver/{sonarr,radarr,prowlarr}` entries in the
   `images:` block to a version tag so a major arrives as a `major` update
   Renovate will not auto-merge. The `sonarr` entry covers both the `sonarr` and
   `sonarr2` Deployments. Tracked in #1129.
3. Revisit v5 when a `v5.x` tag appears on `Sonarr/Sonarr` releases and a
   LinuxServer image carries it. At that point the migration needs a
   `sonarr-main` backup (`2-k3s/maintenance/backup-all-databases.sh`), a read of
   the v5 release notes for API changes used by the census, Cleanuparr and
   newtarr, and a rollback plan that assumes the database cannot be downgraded.
   Nothing watches Sonarr releases today, so this doc cannot be the trigger.
   `docs/accepted-risks.md` carries the dated entry and its reopen condition.

## Sources

Every command above ran on 2026-08-24 and each one is quoted where its result is
claimed. Two claims here rest on other sources rather than on those commands: the
648-row and 16-re-grab counts come from the #834 comments of 2026-08-24, which
quote the SQL and the Cleanuparr `/api/events` reads that produced them, and the
code claims come from reading
`src/NzbDrone.Core/Download/TrackedDownloads/TrackedDownloadService.cs`,
`DownloadMonitoringService.cs` and `src/NzbDrone.Core/Queue/QueueService.cs` at
both tag `v4.0.19.2979` and `v5-develop` head `e27c1f47`.
