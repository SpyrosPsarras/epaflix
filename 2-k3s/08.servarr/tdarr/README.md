# Tdarr for the LG C1

UI labels below come from the Tdarr 3.08.00 web bundle and are checked again in the first live rollout.

## What the flow changes

`lg-c1-compat` converts DTS tracks to E-AC3 at 640 kb/s. In MKV files, it strips Dolby Vision profile 8 or single-stream profile 7 from the primary video stream and keeps the underlying video. The base layer is not always HDR10. Profiles 4, 5 and dual-layer 7 go to Require Review without conversion, even if they also have DTS. The flow never strips Dolby Vision outside MKV.

Tdarr does not re-encode or resize video, change filenames or containers, or remove tracks. Other audio, subtitles and attachments are copied when a file needs a fix. If neither fix applies, Tdarr leaves the file alone.

## First rollout

1. In `movies`, `tvshows` and `animes`, confirm **Transcodes**, **Folder Watch**, **Hourly Scan (Find new)** and **Scan on startup** are off. Do not run a whole-library scan yet. Turning on Transcodes can start any queued file in that library, not just files queued through the bot.
2. In the Tdarr UI, create a temporary library at `/media/movies/_tdarr-test` with flow `lg-c1-compat`, **Scan with MediaInfo** on, cache `/temp` and **Transcodes** on. Leave automatic scans off. Copy five-minute samples of The Odyssey and Heart of the Beast plus a known-good sample into that folder and scan it. Use `ffprobe` before and after to check that Dolby Vision is gone, the base layer and any HDR10 or HDR10+ metadata remain, DTS is now E-AC3 at 640 kb/s, and other streams are unchanged. The known-good sample must end `Not required`. See [Task 7](../../../docs/superpowers/plans/2026-11-19-tdarr-lg-c1.md#task-7-live-rollout-owner-confirms-each-step) for the copy and probe commands. After the checks pass, delete the temporary library and test folder.
3. Index only the two originals, The Odyssey (2026) and Heart of the Beast (2026). From an owner terminal run `kubectl -n servarr port-forward svc/tdarr 8265:8265`. In another terminal set `file` to one exact original `/media/movies/...` file path from Radarr and run this once per film:

   ```sh
   curl --fail-with-body -sS --max-time 15 --json \
     "$(python3 -c 'import json, sys; print(json.dumps({"data": {"_id": sys.argv[1], "DB": "movies"}}))' "$file")" \
     http://127.0.0.1:8265/api/v2/rescan-file
   ```

   `OK` accepts the scan; it does not confirm indexing. In **Search**, check that both paths are indexed under `movies` and that no other files are indexed in that library. Stop if the check fails. Queue both films through `#media` while Transcodes is still off. After the bot confirms both are queued, turn on **Transcodes** for `movies` only. Wait for both conversions and confirm playback on the C1.
4. Process the backlog. Turn on **Transcodes** for `tvshows` and `animes`, then run **Scan (Find new)** once in each of the three libraries. Leave automatic scans off. Wait for the scans and the held, queued and running work to finish. Resolve transcode errors before moving on. Leave files marked **Require Review** unapproved.
5. After the backlog, enable **Folder Watch** and **Hourly Scan (Find new)** in all three libraries. Folder Watch polls for new downloads and the hourly scan picks up arrivals the watcher missed.

## Enable and queue work

Find the library controls under **Libraries** → **Source**. New libraries default to 300-second polling with **Use File System Events** off and **Hold Files After Scanning** set to 10 minutes. **Hourly Scan (Find new)** starts off and adds only unindexed files once enabled. Confirm these settings on existing libraries too, since the seed preserves owner changes. To pause automatic work, turn off Folder Watch, Transcodes and Hourly Scan (Find new).

Replacing a file breaks its hardlink to the still-seeding torrent, so converted new downloads use double disk space until the torrent is removed.

The server and nodes share `/temp`, which is `tdarr-cache` on the `servarr-media` NFS volume. Create `/mnt/k3s-media/tdarr-cache` on the share and make it writable by UID 568 before rollout.

To requeue an indexed file, find its exact path in **Search** and click **Requeue Transcode** on that row. For a failed job, check its report first in **Transcode: Error/Cancelled**. In `#media`, ask the bot to fix one movie or episode for the LG C1. Name the exact title and episode or edition if there is more than one. Ask for status in the same thread instead of asking it to queue again.

**Require Review** means Tdarr has paused the item in **Staged Files**, not that it fixed the file. The original is unchanged. Leave it unapproved, read the job report, and choose a compatible release or test a manual conversion on a copy. The bot never approves staged items.

## Pins and recovery

`fetch-plugins` in [tdarr.yaml](tdarr.yaml) pins `checkHDRType` and `checkDoViProfile` to a full upstream Git commit SHA. To bump them, change the SHA in both download URLs, recompute both entries in [plugins.sha256](plugins.sha256) with `sha256sum` against the new unmodified files, and test on copies before enabling processing. The Renovate group `tdarr` updates the server and node images together. It does not update the plugin SHA or checksums.

The `seed` sidecar gates server pod readiness. If seeding fails, the `tdarr` Service loses its ready endpoint. Check `kubectl -n servarr logs deploy/tdarr -c seed` and add `--previous` if it restarted. If Tdarr itself is running, inspect it without the Service via `kubectl -n servarr port-forward pod/<tdarr-pod> 8265:8265`. Use the seed error to fix the input or existing library conflict, then verify the replacement pod is Ready after the fix rolls out.

`notify-proxy` lets the node look up a Radarr or Sonarr item, trigger a refresh and read the command status over loopback without putting Arr credentials in Tdarr.

`media-proxy` lets the bot check or queue one indexed path without exposing Tdarr's full API.

When pausing processing, check for jobs already running. The flow replaces an original only after the duration check and a thorough health check pass. Tdarr keeps no backup of replaced originals. To undo a completed replacement, restore the original from an external backup or obtain the original release again. A refresh error can occur after replacement, so check the job report and the file before retrying.
