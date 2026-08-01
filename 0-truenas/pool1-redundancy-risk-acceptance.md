# pool1 media-stripe redundancy — risk acceptance (2026-06-14)

Tracks issue [#203](https://github.com/SpyrosPsarras/epaflix/issues/203). The
owner reviewed the options in #203 and chose **Option B: accept the media-loss
risk and document it** — no storage/cluster change, no new disks.

## Finding

`pool1` is a **non-redundant 2-disk STRIPE** — two single-disk vdevs with no
parity or mirror:

- `sde` — 14 TB Seagate Exos
- `sdb` — 10 TB Seagate IronWolf Pro

Roughly **14.9T used / 6.9T free**. Because the data is striped across both
single-disk vdevs, a **single-disk failure loses the striped data** (the whole
pool's media).

## Current state

- **Both disks healthy.** Last scrub (2026-06-01) repaired 0B with 0 errors.
- The #124 `sdb` scare was a **transient SATA cable**, reseated 2026-05-24
  (`zpool clear` → ONLINE) — not a dying disk.
- **No spare disks and no confirmed free bay** on the box → adding redundancy
  requires buying disks.
- **No snapshot/replication covers `pool1/dataset01`** — the media data is not
  backed up anywhere.

## Decision

Risk **ACCEPTED** on 2026-06-14 (Option B).

Rationale:

- The ~14.7T of media is **re-acquirable** via the *arr stack (Sonarr / Radarr /
  Prowlarr / qBittorrent etc.) — it is download-replaceable, not unique data.
- The only **irreplaceable** data — the SOPS master age-key backup — is already
  redundant on the `apps` RAIDZ1 pool (#149 / PR #202) plus 2 copies.
- The cost/effort of redundancy (2× ≥14 TB disks + bays, or a full RAIDZ rebuild
  with extended downtime) is **not justified** for re-downloadable media.

## Revisit trigger

If disks are purchased, convert the stripe to two **mirror vdevs in place,
online** (Option A-a1):

```sh
zpool attach pool1 sdb <new-disk-1>   # mirror the 10 TB IronWolf vdev
zpool attach pool1 sde <new-disk-2>   # mirror the 14 TB Exos vdev
```

This makes `pool1` a stripe-of-mirrors (RAID10-like) without recreating the
pool. Cross-links: #203 (this decision), #149 (age-key backup relocated, done),
#124 (the `sdb` DEGRADED scare).

## Independent follow-ups

Risk acceptance does **not** preclude cheap, separate risk-reducers — these are
tracked as their own issues:

- **Periodic ZFS snapshot task on `pool1/dataset01`** — **done** (#311).
  Recursive snapshot task, daily at 03:30, 3-day retention
  (`auto-%Y-%m-%d_%H-%M`). This is a cheap independent guard for accidental
  deletion and bad automation only — it does **not** protect against disk
  failure on this 2-disk stripe. If a disk dies, the snapshots die with it.
- **Back up / relocate the 320G `pool1/dataset01/VMs` Proxmox dir** — this is the
  one chunk on the stripe that is **NOT *arr-re-acquirable** and is currently
  unprotected. The `apps` pool has only ~141G free, so it likely needs an
  external/secondary target.
