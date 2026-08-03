# Babysitter run history insights

Harvested 2026-08-03 (issue #660) from the 52 run directories under
`.a5c/runs/` that had accumulated since onboarding (2026-05-30) without ever
being reviewed - `/babysitter:cleanup` had never run. All 52 were harvested
here before deletion; see the deletion note in PR body for what was removed.

## Overview

- **51 real runs** (52 dirs minus `_secrets`, a stray leftover - see below).
- **Date range:** 2026-05-30 to 2026-06-23 (all runs are 6+ weeks old as of
  this harvest; nothing from July or August is in this batch).
- **Outcome:** 50 completed successfully, 1 failed (see below). 3 runs have
  no top-level `state/output.json` `success` field (older/different process
  shape) but completed - see the table.
- Every run maps 1:1 to a process definition under `.a5c/processes/` (see
  issue #659) and, where the work produced a PR, the PR link is in the table.

## Failed run

- **`tvdb-masterchef-renumber-fix`** (2026-06-09, run `01KTPT5P36QV4Z0JJDW11SV3S2`):
  `success: false`, `reason: tvdb-not-corrected`. TheTVDB's `saveseason` form
  refused to renumber MasterChef GR (tvdb 328975) because all season slots
  0-12 were occupied with no free slot to shift into - 0/12 edits applied,
  zero drift, opened issue #257. Per project memory this was later fixed with
  a temp-slot renumber workaround; the direct-edit approach this run tried
  does not work on TVDB and should not be retried as-is.

## Themes

- **Prune/selfHeal flip runs** (`traefik-prune-flip`, `filebrowser-prune-flip`,
  `renovate-prune-flip`) are the same shape repeated per-app for the ArgoCD
  prune rollout (issue #21) - a recurring pattern, not independent one-offs.
- **ArgoCD adoption runs** (`argocd-selfmanage-reconcile`, `issue-93-crd-argocd-adoption`,
  `issue-147-controlplane-endpoints-gitops`, `issue-148-master-config-standardize`)
  form the bulk of late-May/early-June work - the GitOps-adoption push.
- **Servarr stack fixes** (`cleanuparr-*`, `newtarr-*`, `huntarr-to-newtarr-migration`,
  `sonarr-*`, `seerr-*`) are the largest single cluster of runs - matches the
  project's ongoing servarr maintenance load documented in memory.
- **`deliver-open-issues-oldest-first`** (2026-06-13) is an outlier: 248 task
  subdirectories under one run - a long-lived batch orchestrator that worked
  through many issues sequentially, not a single-purpose process.

## Stray non-run data

`.a5c/runs/_secrets/` held two TVDB Python scripts with no connection to any
run - removed as part of this cleanup (issue #660), not deleted as "insight",
just relocated out of a gitignored runs directory that shouldn't hold source.

## Full run table

| Date | Process | Outcome | PR |
|---|---|---|---|
| 2026-05-30 | argocd-selfmanage-reconcile | done | [#105](https://github.com/SpyrosPsarras/epaflix/pull/105) |
| 2026-05-30 | cradle-project-install | done | - |
| 2026-05-30 | filebrowser-prune-flip | done | [#109](https://github.com/SpyrosPsarras/epaflix/pull/109) |
| 2026-05-30 | renovate-self-major-only | done | [#113](https://github.com/SpyrosPsarras/epaflix/pull/113) |
| 2026-05-30 | renovate-self-update-triage | done | - |
| 2026-05-30 | traefik-prune-flip | done | [#107](https://github.com/SpyrosPsarras/epaflix/pull/107) |
| 2026-05-31 | cleanuparr-orphan-stalled | done | - |
| 2026-05-31 | cleanuparr-strike-runaway | done | - |
| 2026-05-31 | huntarr-to-newtarr-migration | done | [#132](https://github.com/SpyrosPsarras/epaflix/pull/132) |
| 2026-05-31 | issue-93-crd-argocd-adoption | done | [#123](https://github.com/SpyrosPsarras/epaflix/pull/123) |
| 2026-05-31 | issue102-cnpg-130-triage | done | - |
| 2026-05-31 | observability-controlplane-metrics-121 | done | [#146](https://github.com/SpyrosPsarras/epaflix/pull/146) |
| 2026-05-31 | pool1-degraded-remediation | done | - |
| 2026-05-31 | renovate-prune-flip | done | [#120](https://github.com/SpyrosPsarras/epaflix/pull/120) |
| 2026-05-31 | system-upgrade-plans-onboard | done | [#126](https://github.com/SpyrosPsarras/epaflix/pull/126) |
| 2026-05-31 | traefik-drift-codify-or-remove-108 | done | [#143](https://github.com/SpyrosPsarras/epaflix/pull/143) |
| 2026-05-31 | truenas-encrypted-sops-dataset | done | [#122](https://github.com/SpyrosPsarras/epaflix/pull/122) |
| 2026-06-03 | seerr-oidc-research | done | [#153](https://github.com/SpyrosPsarras/epaflix/pull/153) |
| 2026-06-06 | cnpg-operator-bump-129 | done | [#166](https://github.com/SpyrosPsarras/epaflix/pull/166) |
| 2026-06-06 | deliver-issue-138-cleanuparr-blocklist | done | [#181](https://github.com/SpyrosPsarras/epaflix/pull/181) |
| 2026-06-06 | epaflix-issue-125-midclt-doc | done | - |
| 2026-06-06 | issue-130-k3s-upgrade-guardrails | done | [#168](https://github.com/SpyrosPsarras/epaflix/pull/168) |
| 2026-06-06 | issue-134-newtarr-authentik-sso | done | [#173](https://github.com/SpyrosPsarras/epaflix/pull/173) |
| 2026-06-06 | issue-135-sonarr2-hunt-behaviour | done | - |
| 2026-06-06 | issue-136-huntarr-tidyup | done | - |
| 2026-06-06 | newtarr-config-codify-backup | done | [#178](https://github.com/SpyrosPsarras/epaflix/pull/178) |
| 2026-06-06 | odysseus-truenas-gpu-deploy | done | [#183](https://github.com/SpyrosPsarras/epaflix/pull/183) |
| 2026-06-07 | cleanuparr-s04e07-triage | done | - |
| 2026-06-07 | issue-147-controlplane-endpoints-gitops | done | [#198](https://github.com/SpyrosPsarras/epaflix/pull/198) |
| 2026-06-07 | issue-148-master-config-standardize | done | [#200](https://github.com/SpyrosPsarras/epaflix/pull/200) |
| 2026-06-07 | issue-149-pool1-redundancy-migrate | done | [#202](https://github.com/SpyrosPsarras/epaflix/pull/202) |
| 2026-06-07 | issue142-orphan-reaping-deliver | done | [#194](https://github.com/SpyrosPsarras/epaflix/pull/194) |
| 2026-06-07 | odysseus-k3s-migration | done | [#205](https://github.com/SpyrosPsarras/epaflix/pull/205) |
| 2026-06-08 | cleanuparr-nfs-unification | done | - |
| 2026-06-08 | deliver-issue-182-cleanuparr-blocklist-drift | done | [#228](https://github.com/SpyrosPsarras/epaflix/pull/228) |
| 2026-06-08 | deliver-issue-185-authentik-service-account-token | done | [#229](https://github.com/SpyrosPsarras/epaflix/pull/229) |
| 2026-06-08 | deliver-issue-192-image-updater-writeback | done | [#235](https://github.com/SpyrosPsarras/epaflix/pull/235) |
| 2026-06-08 | issue-164-ci-kustomize-pin | done | [#219](https://github.com/SpyrosPsarras/epaflix/pull/219) |
| 2026-06-08 | issue-187-vram-obsolescence | done (no `success` field; `closed: true`) | - |
| 2026-06-08 | issue-188-readme-reconcile | done (no `success` field; `merged: true`) | [#234](https://github.com/SpyrosPsarras/epaflix/pull/234) |
| 2026-06-08 | newtarr-auth-bypass-declarative | done | [#224](https://github.com/SpyrosPsarras/epaflix/pull/224) |
| 2026-06-08 | rotate-authentik-admin-token-175 | done (no top-level output; per-task outputs present) | - |
| 2026-06-09 | jellyfin-empty-anime-folder-fix | done | - |
| 2026-06-09 | seerr-servarr-media-root-fix | done | [#251](https://github.com/SpyrosPsarras/epaflix/pull/251) |
| 2026-06-09 | tvdb-masterchef-renumber-fix | **FAILED** (see above) | - |
| 2026-06-10 | sonarr-greek-alias-fetch-fix | done | - |
| 2026-06-12 | sonarr-import-tuva-ronny-290 | done | - |
| 2026-06-13 | deliver-open-issues-oldest-first | done (248-task batch orchestrator, no top-level output) | - |
| 2026-06-13 | servarr-forwardauth-rollout-176 | done | - |
| 2026-06-14 | devops-sre-platform/postgres-slot-wal-ceiling | done | [#305](https://github.com/SpyrosPsarras/epaflix/pull/305) |
| 2026-06-23 | searxng-followups | done | [#377](https://github.com/SpyrosPsarras/epaflix/pull/377) |

## Cadence going forward

Pick monthly: run `/babysitter:cleanup` at the start of each month so
`.a5c/runs/` never again accumulates 6+ weeks of unreviewed history. Tracked
as issue #660's own follow-up if a monthly reminder/automation is wanted
beyond "remember to run the command."
