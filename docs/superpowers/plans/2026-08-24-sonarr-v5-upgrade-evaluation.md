# Sonarr v5 upgrade: evaluation, 2026-08-24

Question from #834: Sonarr commit `2f9e12a` fixes the stale tracked-download cache
that blinded our queue on 2026-08-07. It is described upstream as "already fixed
in v5". What does moving to v5 cost us, and does it fix what we actually have?

Answer: the question is moot today. There is no Sonarr v5 to move to. When there
is, it arrives through a Renovate rule that will auto-merge it unattended, which
is the finding worth acting on.

## v5 does not exist as a shippable artifact

| check | result |
|---|---|
| `gh api repos/Sonarr/Sonarr/releases` | newest tag `v4.0.19.3001` (pre-release, 2026-08-11). No v5 tag at all. |
| `gh api repos/Sonarr/Sonarr/branches` | v5 work lives on `v5-develop`, head `e27c1f47` (2026-08-19). `develop` is stale since 2025-02-22. |
| branch containing `2f9e12a` | `v5-develop` only. |
| `services.sonarr.tv/v1/update/{main,develop,nightly,v5-develop}` | `main` and `v5-develop` return `{"available":false}`. `develop` and `nightly` both serve 4.0.19.3001. |
| `hub.docker.com/v2/repositories/linuxserver/sonarr/tags` | every tag is 4.0.19.x, including `develop`. No v5 image. |

So no update channel, official build or LinuxServer image carries v5. The only
way to run `2f9e12a` today is to build `v5-develop` ourselves, which means owning
a Sonarr build for one 18-line event handler. Not worth it.

How close it is: the `v5.0` milestone has 142 closed issues against 3 open, no due
date, last touched 2026-08-04, and the v5 API reference is already published at
`sonarr.tv/docs/api/?api=v5`. Treat a v5 GA as plausible within months, not years.

## What v5 would and would not fix

#834 turned up two separate populations behind our blind queue reads:

| population | pinned state | survives restart | fixed by `2f9e12a` |
|---|---|---|---|
| the 16 re-grabs of 2026-08-07 | `Imported`, in memory | no | yes |
| 648 rows, incl. the 5 stalled torrents in #1029 | `Failed`, re-seeded from `DownloadHistory` | yes | no |

`2f9e12a` adds an `EpisodeGrabbedEvent` handler that evicts cache entries in
`Imported`, `Failed` or `Ignored`. That clears the in-memory population on the
next grab. It does not help the second population, because
`TrackedDownloadService.TrackDownload()` re-seeds `Failed` from `DownloadHistory`
on every process start, and `DownloadIsTrackable()` excludes `Failed`.

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

This is exactly the trap already documented in that file's homarr rule:

> on a digest-only entry Renovate resolves `:latest` and every release, major
> included, arrives as an untyped digest bump that no updateType rule can catch
> (#540)

homarr was fixed by pinning it to a `vX.Y.Z` tag. Sonarr was not, and #540's
closing claim that "every other image in the same automerge rule is either
genuinely tag-pinned to a version stream or stateless/easy to roll back" does not
hold for it. Sonarr is stateful on PostgreSQL and its schema migrations are
one-way, with no downgrade path once v5 has migrated `sonarr-main`.

On the day LinuxServer flips `:latest` to v5, we take a major version with a
one-way database migration through an auto-merged PR, at 2am, with
`imagePullPolicy: Always` on all three replicasets. Same exposure applies to
`radarr` and `prowlarr`, which are digest-only on `:latest` in the same block.

LinuxServer does publish usable version tags (`4.0.19`, `4.0.19.2979-ls322`,
`version-4.0.19.2979`), so the homarr fix transfers directly.

## Recommendation

1. Do not pursue v5 now. Nothing to install, and it fixes the lesser half of the
   problem.
2. Pin `sonarr`, `sonarr2`, `radarr` and `prowlarr` to a version tag so a major
   arrives as a `major` update Renovate will not auto-merge. Tracked separately.
3. Revisit v5 when a `v5.x` tag appears on `Sonarr/Sonarr` releases and a
   LinuxServer image carries it. At that point the migration needs a
   `sonarr-main` backup, a read of the v5 release notes for API changes used by
   the census, Cleanuparr and newtarr, and a rollback plan that assumes the
   database cannot be downgraded.

## Sources

All checks are reproducible from the commands in the table above, run 2026-08-24.
Code claims are read from `Sonarr/Sonarr` at tag `v4.0.19.2979`:
`src/NzbDrone.Core/Download/TrackedDownloads/TrackedDownloadService.cs`,
`DownloadMonitoringService.cs`, `src/NzbDrone.Core/Queue/QueueService.cs`.
Evidence for the two populations is in #834 (2026-08-24 comments).
