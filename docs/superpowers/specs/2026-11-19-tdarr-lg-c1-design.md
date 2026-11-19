# Tdarr: make media playable on the LG C1

## Why

A family member's LG C1 cannot play two movies in Jellyfin. Probing showed two causes:

- DTS audio (The Odyssey 2026, DTS-HD MA). The C1 cannot decode DTS.
- Dolby Vision profile 8.1 inside MKV (both movies). LG's TV software plays Dolby Vision only from MP4, according to the authors of the LG Dolby Vision Tdarr plugins.

The `#media` bot could not help. Spyros wants the same request to be solvable from Discord next time, and Radarr and Sonarr to stop grabbing such releases.

## Goals

- G1: Tdarr fixes files with DTS audio or Dolby Vision in MKV, and nothing else.
- G2: Configarr scores those releases down so Radarr and Sonarr prefer playable ones.
- G3: In `#media`, someone can ask for a file to be fixed and the bot queues it in Tdarr and reports the result.
- G4: No pod crashes. A conversion running out of memory restarts only a Tdarr node. The `#media` gateway OOM restarts (10 restarts, last one `OOMKilled` at 2Gi) are investigated and fixed.

## Non-goals

- No video re-encode, resolution change or size reduction.
- No general library cleanup (subtitles, languages, containers, codecs).
- No splitting one file across several nodes. Tdarr does not support it, and the job is limited by disk and network speed, not CPU.

## Anti-goals

- Tdarr changes a file that has neither problem.
- A file is replaced before the new one is checked.
- The bot gets ffmpeg, write access to the library or a Kubernetes token.

## Approaches

- O1: ffmpeg worker container in the bot's pod. Rejected: gives the bot pod write access to the library, and it is custom code.
- O2: Tdarr. **Chosen by Spyros.** An established tool built for this, already used by Spyros.
- O3: One Kubernetes Job per conversion. Rejected: the bot would need a ServiceAccount token and an admission policy.

Dolby Vision fix:

- O4: strip the Dolby Vision layer and keep MKV. **Chosen by Spyros.** The file keeps its base layer, name and subtitles.
- O5: remux to MP4 and keep Dolby Vision. Rejected: needs third-party plugins, changes the file extension under Radarr and drops image subtitles.

## Design

### Tdarr (namespace `servarr`)

- Tdarr server as a Deployment with its config on an iSCSI PVC, behind the existing ingress and Authentik like the other servarr apps.
- Tdarr node as a DaemonSet on the 5 k3s workers: one node per host. Each node runs one CPU transcode worker and one health-check worker. Each has CPU and memory limits, so an out-of-memory kill restarts only that node's container. Free workers take the next file from the queue; Tdarr has no "least busy node" choice.
- Server and nodes mount `servarr-media` at the same paths, so no path translation is needed. They mount the transcode cache at `/temp` from the `tdarr-cache` subPath of the `servarr-media` NFS share.
- Libraries: movies, TV shows, animes. Once Folder Watch is enabled, it polls NFS every 300 seconds instead of relying on file-system events. Tdarr holds newly indexed files for 10 minutes before processing to give copy imports time to finish. This is a queue delay, not a check that the file has stopped changing.
- The seed leaves Transcodes, Folder Watch, Hourly Scan (Find new) and Scan on startup off on creation and preserves the owner's settings on restart. Setting a file to `Queued` through the bot does not give it priority over other queued files. The owner can index a single exact path with Tdarr's `rescan-file` API without scanning the library. The bot still queues only already indexed paths.

### Flow (community flow plugins, official or third-party)

Non-official community plugins are allowed. No custom code in the flow.

1. Check Audio Codec for `dts` (official). For Dolby Vision: Check HDR type, then Check DoVi Profile, both from [nichols89ben/Tdarr_DoVi_Processing](https://github.com/nichols89ben/Tdarr_DoVi_Processing), pinned to commit `5be0b5a34114` (2026-11-14, Tdarr 3.08.00). Strip only profile 8 and single-stream profile 7. The strip keeps whatever base layer exists, usually HDR10 but sometimes HLG or SDR. Check that it survives the copy test. Profiles 4, 5 and dual-stream 7 go to Require Review and are not touched.
2. Neither problem: end with "not required". The file is not touched.
3. Otherwise build one ffmpeg command: copy the video, copy subtitles and other audio, convert DTS tracks to E-AC3 at 640k, and strip the Dolby Vision layer with `-bsf:v:0 dovi_rpu=strip=1` (ffmpeg 7.1 or newer).
4. Check the new file: duration ratio close to 1, and a health check.
5. Replace the original file, then tell Radarr or Sonarr to rescan (Notify Radarr or Sonarr plugin). The plugin calls a loopback proxy in the node pod that holds the Arr keys and forwards only lookups, refresh commands and command status reads (`GET /api/v3/command/<id>`), so no key sits in the flow, Tdarr's database or job reports.

Both third-party steps read MediaInfo fields (`HDR_Format`, `HDR_Format_Profile`). Check DoVi Profile throws on files without Dolby Vision, so it runs only on Check HDR type's Dolby Vision branch. The repo has no license file; it is used unmodified for personal use. The plugins are copied into Tdarr's `LocalFlowPlugins` from the pinned commit at deploy time.

### Bot (`#media`)

- `AGENTS.md` gets a Tdarr section: find the file in Radarr or Sonarr, queue it through the Tdarr allowlist sidecar, check its status, and report the result. Tdarr runs without its own auth, like Radarr: the UI sits behind Authentik and only Traefik reaches the API port, so the bot needs no key.
- The gateway's NetworkPolicy allows only the sidecar's port on the Tdarr server.
- The bot cannot pick a flow or pass ffmpeg arguments, only queue a file. It never reaches Tdarr's raw API, which could rewrite the flow: a sidecar in the Tdarr server pod exposes only "read one file's status by exact path" and "queue one file", and the NetworkPolicy gives the bot only that port.

### Configarr

Radarr profile `UHD Bluray + WEB` scores the five DTS formats -1 instead of TRaSH's +1250 to +4500, so DTS ranks below every other audio format while a DTS-only release still clears `min_format_score` -2. Sonarr profile `WEB-2160p` leaves DTS at 0: its floor is +1, so any penalty would reject DTS-only releases. Tdarr fixes the DTS files that still arrive. `DV (w/o HDR fallback)` stays at its existing score.

### Observability

- Tdarr server and node logs go to Loki like the other servarr apps.
- No alert: Tdarr 3.08.00 logs transcode failures only in job reports. Follow-up: alert from Tdarr's API.

## Verified on 2026-11-19

- The tested Tdarr node image ships `tdarr-ffmpeg` 8.1.2-Jellyfin with bitstream filter `dovi_rpu` and option `strip`. The manifests pin the server and node to 3.08.00 by digest.
- On 5-second video samples of both movies, a stream copy with `dovi_rpu=strip=1` removed the DOVI configuration record and the per-frame Dolby Vision RPU, and kept HEVC Main 10, PQ transfer, BT.2020, mastering display, content light level and HDR10+ (SMPTE 2094-40) metadata. Tested with Jellyfin's ffmpeg 8.1.3, same filter; nothing was written to the library.

## Left to the builder

- The cause of the gateway OOM kills.
- Confirm Tdarr fills `mediaInfo` for library files (library scan setting), since both Dolby Vision steps depend on it.
- Test copies of both movies in a temporary library while Transcodes and automatic scans are off in the seeded libraries. Remove the test library and folder. Then index only The Odyssey (2026) and Heart of the Beast (2026), queue both through `#media` and enable Transcodes only in `movies`. Verify both conversions and playback on the C1 before scanning the backlog.
- Enable Transcodes in `tvshows` and `animes` and run a manual find-new scan in all three libraries. Finish the backlog before turning on Folder Watch with polling and Hourly Scan (Find new) for new downloads.
