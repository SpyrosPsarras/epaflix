# Proxmox Backup Server (PBS)

Backing store for the nightly `vzdump` job. Undocumented in git until #597 —
this file is the source of truth for where PBS lives and how it must be sized.

## What / where

| Item | Value |
|------|-------|
| Form factor | **LXC container**, VMID `1031`, `onboot: 1` |
| Host | **takaros** (192.168.10.10) |
| IP | **192.168.10.31** (`vmbr0`, static, gw 192.168.10.1) |
| Hostname | `proxmox-backup-server.epaflix.com` |
| API | `https://192.168.10.31:8007` |
| Built from | community-scripts `ProxmoxVE` PBS helper script |
| Datastore | `VMs-NFS`, path `/mnt/VMs` |
| Datastore disk | `local-raid:vm-1031-disk-1` (LVM-thin, 1000G) |
| Root disk | `local-raid:vm-1031-disk-0` (10G) |

The datastore name `VMs-NFS` is now **misleading** — it is a leftover from when
the datastore lived on TrueNAS NFS. Since #398 it is local LVM-thin on takaros.

PVE storage definition (`/etc/pve/storage.cfg`, cluster-shared):

```
pbs: pbs-backup-local
	datastore VMs-NFS
	server 192.168.10.31
	content backup
	prune-backups keep-daily=7,keep-weekly=4,keep-monthly=3
	username proxmox@pbs
```

## Required sizing — cores are load-bearing, memory is headroom

```
cores: 4
memory: 8192
```

> **Do not shrink `cores`.** One core was the measured cause of #597 and the
> evidence for that is below.
>
> **`memory` is a different claim.** It was raised 1024 -> 4096 on 2026-08-03 and
> 4096 -> 8192 on 2026-08-21 (#763). Only the first raise fixed a failure. At 4 GiB
> nothing was being OOM-killed, and at 8 GiB nothing is either; the container simply
> fills whatever it is given with page cache. So 8192 is deliberate headroom for
> chunk IO rather than a proven floor. Do not shrink it casually, but do not read it
> as a value that has been shown to be necessary. See "The 8 GiB raise" below.

The community-scripts default was `cores: 1` / `memory: 1024`. **That is far too
small** and was the root cause of #597. Evidence gathered 2026-08-03:

- Container RRD showed the single core pinned at **96–102%** for the whole
  backup window (01:00–02:30+) on *every* night sampled 07-28 → 08-03.
- `cpuset.cpus.effective` was literally `1` — one physical CPU.
- `memory.events` had **11,360,306** `high` events against the 1 GiB limit, and
  `memory.pressure` recorded ~50,000 s of stall. PBS chunk IO wants page cache;
  1 GiB starves it.

Applied live with (no restart needed, cgroups take it immediately):

```bash
ssh root@192.168.10.10 'pct set 1031 --cores 4 --memory 8192'
ssh root@192.168.10.10 'cat /sys/fs/cgroup/lxc/1031/cpuset.cpus.effective'  # 1-4
```

### The 8 GiB raise, and why it is not a fix (2026-08-21, #763)

`memory: 4096` was raised to `8192`. What happened next is the useful part:

```
before:  memory.current ~3.91 GiB against a 3.97 GiB memory.high, high events 11,684,560
after:   memory.current  7.07 GiB within seconds, high events 15,290,472 and climbing
         memory.max 8589934592   memory.high 8522825728   oom 0   oom_kill 0
```

PBS took the entire new ceiling immediately, because most of `memory.current` is
reclaimable page cache over a chunk store holding 285,876 chunks and 7.12 TB
logical. It will fill 16 GiB the same way. So more memory buys more cache, which
is a real benefit for chunk IO, but the ceiling was never the binding constraint
and `oom_kill` was 0 at both sizes.

**Read these numbers from the host, not from inside the container.** Inside, the
namespaced cgroup reports `memory.max = max` and `memory.high = max` while
`free -m` reports the configured size via lxcfs, so the container looks unlimited
and the real limit is invisible. The enforced values live at
`/sys/fs/cgroup/lxc/1031/` on takaros:

```bash
ssh root@192.168.10.10 'cat /sys/fs/cgroup/lxc/1031/memory.max /sys/fs/cgroup/lxc/1031/memory.high'
```

### What actually broke on 2026-08-21, which was not sizing (#1075)

Separately from CPU or memory: the datastore filled to **100%, 0 bytes free**, and
**eight guests had no restore point from 2026-08-10 to 2026-08-21**, including k3s
master `vm-1053`. Nightly runs were failing 4-6 guests out of 11 with
`No space left on device (os error 28)` and nobody was told, because PVE's
`ntfy-pve` notification target returns HTTP 400 and the only other leg is dead
mail (#461, #1076).

The cause is not a broken prune or GC — both run. It is that there is no headroom
for the garbage PBS is *required* to keep:

```
index-data-bytes   7,079,643,473,849   (7.08 TB logical, 8.5x dedup)
disk-bytes           836,432,094,151   (836 GB live)
pending-bytes        163,457,865,360   (163 GB unreferenced, held by the 24h+5m grace window)
filesystem                      984 GB
```

840 GB live plus up to 163 GB of grace-held garbage does not fit in 984 GB. GC runs
at 00:01 and the backup job at 01:00, so on any night the grace window has not
expired for that garbage, the datastore is full an hour before backups start.
Running GC by hand reclaimed 152 GB and deleted no snapshot.

So the open sizing question for this container is the **datastore**, not its RAM:
grow `mp0` beyond 1000G (the `pve-raid` thin pool `data` was at 56% so there is
room), cut retention below the current `keep-daily 1, keep-weekly 1, keep-monthly 1`
PBS-side prune job, or accept it and alert on datastore usage. Tracked in #1075.

Reading the task history needs `/var/log/proxmox-backup/tasks/archive`. There is no
`tasks/index` file; a read of that path returns nothing and looks like an idle
server.

## #597 root cause — one cause, two symptoms

Both failure modes in the nightly job come from **PBS being CPU-starved during
the backup window**, not from the network and not from a guest agent.

Why the window is so hot — three things pile onto one core at 01:00:

1. `/etc/pve/jobs.cfg` holds **one** vzdump job at `01:00`, but PVE runs it on
   **every node**, each backing up its own guests and skipping "external" ones.
   So takaros and evanthoulaki both stream into the same PBS at the same second
   (`UPID:takaros:...:6A6E7A87` and `UPID:evanthoulaki:...:6A6E7A82`).
2. The datastore has **`verify-new true`**, so every fresh snapshot is
   immediately re-read, decompressed and SHA-256'd. Sampling either night shows
   **5–6 concurrent `verify` tasks** running through the whole window; the
   `vm-1041` verify alone runs **3–3.5 hours**.
3. The datastore volume and seven of the backed-up guests share **one physical
   PV, `/dev/sda2`** (2.18 TiB, 888 MiB free) — reads and writes on the same
   spindle. See #564.

### Symptom 1 — "PBS connectivity drops"

The data streams actually succeed. What fails is the small *management* API call
PVE makes between guests:

```
ERROR: Backup of VM 1053 failed - pbs-backup-local: error fetching datastores
       - 500 Can't connect to 192.168.10.31:8007        (evanthoulaki, 01:11:41)
ERROR: Backup of VM 1061 failed - pbs-backup-local: error fetching datastores
       - 500 read timeout                               (takaros, 01:31:41)
```

with PBS's own side of the same starvation:

```
proxmox-backup-proxy[226]: Failed to get api service:
    Transport endpoint is not connected (os error 107)
```

`GET /api2/json/admin/datastore` normally answers in milliseconds. Under a
saturated single core the proxy cannot service the accept/TLS path or forward to
the privileged api daemon on `127.0.0.1:82` inside the client timeout. Nothing
crashes, nothing is OOM-killed (`oom_kill 0`), no interface flaps.

### Symptom 2 — VM 1063 `qmp command 'backup' failed - got timeout`

**Not a guest-agent problem.** `qemu-guest-agent` in 1063 is `active` with
`NRestarts=0`, the task log shows `fs-freeze` *and* `fs-thaw` both issued
without error, and its only network mount (`/mnt/k3s-media`, NFS from TrueNAS)
is skipped by QGA anyway because NFS has no `FIFREEZE`.

What actually times out is the PBS-side work that QEMU's `backup` QMP command
does inline, while the guest is frozen. VM 1063 is the only guest with a **1 TiB
disk plus a second disk**, and a 1 TiB fixed index is ~262,144 chunk digests to
register. Same box, same index, two consecutive nights:

| Night | `register chunks` for `drive-scsi1` (1 T) | Job result |
|-------|------------------------------------------|------------|
| 08-02 (bad) | 01:12:32 → 01:16:27 = **235 s** | QMP timeout fired at 01:13:47 |
| 08-03 (good) | 01:13:30 → 01:13:50 = **20 s** | `TASK OK` |

PVE's QMP timeout for `backup` is ~125 s. On a contended night the 1 TiB index
registration takes nearly 4 minutes, so the command is abandoned long before PBS
finishes — PBS then logs `backup ended but finished state is not set` and
removes the half-written snapshot. 1063's 50 G `drive-scsi0` registers in 13 s on
the same bad night, which is why only 1063 is hit: it is a tail-latency victim,
25 times out of ~38 nights.

Onset lines up with the datastore move (#398): the first 1063 QMP timeout was
**2026-06-26**, the first night after PBS came up on takaros `local-raid`
(container uptime confirms a 2026-06-25 boot).

## Still open — needs an owner decision

The CPU/RAM bump removes the starvation that produced every observed failure,
but these are real and were found while investigating:

1. **`verify-new true` is the dominant CPU consumer.** Moving verification out
   of the backup window (`verify-new false` + a scheduled verify job at, say,
   06:00) would cut the load far more than any resource bump. It changes
   verification *policy*, so it is deliberately not applied here.
2. **Both nodes start at 01:00.** Staggering the second node (e.g. 03:00) would
   halve peak concurrency. Changing the schedule was out of scope for #597.
3. **#564 — `/dev/sda2` is a single disk** carrying both the PBS datastore and
   the guests being read. No redundancy, and self-contending.
4. **Thin pool is overcommitted** and unguarded: vzdump logs
   `Sum of all thin volume sizes (2.40 TiB) exceeds ... 2.18 TiB` and
   `You have not turned on protection against thin pools running out of space`.
   Set `activation/thin_pool_autoextend_threshold` below 100. Currently
   `data` is 37.3% / meta 22.9% so it is not urgent.
5. **PBS packages are ahead of the running daemons** —
   `proxmox-backup-server 4.2.4-1 running version: 4.1.4`. The daily update
   service upgrades but never restarts. Needs a service restart in a window.
6. **Local mail delivery is dead on both nodes** — postfix has no
   `/etc/aliases.db`, so every `mail-to-root` notification defers forever. See
   `notifications.cfg` in this directory.

## Notifications
### Applying `notifications.cfg`

```
scp 1-proxmox/pbs/notifications.cfg root@192.168.10.10:/etc/pve/notifications.cfg
ssh root@192.168.10.10 'pvesh get /cluster/notifications/targets'
ssh root@192.168.10.10 'pvesh create /cluster/notifications/targets/ntfy-pve/test'
```

`/etc/pve` is pmxcfs, so one copy covers both takaros and evanthoulaki.

**The file must contain no comments.** PVE's section-config parser rejects any
line before the first section header - a leading `#` block makes the whole file
unreadable and `pvesh get /cluster/notifications/targets` fails with:

```
could not deserialize configuration: parsing "notifications.cfg" failed: line 1 - syntax error (expected header)
```

which silently disables **all** notification routing, including the mail path.
Keep `notifications.cfg` byte-identical to what PVE itself writes, and put
explanation here instead. Regenerate it with:

```
ssh root@192.168.10.10 'cat /etc/pve/notifications.cfg' > 1-proxmox/pbs/notifications.cfg
```

What the stanzas do:

- `mail-to-root` / `default-matcher` - PVE builtins, kept so info-level
  behaviour is unchanged. Local mail delivery is dead on both nodes (postfix has
  no `/etc/aliases.db`, every notification sits deferred forever), which is why
  weeks of nightly backup failures went unnoticed. Tracked in #720.
- `ntfy-pve` - webhook to `http://192.168.10.112:8091/pve-backups`, the LAN-only
  kube-vip LoadBalancer on the in-cluster ntfy Service
  (`2-k3s/13.odysseus/ntfy.yaml`). Header and body values are base64 per the API
  schema: `Title = {{ title }}`, `Tags = warning`, `Priority = 4`,
  body = `{{ title }}\n\n{{ message }}`.
- `ntfy-failures` - routes `warning,error` only, so a successful nightly job
  stays silent and a failed one pushes.

Prefer creating changes through the API rather than editing the file, since PVE
then writes canonical syntax for you:

```
pvesh create /cluster/notifications/endpoints/webhook --name ntfy-pve --method post \
  --url "http://192.168.10.112:8091/pve-backups" \
  --header "name=Title,value=e3sgdGl0bGUgfX0=" ... 
pvesh create /cluster/notifications/matchers --name ntfy-failures --mode all \
  --match-severity "warning,error" --target ntfy-pve
```


Job failures now route to ntfy instead of unread local root mail — see
[`notifications.cfg`](notifications.cfg) in this directory, and
`2-k3s/13.odysseus/ntfy.yaml` for the LAN-only LoadBalancer that makes ntfy
reachable from the PVE hosts.
