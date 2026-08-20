# Newtarr & Cleanuparr Database Recovery

> **Note (#131, 2026-05-31):** Huntarr was renamed to **Newtarr** (ElfHosted
> fork, `ghcr.io/elfhosted/newtarr:rolling`); the config PVC is now
> `newtarr-config` and the Deployment/Service/PDB are `newtarr`. The historical
> commands below are preserved verbatim and still reference the old `huntarr`
> Deployment, `huntarr-config` PVC, and `huntarr.db*` filenames — substitute the
> `newtarr` equivalents when running this runbook today. The on-disk SQLite file
> may remain `huntarr.db` if the fork keeps the original filename, so do NOT
> blindly rename it (see TODO in `newtarr/newtarr.yaml`).

## Recovery Date
February 3, 2026 (original; Huntarr-era)

## Problem
- huntarr.epaflix.com and cleanuparr.epaflix.com showed no data from previous installation
- User suspected PostgreSQL was never migrated for these apps

## Investigation
Both apps use **SQLite databases** stored in config directories on local-path PVCs, not PostgreSQL.

### Huntarr
- Current PVC: `pvc-47b294c2-1bd8-4c11-a95a-874873102d3b` on worker-61
- **Already using old PVC from previous cluster** but database was smaller (248KB)
- Found better backup on worker-62: `huntarr.db` (372KB, last modified Feb 1, 2026)

### Cleanuparr
- Current PVC: `pvc-524fa1cf-3fc1-4d1c-8840-dad08d76805e` on worker-65
- Had fresh/smaller database (228KB created Feb 2)
- Found old backup on worker-62: `cleanuparr.db` (228KB, Jan 25) + `events.db` (216KB, Jan 30)

## Old PVC Locations (Pre-Cluster Crash)
Found on worker-62:
```bash
/var/lib/rancher/k3s/storage/pvc-c6a9b9fb-ae95-47c7-898b-7df4fe93114b_servarr_huntarr-config/
├── huntarr.db (372KB, Feb 1 11:23)
├── logs.db (11MB, Feb 1 11:33)
└── backups/ (5 backups)

/var/lib/rancher/k3s/storage/pvc-94339164-2f53-4459-90eb-2dba82c474a2_servarr_cleanuparr-config/
├── cleanuparr.db (228KB, Jan 25 09:44)
├── events.db (216KB, Jan 30 23:32)
└── logs/ (69 log files from Dec 3, 2025 to Jan 31, 2026)
```

## Recovery Procedure

### 1. Scale Down Apps
```bash
kubectl --context epaflix -n servarr scale deployment huntarr --replicas=0
kubectl --context epaflix -n servarr scale deployment cleanuparr --replicas=0
```

### 2. Copy Old Databases from Worker-62
```bash
# Huntarr
scp ubuntu@192.168.10.62:/var/lib/rancher/k3s/storage/pvc-c6a9b9fb-ae95-47c7-898b-7df4fe93114b_servarr_huntarr-config/huntarr.db /tmp/
scp ubuntu@192.168.10.62:/var/lib/rancher/k3s/storage/pvc-c6a9b9fb-ae95-47c7-898b-7df4fe93114b_servarr_huntarr-config/logs.db /tmp/

# Cleanuparr
scp ubuntu@192.168.10.62:/var/lib/rancher/k3s/storage/pvc-94339164-2f53-4459-90eb-2dba82c474a2_servarr_cleanuparr-config/cleanuparr.db /tmp/
scp ubuntu@192.168.10.62:/var/lib/rancher/k3s/storage/pvc-94339164-2f53-4459-90eb-2dba82c474a2_servarr_cleanuparr-config/events.db /tmp/
```

### 3. Restore to Current PVCs
```bash
# Huntarr (worker-61)
scp /tmp/huntarr.db /tmp/logs.db ubuntu@192.168.10.61:/tmp/
ssh ubuntu@192.168.10.61 'sudo rm -f /var/lib/rancher/k3s/storage/pvc-47b294c2-1bd8-4c11-a95a-874873102d3b_servarr_huntarr-config/huntarr.db*'
ssh ubuntu@192.168.10.61 'sudo rm -f /var/lib/rancher/k3s/storage/pvc-47b294c2-1bd8-4c11-a95a-874873102d3b_servarr_huntarr-config/logs.db*'
ssh ubuntu@192.168.10.61 'sudo cp /tmp/huntarr.db /var/lib/rancher/k3s/storage/pvc-47b294c2-1bd8-4c11-a95a-874873102d3b_servarr_huntarr-config/'
ssh ubuntu@192.168.10.61 'sudo cp /tmp/logs.db /var/lib/rancher/k3s/storage/pvc-47b294c2-1bd8-4c11-a95a-874873102d3b_servarr_huntarr-config/'
ssh ubuntu@192.168.10.61 'sudo chown 568:568 /var/lib/rancher/k3s/storage/pvc-47b294c2-1bd8-4c11-a95a-874873102d3b_servarr_huntarr-config/*.db'

# Cleanuparr (worker-65)
scp /tmp/cleanuparr.db /tmp/events.db ubuntu@192.168.10.65:/tmp/
ssh ubuntu@192.168.10.65 'sudo rm -f /var/lib/rancher/k3s/storage/pvc-524fa1cf-3fc1-4d1c-8840-dad08d76805e_servarr_cleanuparr-config/*.db*'
ssh ubuntu@192.168.10.65 'sudo cp /tmp/cleanuparr.db /var/lib/rancher/k3s/storage/pvc-524fa1cf-3fc1-4d1c-8840-dad08d76805e_servarr_cleanuparr-config/'
ssh ubuntu@192.168.10.65 'sudo cp /tmp/events.db /var/lib/rancher/k3s/storage/pvc-524fa1cf-3fc1-4d1c-8840-dad08d76805e_servarr_cleanuparr-config/'
ssh ubuntu@192.168.10.65 'sudo chown 568:568 /var/lib/rancher/k3s/storage/pvc-524fa1cf-3fc1-4d1c-8840-dad08d76805e_servarr_cleanuparr-config/*.db'
```

### 4. Scale Up Apps
```bash
kubectl --context epaflix -n servarr scale deployment huntarr --replicas=1
kubectl --context epaflix -n servarr scale deployment cleanuparr --replicas=1
```

## Verification

### Huntarr Status
```bash
kubectl --context epaflix -n servarr logs huntarr-847bd4b69f-j9klj --tail=50
```

**Results:**
- ✅ **8 items tracked** (4 in sonarr + 4 in sonarr2)
- ✅ State management working: next reset 2026-02-07 (168h interval)
- ✅ User authentication ready
- ✅ Templates loaded successfully
- ✅ Next cycle scheduled for 2026-02-03 16:44:23

### Cleanuparr Status
```bash
kubectl --context epaflix -n servarr logs cleanuparr-c8c65cb64-lx75l --tail=30
```

**Results:**
- ✅ Event cleanup service started (4h interval, 30 days retention)
- ✅ BackgroundJobManager started
- ✅ Application started successfully

## Summary
- **Huntarr**: Restored 372KB database from Feb 1 with 8 tracked items across 2 Sonarr instances
- **Cleanuparr**: Restored 228KB database from Jan 25 + 216KB events database from Jan 30
- Both apps successfully started and loaded their configurations
- No PostgreSQL migration was needed - these apps use SQLite

## Key Insight
Newtarr (formerly Huntarr) and Cleanuparr are **configuration management tools** that store their settings and state in SQLite databases within their config directories. They don't have dedicated PostgreSQL databases like the *arr apps (Sonarr, Radarr, etc.).

The databases were preserved in old PVCs on worker-62 from before the cluster crash and have now been successfully restored to the new cluster.

---

## Incident 2026-05-31 — Cleanuparr seriesId 40 S04E13 strike runaway (#138)

**Symptom:** Cleanuparr reported "download keeps coming back after deletion" for
the Sonarr seriesId 40 / episodeId 3143 (S04E13) release
(hash `66a4dc6201cb149ff70eed12b9902317cb82ed87`), strikeCount up to 285, 76
Action Required events.

**Reality:** the active loop had already ended months earlier — all 76 events
dated 2026-03-26..03-29, the torrent was gone from qbittorrent, and Cleanuparr's
queue-cleaner striking for Sonarr was already disabled (per-arr
`failed_import_max_strikes=-1`). What remained was stale residue plus a
still-monitored+missing S04E13 that could **re-arm** via newtarr
(`hunt_missing_items=1`, 15-min cadence) combined with Sonarr
`autoRedownloadFailed=true`. The Sonarr blocklist is keyed by
**title+indexer+GUID, not infohash**, so cross-posted variants slipped past it.

**Fix applied (4 steps, all verified):**
1. Archived + resolved the 76 stale Cleanuparr `manual_events` (76 → 0).
2. Unmonitored Sonarr `episodeId 3143` (seriesId 40).
3. Cleanuparr content-blocker: created `/config/custom-blocklist-sonarr.txt`
   (850 upstream `flmorg/cleanuperr` entries + a regex matching the
   seriesId 40 / episodeId 3143 (S04E13) release name) and repointed
   `sonarr_blocklist_path` at it.
4. Confirmed no live S04E13 torrent. Cleanuparr healthy after restart.
   DBs backed up in-pod (`.bak-20260531-135309`).

> **Durability caveat (UPDATED #138 done):** the `custom-blocklist-sonarr.txt`
> file is now **codified in git** as a SOPS-encrypted seed Secret and restored on
> a fresh PVC by the cleanuparr `seed-config` initContainer (see the "#138" section
> below). The repointed `sonarr_blocklist_path` is SQLite-resident
> (`cleanuparr.db`) and remains the one **manual** action on a PVC rebuild. The
> custom list is still a static snapshot of upstream (won't auto-track updates,
> unlike the Radarr list which still uses the live URL) — refresh tracked under
> **#180**. A separate stalled torrent seriesId 40 S04E07
> (`828ea9eb36f00f821772d4d431dddf12ea6bd0c2`, `stalledDL`) is triaged
> independently in **#139**.

---

## Incident 2026-05-31 — Orphaned-stalled torrents (manual-remove orphans, #142)

**Symptom:** 12 dead 0-seed torrents stuck in qBittorrent (10 Sonarr seriesId 272
episodes + Sonarr seriesId 40 S04E07 (episodeId 3137) + Sonarr2 seriesId 36 S02E11
(episodeId 1990)) that neither Cleanuparr nor newtarr ever cleaned or replaced.

**Orphan mechanism:** a bulk **manual Sonarr "Remove from queue" on 2026-05-08
19:47 with "Remove from download client" UNCHECKED** marked the releases
`downloadIgnored` — the torrents stayed in qBittorrent but were no longer
referenced by any *arr queue. Cleanuparr's **QueueCleaner only strikes torrents
that appear in an arr queue**, so these orphans are invisible to it. Meanwhile the
underlying episodes stayed `missing + monitored`, and newtarr (v1.0.0,
`hunt_missing_items=1`, no dead-release blocklist) kept recycling dead 0-seed
grabs.

**Fix applied (4 steps, all verified):**
1. Deleted the 12 orphans **with data** (backups taken first); verified 139
   healthy seeders intact (none wrongly deleted).
2. Blocklisted 11/12 dead releases. **Sonarr seriesId 40 S04E07 (episodeId 3137)
   could not be blocklisted** — its Sonarr history row was already purged and Sonarr
   v3 has no API to add an arbitrary blocklist entry without a history id (low
   impact; already `hasFile=true`).
3. Triggered EpisodeSearch for the 13 missing seriesId 272 episodes (re-search grabs are
   currently 0-seed — a separate "no healthy release available" condition, not the
   orphan bug; needs soak).
4. **Left the Cleanuparr DownloadCleaner unlinked/orphan rule OFF** (verified still
   0). New grabs are now queue-tracked rather than orphaned.

> **⚠️ HISTORICAL (pre-#195) — the EXDEV topology below is now REMOVED.** As of
> 2026-06-08 the single-`/media`-mount migration (#195, see the "Option A —
> DELIVERED" section further down) collapsed the four child exports to one and
> hardlinks now work (`nlink>=2`). The warning below describes the OLD topology
> and is kept for history. The unlinked rule is STILL OFF, but now for a
> different reason (pre-fix copy-seeders, not EXDEV) — see that section.
>
> **⚠️ DO NOT enable Cleanuparr's DownloadCleaner unlinked/orphan rule in this
> topology — it would delete ALL the healthy imported seeders (~118 live as of
> 2026-06-07; was ~139 at the PR #144 firefight, decayed via normal
> ratio/seed-time expiry).** Downloads (`pool1/dataset01/downloads`) and library
> (`pool1/dataset01/{tvshows,animes,movies}`) are **plain directories on ONE ZFS
> dataset** (`pool1/dataset01`; all four report the same `st_dev`). Hardlinks are
> blocked **NOT by a dataset boundary** but because each media root is published as
> a **separate NFS4 export** (its own `fsid`), so a cross-export `link()` returns
> `EXDEV` — every imported seeder lands as `nlink=1`, indistinguishable from a true
> orphan. Sonarr2 also uses `copyUsingHardlinks=false`, and the Cleanuparr
> container has **no mount of the library roots** (verified: `cleanuparr.yaml`
> mounts only `config`→`/config` and `servarr-media-downloads`→`/data`), so it
> physically cannot tell a seeder from an orphan. Bottom line: the unlinked/orphan
> rule stays **OFF**. Safe automated reaping requires either the single-NFS-export
> Option A migration below (so intra-export hardlinks give `nlink>1`) AND mounting
> the library roots into Cleanuparr, or a category-scoped / seed-time / ratio
> guarded rule that never keys on `nlink`. Tracked in **#142** (cross-links #138
> PVC-only config, #131/#137 newtarr migration).

> **Operator rule:** when manually removing items from an *arr queue, ALWAYS tick
> **"Remove from download client"** (and usually **"Blocklist"**) — otherwise the
> torrent is orphaned out of QueueCleaner's reach.

### Safe reaper — already in place (#142 deliverable #2)

The **safe automated reaper** that #142 deliverable #2 calls for **already exists
and is enabled** — no live DB change was made for this close-out:

- **Cleanuparr `download_cleaner_configs` → `q_bit_seeding_rules`** (`enabled=1`),
  per-category (`radarr`, `tv-sonarr`, `animes`), gated **purely** on `max_ratio=1.0`
  / `max_seed_time=400h`. It **never keys on `nlink`**, so it cannot touch an
  `nlink=1` imported seeder.
- **QueueCleaner stall rules** (public `max_strikes=3`), which only strike torrents
  still **referenced in an *arr queue**.

This seeding-rule + QueueCleaner combination is what **#142 #2 was closed
against**.

> **⚠️ Scope caveat (#614).** Between them these two see *completed* torrents
> (seeding rules) and *arr queue rows* (QueueCleaner) - an **incomplete torrent
> with no queue row is covered by neither**. Do not read this section as full
> orphan coverage. Source-level detail is in the **#249** section at the end of
> this document; #483 is the reaper meant to close the gap, and it is disarmed.

The
`nlink` / `unlinked_configs` detector stays **OFF** (`enabled=0`) until the
single-NFS-export Option A migration below lands. The Cleanuparr DB is PVC-only
SQLite (durability class per **#138**), so this state is not codified — any change
is a manual repoint.

> *(Optional future tightening — a stalled-age / 0-seed rule scoped to a DOWNLOAD
> category, never `nlink` — is explicitly NOT this run and must be soak-gated.)*

### seriesId 272 soak — PASSED

The post-fix re-search soak **passed**. seriesId 272 **S03E04** went from a
0-seed grab→fail loop to a **seeded release grabbed AND `downloadFolderImported`
on 2026-06-01** (now seeding healthily, `prog=1.00`, `hasFile=true`). Series state
is **27/36 `hasFile`**, 9 missing + monitored. Residual
grab→stall→Cleanuparr-strike→"manually marked as failed" churn persists **only** on
episodes with **no healthy release available** (S03E03 / S03E05 / S03E08) and is
the **QueueCleaner stall rule working as designed** (3 strikes → fail + blocklist),
**NOT an orphan regression** — 0 unreferenced + incomplete torrents exist.

### newtarr `hunt_missing_items` — KEEP at 1

`newtarr hunt_missing_items=1` is **KEPT** (do **NOT** bump). It is **GLOBAL across
BOTH Sonarr instances** (#135). Raising it would multiply the `seasons_packs`
add-search-on-add race and the seriesId 272 no-healthy-release churn for **zero
orphan benefit** — the orphan class was a one-time **manual operator action**, not a
hunt-cadence symptom. Cross-link **#135**.

### Option A — heavyweight fix (DEFERRED, trigger-gated)

The heavyweight fix is **medium-risk / low-urgency** (orphan recurrence is
currently **zero**) and is **DEFERRED to its own gh issue**. Steps, when triggered:

1. Collapse to **ONE NFS export** of `/mnt/pool1/dataset01` (so intra-export
   `link()` works — **no data move** required, all four roots are already on one
   dataset).
2. Remap qbt save path + Sonarr / Sonarr2 / Radarr root folders + media PV/PVC
   `subPath`s onto the single export.
3. Set `copyUsingHardlinks=true`.
4. Mount the library roots **read-only** into Cleanuparr.
5. Enable the `unlinked` rule in **dry-run first**, then live.

**Documented trigger to revisit: only if orphan recurrence returns.** Follow-up
issues will be opened — (1) the Option-A migration; (2) codify / runbook the
PVC-only Cleanuparr DB state per **#138**; (3) optional owner-gated quieting of the
seriesId 272 no-healthy-release loop.

### Option A — DELIVERED 2026-06-08 (#195): single `/media` mount, EXDEV barrier REMOVED

Issue **#195** delivered the single-NFS-export migration. The EXDEV barrier
described in the "DO NOT enable" warning above is now **structurally REMOVED** for
the five media pods. State of play:

- **Unified export.** The four child exports of `/mnt/pool1/dataset01`
  (animes/downloads/movies/tvshows) were collapsed to **ONE** parent export
  (id **32**, apps:apps 568) of `/mnt/pool1/dataset01`. One export = one on-wire
  `fsid`, so a cross-directory `link()` no longer returns `EXDEV`.
- **Single `/media` mount (no subPath).** All five media pods (qbittorrent /
  sonarr / sonarr2 / radarr / cleanuparr) now mount the whole export at a SINGLE
  `/media` mount — **no subPath**. Path mapping:
  `qbt downloads -> /media/downloads`, `sonarr -> /media/tvshows`,
  `sonarr2 -> /media/animes`, `radarr -> /media/movies`.
- **Root folders repointed (DB-only, no byte move).** Sonarr/Sonarr2/Radarr root
  folders repointed to `/media/{tvshows,animes,movies}` and qbt save path to
  `/media/downloads`; `copyUsingHardlinks=true` on sonarr/sonarr2/radarr.
- **Hardlink PROVEN.** A real in-pod import is confirmed hardlinked
  (`nlink>=2`, one device) — the #142 EXDEV barrier is gone.

> **⚠️ kubelet subPath = separate-submount EXDEV GOTCHA — never repeat it.**
> The FIRST attempt (PR #239) mounted the unified PVC via **subPath** per
> directory to keep container paths identical. This FAILED: the kubelet
> materializes **each subPath as a separate NFS submount** (its own mount,
> distinct device), so `link(2)` across `/downloads` and `/tv` still returned
> **EXDEV** — exactly the defect we were fixing. The fix (PR #242, issue #240)
> was to drop subPath entirely and mount the unified PVC **once** at `/media`;
> downloads + library are then subdirectories under one mount (one device) and
> hardlinks work. **Rule: to hardlink across two paths in one pod they must share
> a single non-subPath volumeMount.**

> **Reaper still OFF / DEFERRED — pre-fix copy-seeder reason.** Even though
> hardlinking is now unblocked, the Cleanuparr **unlinked/orphan rule remains
> OFF (`enabled=0`)**. A dry-run found **96 of 97** candidates are **pre-fix
> copy-seeders** — they were imported BEFORE the hardlink fix and are genuinely
> `nlink=1` (real seeders, not orphans). Arming the rule now would delete ~96
> healthy seeders. The rule will be armed only after those pre-fix copies age out
> (re-run dry-run; arm when false-positives = 0) — tracked in a follow-up issue.
> So #195 **UNBLOCKS** safe reaping but production orphan-reaping is **NOT yet
> active**.

> **Teardown DEFERRED.** The four OLD child exports + their node mounts + the old
> four PV/PVC are **retained** as the soak-window rollback (ZFS snapshot
> `pool1/dataset01@pre-unify-issue195`) AND because bazarr + lingarr still bind
> the old movies/tvshows claims. Teardown happens only after soak AND after
> bazarr/lingarr migrate to the unified `/media` mount (follow-up issues).
>
> **UPDATE 2026-07-31 (#247): teardown executed.** Both gates cleared (53d soak;
> bazarr/lingarr on unified `/media` since #251) and the rollback snapshot was
> already destroyed in the #444 pool1 reclaim, so the legacy PV/PVC, node
> mounts, and TrueNAS exports 19-22 were removed — see #247 for the full record.

> **PVC/live-only config (cross-ref #244, #196).** Two changes made during the
> #195 cutover live ONLY in PVC/live state, not in git: the Cleanuparr
> unlinked-rule DB config (SQLite `cleanuparr.db`, durability class per #138 —
> see #196) and the qbittorrent `LocalHostAuth=false` WebUI change (#244).
> Reconcile/codify per those issues.

## Incident 2026-07-10 — Cleanuparr blind for 27 days behind forward-auth (#176 fallout)

**Symptom:** stuck Sonarr queue items piling up again — 14 stalled dead-swarm
torrents, 22 importBlocked `Episode file already imported` (a series-314
season-pack + singles double grab), 20 importPending `Not an upgrade ...` —
plus 224 finished torrents left in qBittorrent `stoppedUP` (~1.85 TB of
download-dir data) never reaped.

**Root cause:** the #176 forward-auth rollout (2026-06-13) put
`qbittorrent.epaflix.com` behind Authentik with a then-current priority-20 `/api`
PathPrefix bypass (removed since, by #296 on 2026-08-10).
Cleanuparr's .NET `QBittorrent.Client` probes the LEGACY endpoint
`/version/api` first — not under `/api` — so it received the Authentik login
HTML and threw `FormatException` in `QBitService.LoginAsync()`. With no
download client initialized, QueueCleaner skipped EVERY item
(`skip | torrent not found in any torrent client`,
`failed_import_skip_if_not_found_in_client=1`) and Download Cleaner never
reaped. Sonarr itself was unaffected (its qbt client only calls `/api/v2/*`,
which the bypass covered); newtarr unaffected (already on internal URLs).

**Log signature:** `System.FormatException: The input string '...' was not in a
correct format` from `GetLegacyApiVersionPrivateAsync`, ~2,500/day in
`/config/logs/cleanuparr-*.txt.gz` from 2026-06-13 on; zero before.

**Fix (2026-07-10):**

1. Download client host repointed `https://qbittorrent.epaflix.com` →
   `http://qbittorrent:8080` (internal Service; UI edit, applied live — client
   health recovered immediately).
2. Added the 2 missing failed-import patterns `Not an upgrade for existing` +
   `Not a quality revision upgrade` (live queue messages use these strings; the
   existing 3 patterns did not match them). Applied via `sqlite3` UPDATE on the
   worker-61 PVC file (backup `cleanuparr.db.bak-20260710-patterns` next to it)
   + pod bounce — Cleanuparr's config API is JWT-gated, so DB edit + restart is
   the scripted path.

**Rule going forward:** an IN-CLUSTER consumer of another app uses the INTERNAL
Service URL (`http://qbittorrent:8080`, `http://sonarr:8989`, ...), never the
public `*.epaflix.com` hostname. When this incident happened the public route
still bypassed Authentik under `/api`, so `/api` callers survived and only
other paths (legacy qbt API, health endpoints) got the login page. Since #296
(2026-08-10) that bypass is gone on all six hosts, so for `sonarr`, `sonarr2`,
`radarr`, `prowlarr` and `bazarr` **every** path on the public hostname now
returns the Authentik redirect and a caller left there fails outright rather
than half-working. `qbittorrent.epaflix.com` is the exception and fails a
different way: it resolves to `192.168.10.102` (`traefik-internal`) where the
route carries no middleware, so qBittorrent itself answers: `200` on root,
`404` on the legacy `/version/api` probe and `403` from its own auth on
`/api/v2/*` - never an Authentik redirect. The internal Service URL is still
the only correct value for both cases.

**All three *arr instances were repointed to internal Service URLs by #468** -
the "still the public hostnames" note that used to sit here is out of date.
Verified live 2026-08-02:

| instance | `url` - actually called | `external_url` - display only |
|---|---|---|
| Sonarr | `http://sonarr.servarr.svc.cluster.local:8989` | `https://sonarr.epaflix.com/` |
| Sonarr2 | `http://sonarr2.servarr.svc.cluster.local:8989` | `https://sonarr2.epaflix.com/` |
| Radarr | `http://radarr.servarr.svc.cluster.local:7878` | `https://radarr.epaflix.com/` |
| qbittorrent (download client, `host`) | `http://qbittorrent:8080` | `https://qbittorrent.epaflix.com/` |

Cross-links: #176, #296, #287, #468.

### #561 — the last two callers on public hostnames (fixed 2026-08-03)

#468 covered Cleanuparr only. Two more in-cluster callers were still reaching
`*arr` APIs over the public hostnames, which is what kept `?apikey=` query
strings flowing through Traefik and into Loki (#702, #730). Both are now on
internal Service DNS. Verified live 2026-08-03.

**Seerr's `*arr` integrations** — Seerr has a per-instance `externalUrl`, which
is the same split Cleanuparr uses: `hostname`/`port` is called, `externalUrl` is
only rendered in the UI, so deep-links still work.

| instance | called | `externalUrl` - display only |
|---|---|---|
| Radarr | `http://radarr:7878` | `https://radarr.epaflix.com` |
| Sonarr | `http://sonarr:8989` | `https://sonarr.epaflix.com` |
| Sonarr2 | `http://sonarr2:8989` | `https://sonarr2.epaflix.com` |

**Each `*arr`'s own qBittorrent download client** — this was the largest single
source of logged API keys (222 lines in an 800-line Traefik log sample, versus
47/46/46 for the Seerr calls).

| app | download client `host` | port | `useSsl` |
|---|---|---|---|
| Sonarr | `qbittorrent` | `8080` | `false` |
| Sonarr2 | `qbittorrent` | `8080` | `false` |
| Radarr | `qbittorrent` | `8080` | `false` |

**Both are config-only, not in git** — Seerr's lives in `settings.json` on its
PVC (seeded by `seerr-config-seed.enc.yaml`, which is kept in step), and the
download-client entries live in each `*arr`'s Postgres DB. If an `*arr` DB is
ever restored from before 2026-08-03, re-apply the download-client table above
or the keys start leaking into Loki again.

**Apply them through the app's own API, not by editing files.** Each `*arr` has
`POST /api/v3/downloadclient/test` and Seerr has
`POST /api/v1/settings/{radarr,sonarr}/test` — test returns 200 before you save,
so a bad host is caught before it is persisted. Two traps found doing this:

- Editing an `*arr` `config.xml` by hand while the app is running does **not**
  reliably take. Radarr ended up on a value nobody chose. Use the API.
- Seerr's `PUT /api/v1/settings/sonarr/{id}` rejects a body containing `id`
  (`request.body.id is read-only`). Strip it from the object you GET first.

Effect, measured: `apikey=` lines in the Traefik log went from **20,984 per 24h**
(4,490 in the last hour before the change) to **0** — confirmed both on a 90s
live log tail and via Loki over 2m and 5m windows afterwards.

This does not remove the ~6 months of historical `?apikey=` lines already in
Loki; those are neutralised by the #730 rotation, not by this change.

Cross-links: #561, #466, #468, #702, #730.

> **⚠️ TRAP (#615) - the hostname Cleanuparr SHOWS you is not the one it CALLS.**
>
> Every instance carries both a `url` and an `external_url`. Cleanuparr **calls**
> `url` and **displays** `external_url`. In the v2.10.2 source the property is
> `ArrInstance.ExternalOrInternalUrl => ExternalUrl ?? Url`, and its only
> consumers are `NotificationPublisher.cs` and `EventPublisher.cs`. So the **UI
> event log** and **outbound notifications** render the public `*.epaflix.com`
> hostname while the real HTTP call goes to the internal Service.
> `DownloadClientConfig` has the identical pair.
>
> This is **upstream behaviour and intentional** - `external_url` exists to give
> a human a clickable link to the real UI. Nothing is misconfigured here, and
> clearing `external_url` would only break those links.
>
> **Consequence when debugging:** a public hostname in the UI event log is **not**
> evidence that #468 was reverted. It cost real time in the #482/#483 triage. To
> see what is actually being called, read the `url` column - never the UI:
>
> ```bash
> kubectl --context epaflix exec -n servarr deploy/cleanuparr -- python3 -c "import sqlite3
> c=sqlite3.connect('file:/config/cleanuparr.db?mode=ro',uri=True)
> for r in c.execute('SELECT name,url,external_url FROM arr_instances ORDER BY name'): print(r)"
> ```
>
> (Selects only the URL columns on purpose - `arr_instances` also holds
> `api_key`, so never `SELECT *` here.)
>
> **`arr_instances.api_key` makes Cleanuparr a consumer of every radarr /
> sonarr / sonarr2 API-key rotation** - PVC-only, so a git-side rotation does
> not reach it and Cleanuparr goes silently blind exactly like the #468
> forward-auth incident above. Rotation steps for all consumers:
> `2-k3s/08.servarr/README.md` → "Rotating the radarr / sonarr / sonarr2 API
> keys (#712)".
>
> The **console log** renders neither. It carries no *arr hostname at all, only
> the `[Sonarr]` / `[Radarr]` type tag, so the console cannot answer this
> question either.

## #196 — Cleanuparr safe-reaping DB state (restore runbook)

The safe-reaping configuration that **#142** relies on (the seeding rules +
QueueCleaner stall rules that purge healthy seeders by ratio/seed-time, and the
deliberately-OFF `unlinked`/orphan rule) lives **PVC-only inside the Cleanuparr
SQLite DB** (`/config/cleanuparr.db`, binary). It is **NOT git-seeded.** Unlike
the **#138** flat blocklist (`custom-blocklist-sonarr.txt`), which IS codified as
a SOPS seed + non-clobber initContainer, the rest of Cleanuparr's config is churny
binary SQLite the app rewrites at runtime — so a SOPS seed / drift-detector is the
wrong tool (binary, no stable diff). This section is the **runbook** instead: an
authoritative values table + a numbered fresh/reset-PVC restore procedure. On a
lost or reset `cleanuparr-config` PVC these settings come up **empty** and must be
**re-entered by hand in the UI**.

### (a) Authoritative values table

Sourced **LIVE** from a read-only copy of `/config/cleanuparr.db` on
2026-06-13 (`cp /config/cleanuparr.db /tmp/cu-ro.db` in-pod → `kubectl cp` out →
`sqlite3` SELECTs on the copy; **no writes, no rule changes, no pod restart**).
Download-client host + failed-import rows re-verified/updated 2026-07-10 (see
the 2026-07-10 incident section above).
All values below are live unless the source column says otherwise.

| Setting | Value | Where it lives (`cleanuparr.db` table) | Source |
|---|---|---|---|
| Download client (qBittorrent) host | `http://qbittorrent:8080` - INTERNAL Service URL, NEVER `https://qbittorrent.epaflix.com` (2026-07-10 incident: the public route bypassed Authentik only under `/api` at the time, and the qbt client probes legacy `/version/api`, which got the login page. #296 deleted that bypass, but the public name still must not be used: it resolves to `192.168.10.102` (`traefik-internal`), an un-gated route, so it answers `200`/`403` from qBittorrent itself and hides whether the credential or the gate is at fault) | `download_clients.host` | live |
| Download Cleaner enabled | `1` (ON), cron `0 0 0/1 ? * * *` (hourly) | `download_cleaner_configs.enabled` | live |
| qBit seeding rule — `radarr` | `max_ratio=1.0`, `min_seed_time=0`, `max_seed_time=400h`, `delete_source_files=1`, categories `["radarr"]` | `q_bit_seeding_rules` | live |
| qBit seeding rule — `tv-sonarr` | `max_ratio=1.0`, `min_seed_time=0`, `max_seed_time=400h`, `delete_source_files=1`, categories `["tv-sonarr"]` | `q_bit_seeding_rules` | live |
| qBit seeding rule — `animes` | `max_ratio=1.0`, `min_seed_time=0`, `max_seed_time=400h`, `delete_source_files=1`, categories `["animes"]` | `q_bit_seeding_rules` | live |
| QueueCleaner — enabled | `1` (ON), cron `0 0 0/1 ? * * *` (hourly) | `queue_cleaner_configs.enabled` | live |
| QueueCleaner stall — `Stall` (public) | `enabled=1`, `max_strikes=3`, `privacy_type=public`, `reset_strikes_on_progress=1` | `stall_rules` | live |
| QueueCleaner stall — `private stalled` (the `0-99` half of the #483 split) | `enabled=1`, `max_strikes=30`, `privacy_type=private`, `min_completion_percentage=0`, **`max_completion_percentage=99`**, **`delete_private_torrents_from_client=1`**, `reset_strikes_on_progress=1`, `change_category=0`, `minimum_progress=NULL` | `stall_rules` | live — **APPLIED 2026-08-02** (#618), see (c) |
| QueueCleaner stall — `private stalled 99-100` (the protected half) | `enabled=1`, `max_strikes=30`, `privacy_type=private`, **`min_completion_percentage=99`**, `max_completion_percentage=100`, **`delete_private_torrents_from_client=0`**, `reset_strikes_on_progress=1`, `change_category=0`, `minimum_progress=NULL` | `stall_rules` | live — **APPLIED 2026-08-02** (#618), see (c) |
| QueueCleaner slow — `Slow-rule` | `enabled=1`, `max_strikes=3`, `max_time_hours=336`, `min_speed=10KB` | `slow_rules` | live (not in earlier prose) |
| QueueCleaner failed-import strikes (global) | `failed_import_max_strikes=3`, pattern_mode `include`, 5 `failed_import_patterns`: `Episode file already imported`, `Not a Custom Format upgrade`, `One or more episodes expected in this release were not imported`, `Not an upgrade for existing`, `Not a quality revision upgrade` (last 2 added 2026-07-10) | `queue_cleaner_configs` | live |
| **Dry Run — OFF** | **`dry_run=0`** (reaper actually deletes; if `1` every rule silently no-ops) | `general_configs.dry_run` | live |
| **`unlinked`/orphan rule — DISABLED** | **`enabled=0`** (target_category `cleanuparr-unlinked`) | `unlinked_configs.enabled` | live |
| Per-arr `failed_import_max_strikes` (all 5 arr types) | `-1` = **inherit the global `3`** (NOT "disabled" as earlier revisions of this runbook claimed — upstream `ArrClient.ShouldRemoveFromQueue`: `0` disables, `>0` overrides, negative falls back to the global value) | `arr_configs` | live, semantics verified in source 2026-07-10 |

> **⚠️ DO NOT ARM the `unlinked`/orphan rule.** `unlinked_configs.enabled` MUST
> stay `0`. Even though #195 removed the EXDEV barrier (hardlinks now work,
> `nlink>1`), a dry-run found ~96/97 candidates are **pre-fix copy-seeders**
> (genuinely `nlink=1`, real seeders not orphans) — arming now would delete them.
> The flip `0 -> 1` is gated and owned by **#246** (arm only after pre-fix copies
> age out / dry-run false-positives = 0). See the #195 + #142 sections above and
> #249.

### (b) Fresh / reset-PVC restore procedure

Run in the Cleanuparr UI (`cleanuparr.epaflix.com`). Order matters — restore the
file seed first so the blocklist pointer has a target, then the reaping rules.

1. **Confirm the #138 blocklist file is present.** On a fresh PVC the
   `seed-config` initContainer restores `/config/custom-blocklist-sonarr.txt`
   automatically (non-clobber; see the "#138" section). Verify:
   `kubectl --context epaflix -n servarr exec deploy/cleanuparr -- ls -l /config/custom-blocklist-sonarr.txt`.
2. **Re-point the Sonarr content-blocker (manual, SQLite-resident).** UI →
   **Content Blocker → Sonarr**: set blocklist path =
   `/config/custom-blocklist-sonarr.txt`, confirm Sonarr content-blocker
   **enabled**. This pointer is the **same one manual DB step as #138 (ii)** — it
   is NOT file-seedable. (Radarr keeps the live upstream URL
   `https://raw.githubusercontent.com/flmorg/cleanuperr/refs/heads/main/blacklist`.)
   Verify: `content_blocker_configs.sonarr_blocklist_path` +
   `sonarr_enabled=1`.
3. **Re-create the download client with the INTERNAL URL.** UI → **Download
   Client**: qbittorrent, host `http://qbittorrent:8080` (NEVER the public
   `https://qbittorrent.epaflix.com` — see the 2026-07-10 incident section),
   WebUI credentials from the credential store (`qbittorrent_webui_username` /
   `qbittorrent_webui_password`). Then **re-enable Download Cleaner +
   qBit seeding rules.** UI → **Download Cleaner**:
   set enabled, then add one qBit seeding rule per category exactly as the table
   above — `radarr`, `tv-sonarr`, `animes`, each `max_ratio=1.0` /
   `max_seed_time=400h` / `delete_source_files=on`. These are gated **purely** on
   ratio/seed-time and **never key on `nlink`**, so they cannot touch an
   `nlink=1` imported seeder. Verify: `download_cleaner_configs.enabled=1` and
   three rows in `q_bit_seeding_rules`.
4. **Re-create QueueCleaner stall/slow rules.** UI → **Queue Cleaner**: enable,
   add the `Stall` (public, `max_strikes=3`), `private stalled`
   (private, `max_strikes=30`, `0-99`, delete-from-client **on**), the second
   private half `private stalled 99-100` (`99-100`, delete-from-client **off**),
   and `Slow-rule` (`max_strikes=3`,
   `max_time_hours=336`, `min_speed=10KB`) rules, and set the global
   failed-import strikes = `3` with the 5 include-patterns from the table
   above. QueueCleaner only
   strikes torrents still **referenced in an *arr queue**. Verify rows in
   `stall_rules` / `slow_rules` / `queue_cleaner_configs`.
5. **Leave per-arr `failed_import_max_strikes = -1` (= inherit the global `3`).**
   Earlier revisions called `-1` "striking disabled" — that is wrong. Upstream
   `ArrClient.ShouldRemoveFromQueue` treats `0` as disabled, `>0` as a per-arr
   override, and any negative value as "use the global
   `queue_cleaner_configs.failed_import_max_strikes`". Failed-import striking
   IS active at 3 strikes. Set `0` only if you deliberately want it off for an
   arr type.
6. **Leave the `unlinked`/orphan rule DISABLED.** Do NOT enable
   `unlinked_configs` (see the DO-NOT-ARM warning above; arming is #246's job).
7. **Confirm Dry Run is OFF.** UI → **General settings**: verify Dry Run is
   **disabled** (`general_configs.dry_run=0`). If Dry Run is ON the reaper
   silently no-ops — every rule above evaluates but deletes nothing.
8. **Restart + verify.** `kubectl --context epaflix -n servarr rollout restart deploy/cleanuparr`;
   confirm the pod is healthy and the Download Cleaner / Queue Cleaner schedules
   show in the logs. Re-confirm against live with a read-only DB copy if in doubt
   (same `cp … /tmp/cu-ro.db` + `sqlite3` recipe used to source this table —
   operate on a COPY, never the live DB).

> **Why runbook, not seed/detector.** The DB is binary SQLite that the app
> rewrites at runtime, so there is no stable text to diff or non-clobber-seed
> (the #138 precedent only seeded the flat `.txt`). The blocklist *pointer*
> (`sonarr_blocklist_path`) stays a **manual** re-entry on a PVC rebuild, exactly
> as #138 (ii) documents.

### (c) #483 / #618 — split the `private stalled` rule (APPLIED 2026-08-02)

**Status: APPLIED 2026-08-02 22:11 Oslo.** The values in the table above are
live. This section is now both the record of what was done and the recipe to
redo it on a fresh PVC.

**Why.** `private stalled` used to fire at `max_strikes=30` with
`delete_private_torrents_from_client=0`. When it fires it removes the *arr queue
row and **deliberately leaves the torrent in the client** — that is the
orphan-creation event in #483, and it happened automatically every time a private
torrent stalled for 30 hours. It is **not** the #142 manual-removal pattern, which
is why the #142 operator runbook could never have fixed it. Log proof, two
siblings on 2026-07-11 16:00: `strike 30` → `Removing item with max strikes` →
`queue item removed from arr`, with no client-side delete.

**Why the split and not just `delete_private_torrents_from_client=1`.** The rule
spanned `0-100`, so flipping the flag alone would also delete a private
torrent that stalled at 95% — one that may already owe seed time. The honest
version keeps the flag OFF near the top of the range:

- `0-99%` → delete from client **ON**. A private torrent that never completed has
  **no seed obligation and no hit-and-run exposure**: H&R attaches to a *snatch*
  (a completed download you then stop seeding). All five live #483 orphans are
  `ratio 0.00`, `uploaded 0 bytes`, never completed.
- `99-100%` → delete from client **OFF**, as today. A torrent at the top of the
  range is a real seeder and stays protected, consistent with
  `feedback_private_trackers` and the #249 decision.

**Boundary semantics, verified in v2.10.2 source**
(`Cleanuparr.Persistence/Models/Configuration/QueueCleaner/QueueRule.cs`,
`MatchesCompletionPercentage`): the lower bound is **exclusive** unless it is `0`,
the upper bound is **inclusive**. So `0-99` covers `[0, 99]` and `99-100` covers
`(99, 100]` — no overlap, no gap. Do **not** "fix" this to `0-98`/`99-100`; that
would leave `98 < pct <= 99` uncovered.

**Order matters, and an overlap is worse than a rejected save.**
`RuleIntervalValidator.FindAllOverlappingIntervals` rejects any
new rule that overlaps an existing one for the same `privacy_type`. Adding
`99-100` while `private stalled` still spans `0-100` overlaps at `99-100` and the
save is **refused**. Shrink the existing rule first. The validator is the only
thing standing between us and a silent outage: if two enabled rules ever did
match the same torrent, `QueueRuleManager.GetMatchingQueueRule` returns `null`
and logs `skip | multiple StallRule rules matched` - that torrent is then never
struck at all.

**No restart needed.** `Features/Jobs/QueueCleaner.cs` re-reads
`_dataContext.StallRules` into the run context at the **start of every run**, so
a config write is picked up by the next hourly run. There is no in-memory rule
cache to invalidate. Do not roll the pod for this.

#### Recipe - via the HTTP API (what was actually used, 2026-08-02)

The UI works too (see the variant below), but the API is scriptable, gives you
the HTTP status code, and reads back through the running app. Endpoints are
`GET|POST /api/queue-rules/stall` and `PUT|DELETE /api/queue-rules/stall/{id}`
(controller `Cleanuparr.Api/Features/QueueCleaner/Controllers/QueueRulesController.cs`).

Auth: Cleanuparr has its **own** login, separate from Authentik. Unauthenticated
calls to `/api/*` return `401`. The per-user API key lives in
`/config/users.db` → `users.api_key` and is sent as `X-Api-Key`. **Never print
it.** The container has `curl`, `openssl` and a `python3` (the apprise venv, so
`sqlite3` as a stdlib module) - it does **not** have a `sqlite3` binary.

Everything below runs **inside the pod**, so nothing that could hold a
credential is copied to a workstation.

1. **Back up first.** Use the SQLite backup API, not `cp` - the DB is in WAL
   mode, so a plain `cp` of `cleanuparr.db` silently drops anything still in
   `cleanuparr.db-wal`.

   ```bash
   POD=$(kubectl --context epaflix -n servarr get pod -l app=cleanuparr -o jsonpath='{.items[0].metadata.name}')
   kubectl --context epaflix -n servarr exec "$POD" -- python3 -c "
   import sqlite3
   src=sqlite3.connect('file:/config/cleanuparr.db?mode=ro',uri=True)
   dst=sqlite3.connect('/config/cleanuparr.db.bak-PRE-CHANGE')
   src.backup(dst); dst.close()"
   ```

   Also dump the rules as JSON (`GET /api/queue-rules/stall` → a file under
   `/config/`). That JSON is the thing you replay to revert.
2. **Shrink the existing rule** - `PUT /api/queue-rules/stall/{id}` with the
   full object: same `name`, `enabled: true`, `maxStrikes: 30`,
   `privacyType: "Private"`, `resetStrikesOnProgress: true`,
   `minCompletionPercentage: 0`, `maxCompletionPercentage: 99`,
   `deletePrivateTorrentsFromClient: true`, `changeCategory: false`.
   Expect `200`. The `PUT` is a full replace, not a patch - omitted fields fall
   back to DTO defaults (`maxStrikes` would drop to `3`), so always send the
   whole object.
3. **Add the protected top-of-range rule** - `POST /api/queue-rules/stall` with
   `name: "private stalled 99-100"`, `minCompletionPercentage: 99`,
   `maxCompletionPercentage: 100`, `deletePrivateTorrentsFromClient: false`,
   everything else as above. Expect `201`.
4. **Read it back through the running app**, not out of the DB file:
   `GET /api/queue-rules/stall`. A `200` on the write is not proof; the read-back
   is. Expect three rows - `Stall` (public, `0`-`100`, `3`),
   `private stalled` (private, `0`-**`99`**, `30`, delete-from-client **`1`**),
   `private stalled 99-100` (private, **`99`**-`100`, `30`, delete-from-client
   **`0`**). The DB is a fine second opinion, read read-only in-pod with
   `sqlite3.connect('file:/config/cleanuparr.db?mode=ro',uri=True)`.
5. **Confirm Dry Run is still OFF** (`general_configs.dry_run=0`) — otherwise the
   split changes nothing.
6. **Force one run and check it is clean.**
   `POST /api/jobs/QueueCleaner/trigger` → `200`, then confirm the new row in
   `events.db` `job_runs` has `status='completed'` and the pod log has no `[WRN]`
   / `[ERR]` after the change.

**UI variant.** Same two steps at `cleanuparr.epaflix.com` → **Queue Cleaner →
Stalled rules**: edit `private stalled` to range `0` → `99` and tick "delete
private torrents from client", **save**, then **Add rule** `private stalled
99-100`, range `99` → `100`, leave the tick off. Same order rule applies.

**Revert.** Reverse order - delete the new rule, then widen the old one back:

```
DELETE /api/queue-rules/stall/<id of private stalled 99-100>     -> 204
PUT    /api/queue-rules/stall/79684bce-1e28-45bc-b46d-cefb8bd9a099
       {maxCompletionPercentage: 100, deletePrivateTorrentsFromClient: false, ...} -> 200
```

Deleting first is required: widening back to `0-100` while `99-100` still exists
overlaps and is refused. Last resort is the file backup from step 1 - stop the
pod, replace `/config/cleanuparr.db`, remove the stale `-wal` / `-shm`, start it.

**What this does NOT do.** It stops *new* orphans; it does not clear the ones
already leaked. Those have no *arr queue row left, so no QueueCleaner rule will
ever revisit them — that is the `orphan-census` CronJob's job
(`2-k3s/maintenance/orphan-census-cronjob.yaml`, #483), which ships **disarmed**
(arming is #618).

**Why the `avistaz-reseed` torrents (#479) are not at risk.** Two private
torrents are held `stoppedDL` on purpose at 83.5% and 93.5%, so by percentage
alone they sit inside the new `0-99` band. They still cannot be touched, for two
independent reasons:

- **No *arr queue row.** `QBitService.ShouldRemoveFromArrQueueAsync(string hash, …)`
  (`QBitServiceQC.cs`) is called **per *arr queue record**, keyed on that
  record's `downloadId`. A torrent that no *arr references is never enumerated,
  so no stall rule is ever evaluated against it.
- **`stoppedDL` is not `stalledDL`.** `QBitItemWrapper.IsStalled()` is
  `Info.State is TorrentState.StalledDownload` and nothing else, and
  `CheckIfStuck` early-returns on `!IsStalled()`. A stopped torrent cannot take
  a stall strike at any completion percentage.

Deliberately pausing a torrent is therefore a valid way to park it, and the
`orphan-census` CronJob excludes the `avistaz-reseed` category on top of that.

**Cross-links:** #138 (the flat-blocklist SOPS seed sibling — same PVC-only
durability gap, file-seedable half), #142 (the safe reaper this state implements
and protects), #195 (single-`/media` mount; copy-seeder gate that keeps the
`unlinked` rule off), #244 (qbittorrent `LocalHostAuth=false` — sibling
PVC-only/live-only config from the same #195 cutover), #246 (arming the reaper —
flips `unlinked_configs.enabled` `0 -> 1` once pre-fix copies age out), #249,
#479 (the held `avistaz-reseed` torrents), #483 (the `private stalled` split in
(c) + the `orphan-census` CronJob that cleans up what the un-split rule already
leaked), #618 (arming that CronJob, gated on this split landing first).

## #137 — newtarr JSON config seed (SOPS)

newtarr v1.0.0 stores its entire configuration as JSON files under the
`newtarr-config` PVC (`/config`), NOT in `huntarr.db`: per-app instance lists +
hunt settings (`sonarr.json`, `radarr.json`, `lidarr.json`, `readarr.json`,
`whisparr.json`, `eros.json`), `swaparr.json`, `general.json` (holds
`proxy_auth_bypass` for the Authentik forward-auth "No Login Mode", #134), and
the scheduler `scheduling/list.json`. Because the PVC is `local-path` on a single
worker, a node loss / fresh re-provision would bring newtarr up **empty** — every
instance and the global hunt cadence (#135) would have to be re-entered by hand.

**What's codified.** Those JSON files are captured into a SOPS-encrypted Opaque
Secret `newtarr-config-seed`
(`_shared/secrets/newtarr-config-seed.enc.yaml`), reconciled by the servarr ksops
generator. The scheduler file is stored under the flat key
`scheduling-list.json` because k8s Secret keys cannot contain `/`.

**How it's restored.** The newtarr Deployment runs a `seed-config` initContainer
(busybox, `runAsUser: 0`) that mounts the PVC at `/config` and the Secret at
`/seed` (readOnly). It is **idempotent and NON-CLOBBERING**: each key is copied
ONLY when the destination does not already exist (`scheduling-list.json` is
remapped to `/config/scheduling/list.json`), then `chown -R 568:568 /config`.

- **Fresh / empty PVC:** files are seeded → newtarr boots pre-configured.
- **Populated PVC (normal case):** every file already exists → all skipped, no-op.

This deliberately never overwrites live state, because **newtarr rewrites these
JSONs at runtime** (the app persists hunt-setting and instance edits straight to
`/config`, #135/#177). The seed is therefore a point-in-time floor, not a
source of truth that tracks live edits.

### DRIFT-REFRESH runbook (manual)

Because the app mutates `/config` at runtime, the committed seed drifts from
live. Re-snapshot it periodically (or after any deliberate config change you want
preserved):

```sh
# 1. Pull the live JSON verbatim from the running pod
mkdir -p /tmp/newtarr-seed
for f in sonarr.json radarr.json lidarr.json readarr.json whisparr.json \
         eros.json swaparr.json general.json; do
  ssh ubuntu@192.168.10.51 \
    "kubectl --context epaflix -n servarr exec deploy/newtarr -- sh -c 'cat /config/$f'" \
    > /tmp/newtarr-seed/$f
done
ssh ubuntu@192.168.10.51 \
  "kubectl --context epaflix -n servarr exec deploy/newtarr -- sh -c 'cat /config/scheduling/list.json'" \
  > /tmp/newtarr-seed/scheduling-list.json

# 2. Rebuild a PLAINTEXT manifest (kind: Secret, name newtarr-config-seed,
#    namespace servarr, type Opaque) under stringData, one block scalar (|-)
#    per file, indented 4 spaces. Write it OUTSIDE the repo, e.g.
#    /tmp/newtarr-seed-plain.yaml. (Key for the scheduler file = scheduling-list.json.)

# 3. Re-encrypt OVER the committed file. --filename-override makes the .sops.yaml
#    `\.enc\.yaml$` creation rule apply to the /tmp input path.
sops --filename-override \
  2-k3s/08.servarr/_shared/secrets/newtarr-config-seed.enc.yaml \
  -e /tmp/newtarr-seed-plain.yaml \
  > 2-k3s/08.servarr/_shared/secrets/newtarr-config-seed.enc.yaml

# 4. Destroy the plaintext, verify, commit.
shred -u /tmp/newtarr-seed-plain.yaml && rm -rf /tmp/newtarr-seed
sops -d 2-k3s/08.servarr/_shared/secrets/newtarr-config-seed.enc.yaml | head
```

> **Note:** the initContainer's non-clobber guard means a refreshed seed does
> NOT auto-apply to the existing PVC — it only changes what a *future* empty PVC
> would receive. To force live re-seeding you must first remove the specific
> `/config/*.json` files (or recreate the PVC). Tracked alongside #135/#177
> (PVC-only config not yet fully in git).

### Tuned values inside the seed: `hourly_cap` and `command_wait_*`

The seed is ciphertext, so these two deliberate values are invisible in a PR diff.
Documenting them here so nobody "restores" them to the defaults.

**`sonarr.json: hourly_cap = 200`** (default 20). The cap does NOT count API calls
despite the `api_hits` key name — it counts **hunted media items**. `increment_stat()`
calls `increment_hourly_cap(app_type, count)` (`stats_manager.py:402`), and in
`seasons_packs` mode `missing.py:342` increments once per missing episode in the pack.
So one legitimate season-pack search registers as many hits as the season has missing
episodes — a 24-episode season instantly blows a cap of 20 and the next cycle inside the
same clock hour is skipped with:

```
SONARR hourly cap reached 24 of 20 (app-specific limit). Skipping cycle!
```

The cap must therefore exceed the LARGEST single missing season, not the number of
searches. 200 covers the current worst case (94 missing episodes in one season) with
headroom for raising `hunt_missing_items` above 1.

> There is no disable value. `get_hourly_cap_status()` computes
> `exceeded = current_usage >= hourly_limit`, so `0` or `-1` means *permanently*
> exceeded, i.e. hunting never runs. Only a high number works.

**`general.json: command_wait_delay = 5`, `command_wait_attempts = 150`**
(defaults 1 and 600). After triggering the search, `missing.py` calls
`wait_for_command(...)` and **discards the result** (`if wait_for_command(...): pass`),
so the wait buys nothing functionally — it only paces hunts. At the defaults that was up
to 600 status polls at 1s each per hunt.

`5 x 150 = 750s` keeps the same ~12.5 min ceiling (an observed real `SeasonSearch` took
11 min) while cutting the polling 4x. Do NOT simply lower `attempts` — any ceiling below
the real search duration makes every cycle log
`Timed out waiting for command <id> to complete`.

Radarr is intentionally left at `hourly_cap: 20`: it hunts whole movies, so one hunt is
one increment, and the episode-inflation problem does not apply.

### Re-assert No Login Mode (proxy_auth_bypass) — #174

The `seed-config` initContainer only writes `general.json` on an *empty* PVC (non-clobber), and newtarr rewrites `/config/*.json` at runtime — so a live UI/edit (or a partial restore) can silently flip `proxy_auth_bypass` back to `false`, which would re-expose newtarr's own login page behind the Authentik forward-auth route. To keep "No Login Mode" durable, the Deployment runs a second initContainer `enforce-auth-bypass` (the app image, `runAsUser: 0`) AFTER `seed-config`. It opens `general.json`, sets ONLY the top-level `proxy_auth_bypass` to `true` (atomic tmp + `os.replace` on the same `/config` filesystem, then `chown 568:568`), and preserves all other keys. It is idempotent — a correct file logs `ok: already true` and writes nothing — and it is write-only-to-`true`, so it can never re-enable in-app login. No Secret / `*.enc.yaml` change is needed: the committed `newtarr-config-seed` already carries `proxy_auth_bypass: true`.

To force it live immediately without waiting for a restart (e.g. if the flag was flipped off in the UI), patch it in-pod then bounce:

```sh
ssh ubuntu@192.168.10.51 "kubectl -n servarr exec deploy/newtarr -- python3 -c 'import json;p=\"/config/general.json\";c=json.load(open(p));c[\"proxy_auth_bypass\"]=True;json.dump(c,open(p,\"w\"),indent=2)' && kubectl -n servarr rollout restart deploy/newtarr"
```

> **Caveat:** this initContainer assumes `python3` is on PATH in `ghcr.io/elfhosted/newtarr:rolling`; if a future tag drops it the pod CrashLoops at init and the enforcer must be re-expressed in busybox. Like the #137 seed it does NOT codify the rest of newtarr's live config — it guarantees ONLY that `proxy_auth_bypass` is `true` on each pod start.

### #179 — drift detection (weekly detect-and-alert)

Because the app mutates `/config` at runtime (#135/#177), the committed
`newtarr-config-seed` steadily drifts from live. A **weekly drift-DETECTOR
CronJob** `newtarr-config-drift`
(`2-k3s/maintenance/newtarr-config-drift-cronjob.yaml`, #179) makes that drift
**visible** — it mirrors the #182 `cleanuparr-blocklist-drift` job:

- It reads the **baseline** from the ksops-rendered `newtarr-config-seed` Secret
  mounted **read-only** (NO sops decrypt in-job, no private key — same Secret the
  `seed-config` initContainer consumes), and the **live** side via
  `kubectl exec` into the newtarr pod (`cat /config/<file>`), using a **scoped**
  ServiceAccount/Role/RoleBinding in `servarr` (pods get/list, pods/exec create,
  deployments get — no cluster-wide/wildcard rules).
- It normalizes **both** sides with `jq -S .` (canonical sort-keys) and diffs,
  comparing ONLY the declarative keys: `sonarr.json`, `radarr.json`,
  `lidarr.json`, `readarr.json`, `whisparr.json`, `eros.json`, `swaparr.json`,
  `general.json`, and the scheduler (`scheduling-list.json` →
  `/config/scheduling/list.json`).
- It runs **Mondays 06:45 UTC** and exits **non-zero on ANY normalized diff OR
  any read/exec failure** (fail loud), firing the chart's generic `KubeJobFailed`
  rule + the scoped `NewtarrConfigDriftCheckFailed` PrometheusRule.

> **It DETECTS, it does NOT auto-fix.** There is deliberately **no auto-PR**
> write-back (rejected per the #192 lesson — the git push to `main` is gated by
> the required `validate` check). When the alert fires, **refresh stays the
> MANUAL #137 "DRIFT-REFRESH runbook (manual)" above** (re-snapshot the live JSON
> into the SOPS seed). The detector only tells you *when* to do it.

> **`nzb_hunt_bandwidth.json` is intentionally EXCLUDED** — both from the seed and
> from this comparison. It is volatile runtime state (per-run bandwidth
> bookkeeping), not declarative config, so codifying or diffing it would produce
> constant false-positive drift. Any other non-declarative/runtime JSON the app
> writes is likewise out of scope.

To inspect a failed run's per-file diff:

```sh
kubectl --context epaflix -n servarr logs job/$(kubectl --context epaflix -n servarr get jobs \
  -l app=newtarr-config-drift --sort-by=.metadata.creationTimestamp \
  -o name | tail -1 | cut -d/ -f2)
```

## #138 — Cleanuparr Sonarr custom-blocklist seed (SOPS) + the seriesId 40 S04E13 re-arm guard

Cleanuparr v2+ stores its configuration in **SQLite** (`/config/cleanuparr.db`),
not JSON. The only **file-seedable** artifact is the flat
`/config/custom-blocklist-sonarr.txt` referenced by the content-blocker
`sonarr_blocklist_path` DB row. On a fresh `local-path` PVC Cleanuparr comes up
without that file, dropping the seriesId 40 S04E13 re-arm guard.

**(i) What's codified — the durable SOPS seed.** The
`custom-blocklist-sonarr.txt` file (the ~850 upstream `flmorg/cleanuperr`
entries plus the seriesId 40 S04E13 re-arm guard regex) is captured verbatim
into a SOPS-encrypted Opaque Secret `cleanuparr-blocklist-seed`
(`_shared/secrets/cleanuparr-blocklist-seed.enc.yaml`, single key
`custom-blocklist-sonarr.txt`), reconciled by the servarr ksops generator. The
cleanuparr Deployment runs a `seed-config` initContainer (busybox `1.36`,
`runAsUser: 0`) that mounts the PVC at `/config` and the Secret at `/seed`
(readOnly), copies each seed file ONLY when the destination does not already
exist, then `chown -R 568:568 /config`. It is **idempotent and
NON-CLOBBERING**:

- **Fresh / empty PVC:** `custom-blocklist-sonarr.txt` is seeded → the guard is
  present (still needs the pointer step below).
- **Populated PVC (normal case):** the file already exists → skipped, no-op. We
  never clobber a live, possibly UI-edited, blocklist.

**(ii) ONE-TIME manual UI repoint on a PVC rebuild (pointer durability).** The
`sonarr_blocklist_path` pointer is **SQLite-resident** (`cleanuparr.db`), so it
is NOT file-seedable and is rewritten at runtime. After the initContainer
restores the file on a fresh PVC, set it once in the Cleanuparr UI →
**Content Blocker → Sonarr → blocklist path = `/config/custom-blocklist-sonarr.txt`**
(and confirm the Sonarr content-blocker is enabled). Until this is set the
restored file is present but **unreferenced** (the guard is silently inactive).
The companion `failed_import_max_strikes = -1` (Sonarr striking disabled) is also
SQLite-resident and intentionally KEPT — do not touch it.

**(iii) Soak result (part a) — PASS.** The seriesId 40 S04E13 re-arm guard held
across **145 hunt intervals** with **zero** recurrence (no post-fix grabs /
strikes / Action-Required events).

**(iv) Static-snapshot caveat (RESOLVED via drift-detector, #182).** This is a
frozen point-in-time snapshot of upstream `Cleanuparr/Cleanuparr` (`flmorg/cleanuperr`
redirects there); it will NOT auto-track upstream blocklist updates (unlike the
Radarr list, which still consumes the live upstream URL). Rather than auto-merge
(rejected — overkill for ~1 upstream change/yr, and the git write-back to `main` is
broken per #192), the chosen mechanism is a **weekly drift-DETECTOR CronJob**
`cleanuparr-blocklist-drift` (`2-k3s/maintenance/cleanuparr-blocklist-drift-cronjob.yaml`,
#182): it diffs the live upstream `blacklist` against a committed **upstream-only**
baseline (`2-k3s/maintenance/files/cleanuparr-blocklist-expected.txt`, the local
seriesId 40 overlay deliberately excluded so it never false-positives) and exits
non-zero on drift or fetch failure, firing `KubeJobFailed` + the scoped
`CleanuparrBlocklistDriftCheckFailed` alert. The re-merge/re-encrypt is the **manual
runbook** in the "#182 — Drift refresh" subsection below. (Earlier follow-up
umbrella **#180** is superseded by this detector.)

## #182 — Drift refresh (manual re-snapshot when the drift alert fires)

When **`CleanuparrBlocklistDriftCheckFailed`** (or the chart's generic
`KubeJobFailed` for the `cleanuparr-blocklist-drift` Job, namespace `servarr`)
fires, upstream has changed (or the fetch failed). First check the failed Job's
logs for the printed diff:

```sh
kubectl --context epaflix -n servarr logs job/$(kubectl --context epaflix -n servarr get jobs \
  -l app=cleanuparr-blocklist-drift --sort-by=.metadata.creationTimestamp \
  -o name | tail -1 | cut -d/ -f2)
```

If it is a transient fetch failure, no action is needed (next weekly run clears
it).

> ### ⚠️ 2026-08-19 — steps 1–2 and 3b–5 below are OBSOLETE. Do not run them.
>
> The seed **no longer contains the upstream blacklist at all.** It was removed
> on **2026-06-13** by owner request because the upstream file patterns blocked
> unpackerr: RAR-packed scene releases need their `.rar`/`.rNN` volumes to
> download. The seed body is now 9 lines — header comments plus the Epaflix
> release-title guard — and the MalwareBlocker is effectively inert. Re-merging
> upstream into the seed, as the original steps instruct, **re-breaks unpackerr.**
>
> On drift, the whole remedy is a one-line baseline bump:
>
> ```sh
> curl -fsSL https://raw.githubusercontent.com/Cleanuparr/Cleanuparr/refs/heads/main/blacklist \
>   > 2-k3s/maintenance/files/cleanuparr-blocklist-expected.txt
> # branch + PR, wait for `validate`, gh pr merge --merge
> ```
>
> No SOPS, no seed edit, no `kubectl cp`, no cleanuparr restart. The detector
> goes green on its next weekly run (or a manual `kubectl create job --from=`).
>
> This bit the 2026-08-17 drift (`+ *.m2ts`): the `maintenance` App sat
> `Degraded` for two days, and following the steps below verbatim would have
> reintroduced the unpackerr conflict fixed in June.

The original both-sides re-snapshot is kept below for historical context only —
it applies **only** if a future owner decision puts the upstream blacklist back
into the seed. It refreshes BOTH the SOPS seed's upstream portion AND the diff
baseline, **without ever retyping the seriesId 40 regex**:

```sh
# 1. Pull the live upstream blacklist (single shared upstream file -- there is NO
#    separate sonarr file upstream; the local filename is an Epaflix convention).
curl -fsSL https://raw.githubusercontent.com/Cleanuparr/Cleanuparr/refs/heads/main/blacklist \
  > /tmp/blacklist-upstream

# 2. Recover the local overlay VERBATIM from the committed seed -- do NOT retype it
#    (scrub-media-titles rule; it carries the seriesId 40 re-arm guard only). The
#    overlay = the marker line onward. (yq if available; otherwise the python
#    fallback below avoids a yq dependency.)
sops -d 2-k3s/08.servarr/_shared/secrets/cleanuparr-blocklist-seed.enc.yaml \
  | python3 -c 'import sys,yaml; \
      v=yaml.safe_load(sys.stdin)["stringData"]["custom-blocklist-sonarr.txt"]; \
      ls=v.split("\n"); \
      i=next(j for j,l in enumerate(ls) if l.strip()=="# --- Epaflix custom entries ---"); \
      sys.stdout.write("\n".join(ls[i:]))' \
  > /tmp/overlay

# 3a. Refresh the drift baseline (UPSTREAM-ONLY) -- this is the single source of
#     truth for the detector's configMapGenerator:
cp /tmp/blacklist-upstream \
   2-k3s/maintenance/files/cleanuparr-blocklist-expected.txt

# 3b. Re-merge upstream + overlay into the seed body, preserving the 2-line
#     Epaflix header comments that prefix the seed (keep them OUT of the baseline
#     above -- baseline is pure upstream). Build the merged body OUTSIDE the repo:
{ printf '# Cleanuparr custom Sonarr blocklist\n'; \
  printf '# Composed: upstream Cleanuparr/Cleanuparr blacklist + Epaflix custom entries (seriesId 40 re-arm guard)\n'; \
  cat /tmp/blacklist-upstream; printf '\n'; cat /tmp/overlay; } \
  > /tmp/custom-blocklist-sonarr.txt

# 4. Re-encrypt the seed OVER the committed file (same recipe as #137 newtarr):
#    build a PLAINTEXT kind:Secret (name cleanuparr-blocklist-seed, ns servarr,
#    type Opaque, stringData key custom-blocklist-sonarr.txt = the merged body)
#    OUTSIDE the repo (/tmp/seed-plain.yaml), then:
sops --filename-override \
  2-k3s/08.servarr/_shared/secrets/cleanuparr-blocklist-seed.enc.yaml \
  -e /tmp/seed-plain.yaml \
  > 2-k3s/08.servarr/_shared/secrets/cleanuparr-blocklist-seed.enc.yaml
shred -u /tmp/seed-plain.yaml

# 5. Live-apply the merged file. The non-clobber seed-config initContainer will
#    NOT overwrite a populated PVC, so push the file into the running pod once
#    (the sonarr_blocklist_path pointer is unchanged, still SQLite-resident):
kubectl --context epaflix -n servarr cp /tmp/custom-blocklist-sonarr.txt \
  "$(kubectl --context epaflix -n servarr get pod -l app=cleanuparr -o name | head -1 | cut -d/ -f2)":/config/custom-blocklist-sonarr.txt
kubectl --context epaflix -n servarr rollout restart deploy/cleanuparr   # reload patterns

# 6. Commit the refreshed seed + baseline via a branch+PR (rebase onto origin/main,
#    push --force-with-lease, wait for `validate`, gh pr merge --merge). The drift
#    Job goes green on its next weekly run. Destroy all /tmp plaintext.
shred -u /tmp/seed-plain.yaml 2>/dev/null; rm -f /tmp/blacklist-upstream /tmp/overlay /tmp/custom-blocklist-sonarr.txt
```

> **Two-place update caveat:** the seed body (step 3b/4) and the baseline file
> (step 3a) must both be refreshed in lockstep, or the detector will keep alerting
> against a stale baseline. The baseline is the upstream-only slice; the seed is
> upstream + the 2 Epaflix header comments + the overlay.
>
> **Live-apply caveat (carried from #138):** the non-clobber `seed-config`
> initContainer means a refreshed seed does NOT auto-apply to the existing PVC
> (step 5 is the live push). The `sonarr_blocklist_path` pointer remains
> SQLite-resident / manual on a PVC rebuild (#138 (ii)).

## #241 — arr `copyUsingHardlinks` is PVC-only runtime state

**Desired state.** `copyUsingHardlinks=true` on **all three arrs** — `sonarr`
(HD), `sonarr2` (anime), and `radarr`. This is **required** so that #195's unified
single `/media` mount actually **hardlinks** imports (downloads → library) instead
of byte-copying them. With it `false`, every import is a full copy: double the disk
usage and a fresh `nlink=1` file indistinguishable from an orphan. The in-pod
EXDEV blocker that previously made hardlinking impossible regardless of this flag
was **#240**, resolved by **#242** (drop the kubelet `subPath`, mount the unified
PVC once at `/media`); see the "#195" / "subPath EXDEV gotcha" sections above. With
that barrier gone, `copyUsingHardlinks=true` is the flag that makes the unification
pay off.

**Where it lives.** Each arr persists this in its **config PVC SQLite DB**
(`/config/sonarr.db`, `/config/radarr.db`, etc.), which the app **rewrites at
runtime**. It is **NOT git-managed.** The newtarr (#137, JSON) and cleanuparr
(#138, flat `.txt`) SOPS-seed pattern does **NOT** apply here: a binary SQLite DB
has no stable text to diff or non-clobber-seed. arr config is **PVC-managed runtime
state — same durability class as #196** (the Cleanuparr DB). The current live value
is `copyUsingHardlinks=true` on all three (set during the #195 cutover, see the
"Option A — DELIVERED" section).

### Re-apply runbook (only triggered by a pre-#195 PVC restore)

The **sole revert trigger** is restoring an arr config PVC from a backup taken
**before** the #195 cutover — such a backup silently carries `copyUsingHardlinks=false`
and the arr reverts to copy-mode on next start. (There is no other way this flips:
the app never changes it on its own.) When that happens, re-apply with a
**read-modify-write** against the arr API so other `mediamanagement` fields are
preserved — never blind-PUT a hand-built body.

The ingress for each arr is **forward-auth gated** (#176), so call the in-cluster
**ClusterIP Service** directly. Service names / ports (confirmed against the
manifests, namespace `servarr`): `sonarr:8989`, `sonarr2:8989`, `radarr:7878`.
The API key for each arr lives in that pod's `/config/config.xml` `<ApiKey>`
element (also mirrored in the credential store as `sonarr_api_key`,
`sonarr2_api_key` and `radarr_api_key`); substitute it for `<APIKEY>` below -
**never** print or commit the real key.

```sh
# Per arr — example uses sonarr (svc sonarr:8989). Repeat for sonarr2:8989 and radarr:7878.
# Run from a pod/host with in-cluster network reach (e.g. kubectl --context epaflix exec into the arr pod,
# or a debug pod in namespace servarr).

# 1. GET the current mediamanagement config (read).
curl -s -H "X-Api-Key: <APIKEY>" http://sonarr:8989/api/v3/config/mediamanagement -o /tmp/mm.json

# 2. Flip ONLY copyUsingHardlinks, preserving every other field (modify), then PUT it back (write).
#    (jq keeps all other keys intact; the resource id is required on the PUT path.)
ID=$(jq -r .id /tmp/mm.json)
jq '.copyUsingHardlinks = true' /tmp/mm.json \
  | curl -s -X PUT -H "X-Api-Key: <APIKEY>" -H "Content-Type: application/json" \
      --data @- "http://sonarr:8989/api/v3/config/mediamanagement/${ID}"
```

**OR simply, in the UI:** Sonarr / Sonarr2 / Radarr → **Settings → Media
Management → enable "Use Hardlinks instead of Copy" → Save.**

### Verify (post-restore)

1. **API check all 3** — confirm the flag is back on:
   ```sh
   for svc in sonarr:8989 sonarr2:8989 radarr:7878; do
     echo -n "$svc copyUsingHardlinks="; \
     curl -s -H "X-Api-Key: <APIKEY>" "http://${svc}/api/v3/config/mediamanagement" \
       | jq -r .copyUsingHardlinks
   done
   ```
   All three must print `true`.
2. **Spot-check a real cross-dir hardlink in-pod** — pick a recently imported file
   and confirm it shares an inode (`nlink>=2`, same device) across
   `downloads/` and the library (e.g. `tvshows/`):
   ```sh
   kubectl --context epaflix -n servarr exec deploy/sonarr -- \
     stat -c '%i %h %n' /media/downloads/<file> /media/tvshows/<path>/<file>
   ```
   Same inode number + link-count `2` = the import hardlinked (not copied).

**Cross-links:** #195 (origin — the single-`/media` unification that makes
hardlinking possible), #240 / #242 (the kubelet-subPath EXDEV fix that removes the
in-pod barrier so this flag is effective), #142 / #196 (the safe reaper depends on
hardlinks: `nlink>1` is what distinguishes a healthy imported seeder from a true
orphan), and the **#180-family** arr-config off-PVC backup gap. That last is the
**real durability gap**: the arr SQLite DBs have **no scheduled off-PVC backup**, so
a PVC restore from old media is the *only* thing that can revert this flag — and the
arrs have no SOPS-seedable config like newtarr/cleanuparr. This runbook makes the
revert **one-command-recoverable**, but it is **not itself a backup**; closing the
backup gap is tracked in the #180 family.

## #249 — #195 orphan-reaping: FINISHED (safe reaper canonical; nlink unlinked detector OFF by design)

> **⚠️ Read the scope correction in "Operationally covered" below before relying
> on this section.** The coverage claim this decision-of-record rested on was
> wrong (#614). The structural half (EXDEV gone, hardlinks proven) still holds;
> the "operationally covered" half does not. **Whether #249 should be reopened or
> superseded by #483 is an open owner decision - tracked in #614, not decided
> here.**

**#195 is structurally FINISHED** - the EXDEV barrier that defined the original
orphan problem is gone. Orphan *reaping* is **not** fully covered: see the scope
correction below. This section is the decision-of-record (Path D) for #249.

### Structurally solved — single `/media` mount, hardlinks PROVEN

The EXDEV barrier that defined the original orphan problem is **gone**: the four
child NFS exports of `/mnt/pool1/dataset01` were collapsed to ONE unified export,
all five media pods mount it once at a single non-subPath `/media`, and the arr
root folders / qbt save path were repointed under that one mount with
`copyUsingHardlinks=true` (see the "Option A — DELIVERED (#195)" and "#241"
sections above). Cross-directory `link()` (downloads → library) now succeeds.

**Proven by spot-check (2026-06-14, read-only `find -printf "%n %p"` in-pod):**

- **sonarr** (`/media/tvshows`): 3 files at `nlink>=2`.
- **sonarr2** (`/media/animes`): 3 files at `nlink>=2`.
- **radarr** (`/media/movies`): 3 files at `nlink>=2`.

This generalizes the single #240 proof: cross-dir hardlinking is **consistent
across all three arrs**, not a one-off. Imports are hardlinked (one copy on disk),
so a healthy imported seeder is `nlink>=2` and structurally distinguishable from a
true orphan.

### Operationally covered - COMPLETED torrents and QUEUED items ONLY

**Scope correction (#614).** This subsection used to say the two mechanisms below
"reap orphaned and stalled queue items safely and continuously". That was **false**,
and #249 was closed partly on the strength of it. What they actually cover, read
out of the v2.10.2 source we run:

- **Cleanuparr `download_cleaner` → qBit seeding rules** (`enabled=1`, hourly),
  per category (`radarr` / `tv-sonarr` / `animes`), gated **purely** on
  `max_ratio=1.0` / `max_seed_time=400h`. It can never touch a torrent on
  `nlink` grounds. **Sees COMPLETED torrents only.**
- **Cleanuparr `QueueCleaner`** (stall + slow rules, hourly). **Sees *arr queue
  rows only** - it enumerates from the *arr queue, so a torrent with no queue row
  is invisible to it by construction.

**Neither can see an incomplete torrent that has no *arr queue row.** That blind
spot is structural, not a misconfiguration:

- `DownloadCleaner.cs:105` makes exactly **one** torrent-fetch call,
  `GetSeedingDownloads()`.
- For qBittorrent that resolves to `QBitServiceDC.cs:16` -
  `GetTorrentListAsync(new TorrentListQuery { Filter = TorrentListFilter.Completed })`.
- That **single** list is then handed to **all three** consumers:
  `_unlinkedService` (`:161`), `_deadTorrentService` (`:165`) and
  `_seedingRulesService` (`:172`). A sub-100% torrent never enters the list, so
  none of the three can act on it.
- `QueueCleaner.cs:118` iterates `_arrArrQueueIterator.Iterate(arrClient, instance, …)`
  - the *arr queue. An orphan is by definition present in qBittorrent and **absent**
  from that queue.

Visible in the logs too: the hourly `[DownloadCleaner] Evaluating N downloads for
cleanup` line tracks the completed-in-rule-categories count, not the client total
(`N=173` at 2026-08-02 22:00).

**The one module that does see every torrent does not close the gap either.**
`OrphanedFilesCleanupService` (`DownloadCleaner.cs:182`) calls `GetAllTorrentsLite()`,
which is unfiltered - but it uses those torrents only to build a **claimed-paths**
set, then moves *unclaimed files on disk* into an orphaned directory. It never
deletes a torrent. An incomplete queue-less torrent's files are **claimed**, so
that torrent is *protected* by this module, not reaped by it.

**What is meant to cover the gap:** `2-k3s/maintenance/orphan-census-cronjob.yaml`
(#483), which ships **disarmed**. Until it is armed, incomplete + queue-less
torrents are **detected and alerted on** but not removed - removal is the owner
gate in #618, and the manual fallback is the #142 operator runbook below.

### The census orphan signal is NO PROGRESS, not queue absence (#631/#632)

**Queue absence on its own is not evidence of an orphan.** An *arr queue is a
pure in-memory projection of the last successful download-client poll
(`QueueService._queue`, a `static List<Queue>` rebuilt from
`TrackedDownloadRefreshedEvent`), so it reads empty for at least two reasons that
have nothing to do with the torrent:

- **any client outage or backoff** - the queue drops to zero instantly and comes
  back on the next good poll;
- **the Sonarr `TrackedDownloadService` cache bug (#631)** - a re-grabbed
  infohash that this Sonarr process already imported is pinned at `Imported` in
  an untimed in-memory cache, short-circuits `TrackDownload()`, and can therefore
  **never** enter the queue again. Permanent, with a healthy client and clean
  health checks - no external signal at all. Nine actively-downloading Sonarr
  grabs were queue-less this way on 2026-08-02.

So the census requires, on top of incomplete + no queue row + older than
`MIN_AGE_HOURS`, that the torrent has **moved zero bytes for `NO_PROGRESS_HOURS`
(default 24)** - `dlspeed`/`upspeed` both 0 and qBittorrent's own
`last_activity` older than the window (falling back to `added_on` when the
client reports it as never active). Progress comes from the download client, so
no *arr fault can fake it. The separation is wide, not marginal: on 2026-08-03
the five real orphans were idle 38h / 48h / 160h / 645h / 645h while the four
healthy incomplete downloads were idle 0.0-0.6h.

**Per-*arr attribution (#632).** The census maps qBittorrent category -> *arr
(`CATEGORY_ARR_MAP`, default `tv-sonarr=sonarr,animes=sonarr2,radarr=radarr`),
prints orphan counts per owning *arr, and exits 2 if an *arr reports **0 queue
rows while orphan-shaped torrents sit in the categories it owns**. That replaces
the fleet-wide "all three *arrs at 0" gate, which stays quiet exactly when one
*arr is blind and its siblings happen to be busy - the live 2026-08-02 state was
`sonarr=0 sonarr2=1 radarr=0`, so the all-zero gate never fired while Sonarr was
the broken one. Categories no *arr owns (`avistaz-reseed`, `manual-import`,
uncategorized) keep the old fleet-wide rule, since nothing else can speak for
them.

The classifier and both guards are covered by `python3 census.py --selftest`
(assert-based, no network) - extract the script from the ConfigMap or run it
straight from a checkout.

See the "#196" authoritative values table + restore runbook for the exact live
config and how to rebuild it on a fresh PVC.

### The `nlink`-based unlinked reaper is OFF PERMANENTLY — NOT a TODO

`unlinked_configs.enabled` stays **`0` by design**. This is a deliberate terminal
decision, not deferred work. Rationale:

1. **Private-tracker safety.** The owner runs **private trackers** with ratio
   requirements and hit-and-run (H&R) penalties. Deleting a torrent to satisfy an
   `nlink` rule **burns ratio and can trigger H&R**. The long-lived `nlink=1`
   seeders are exactly those private/ratio-protected torrents that must keep
   seeding. Per `feedback_private_trackers`, private torrents must **not** be
   deleted by automation.
2. **FP=0 is unreachable.** A dry-run can never reach zero false-positives,
   because those private seeders **never age out** — they are kept seeding on
   purpose. The #246 census found **273 `nlink=1` torrents, 111 of them >90d and
   still seeding**: these are protected seeders, not orphans, so an unscoped
   `nlink` reaper would always have a non-empty (and harmful) delete set.

Therefore the `unlinked` detector is left OFF as the **final** state. #246 (the
"arm once FP=0" tracker) is superseded by this decision and closed.

### Alternative for the record (Path C) — privacy-scoped, if ever wanted

IF nlink-based reaping of **public** orphans is ever desired, the ONLY acceptable
form is a **privacy-scoped** unlinked rule — **never** an unscoped `nlink` reaper:

- set `delete_private=false` (a.k.a. ignore-private) so private torrents are
  excluded from the delete set;
- set `ignored_root_dir` at the **library root** so imported library files are not
  treated as orphans;
- keep it **dry-run-gated**, and arm only after confirming **zero private
  torrents** appear in the delete set.

This is documented as a contingency only; it is not planned work.

### Manual orphan handling — the rare genuine orphan

The rare genuine public orphan (e.g. the one-time manual-remove-without-"remove
from client" class) is handled by the **#142 operator runbook**: manual queue
removal **with** "Remove from download client" ticked + content-blocker entry.
This is accepted as a low-frequency manual task, which is why no automated
`nlink` reaper is warranted.

**Cross-links:** #142 (orphan mechanism + manual operator runbook + safe-reaper
deliverable), #195 (single-`/media` unification that removed EXDEV), #240 / #242
(kubelet-subPath EXDEV fix), #246 (superseded — the "arm the unlinked reaper"
tracker, now closed by this Path D decision), #196 (Cleanuparr safe-reaper DB
values + restore runbook), `feedback_private_trackers` (never delete private
torrents).

## #739 — dead-key cleanup sweep on pod-side `.bak-*` copies (2026-08-04)

Follow-up to the #712/#737 and #740/#741 `radarr`/`sonarr`/`sonarr2` API-key
rotations: every `.bak-*` file below held a **pre-rotation** copy of one of
those keys. Verified dead by comparing `sha256(value)` against the current
live key on each *arr `config.xml` before deleting anything — never by
printing the value itself.

**Deleted** (confirmed via hash: value ≠ current live key, and not named by
any open issue as a rollback target):

- `radarr` / `sonarr` / `sonarr2` pods: `/config/config.xml.bak-712`,
  `/config/config.xml.bak-rotate740`
- `newtarr` pod: `/config/sonarr.json.bak-hourlycap-202607281332`,
  `/config/sonarr.json.bak-issue135-202606061706`, and the `huntarr.db` file
  (only) inside each of the three `/config/backups/huntarr_backup_v9.4.3_*`
  directories — `backup_info.json`/`logs.db` in those directories hold no
  credential and were left in place
- `seerr` pod: `/app/config/settings.json.bak-issue250`,
  `/app/config/settings.old.json`
- `bazarr` pod: `/config/config/config.yaml.bak-preinternal` — this file also
  carried still-current `jellyfin`/`plex`/own-`auth` keys (never rotated, out
  of this sweep's scope); deleting the whole stale pre-migration backup was
  judged lower-risk than leaving a partial file behind
- `cleanuparr` pod: `/config/cleanuparr.db.bak-20260531-135309`,
  `/config/cleanuparr.db.bak-20260531-160146`,
  `/config/cleanuparr.db.bak-20260710-patterns` (+ its `-shm`/`-wal` siblings)

**Left in place on purpose** — same dead-key content, but each is a named
rollback point:

- `cleanuparr.db.bak-20260802-pre618` / `-pre618-split` (+ siblings): tied to
  open issue #618 (reaper arming), not yet actioned
- `cleanuparr.db.bak195` (+ siblings): named after #195, which is now closed;
  left alone anyway pending an explicit retention decision (#739 outcome
  item 1) rather than pruning it on this pass
- `cleanuparr.db.bak-qbtpw-20260803-225508`: qBittorrent-password rotation
  artifact, out of scope for this sweep (owned by the AirVPN/qbittorrent
  work)
- `.a5c/processes/sonarr-import-series-290.{js,inputs.json}`: checked —
  these only ever held the `<SONARR_API_KEY>` placeholder, never a real
  value, so there was nothing to clean

**Not reachable from this pass:** the two workstation-only files named in
#739 (`2-k3s/08.servarr/research/app-configs.xml`,
`2-k3s/08.servarr/_backups/postgres-dumps/prowlarr-main-backup-20260124-191947.sql`)
are git-ignored and were not present in the isolated worktree this cleanup
ran from — still need checking/deleting directly on the primary checkout.

If any `.bak-*` copy above is ever used as an actual restore target, its
`api_key`/`ApiKey` values are stale and must be re-pointed to the current
live key afterward — see the #137/#182 refresh recipes elsewhere in this
file for the pattern.
