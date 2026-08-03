# Process: Deliver issue #142 — safe orphan reaping + seriesId 272 soak + newtarr decision

Issue #142 is OPEN. The 2026-05-31 firefight (deleting the 12 dead orphans) and the
operator runbook were already shipped in PR #144. This run delivers the three
remaining forward-looking deliverables and closes the loop per repo policy.

## Scope (the remaining #142 deliverables)

1. **#2 Safe automated orphan reaping** — decide and implement one of:
   - **Option A**: co-locate downloads + library on one ZFS dataset AND mount the
     library roots into Cleanuparr so `nlink>1` detection is real, then enable the
     unlinked/orphan rule. Heavyweight, destructive storage change.
   - **Option B**: a guarded reaping rule (seed-time / ratio / stalled-age,
     category-scoped) that cannot touch the ~139 healthy `nlink=1` seeders.
   - **Option C**: documented deferral — keep the unlinked rule OFF, rely on the
     operator runbook, record the trigger to revisit.
2. **#3 Soak-confirm seriesId 272** — did the post-firefight re-search eventually
   grab a SEEDED release, or is it still 0-seed ("no healthy release", external)?
3. **#4 newtarr** — decide whether to bump `hunt_missing_items` above 1 (default
   NO; #135 says the setting is global and the owner restored it live, and a bump
   can worsen the add-search race).

Then: update issue #142 description with outcomes (tick / strike), open follow-ups,
cross-link #138 (PVC-only Cleanuparr config durability).

## Phases

1. **Investigate (read-only)** — orphan recurrence since 2026-05-31, seriesId 272
   soak status, Cleanuparr/qbt config + guard knobs + categories, dataset topology,
   newtarr hunt value. No changes.
2. **Design** — produce options A/B/C with concrete config + a recommendation +
   durability story + newtarr recommendation. Authoring only.
3. **Adversarial verify** — try to prove the recommended design could delete a
   healthy seeder; refine until safe (max 3).
4. **GATE 1 — owner decision** — pick A / B / C (architecture breakpoint).
5. **Implement** — author the approved changes on a feature branch; no merge, no
   live mutation. Live-only DB rules are documented, not applied here.
6. **Validate (shell)** — `kubectl kustomize 2-k3s/08.servarr` + SOPS pre-commit guard.
7. **GATE 2 — deploy** — approve PR push + rebase + `validate` + `gh pr merge`, and
   any owner-approved live Cleanuparr change.
8. **Deliver** — land the PR, apply approved live change, soak-confirm 272, edit the
   #142 description with outcomes, open follow-ups.
9. **Final verify (read-only)** — PR state, ArgoCD Synced/Healthy, unlinked rule still
   OFF unless option A landed, no seeder lost, #142 updated.

## Safety

- **No torrent is deleted in this run.** The risky `DownloadCleaner` unlinked/orphan
  rule stays OFF unless option A (library roots mounted) actually lands.
- Two breakpoints only (low breakpoint tolerance): the architecture decision and the
  deploy/merge gate. Both route to `owner`.
- Repo merge policy enforced: rebase onto origin/main, force-with-lease, wait for
  `validate`, `gh pr merge --merge`.
