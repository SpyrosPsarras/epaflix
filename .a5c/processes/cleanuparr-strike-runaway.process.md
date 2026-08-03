# Cleanuparr strike-runaway investigation + mitigation

## Problem
Cleanuparr fires "Download keeps coming back after deletion" for
the Sonarr seriesId 40 / episodeId 3143 (S04E13) release
(hash `66a4dc6201cb149ff70eed12b9902317cb82ed87`), strikeCount **248**, 1 of **76**
such events. User believes mitigations are already in place — so the real gap is
something they think is fixed but isn't.

## Approach (incident-response + systematic-debugging)
1. **Diagnose (read-only)** — read live Cleanuparr v2 JSON config off the PVC, the
   owning *arr's remove/blocklist behaviour, qbittorrent state for the hash, and the
   Newtarr re-hunt schedule. Evidence first, then a single root cause + concrete
   config gaps.
2. **Adversarially verify** the root cause (skeptic re-reads live config, tries to
   refute). Sharpens mitigation into exact ordered steps.
3. **Gate 1 (deploy)** — mandatory approval before any live change.
4. **Apply mitigation** — Cleanuparr + *arr config (blocklist-on-removal etc.) and
   one-off cleanup of the stuck item; config backed up first.
5. **Verify fix** — hash gone, re-grab blocklisted, no new strikes. Verify gate if
   not confirmed.
6. **Gate 2 (git/outward)** — open a `gh` follow-up issue (soak + durable capture of
   PVC-only config) and a doc note per repo policy.

## Breakpoints (low tolerance: only live-change + git)
- **Gate 1** — `deploy`: approve applying live mitigation.
- **Verify gate** — only if fix unconfirmed.
- **Gate 2** — `destructive-git`/outward: approve issue + doc/PR.

## Outputs
`{ success, rootCauseConfirmed, rootCause, mitigationApplied, fixed, needsSoak, issueUrl, summary }`
