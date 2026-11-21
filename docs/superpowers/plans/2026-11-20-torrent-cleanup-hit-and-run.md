# Torrent Cleanup Without Hit-and-Runs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cleanuparr is the only tool that deletes finished torrents, private torrents never stop or get deleted before their tracker's hit-and-run rule is met, and an alert fires when one does.

**Architecture:** One tracker-rule table in Git (`tracker-rules.json`) feeds three consumers: Cleanuparr's boot-time init script (SQLite seeding and stall rules), the hourly `cleanuparr-policy` reconcile (Cleanuparr API, qBittorrent preferences and per-torrent limits, Prowlarr seed goals), and a new read-only `torrent-hnr-check` script run by two CronJobs (early stop, tracker not working). Alerts use the existing failed-Job pattern.

**Tech Stack:** Kubernetes manifests with kustomize, Argo CD, Python 3.13 stdlib scripts with `--selftest`, Cleanuparr 2.11.1, qBittorrent WebUI API v2, Prowlarr API v1, PrometheusRule alerts with promtool tests.

**Spec:** `docs/superpowers/specs/2026-11-20-torrent-cleanup-hit-and-run-design.md`

## Global Constraints

- Private rules: every tracker and the default private `max_ratio 5.0`, `max_seed_time 2160` h; every private rule `min_seed_time 1440` h. Public rules unchanged: `1.0` / `400` h.
- Hit-and-run rules (alert): IPTorrents ratio 1.0 or 336 h; TorrentDay ratio 1.0 or 72 h; AvistaZ ratio 0.9 or 72 h + 2 h per GB under 50 GB, 1440 h from 50 GB (decimal GB, as the site states); Mpellas none; default private 1440 h (time only).
- Cleanuparr categories: `radarr`, `tv-sonarr`, `animes`, `music`. Stalled private downloads move to `private-stalled`.
- Queue Cleaner `ignoredDownloads` holds every listed private host plus tag `private-guard`; the hourly job tags every private torrent `private-guard`.
- Stall rule `private stalled` (0–99%): `delete_private_torrents_from_client=0`, `change_category=0`. The hourly reconcile moves incomplete private torrents in category `radarr` or `tv-sonarr` whose hash is in no Radarr or Sonarr queue to `private-stalled`.
- qBittorrent global `max_ratio_enabled`, `max_seeding_time_enabled`, `max_inactive_seeding_time_enabled` stay `false`.
- Private seed goals (Prowlarr and per-torrent): no ratio, seed time `(max_seed_time + 24) * 60` minutes.
- No automated process logs in to a tracker website. Radarr/Sonarr Remove Completed stays off.
- The check scripts never call a modifying qBittorrent endpoint. Logs print hash prefixes, never torrent names, keys or response bodies.
- Stdlib-only Python, `--selftest` prints `selftest OK`. Comment only the why. Conventional commits, no co-author.

## Review Focus

1. A torrent on a tracker host not in the table (new tracker, new announce domain) must get the default private rule, never the public one. Pinned by `test_unknown_private_host_gets_default` in Task 1 and `selftest_unknown_host` in Task 4.
2. A torrent stopped at exactly the threshold (ratio 1.0 on IPTorrents, 336.0 h) must count as met; 0.99 / 335.9 h must not. Pinned by `selftest_boundaries` in Task 4.
3. A Cleanuparr rule edited in the UI, or an extra private rule someone added, must be put back or removed on the next reconcile. Pinned by `selftest_seeding_rules_exact` in Task 2.
4. Prowlarr returns masked secrets (`********`). A PUT must not overwrite a stored password or cookie with the mask. Pinned by `selftest_prowlarr_body_untouched` in Task 3 and the DRY_RUN check in Task 5.
5. One unreachable service (Cleanuparr, qBittorrent or Prowlarr down) must fail its step loudly (exit 2) without skipping the others. Already the pattern in `cleanuparr-policy`; pinned by `selftest_step_isolation` in Task 2.

---

### Task 1: Tracker-rule table and boot-time Cleanuparr rules

**Files:**
- Create: `2-k3s/08.servarr/cleanuparr/files/tracker-rules.json`
- Modify: `2-k3s/08.servarr/cleanuparr/files/enforce-private-seeding.py`
- Modify: `2-k3s/08.servarr/kustomization.yaml` (generator `tracker-rules`, `options.disableNameSuffixHash: true`), `2-k3s/08.servarr/cleanuparr/cleanuparr.yaml` (mount `tracker-rules` at `/rules` in the init container)
- Create: `2-k3s/maintenance/torrent-policy.test.sh`; Modify: `.github/workflows/ci.yml` (run it next to `aaaa-tripwire-cronjob.test.sh`)

**Interfaces:**
- Produces `tracker-rules.json`:
  ```json
  {"categories": ["radarr", "tv-sonarr", "animes", "music"],
   "stalled_category": "private-stalled",
   "trackers": [
     {"name": "IPTorrents", "prowlarr": "IPTorrents", "hosts": ["127.0.0.1.stackoverflow.tech", "async.empirehost.me", "routing.bgp.technology"],
      "hnr": {"ratio": 1.0, "hours": 336}, "cleanuparr": {"max_ratio": 5.0, "max_seed_hours": 2160}},
     {"name": "TorrentDay", "prowlarr": "TorrentDay", "hosts": ["sync.td-peers.com", "td.jumbohostpro.eu"],
      "hnr": {"ratio": 1.0, "hours": 72}, "cleanuparr": {"max_ratio": 5.0, "max_seed_hours": 2160}},
     {"name": "AvistaZ", "prowlarr": "AvistaZ", "hosts": ["tracker.avistaz.to"],
      "hnr": {"ratio": 0.9, "hours": 72, "hours_per_gb": 2, "large_gb": 50, "large_hours": 1440},
      "cleanuparr": {"max_ratio": 5.0, "max_seed_hours": 2160}},
     {"name": "Mpellas", "prowlarr": "Mpellas", "hosts": ["magtre.com"],
      "hnr": null, "cleanuparr": {"max_ratio": 5.0, "max_seed_hours": 2160}}],
   "default_private": {"hnr": {"ratio": null, "hours": 1440}, "cleanuparr": {"max_ratio": 5.0, "max_seed_hours": 2160}}}
  ```
  Before committing, confirm the host list against live qBittorrent (`/api/v2/torrents/info`, private torrents, grouped by announce host) and add any host missing.
- Produces ConfigMap `tracker-rules` (namespace `servarr`, fixed name, key `tracker-rules.json`), consumed by Tasks 2–4.
- `enforce-private-seeding.py` keeps its role (boot-time, idempotent) and gains `desired_seeding_rules(rules) -> list[dict]` and `desired_stall_rule() -> dict`.

- [ ] **Step 1: Confirm Cleanuparr 2.11.1 semantics from its source** (tag `v2.11.1`): how `tracker_patterns` matches (substring, glob or regex, against which URL part), which rule wins when several match (`priority` order), units of `min_seed_time`/`max_seed_time`. Write the answers as a docstring paragraph in the script.
- [ ] **Step 2: Failing tests** in the script's `--selftest`, against a temp SQLite file with the live schema (columns as in `q_bit_seeding_rules` and `stall_rules`):
  - `test_one_rule_per_tracker_and_category`: 4 trackers + default × 4 categories private rules exist with the table's ratio/hours and `tracker_patterns` from `hosts`; tracker rules sort before the default.
  - `test_unknown_private_host_gets_default`: the default private rule has empty `tracker_patterns` and the lowest private priority.
  - `test_public_rules_untouched`: public rows keep `1.0` / `400`.
  - `test_old_generic_private_rules_replaced`: existing `radarr (private)` rows are updated or removed so no private rule lacks a table source.
  - `test_stall_rule`: `private stalled` ends with `delete_private_torrents_from_client=0`, `change_category=0`; `private stalled 99-100` unchanged.
  - `test_idempotent`: a second run changes 0 rows.
- [ ] **Step 3:** `bash 2-k3s/maintenance/torrent-policy.test.sh`. Expected: FAIL.
- [ ] **Step 4:** Implement. The script reads `/rules/tracker-rules.json` (env `TRACKER_RULES`), writes inside one transaction, prints each change.
- [ ] **Step 5:** Run the test script and `kustomize build 2-k3s/08.servarr`. Expected: PASS, init container mounts `/rules`.
- [ ] **Step 6:** Commit `feat(cleanuparr): per-tracker seeding rules and no private deletes on stall`.

### Task 2: Hourly reconcile of Cleanuparr rules and qBittorrent limits

**Files:**
- Move: inline `policy.py` from `2-k3s/maintenance/cleanuparr-policy-cronjob.yaml` to `2-k3s/maintenance/files/cleanuparr-policy.py`; generate ConfigMap `cleanuparr-policy-script` from it in `2-k3s/maintenance/kustomization.yaml`; mount ConfigMap `tracker-rules` at `/rules`.
- Test: `2-k3s/maintenance/torrent-policy.test.sh` (add `python3 2-k3s/maintenance/files/cleanuparr-policy.py --selftest`)

**Interfaces:**
- Consumes: `tracker-rules.json` (Task 1).
- Produces: `rule_for(tracker_url: str, rules: dict) -> dict` (tracker entry or `default_private`; matches by host suffix) and `seed_goal_minutes(rule: dict) -> int` returning `(max_seed_hours + 24) * 60`. Task 3 and Task 4 reuse the same matching logic; Task 4 copies `rule_for` (the scripts run in separate pods; keep the two copies identical and covered by the same selftest cases).

- [ ] **Step 1:** From Cleanuparr 2.11.1 source, find the API for qBittorrent seeding rules and stall rules (path, method, body). If none exists, record that in the script docstring and skip Step 3's Cleanuparr part: the boot-time init script from Task 1 is then the only writer, as the spec allows.
- [ ] **Step 2: Failing selftests:**
  - `selftest_seeding_rules_exact`: live rules with a UI-edited max ratio (3.0) and an extra private rule → desired set restored, extra removed, changes listed; second pass no-op.
  - `selftest_qbt_globals`: preferences with `max_ratio_enabled: true` → one change to `false`; all three flags covered.
  - `selftest_per_torrent_limits`: private torrent in `tv-sonarr` on IPTorrents with `ratio_limit 1`, `seeding_time_limit 43200` → target `ratio_limit -1`, `seeding_time_limit 131040`; torrents in `private-stalled` get the same safe limits (a set-aside torrent that later completes must not stop early); `ratio-seed` and public ones untouched.
  - `selftest_unknown_host`: private torrent on `tracker.example.org` → default rule, `seeding_time_limit 131040`.
  - `selftest_set_aside_stalled`: private torrent in `tv-sonarr` with `progress 0.4` and hash in no Radarr/Sonarr queue → moved to `private-stalled`; same torrent with its hash in the Sonarr queue → not moved; complete (`progress 1`), public, or in `animes`/`music` → not moved.
  - `selftest_step_isolation`: one step raising `URLError` → exit 2, other steps still run.
- [ ] **Step 3:** Implement. qBittorrent: `setPreferences` for the three flags; `torrents/setShareLimits` (hashes grouped by target values, `inactiveSeedingTimeLimit=-1`). Set-aside: read `GET /api/v3/queue?pageSize=1000` from Radarr and Sonarr (keys `UN_RADARR_0_API_KEY`, `UN_SONARR_0_API_KEY` from Secret `unpackerr-secret`), compare `downloadId` case-insensitively, then `torrents/setCategory` to `private-stalled`; skip the step if either queue read fails. Cleanuparr: reconcile seeding and stall rules through the API found in Step 1. DRY_RUN keeps working for every step.
- [ ] **Step 4:** Run the test script and `kustomize build 2-k3s/maintenance`. Expected: PASS, CronJob references the generated ConfigMap.
- [ ] **Step 5:** Commit `feat(maintenance): reconcile per-tracker seeding limits hourly`.

### Task 3: Prowlarr seed goals

**Files:**
- Modify: `2-k3s/maintenance/files/cleanuparr-policy.py`, `2-k3s/maintenance/cleanuparr-policy-cronjob.yaml` (env `PROWLARR_API_KEY` from Secret `prowlarr-api-key`, env `PROWLARR_URL=http://prowlarr:9696`)

**Interfaces:**
- Consumes: `rule_for`, `seed_goal_minutes` (Task 2).

- [ ] **Step 1: Failing selftests:**
  - `selftest_prowlarr_private_goals`: private indexer `IPTorrents` with `seedRatio 1, seedTime 43200` → `seedRatio null`, `seedTime 131040`, `packSeedTime 131040`; public indexer untouched; unknown private indexer name → default rule `131040`.
  - `selftest_prowlarr_body_untouched`: every field other than `torrentBaseSettings`, including masked `********` values, is sent back byte-identical.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement `GET /api/v1/indexer`, then `PUT /api/v1/indexer/{id}` only for drifted private indexers. Before relying on the PUT, confirm from Prowlarr source (or a DRY_RUN in Task 5) that a masked value is kept, not stored.
- [ ] **Step 4:** Run. Expected: PASS.
- [ ] **Step 5:** Commit `feat(maintenance): private indexers seed by time, not ratio`.

### Task 4: Hit-and-run and tracker checks with alerts

**Files:**
- Create: `2-k3s/maintenance/files/torrent-hnr-check.py`, `2-k3s/maintenance/torrent-hnr-check-cronjob.yaml` (two CronJobs: `torrent-hnr-check` hourly at :50 with `--mode early-stop`; `torrent-tracker-check` hourly at :10 with `--mode tracker`, `ttlSecondsAfterFinished: 23400`, `failedJobsHistoryLimit: 6`)
- Modify: `2-k3s/maintenance/kustomization.yaml`, `2-k3s/10.observability/alertmanager-config/custom-alerts.yaml`, `custom-alerts-tests.yaml`, the CronJob owner list at `custom-alerts.yaml:1135`

**Interfaces:**
- Consumes: `tracker-rules.json`, `rule_for` copy (Task 2), qBittorrent credentials as `qbt-stuck-torrent-check` uses them.
- Produces: `hnr_met(torrent: dict, rule: dict) -> bool | None` (`None` when the tracker has no rule).

- [ ] **Step 1: Failing selftests** in `torrent-hnr-check.py --selftest`:
  - `selftest_boundaries`: IPTorrents `ratio 1.0` met, `0.99` with `seeding_time 335.9 h` not met, `336.0 h` met; TorrentDay `72 h` met; AvistaZ 10 GB needs 92 h, 60 GB needs 1440 h, ratio 0.9 met; Mpellas → `None`.
  - `selftest_early_stop`: states `stoppedUP`, `stoppedDL`, `pausedUP`, `pausedDL` with rule not met → reported; `stalledUP` → not; category `private-stalled` or `ratio-seed` → not; public torrent → not.
  - `selftest_unknown_host`: private torrent on an unknown host uses the default rule.
  - `selftest_tracker_mode`: a private torrent whose trackers all have status 4 (not working) → reported; one working tracker → not.
  - `selftest_lookalike_host`: a private torrent on `notmagtre.com` (ends with a listed host but not equal to it and not a subdomain of it) → reported in early-stop mode as `lookalike host`, because Cleanuparr's boundary-less suffix match would give it the shorter tracker rule.
  - `selftest_no_names`: report lines contain the hash prefix and category, never the name.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement. Read-only: `auth/login`, `torrents/info`, `torrents/trackers?hash=`. Exit 0 clean, 1 findings, 2 could not run.
- [ ] **Step 4: Alerts with promtool tests:**
  - `TorrentPrivateStoppedEarly`: latest `torrent-hnr-check` Job failed (same expression shape as `qbt-stuck-torrent-check`).
  - `TorrentTrackerNotWorking`: the six newest `torrent-tracker-check` Jobs by start time all failed, counted once per Job (`topk(6, …)` over `kube_job_status_start_time`). With the 6.5 h TTL this means six hourly runs in a row.
  - Tests: one failed run fires the first alert and not the second; six failed tracker runs fire the second; five do not.
- [ ] **Step 5:** Run the test script, `kustomize build 2-k3s/maintenance` and the promtool step from `ci.yml`. Expected: PASS.
- [ ] **Step 6:** Commit `feat(maintenance): alert on private torrents stopped early or tracker down`.

### Task 5: Rollout (owner confirms each live step)

- [ ] **Step 1:** Squash or rewrite the branch so no outgoing commit contains the removed release titles (check `git log -p origin/main..` before pushing). Before the merge, confirm Cleanuparr's orphaned-file purge is off (it deletes files directly, even in Dry Run), turn on Cleanuparr's own Dry Run (General settings) and confirm in its log that it is on: no Cleanuparr job deletes anything from here until Spyros approves the candidate list in Step 4, including the old `private stalled` rule. With Spyros's yes, one PR, review gate, merge. Request Argo sync of `servarr` and `maintenance` at the merged SHA, dry-run first, per AGENTS.md.
- [ ] **Step 2:** The merged manifests run in report mode (`SEEDING_POLICY=report`). Read the Cleanuparr init container log right after its roll (it fails Cleanuparr's start if a public template is missing), then the next `cleanuparr-policy` log: PENDING lines list the Cleanuparr, qBittorrent and Prowlarr changes, field names only. Stop if any line is unexpected. Cleanuparr cleans nothing while Dry Run is on, so keep the gap to Step 3 short for disk space.
- [ ] **Step 3:** Before the enforce PR: confirm Cleanuparr Dry Run is still on and Radarr and Sonarr Remove Completed is off. With Spyros's yes, merge the PR setting `SEEDING_POLICY: enforce` on both, sync.
- [ ] **Step 4:** While Cleanuparr is still in Dry Run: check the init log and the first enforcing `cleanuparr-policy` run; confirm qBittorrent global flags off, private torrents in Cleanuparr categories have `ratio_limit -1` and action `Stop`, Prowlarr private indexers show no seed ratio; read back the synced indexers in Radarr and Sonarr (`GET /api/v3/indexer`) and stop if they did not follow Prowlarr. After one completed enforcing Download Cleaner run, list the would-delete candidates: take the Dry Run `DownloadCleaned` events' `itemHash` values (Cleanuparr's log has names and reasons, not hashes or sizes), join them to a read-only qBittorrent `torrents/info` snapshot, and show hash prefix, count and total size only; read every page of events for that exact job run; the list counts only if that same run logged no login, listing, seeding-evaluation, cleanup-action or event-write error (`Failed to clean downloads` included; `Finished cleanup evaluation` alone proves nothing) and logged `Evaluating N downloads for cleanup` for qBittorrent, and its `download cleaned` log lines equal the number of Dry Run events. Zero deletions after that evaluation line is a valid result; no evaluation line counts only if the read-only qBittorrent snapshot shows no completed private torrent in a Cleanuparr category. Otherwise keep Dry Run on and repeat; if any event cannot be matched, keep Dry Run on. Also show the first `torrent-hnr-check` and `torrent-tracker-check` findings. Turn Dry Run off only with Spyros's yes on that list.
- [ ] **Step 5:** With Spyros's yes, resume private torrents in Cleanuparr categories that are stopped. List the count first.
- [ ] **Step 6:** Confirm the first `torrent-hnr-check` and `torrent-tracker-check` runs. Report findings by hash prefix. If stopped torrents keep a stale "not working" tracker status and make the tracker check noisy, report it.
