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

- O4: strip the Dolby Vision layer and keep MKV. **Chosen by Spyros.** The file becomes HDR10 and keeps its name and subtitles.
- O5: remux to MP4 and keep Dolby Vision. Rejected: needs third-party plugins, changes the file extension under Radarr and drops image subtitles.

## Design

### Tdarr (namespace `servarr`)

- Tdarr server as a Deployment with its config on an iSCSI PVC, behind the existing ingress and Authentik like the other servarr apps.
- Tdarr node as a DaemonSet on the 5 k3s workers: one node per host. Each node runs one CPU transcode worker and one health-check worker. Each has CPU and memory limits, so an out-of-memory kill restarts only that node's container. Free workers take the next file from the queue; Tdarr has no "least busy node" choice.
- Server and nodes mount `servarr-media` at the same paths, so no path translation is needed. The transcode cache is an emptyDir per node, sized and limited so it cannot fill the node disk.
- Libraries: movies, TV shows, animes. Scheduled scans of the whole library are allowed, because the flow below touches only problem files.

### Flow (community flow plugins, official or third-party)

Non-official community plugins are allowed. No custom code in the flow.

1. Check Audio Codec for `dts` (official). For Dolby Vision: Check HDR type, then Check DoVi Profile, both from [nichols89ben/Tdarr_DoVi_Processing](https://github.com/nichols89ben/Tdarr_DoVi_Processing), pinned to commit `5be0b5a34114` (2026-11-14, Tdarr 3.08.00). Strip only profile 8, and profile 7 with one video stream, both of which have an HDR10 base layer. Profiles 4 and 5 have no HDR10 base, so they go to Require Review and are not touched.
2. Neither problem: end with "not required". The file is not touched.
3. Otherwise build one ffmpeg command: copy the video, copy subtitles and other audio, convert DTS tracks to E-AC3 at 640k, and strip the Dolby Vision layer with the bitstream filter `dovi_rpu=strip=1` (ffmpeg 7.1 or newer).
4. Check the new file: duration ratio close to 1, and a health check.
5. Replace the original file, then tell Radarr or Sonarr to rescan (Notify Radarr or Sonarr plugin).

Both third-party steps read MediaInfo fields (`HDR_Format`, `HDR_Format_Profile`). Check DoVi Profile throws on files without Dolby Vision, so it runs only on Check HDR type's Dolby Vision branch. The repo has no license file; it is used unmodified for personal use. The plugins are copied into Tdarr's `LocalFlowPlugins` from the pinned commit at deploy time.

### Bot (`#media`)

- `AGENTS.md` gets a Tdarr section: find the file in Radarr or Sonarr, queue it in Tdarr through its API, check its status, and report the result. Tdarr's API key goes in Secret `openclaw` like the other keys.
- The NetworkPolicy allows the gateway to reach the Tdarr server only.
- The bot cannot pick a flow or pass ffmpeg arguments, only queue a file.

### Configarr

Radarr profile `UHD Bluray + WEB` scores the five DTS formats -1 instead of TRaSH's +1250 to +4500, so DTS ranks below every other audio format while a DTS-only release still clears `min_format_score` -2. Sonarr profile `WEB-2160p` leaves DTS at 0: its floor is +1, so any penalty would reject DTS-only releases. Tdarr fixes the DTS files that still arrive. `DV (w/o HDR fallback)` stays at its existing score.

### Observability

- Tdarr server and node logs go to Loki like the other servarr apps.
- Alert when a Tdarr transcode fails, if a log line marks it reliably.

## Verified on 2026-11-19

- `ghcr.io/haveagitgat/tdarr_node:latest` ships `tdarr-ffmpeg` 8.1.2-Jellyfin with bitstream filter `dovi_rpu` and option `strip`. Pin that image by digest.
- On 5-second video samples of both movies, `-c copy -bsf:v dovi_rpu=strip=1` removed the DOVI configuration record and the per-frame Dolby Vision RPU, and kept HEVC Main 10, PQ transfer, BT.2020, mastering display, content light level and HDR10+ (SMPTE 2094-40) metadata. Tested with Jellyfin's ffmpeg 8.1.3, same filter; nothing was written to the library.

## Left to the builder

- Tdarr image tags and digests, resource numbers, cache size.
- The cause of the gateway OOM kills.
- Confirm Tdarr fills `mediaInfo` for library files (library scan setting), since both Dolby Vision steps depend on it.
- Test on a copy of one file before the flow runs on the library, and process The Odyssey and Heart of the Beast last.
