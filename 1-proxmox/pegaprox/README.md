# PegaProx (LXC 1021) — Operational Runbook

Multi-cluster Proxmox management UI. Runs as a systemd service (`pegaprox.service`,
user `pegaprox`) inside an unprivileged LXC.

## Inventory

| Item          | Value                                    |
|---------------|-------------------------------------------|
| Proxmox host  | evanthoulaki (192.168.10.11)              |
| VMID          | 1021                                      |
| IP            | 192.168.10.21                             |
| Public URL    | `https://pegaprox.epaflix.com` (via Traefik) |
| Install path  | `/opt/PegaProx`                           |
| Service       | `pegaprox.service`, runs as `pegaprox` user |
| Current version | 1.0 (upgraded from 0.9.15 on 2026-08-01) |

## Never run `pegaprox.*` code as root

`pegaprox/constants.py` mutates the filesystem at **import time**, not in
`main()` — it creates/chmods `config/ssl` and (on the 0.9.15 upgrade path)
copies legacy certs into it, as whatever user does the importing.

**Symptom:** any root-owned import re-owns `config/ssl` to `root:root`. The
service runs as `pegaprox`, so on the next start it can't read its own cert.
The failure mode depends on version:

- **≤ 0.9.15** — fails **open**: logs `WARNING: Could not generate SSL
  certificate: [Errno 13] Permission denied: 'config/ssl/cert.pem'` then
  `Starting without HTTPS (noVNC may not work)`, and silently serves
  **plaintext HTTP on the TLS port** (5000). Traefik's TLS handshakes then
  fill the log with `Invalid http version: '\x16\x03\x01...'`. This is the
  outage mode from 2026-07-29 (issue #484) — the process looks "up" the whole
  time, which is what made it dangerous.
- **≥ 1.0** — fails **closed** (fixed upstream, PegaProx/project-pegaprox#633,
  our PR #637): the service refuses to start. `systemctl status` shows
  `Main process exited, code=exited, status=1/FAILURE`, log says `TLS is
  enabled but there is no usable certificate: cannot read
  config/ssl/cert.pem: Permission denied`. Loud instead of silent, but still
  down — don't run app code as root just because a bad cert can no longer
  hide behind a fake-healthy process.

**Safe invocation:** `sudo -u pegaprox /opt/PegaProx/venv/bin/python ...` —
never plain root, not even for a "just reading" throwaway script (a unit-test
runner and a metrics sampler are what caused it in practice).

**Recovery** once ownership is broken:

```bash
chown -R pegaprox:pegaprox /opt/PegaProx/config/ssl /opt/PegaProx/config/branding
systemctl restart pegaprox
journalctl -u pegaprox -n 20 --no-pager   # confirm: "SSL certificates found - starting with HTTPS"
```

## Pre-upgrade snapshot lifecycle

Take a full container snapshot before every PegaProx upgrade. `update.sh`'s own
code backup does not include the live config/database state.

```bash
SNAPSHOT="preupgrade_$(date +%Y%m%d_%H%M%S)"
pct snapshot 1021 "$SNAPSHOT" --description "Before PegaProx upgrade"
pct listsnapshot 1021 | grep -F "$SNAPSHOT"  # must exist before continuing
pct exec 1021 -- bash -lc 'cd /opt/PegaProx && ./update.sh'
```

After the upgrade, run the checklist below and keep the snapshot through the
planned soak window. Retire it only after a deliberate issue-backed review.
Before deleting it, search open issues for the snapshot name and confirm every
referenced rollback gate is met.

## Post-`update.sh` checklist

1. Confirm the log line `SSL certificates found - starting with HTTPS` after
   the restart. On 1.0+ a merely-active service already implies this (it
   fails closed otherwise), but check anyway — cheap insurance.
2. ~~Re-apply the local `manager.py` netin/netout live-rate patch~~ — **no
   longer needed as of 2026-08-01.** That patch (branch
   `fix/node-net-live-rate`, upstream PegaProx/project-pegaprox#632) now
   ships natively in PegaProx **1.0**. Verify instead of assuming:
   `grep -n "cluster/metrics/export" /opt/PegaProx/pegaprox/core/manager.py`
   should show a live-rate code path tagged `#632, #419 follow-up`. If a
   future release regresses this, the fix lives upstream now — pull the
   release, don't hand-patch `manager.py` again.
3. `update.sh` itself is not, and never was, the cause of the root-ownership
   trap — see below. Don't waste time re-suspecting it.

## Corrected finding: `update.sh` was not the cause

`update.sh` was the first suspect for the ssl-ownership break on 2026-07-29,
and that reading was wrong. Reproduced its `tar | tar` copy path in isolation
(GNU tar 1.35, as root, same exclude list): `config/ssl` ownership was left
untouched by the copy itself.

As of the 1.0 `update.sh`, this is doubly true — upstream hardened the
ownership-restore block specifically citing #633:

```bash
# images/ was missing here - left root:root on a non-root install (#633)
[ -d "images" ] && chown -R "$ORIGINAL_OWNER" images/ 2>/dev/null
# config/ too: we chmod 700 it further down, so a single root-owned file in
# there (a root-run import can create config/ssl/cert.pem) locks the service
# user out of its own certs. ORIGINAL_OWNER is read from config/ itself, so
# this only ever repairs children (#633).
[ -d "config" ] && chown -R "$ORIGINAL_OWNER" config/ 2>/dev/null
```

So on 1.0+, running `update.sh` will *self-heal* a root-owned `config/ssl`
left over from an earlier mistake. That's a safety net, not a substitute for
following the rule above — between two `update.sh` runs the trap still bites
exactly as described.

The `images/` root-ownership this was tracking down was harmless either way:
mode stayed `644`/`755`, world-readable, the app only ever serves those files.

## Version / upgrade history

| Date       | Version           | Notes |
|------------|-------------------|-------|
| 2026-07-29 | 0.9.10.3 → 0.9.15 | Root-import trap first hit outage-shaped (issue #484). |
| 2026-08-01 | 0.9.15 → 1.0      | Ships the fail-closed SSL fix (#633 / our PR #637) and the netin/netout live-rate fix (#631 / #632) natively. Local `manager.py` patch dropped. |

The 0.9.15 rollback set was retired on 2026-08-05 after version 1.0 soaked
successfully (issue #623). It was two releases behind and restoring it would
have discarded current config/database state and the 1.0 security fixes.

No container snapshot was taken for the 0.9.15 → 1.0 jump. The remaining
`backups/backup_0.9.15_20260801_195658/` is `update.sh`'s code backup plus
`version.json`, not a full container/config rollback point. Future upgrades
must follow the pre-upgrade snapshot lifecycle above.

## Related

- Repo issue: SpyrosPsarras/epaflix#484
- Upstream: PegaProx/project-pegaprox#631 (bug), #632 (fix PR — closed, not
  merged as such, but shipped in 1.0), #633 (bug — our report, closed), #637
  (our fix PR for #633, shipped in 1.0)
- `.github/instructions/proxmox.instructions.md` — general Proxmox VM/LXC guidance
