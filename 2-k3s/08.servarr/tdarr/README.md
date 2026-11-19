# Tdarr for the LG C1

UI labels below come from the Tdarr 3.08.00 web bundle and are checked again in the first live rollout.

## What the flow changes

`lg-c1-compat` converts DTS tracks to E-AC3 at 640 kb/s. In MKV files, it strips Dolby Vision profile 8 or single-stream profile 7 from the primary video stream and keeps the underlying video. The base layer is not always HDR10. Profiles 4, 5 and dual-layer 7 go to Require Review without conversion, even if they also have DTS. The flow never strips Dolby Vision outside MKV.

Tdarr does not re-encode or resize video, change filenames or containers, or remove tracks. Other audio, subtitles and attachments are copied when a file needs a fix. If neither fix applies, Tdarr leaves the file alone.

## Enable and queue work

The seed creates `movies`, `tvshows` and `animes` with transcodes and automatic scans off. After testing a copy, the owner can open **Libraries**, select a library, and turn on **Transcodes** under **Library Processing** in the source settings. The seed preserves that setting on restart. With automatic scans off, use a manual **Scan (Find new)** to index new files. The server and nodes share `/temp`, which is `tdarr-cache` on the `servarr-media` NFS volume. Create `/mnt/k3s-media/tdarr-cache` on the share and make it writable by UID 568 before rollout.

To requeue an indexed file, find its exact path in **Search** and click **Requeue Transcode** on that row. For a failed job, check its report first in **Transcode: Error/Cancelled**. In `#media`, ask the bot to fix one movie or episode for the LG C1. Name the exact title and episode or edition if there is more than one. Ask for status in the same thread instead of asking it to queue again.

**Require Review** means Tdarr has paused the item in **Staged Files**, not that it fixed the file. The original is unchanged. Leave it unapproved, read the job report, and choose a compatible release or test a manual conversion on a copy. The bot never approves staged items.

## Pins and recovery

`fetch-plugins` in [tdarr.yaml](tdarr.yaml) pins `checkHDRType` and `checkDoViProfile` to a full upstream Git commit SHA. To bump them, change the SHA in both download URLs, recompute both entries in [plugins.sha256](plugins.sha256) with `sha256sum` against the new unmodified files, and test on copies before enabling processing. The Renovate group `tdarr` updates the server and node images together. It does not update the plugin SHA or checksums.

The `seed` sidecar gates server pod readiness. If seeding fails, the `tdarr` Service loses its ready endpoint. Check `kubectl -n servarr logs deploy/tdarr -c seed` and add `--previous` if it restarted. If Tdarr itself is running, inspect it without the Service via `kubectl -n servarr port-forward pod/<tdarr-pod> 8265:8265`. Use the seed error to fix the input or existing library conflict, then verify the replacement pod is Ready after the fix rolls out.

`notify-proxy` lets the node trigger a Radarr or Sonarr refresh over loopback without putting Arr credentials in Tdarr.

`media-proxy` lets the bot check or queue one indexed path without exposing Tdarr's full API.

To stop new jobs, turn off **Transcodes** for each library. Check for jobs already running. The flow replaces an original only after the duration check and a thorough health check pass. Tdarr keeps no backup of replaced originals. To undo a completed replacement, restore the original from an external backup or obtain the original release again. A refresh error can occur after replacement, so check the job report and the file before retrying.
