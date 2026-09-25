# Lingarr re-queue runbook

How to find and re-queue missing Greek subtitles, and why `automation_enabled`
stays `false`. Written for #926 (the 113 failed translations from the #902 GPU
window). Verified live on 2026-09-12: of the 113, 87 already had a Greek
sibling, 9 were restored from the sweep archive, 11 had been covered by an
earlier run, and 23 episodes (6 of the 113 plus 17 more with damaged-only
history) were re-queued through this procedure - all completed the same day;
see "Where the #926 numbers went".

## Why automation stays off

`automation_enabled=false` is deliberate. The 4am automation
(`translation_schedule=0 4 * * *`, `max_translations_per_run=1`) enqueues new
requests for the whole library unattended. The #902 window showed what that
costs: an unusable endpoint turned a nightly run into a pile of failures that
nothing retried, and the fake-Greek metric could not even see the damage. A
controlled, observed re-queue beats a nightly blind one here.

Re-queuing is done explicitly (this runbook) and watched by the
`lingarr-drain-watch` CronJob in `2-k3s/maintenance/`.

## Finding victims

Two scans, both from any pod that mounts `servarr-media` (for example a
one-off `python:3.12-alpine` pod on the PVC):

1. English sources with no Greek sibling:

   ```sh
   find /media -path /media/backups -prune -o -name '*.en.srt' -print |
   while read -r en; do
     [ -f "${en%.en.srt}.el.srt" ] || echo "$en"
   done
   ```

2. Damaged Greek files, the #872 scan (below 10% non-ASCII bytes = the file
   carries the source text, not a translation):

   ```sh
   find /media -path /media/backups -prune -o -name '*.el.srt' -type f -print |
   while read -r f; do
     s=$(stat -c%s "$f"); g=$(tr -d '\000-\177' < "$f" | wc -c)
     [ "$s" -gt 0 ] && [ $((g * 100 / s)) -lt 10 ] && echo "$f"
   done
   ```

Both scans prune `/media/backups`, so the sweep archive does not pollute the
counts.

Measured on this tree on 2026-09-12, real Greek subtitles sit between 43% and
72% non-ASCII: the cluster centred on 50-59%, the 43-49% cases are dialogue
dense with English proper nouns or quoted on-screen text, and dialogue-heavy
drama (When They See Us) reached 71.8% - verified by reading the files, not
assumed. 0-10% is English. Ignore anything above ~40%, including files below
the historical 63-67% figure - that number came from a different counting
method (whole-file bytes including timestamps, so 2-byte UTF-8 Greek
characters over-represent themselves).

Before re-translating a missing file, check the 08-07 sweep archive
(`/media/backups/fake-greek-el-2026-08-07`): the sweep archived real Greek
files alongside damaged ones. Measure the archived candidate with the #872
method; anything above ~40% is a real Greek subtitle that can simply be copied
back next to the current `.en.srt` (named after it). In the #926 run this
restored 9 episodes and would have covered 11 more had they still been
missing - the 09-05 run had already translated them. Only re-translate when
the archived candidate is also damaged (0-10%).

## Re-queueing

File-based requests only. The API is open in-cluster (`auth_enabled=false`),
so no key is needed from inside the cluster.

1. **One file** - `POST /api/translate/file` from inside the cluster
   (`http://lingarr:9876`):

   ```json
   {
     "MediaId": 1832,
     "SubtitlePath": "/media/tvshows/.../Episode - S02E04 - Name [tags].en.srt",
     "SourceLanguage": "en",
     "TargetLanguage": "el",
     "MediaType": 3,
     "SubtitleFormat": "srt"
   }
   ```

   `MediaType`: 0 Movie, 1 Show, 2 Season, 3 Episode. `MediaId` is lingarr's
   internal id (`public.episodes.id` / `public.movies.id`), not the Sonarr id.

2. **A whole show** - `POST /api/translate/bulk` with the SHOW id from
   `public.shows.id`; lingarr discovers subtitles per episode and skips
   episodes that already have the target language:

   ```json
   {
     "MediaType": 1,
     "MediaIds": [41],
     "TargetLanguage": "el"
   }
   ```

Dedup: lingarr only skips a request when a Pending/InProgress request for the
same subtitle path exists. Failed rows never block a re-queue.

### Damaged files

If an `.el.srt` exists but measures as damaged, move it aside first - into a
dated folder under `/media/backups/` - and only then re-queue, otherwise the
new translation lands next to a bad file with the same name and players may
pick either.

## Where the #926 numbers went

The 113 failed requests of 2026-08-09 no longer exist as rows: Lingarr's
weekly CleanupJob deletes translation requests older than a week. Their
Hangfire job records survive and carry the file paths, so the 113 episodes
were recovered from `hangfire.job` instead. State on 2026-09-12:

- 87 of the 113 had a real Greek sibling already.
- 11 more had been translated by the 09-05 run under newer file names; a scan
  against the stale 08-08 paths reports them as missing, so always re-check
  against the live tree.
- 9 were real Greek swept into the 08-07 archive; restored from there.
- 6 needed re-translation (4 missing, 2 damaged - the When They See Us S01E02
  file this issue names, and a fake Friendface file from the 09-05 run), and
  17 further episodes had damaged-only history (Mushoku Tensei, Orange Is the
  New Black S03, Fire Force, Hell's Paradise) - 23 re-queued in total, all
  completed the same day through the one-worker translation queue. One
  cliproxy restart mid-drain burned 12 requests as instant failures (refused
  connection, retries at 1-16s cannot outlast an outage); they were resumed
  via the same API and completed.

## Coverage decision (owner)

Decided in #1345 on 2026-09-25: no bulk run. The English-only backlog drains
on its own through `bazarr-autotranslate` in `2-k3s/08.servarr/`. It scans
every hour. For each item missing Greek it runs three provider search rounds,
8h apart, and queues a Lingarr translation when all three find nothing. After
queueing it leaves the item alone for 216h, then starts the search rounds
again if Greek is still missing.

Between 2026-09-20 and 2026-09-25 Lingarr completed about 930 file
translations. The scan on 2026-09-25 still counted 469 English-only files,
because new media keeps arriving. The limit is the free upstream, not the
queue. `or-free` runs out of its daily token quota of 200k and returns 429
until the quota resets. A manual bulk run spends the same quota, so the hourly
requests start getting 429s earlier in the day.

Use the procedure above when you want one file or one show sooner, or when a
file needs the damaged-file handling.

## Do not resume content-mode rows

`status=3/4/5` rows with an **empty `subtitle_to_translate`** are client
("content") translations: a player plugin sends subtitle text, lingarr
translates lines and returns them; no file is written and the rows carry no
path. `POST /api/translationrequest/resume` on such a row enqueues a
`TranslationJob` against an empty path - it fails instantly. The client
re-requests on its own when the subtitle is needed again; these rows are
history, not work. The `resume` endpoint is only for file-based rows.

## Verify afterwards

Re-run scan 2 on the re-queued episodes; completed Greek files must sit in the
43-72% band measured on this tree. The `fake-greek-el-2026-08-07` folder under
`/media/backups/` holds the 2026-08-07 sweep archive (152 files: 100 damaged,
52 real Greek that were swept by mistake); `damaged-el-2026-09-12` holds the
damaged originals moved aside before re-translation - the When They See Us
S01E02 file from this issue and two fake Asterix movie files.
