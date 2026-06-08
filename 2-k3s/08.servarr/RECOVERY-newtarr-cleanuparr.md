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
kubectl -n servarr scale deployment huntarr --replicas=0
kubectl -n servarr scale deployment cleanuparr --replicas=0
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
kubectl -n servarr scale deployment huntarr --replicas=1
kubectl -n servarr scale deployment cleanuparr --replicas=1
```

## Verification

### Huntarr Status
```bash
kubectl -n servarr logs huntarr-847bd4b69f-j9klj --tail=50
```

**Results:**
- ✅ **8 items tracked** (4 in sonarr + 4 in sonarr2)
- ✅ State management working: next reset 2026-02-07 (168h interval)
- ✅ User authentication ready
- ✅ Templates loaded successfully
- ✅ Next cycle scheduled for 2026-02-03 16:44:23

### Cleanuparr Status
```bash
kubectl -n servarr logs cleanuparr-c8c65cb64-lx75l --tail=30
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

This seeding-rule + QueueCleaner combination is what satisfies **#142 #2**. The
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
    "kubectl -n servarr exec deploy/newtarr -- sh -c 'cat /config/$f'" \
    > /tmp/newtarr-seed/$f
done
ssh ubuntu@192.168.10.51 \
  "kubectl -n servarr exec deploy/newtarr -- sh -c 'cat /config/scheduling/list.json'" \
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

### Re-assert No Login Mode (proxy_auth_bypass) — #174

The `seed-config` initContainer only writes `general.json` on an *empty* PVC (non-clobber), and newtarr rewrites `/config/*.json` at runtime — so a live UI/edit (or a partial restore) can silently flip `proxy_auth_bypass` back to `false`, which would re-expose newtarr's own login page behind the Authentik forward-auth route. To keep "No Login Mode" durable, the Deployment runs a second initContainer `enforce-auth-bypass` (the app image, `runAsUser: 0`) AFTER `seed-config`. It opens `general.json`, sets ONLY the top-level `proxy_auth_bypass` to `true` (atomic tmp + `os.replace` on the same `/config` filesystem, then `chown 568:568`), and preserves all other keys. It is idempotent — a correct file logs `ok: already true` and writes nothing — and it is write-only-to-`true`, so it can never re-enable in-app login. No Secret / `*.enc.yaml` change is needed: the committed `newtarr-config-seed` already carries `proxy_auth_bypass: true`.

To force it live immediately without waiting for a restart (e.g. if the flag was flipped off in the UI), patch it in-pod then bounce:

```sh
ssh ubuntu@192.168.10.51 "kubectl -n servarr exec deploy/newtarr -- python3 -c 'import json;p=\"/config/general.json\";c=json.load(open(p));c[\"proxy_auth_bypass\"]=True;json.dump(c,open(p,\"w\"),indent=2)' && kubectl -n servarr rollout restart deploy/newtarr"
```

> **Caveat:** this initContainer assumes `python3` is on PATH in `ghcr.io/elfhosted/newtarr:rolling`; if a future tag drops it the pod CrashLoops at init and the enforcer must be re-expressed in busybox. Like the #137 seed it does NOT codify the rest of newtarr's live config — it guarantees ONLY that `proxy_auth_bypass` is `true` on each pod start.

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
kubectl -n servarr logs job/$(kubectl -n servarr get jobs \
  -l app=cleanuparr-blocklist-drift --sort-by=.metadata.creationTimestamp \
  -o name | tail -1 | cut -d/ -f2)
```

If it is a transient fetch failure, no action is needed (next weekly run clears
it). If it is real drift, re-snapshot — refreshing BOTH the SOPS seed's upstream
portion AND the diff baseline, **without ever retyping the seriesId 40 regex**:

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
kubectl -n servarr cp /tmp/custom-blocklist-sonarr.txt \
  "$(kubectl -n servarr get pod -l app=cleanuparr -o name | head -1 | cut -d/ -f2)":/config/custom-blocklist-sonarr.txt
kubectl -n servarr rollout restart deploy/cleanuparr   # reload patterns

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
