# Torrent cleanup without hit-and-runs

## Why

Radarr and Sonarr upgrades leave the old torrents in qBittorrent. Tdarr conversions break the library hardlink, so a converted file and its torrent each hold a full copy. Disk use grows, and nothing removes the stopped torrents.

Three things stop or delete torrents today, each with its own limits and none aware of tracker rules:

- Prowlarr seed goals, which Radarr and Sonarr pass to qBittorrent per torrent. IPTorrents and Mpellas stop at ratio 1, so a private torrent can stop after 0 hours of seeding and never earn surplus.
- Cleanuparr Download Cleaner: private ratio 5 or 720 h, public ratio 1 or 400 h, minimum seed time 0. A torrent stopped by qBittorrent stops counting seed time, so Cleanuparr never reaches its limit. 100 stopped torrents (about 350 GB) sat untouched on 2026-11-20.
- Cleanuparr Queue Cleaner stall rule `private stalled`: a private download 0–99% complete that stays stalled for 30 hourly strikes is deleted from qBittorrent (`delete_private_torrents_from_client=1`). On a tracker without a small-download exemption, such as TorrentDay, that is a hit-and-run.
- Manual deletes, including "mark as failed" in Radarr or Sonarr, which also removes the torrent because `removeFailedDownloads` is on.

TorrentDay already shows 13 hit-and-runs for torrents no longer in qBittorrent. Spyros decided not to clear them with bonus points.

### What deleted the 13 (checked 2026-11-20)

Sonarr history has 501 `downloadFailed` events with the message "Manually marked as failed", the text Sonarr records when Cleanuparr or a person removes a queue item. Most fall on the hour, matching Cleanuparr's hourly Queue Cleaner.

- 2 episodes: grabbed in August, marked failed on the hour 5 days later, after a better release was imported. Consistent with the stall rule.
- 2 episodes: grabbed and marked failed within 2 hours, off the hour. Most likely by hand or another tool.
- 4 episodes: grabbed and imported the morning after the account was restored. The site shows 0 h seeded. The deleter is not in the remaining data; the tracker may have refused announces then. Not confirmed.
- 4 episodes: no Sonarr history left. Unknown.
- 1 item: not grabbed by Radarr or Sonarr. Manual.

Spyros wants cleanup that frees disk space and never causes a hit-and-run, including on trackers added later.

## Tracker rules (read from each site on 2026-11-20)

| Tracker | Hit-and-run rule | Source |
|---|---|---|
| IPTorrents | Ratio 1:1 or 336 h. Warning after more than 40 torrents unseeded for 120 h | `iptorrents.com/rules` |
| TorrentDay | Ratio 1:1 or 72 h. Warning after more than 40 torrents unseeded for 72 h. The site's peer page counts, not the client | `tday.love/rules.php`, `faq.php` |
| AvistaZ | Ratio 0.9, or 72 h + 2 h per GB under 50 GB. From 50 GB, a chart (an image, not read). Never a hit-and-run after 60 days. Under 10% downloaded is exempt | `avistaz.to/rules/hit-run-rules` |
| Mpellas | No seeding obligation | `mpellas.me/?p=faq&pid=12` |

## Goals

- G1: One tool deletes completed Radarr, Sonarr, anime and music torrents: Cleanuparr Download Cleaner. No automated tool deletes an incomplete private download from qBittorrent (selected files not finished). A torrent finished on a partial file selection counts as complete: its ratio and seed time are measured on what was downloaded, so the Download Cleaner limits (ratio 5, or 2160 h; the default also 1440 h) still exceed every tracker rule.
- G2: No torrent is stopped or deleted before its tracker's rule is met, with margin for the site counting less seed time than the client. This is best-effort: Cleanuparr and qBittorrent count client seed time and cannot see tracker credit. The margin (2160 h against IPTorrents' 336 h) covers about 76 days of failed announces, and the tracker alert fires after 6 hours of them, so a human acts long before the margin runs out. No automated tool can prove tracker credit without logging in to the site, which is an anti-goal.
- G3: Private torrents keep seeding past ratio 1 so they earn surplus.
- G4: A tracker without a known rule gets a strict default.
- G5: The rules live in Git and are put back if they drift.
- G6: An alert fires when a private torrent is stopped before its rule is met, or its trackers stop working. `missingFiles` stays with the existing stuck-torrent check; a torrent already removed from qBittorrent is a manual check on the site.

## Non-goals

- Clearing the 13 TorrentDay hit-and-runs.
- Radarr upgrade behaviour (Configarr cutoff, Remux, the stale DTS score on Tdarr-converted files). Spyros kept those as they are.
- Torrents outside the Cleanuparr categories (`avistaz-reseed`, `ratio-seed`, `manual-import`, `private-stalled`, none).

## Anti-goals

- Any automated process logs in to a tracker website.
- Radarr or Sonarr "Remove Completed" is turned on.
- A deletion of a library file or its hardlink beyond what Cleanuparr does today.

## Approaches

- O8: Cleanuparr is the only deleter, with per-tracker rules in Git. **Chosen by Spyros.**
- O9: Radarr/Sonarr Remove Completed with Prowlarr seed goals at tracker minimums. Rejected: goals live outside Git, torrents still stop at ratio 1, and Cleanuparr remains a second deleter.
- O10: qbit_manage. Rejected: duplicates Cleanuparr.

## Design

### Cleanuparr rules

`2-k3s/08.servarr/cleanuparr/files/enforce-private-seeding.py` already runs as an init container and writes the qBittorrent seeding rules into Cleanuparr's SQLite database. It gains per-tracker private rules using the existing `tracker_patterns` and `min_seed_time` columns, for each category (`radarr`, `tv-sonarr`, `animes`, `music`):

| Rule | Tracker patterns | Max ratio | Min seed | Max seed |
|---|---|---|---|---|
| IPTorrents | IPT announce hosts | 5.0 | 1440 h | 2160 h |
| TorrentDay | TD announce hosts | 5.0 | 1440 h | 2160 h |
| AvistaZ | `tracker.avistaz.to` | 5.0 | 1440 h | 2160 h |
| Mpellas | `magtre.com` | 5.0 | 1440 h | 2160 h |
| Default private | any | 5.0 | 1440 h | 2160 h |
| Public (unchanged) | any | 1.0 | 0 | 400 h |

Each rule's limits meet the tracker's rule with margin: ratio 5 is above 1:1 and 0.9, and every max seed is at least twice the time rule. AvistaZ's 60-day exemption is worded as seed time ("You can seed it for 60 days"), so its 50 GB+ case gets 2160 h, 1.5 times 60 days (builder's call, made in review). Tracker rules sit at a higher priority than the default private rule.

Git is the source of truth. The rules are reconciled on a schedule while Cleanuparr runs, through its API in `cleanuparr-policy-cronjob.yaml`, and UI edits are overwritten, as that job already does for the blocklist settings. If Cleanuparr's API cannot write seeding rules, the init script stays and must update existing rules, not only add missing ones (builder's call, made in review).

### Prowlarr seed goals

Private indexers stop setting a seed ratio and set a seed time above the Cleanuparr max for that tracker, so qBittorrent never stops a private torrent before Cleanuparr deletes it. Public indexers keep their goals or none. The values are enforced from Git by a drift check in the same style as `cleanuparr-policy-cronjob.yaml`.

A torrent grabbed without a seed ratio uses qBittorrent's global share limits. These were off on 2026-11-20 (`max_ratio_enabled` and `max_seeding_time_enabled` false). The drift check keeps them off, so the out-of-scope categories are unaffected.

### Hit-and-run comes first (Spyros, 2026-11-21)

Spyros: "The hit and run must be avoided at all costs." Two consequences, accepted as cost:

- Every private rule uses a 2160 h (90-day) floor, so an unknown, mixed or lookalike tracker host can never get a rule shorter than the strictest known hit-and-run rule (AvistaZ 50 GB+: 1440 h). Per-tracker rules stay in the table so a tracker can be relaxed later. Private torrents seed up to 90 days unless they reach ratio 5.
- A private Prowlarr indexer missing from `tracker-rules.json` is a drift finding every hour until its hosts are added.
- Managed qBittorrent categories inherit the (disabled) global share limits; a category limit or remove action is drift and is reset, because qBittorrent 5.3 applies it to a fresh grab before the hourly job runs.
- Every private rule needs ratio 5 **and** 1440 h (`min_seed_time`) before ratio can delete, because Cleanuparr gives a tracker's rule to any torrent with one matching host (lookalike or mixed hosts included). The unknown-tracker alert rule is time only (1440 h), because an unreviewed tracker may require seed time whatever the ratio.
- Queue Cleaner's qBittorrent checks (stall, slow, skipped files) never touch private torrents; its Radarr/Sonarr failed-import check can still remove the item from the queue, and keeps the torrent in qBittorrent (`deletePrivate=false`) (Spyros accepted the residual on 2026-11-21: a tracker added later, its torrent's first hour, every file skipped by hand). Cleanuparr 2.11.1 deletes a torrent whose files are all skipped, private or not, with no setting to stop it (`QBitServiceQC`). Queue Cleaner's `ignoredDownloads` gets every listed private host (effective at once) and the tag `private-guard`, which the hourly job adds to every private torrent (covers trackers added later within an hour). Stalled private downloads therefore stay in the Radarr or Sonarr queue for a manual decision instead of being re-grabbed. Metadata strikes are 0, because a magnet's privacy is unknown until its metadata arrives.

### Queue Cleaner

The `private stalled` stall rule (0–99%) changes to `delete_private_torrents_from_client=0`, keeping `change_category=0`. With private torrents on Queue Cleaner's ignore list this is a backstop only. Cleanuparr cannot choose the target category itself: `change_category` asks Radarr or Sonarr to apply their Post-Import Category, which would also move every normal import out of the cleanup categories.

The hourly reconcile moves the set-aside torrents to qBittorrent category `private-stalled` (chosen by Spyros as O14). Since private torrents are ignored by Queue Cleaner, this catches items removed from the Radarr or Sonarr queue by hand or by a failed import: a private torrent in category `radarr` or `tv-sonarr` that is incomplete and whose hash is in no Radarr or Sonarr queue. `animes` and `music` are skipped because other tools (Lidarr, seadexarr) add torrents there without a Radarr or Sonarr queue entry. It stays there, outside the Download Cleaner categories, for Spyros to decide by hand. The reconcile enforces the stall rule values from Git.

The alert does not count torrents in `private-stalled` as stopped early, because they are already set aside for a manual decision.

### Stopped torrents today

After rollout, the stopped private torrents are resumed so they count seed time again and Cleanuparr can reach its limits. This needs Spyros's confirmation at rollout.

### Rollout gate

Argo CD syncs `main` automatically. The new write paths ship in report mode (`SEEDING_POLICY=report` on the Cleanuparr init container and the `cleanuparr-policy` CronJob): they print what they would change and write nothing. A second PR sets `enforce` after Spyros has reviewed that output.

### Alert

A CronJob in `2-k3s/maintenance/`, modelled on `qbt-stuck-torrent-check-cronjob.yaml`, reads qBittorrent only. It fails (and so alerts through the existing failed-Job alerting) when a private torrent in a Cleanuparr category is in a stopped state (`stoppedUP`, `stoppedDL`, or the older `pausedUP`/`pausedDL`) and does not meet its tracker's hit-and-run rule by client values (ratio or time where the tracker allows it; time only for an unknown tracker). Stalled torrents are still announcing, so they don't count. Manual stops count. Errors and missing files stay with the stuck-torrent check. The tracker rules (ratio, hours, AvistaZ's per-GB formula) are one table in Git, read by both the Cleanuparr reconcile and the alert (builder's call, made in review).

It also fails when a private torrent's tracker has reported "not working" in qBittorrent for longer than a threshold, because the site then counts no seed time while the client does (chosen by Spyros as O11).

It cannot see torrents already removed. Hit-and-run pages stay a manual check by Spyros.

## Left to the builder

- Confirm Cleanuparr's `tracker_patterns` matching (substring, regex, host) and rule priority order from its source, and that `min_seed_time` / `max_seed_time` units are hours.
- Choose where the shared tracker-rule table lives so the init script and the alert read the same values.
- Read and write Prowlarr seed settings through its API (`torrentBaseSettings`), confirm app-sync pushes them to Radarr and Sonarr, and decide how existing torrents get the new per-torrent limits (they keep the limits set at grab time).
- Collect the full announce host list per tracker from qBittorrent (IPTorrents uses several).
- Pick the "not working" threshold for the alert so a short tracker outage does not fire it.
- Rollout order: Cleanuparr rules first, then Prowlarr goals, then resume stopped torrents, then the alert.
