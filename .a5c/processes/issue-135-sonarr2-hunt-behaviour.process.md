# Process — Issue #135: Confirm Sonarr2 hunt behaviour after the newtarr migration

## Goal
Decide, with live evidence, whether the newtarr v1.0.0 hunt defaults (`episodes` / `900s`)
worsened the known Sonarr2 importBlocked race compared to the old huntarr behaviour
(`seasons_packs` / `3600s`), and either **restore** the old params in newtarr's JSON `/config`
or **keep** the v1.0.0 defaults.

## Key insight
The migration landed **2026-05-31**; today is **2026-06-06**. At a 900s cadence that is hundreds
of hunt cycles already elapsed — the "observe over the next several hunt cycles" soak window in
the issue has effectively already passed. So we observe the **accumulated** post-migration state
now (new importBlocked rate vs the documented pre-migration baseline) instead of sleeping for days.

## Phases
1. **Gather (read-only)** — exec into the newtarr pod and read the ACTUAL per-instance hunt config
   from `/config` (confirm or correct the issue's `episodes/900` claim); pull the Sonarr2
   importBlocked queue + history since the migration; observe the real hunt cadence in newtarr
   logs; check Cleanuparr's `failed_import_patterns` coverage for sonarr2.
2. **Analyze (read-only, refine loop)** — judge `worsened` / `neutral` / `improved` / `inconclusive`
   from the evidence (separating stale March/May residue from genuinely new post-migration events),
   and recommend `restore` vs `keep`, with a concrete exact JSON edit if restoring.
3. **Decision gate (breakpoint, owner)** — present findings + recommendation; owner chooses
   **Restore** / **Keep defaults** / **Request more analysis** / **Abort**. This single gate is also
   the authorization for the live `/config` mutation (deploy-class change; matches your
   `alwaysBreakOn: [deploy]`).
4. **Apply (live, CONDITIONAL on "Restore")** — back up the JSON in the pod, edit only the approved
   keys for the Sonarr2 instance, `rollout restart` newtarr, verify the values persisted and the pod
   is healthy. Optional anomaly gate if the apply fails.
5. **Closeout** — comment the findings + decision on #135 (series referred to by seriesId only),
   close it if resolved (or leave open for keep-and-watch), and open warranted follow-ups
   (declarative hunt config — same gap class as #174; Cleanuparr pattern coverage if still missing).

## Breakpoints (breakpointTolerance = low)
- **Decision + apply gate** (mandatory) — decision is also the live-mutation authorization.
- **Anomaly gate** (conditional) — only if the apply does not verify cleanly.
- Read-only gather/analyze carry no breakpoint.

## Outputs
`{ success, decision, raceWorsened, configChanged, appliedMode, appliedSleep, issueState, followUpIssues }`
