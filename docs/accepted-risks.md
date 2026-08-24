# Accepted risks

Findings we know about and decided not to act on. This file exists so a "we know, and no" answer has a durable home that is not an open GitHub issue (see the follow-up triage rule in `CLAUDE.md`).

Reopening means: open a `gh issue` that references the entry, and delete the entry in the same PR.

## Format

```
### YYYY-MM-DD short title
- Finding: what was observed, with a link to the PR/issue/session where it surfaced.
- Why no action: the reason, stated plainly.
- Reopens when: the concrete condition that would turn this back into work.
```

## Entries

### 2026-08-24 Sonarr stays on v4 despite a known upstream cache fix
- Finding: Sonarr `2f9e12a` fixes the stale tracked-download cache that blinded our queue on 2026-08-07 (#834). It exists only on the `v5-develop` branch. No v5 release tag exists, and no update channel or LinuxServer image we checked carries a v5 build. It would fix only the population whose newest `DownloadHistory` row is `DownloadGrabbed`, not the 648 whose newest row is `DownloadFailed`, which v5 re-pins to `Failed` on start exactly as v4 does. Full evaluation, including what each check can and cannot prove: `docs/superpowers/plans/2026-08-24-sonarr-v5-upgrade-evaluation.md`.
- Why no action: running it today means building and owning a Sonarr image for one event handler, for a fault that self-healed in about 71 minutes when it occurred (15 grabs 07:00:57Z to 07:01:31Z, 15 imports 08:12:34Z to 08:13:09Z). The census guard change on #834 covers the half that does not self-heal.
- Reopens when: a `v5.x` tag appears on `Sonarr/Sonarr` releases and a LinuxServer image carries it, or the `Imported` population causes an incident that does not self-heal. Nothing watches Sonarr releases, so the trigger is a manual check, not an alert. #1129 must land first, or v5 arrives on its own as an auto-merged digest bump.
