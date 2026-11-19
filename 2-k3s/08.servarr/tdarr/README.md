# Tdarr for the LG C1

UI labels below come from the Tdarr 3.08.00 web bundle and are checked again in the first live rollout.

## What the flow changes

`lg-c1-compat` converts DTS tracks to E-AC3 at 640 kb/s. In MKV files, it strips Dolby Vision profile 8 or single-stream profile 7 from the primary video stream and keeps the underlying video. The base layer is not always HDR10. Profiles 4, 5 and dual-layer 7 go to Require Review without conversion, even if they also have DTS. The flow never strips Dolby Vision outside MKV.

Tdarr does not re-encode or resize video, change filenames or containers, or remove tracks. Other audio, subtitles and attachments are copied when a file needs a fix. If neither fix applies, Tdarr leaves the file alone.

## First rollout

1. Keep **Transcodes** and **Folder Watch** off in the three seeded libraries. The hourly find-new scan still indexes new files, but indexing does not run the flow. Turning on Transcodes lets Tdarr process every indexed problem file in that library, including files you did not queue through the bot.
2. Turn off **Hourly Scan** in `movies` so it cannot claim the test samples, and turn it back on in step 3. In the Tdarr UI, create a temporary library at `/media/movies/_tdarr-test` with flow `lg-c1-compat`, **Scan with MediaInfo** on, cache `/temp` and **Transcodes** on. Copy five-minute samples of The Odyssey and Heart of the Beast plus a known-good sample into that folder and scan it. Use `ffprobe` before and after to check that Dolby Vision is gone, the base layer and any HDR10 or HDR10+ metadata remain, DTS is now E-AC3 at 640 kb/s, and other streams are unchanged. The known-good sample must end `Not required`. See [Task 7](../../../docs/superpowers/plans/2026-11-19-tdarr-lg-c1.md#task-7-live-rollout-owner-confirms-each-step) for the copy and probe commands. After the checks pass, delete the temporary library and test folder.
3. Enable **Transcodes** for `movies` only. Queue The Odyssey and Heart of the Beast through the bot and confirm both play on the C1. Only then enable **Transcodes** and **Folder Watch** for all three libraries and turn **Hourly Scan** back on in `movies`.

## Enable and queue work

Find the library controls under **Libraries** → **Source**. New libraries default to 300-second polling with **Use File System Events** off and **Hold Files After Scanning** set to 10 minutes. **Hourly Scan (Find new)** is on by default and adds only unindexed files. Confirm these settings on existing libraries too, since the seed preserves owner changes. To pause automatic work, turn off Folder Watch, Transcodes and Hourly Scan (Find new).

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
