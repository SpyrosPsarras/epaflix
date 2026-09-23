# Offline T3 consolidation

Base commit `8b45bdca9b132cc243433d4d8466474f8f7f9dff`.

The destination is one T3 server pod in namespace `t3code`. This file is the record of the migration, finished on 2026-09-23. The tooling it describes (`cutover.sh`, `migrate.py`, `repair_home.py`, `verify.py`, `resume_smoke.mjs`, the tests) was removed after the sunset; `git show 0f94ffe:2-k3s/13.t3code/migration/<file>` brings any of it back.

## Plan

- D1. Copy projections. Keep UUIDs and stream versions. Concatenate source event ranges in host, t3env-0, t3env-1 order. Translate event sequences, receipt result sequences, activity sequences including event payloads, and turn row IDs. Do not reorder history by timestamps.
- D2. Require identical inspected schemas and 53 matching migration identities. Reject key collisions instead of silently choosing a row. The schema fingerprint includes indexes. Source migration timestamps may differ.
- D3. Give providers source-specific HOME directories. Host instances keep their ids (`opencode`, `codex`, `claudeAgent`) so `defaultModelSelection` and new threads stay on them. Env instances become `t3env-0__opencode` etc. with `HOME=/home/t3env-0`, shown as "[t3env-0 history]" in the picker. Codex, Claude and OpenCode stores stay separate, so the two colliding OpenCode project IDs never meet. No provider database merge.
- D4. Preserve host paths. Map structural `/home/t3` paths to `/home/t3env-0` or `/home/t3env-1`. Message text, summaries, diffs and provider JSONL bytes remain unchanged. JSON transformations use explicit structural field names, never text replacement. `repair_home.py` fixes Git worktree pointers and absolute symlinks inside the copied env homes; nothing else is rewritten.
- D5. Require all data projectors at the source high-water mark. The attachment-cleanup projector may lag: the installed server (`ProjectionPipeline.bootstrap`) replays every `thread.deleted` after its watermark and removes that thread's attachment files, which is idempotent. The merger rewinds the destination cleanup watermark to the earliest unconsumed source tail, offset-translated. A pending `thread.reverted` is refused because that prune depends on message state.

## Inspected evidence

On 2026-09-21, the live sources had 351, 54 and 39 threads respectively, 444 total. The earlier 442 count is stale. Sources remain active, so the final frozen snapshot count is authoritative.

The local CLI reports `v0.0.43-nightly.20260920.2031`. Actual source schemas were inspected with read-only SQLite connections, including both remote pods. The merger checks every snapshot against the local inspected schema fingerprint before copying rows.

Upstream source inspected on 2026-09-21:

- F1. [Provider instance contracts](https://github.com/pingdotgg/t3code/blob/main/packages/contracts/src/providerInstance.ts) define per-instance environment variables.
- F2. [OpenCode driver](https://github.com/pingdotgg/t3code/blob/main/apps/server/src/provider/Drivers/OpenCodeDriver.ts) passes the instance environment into its own spawned server. A configured external `serverUrl` bypasses HOME isolation and needs explicit resolution before cutover.
- F3. [Codex driver](https://github.com/pingdotgg/t3code/blob/main/apps/server/src/provider/Drivers/CodexDriver.ts) supports separate home paths. Configured homePath takes precedence over CODEX_HOME.
- F4. [Projection pipeline](https://github.com/pingdotgg/t3code/blob/main/apps/server/src/orchestration/Layers/ProjectionPipeline.ts) has deletion and revert attachment side effects. Replay was not established safe and is not used.

These links describe moving upstream main, not proof that the chosen destination binary behaves identically. Verify isolation with that exact binary before cutover.

## Rehearsal

Run from this directory with Python 3.11 or later and the existing kubectl context:

```sh
python3 verify.py
mkdir -m 700 private
python3 migrate.py snapshot private/snapshots
python3 migrate.py merge private/snapshots private/merged
```

Use a new output path on every run. Existing outputs are refused. `private/` is ignored by git and must stay private. SQLite snapshots and settings contain secrets and conversations. Do not attach them to a review or print their contents.

`snapshot` uses SQLite online backup into memory through read-only connections. Remote snapshots stream through `kubectl exec`; no remote temporary files are written. Settings are copied separately. This is a database rehearsal, not an application-consistent backup of active providers. A failed capture leaves partial inputs; discard that input directory manually and recapture to a new directory.

Input layout:

```text
snapshots/
  host/state.sqlite
  host/settings.json
  host/home/                 optional frozen home contents
  t3env-0/state.sqlite
  t3env-0/settings.json
  t3env-0/home/
  t3env-1/state.sqlite
  t3env-1/settings.json
  t3env-1/home/
```

The merger copies optional frozen homes to `homes/<source>` with SHA-256 verification of every file. Provider JSONL is never parsed or rewritten. Symlinks and special files are rejected. Stage required symlink targets explicitly from a frozen backup; do not dereference arbitrary live links. This copier is intentionally not a home backup utility.

The output contains `state.sqlite`, `settings.json` and `report.json`, plus copied homes when supplied. The report records source hashes, per-table counts, offsets and original watermarks. It always says `cutover_ready: false`; the database merge alone is not a cutover, `cutover.sh` is.

## Rehearsal result (2026-09-21)

A full dress rehearsal ran against live snapshots, with all three sources still serving. The pod `t3code/t3code-0` is running on k3s-worker-65 with that data and answers on `https://t3new.epaflix.com` (Traefik internal LB, wildcard cert).

- 444 threads, 15 projects, 228,209 events merged. Offsets: host 0, t3env-0 +153,978, t3env-1 +211,248. Turn row offsets 0 / 18,701 / 24,813.
- Cleanup watermark rewound to 150,589 (host tail with one pending `thread.deleted`). After first boot the server drained it to the high-water mark. Six new events were written by the server itself (`thread.session-set`, `thread.settled`), none by a provider.
- 57 attachment refs, 57 files on disk after unioning the three `attachments/` folders.
- Homes copied: host 12G (119,126 entries), env-0 1.6G (37,494), env-1 1.0G (26,250), each verified by archive listing against `find`. One entry is reported missing only because `tar -t` prints a `\303\270` escape for `ø` in a Go test snapshot filename; the file is present.
- `repair_home.py` rewrote 113 (env-0) and 69 (env-1) worktree pointers and symlinks; re-run is a no-op. Skill and instruction links are re-created per home by `private-config.py install`.
- Provider instances loaded: 3 host, 6 env. `t3 connect status` shows unlinked (cloud secrets parked in `userdata/secrets-cloud-premerge`). Vault bridge over SSH answers. MCP paths in `opencode.json`, `config.toml` and `.claude.json` rewritten to `/scripts/*`.
- Resume test: `resume_smoke.mjs` sent "Reply with exactly: RESUME-OK" to one existing thread per source/provider pair over the same WebSocket RPC the browser uses. All seven came back `completed | RESUME-OK`: host opencode, codex, claudeAgent; t3env-0 opencode, claudeAgent; t3env-1 opencode, codex. Each env provider ran against its own home (verified by the new OpenCode message landing in `/home/t3env-0/.local/share/opencode/opencode.db`).
- Two things broke on the first resume attempt and are now fixed in the tooling. OpenCode resumes into `session.directory`, which still said `/home/t3/...`; `repair_home.py` now rewrites the OpenCode path columns too. Host Codex launched with `base_url=https://cliproxy.epaflix.com`, which resolves to Cloudflare from inside the cluster and 404s; the merger now rewrites that to the in-cluster Service for every instance.
- Node k3s-worker-65 hit disk pressure with the three new PVCs on a 50G root. Kubelet evicted `t3code-0` and also `remote-pi/t3env-0`, which shares the node. t3env-0 was rescheduled onto the same PVC and was back in about five minutes (restart count 0, no data change). That was collateral from my rehearsal, not a source write. The disk is now 100G (online `qm resize` + `growpart` + `resize2fs`, no reboot) and the node reports `DiskPressure=False`.
- The LXC server was untouched throughout. Neither env PVC was written to.

Not yet exercised: pairing a browser on `t3new.epaflix.com`. The rehearsal pod is live for that.

## Cutover

`cutover.sh` runs from the laptop. It needs `kubectl` (context epaflix), `ssh proxmox-takaros` (root, to stop the LXC service via `pct exec`) and `ssh spyros@192.168.10.240`. Phases are idempotent and marker-gated in `$WORK` (default `~/t3-cutover`).

```sh
bash cutover.sh all      # freeze, snapshot, merge, load, start
bash cutover.sh route    # after you have opened t3new and checked threads
bash cutover.sh rollback # any time before route: old servers back, pod off
```

- A1. `freeze` stops the LXC `t3code.service` and its update timer, and scales `remote-pi/t3env` to 0 with ArgoCD automation off. Nothing writes after this.
- A2. `snapshot` takes SQLite online backups (WAL included) to a file on each source, copies and hash-checks them.
- A3. `merge` runs `migrate.py` and asserts the thread total equals the source sum.
- A4. `load` applies the `one/` overlay with replicas 0, binds the three PVCs through a helper pod, archives each home on its source with the exclude list, lands it in 256M parts with per-part hashes, extracts, runs `repair_home.py`, installs the merged `state.sqlite` and `settings.json`, unions attachments, parks cloud-link secrets, and asserts attachment refs resolve and no env home carries a second T3 state.
- A5. `start` scales to 1, waits, and checks: http 200, every projector at the high-water mark, `t3 connect` unlinked, vault bridge, Traefik route, then resumes one thread per source/provider and expects `RESUME-OK` from each. Then you pair a browser on `t3new.epaflix.com`.
- A6. `route` moves `t3code.epaflix.com` to the pod. Commit the matching change to `05.traefik-deployment/ingress/t3code-proxy.yaml` in the same PR or ArgoCD reverts it.
- A7. Rollback before `route` is `cutover.sh rollback`. After `route`, also re-apply the old IngressRoute. Old PVCs and the LXC home are never modified; delete them after a two-week bake by hand.

Before `all`, delete the rehearsal data so `load` starts clean: `kubectl -n t3code scale sts t3code --replicas=0`, then delete and recreate the three PVCs (or `rm -rf` inside a helper pod). The rehearsal pod's PVCs hold a merge of the sources as of 09:40 UTC today; the real run must re-snapshot.

## Cutover result (2026-09-21, 15:57 to 17:23 UTC)

Ran from the laptop with `PVE_SSH=takaros`. Frozen sources: host 161,479 events, t3env-0 58,679, t3env-1 17,491. Merged 452 threads, 237,649 events. Homes landed: t3env-0 38,649 entries, t3env-1 26,320, host 119,844 (5.2G archive), all verified against the archive listing. `repair_home.py` made 376 and 184 changes. Load check: 452 threads, 9 provider instances, 57 attachments on disk. All seven resumes returned `completed | RESUME-OK`. `route` applied; `t3code.epaflix.com` answers 200 from the pod. The old EndpointSlice + Service in `traefik-system` were deleted by hand; `route` now does that itself.

Fixed in `cutover.sh` during the run: tar warnings leaked into captured paths and hashes (now sent to stderr), `tar -t` escaped a non-ASCII filename so the listing check reported one false miss (`--quoting-style=literal`), the two heredoc python checks ran `kubectl exec` without `-i`, and the post-start projector check demanded the cleanup projector reach the live high-water mark, which the server keeps moving; it now only has to pass the merged high-water mark. The `one/` StatefulSet image was moved to the same `inputs-` tag as `env/` so `sync-shared.sh --check` passes.

Pi-hole had no record for `t3new.epaflix.com`; added `192.168.10.102 t3new.epaflix.com` to `/etc/pihole/hosts/custom.list` on 192.168.10.30. Remove it with the sunset.

ArgoCD: `app-of-apps`, `t3code-env` and `traefik` had `automated` sync removed by live patch. `t3code-env` so `t3env` stays at 0 replicas, `traefik` because self-heal put the old EndpointSlice route back and `t3code.epaflix.com` returned 502 until the patch. All three come back with `automated` once this PR merges; the committed manifests then match the live state.

## Sunset (after a two-week bake)

- `kubectl -n remote-pi delete statefulset t3env; kubectl -n remote-pi delete pvc home-t3env-0 home-t3env-1`
- Remove `env/statefulset.yaml`, `env/service.yaml`, `env/ingress.yaml`, `2-k3s/13.t3code/kustomization.yaml` and the ArgoCD app `app-t3code-env.yaml`. Keep `env/files`, `env/tools` and `env/Dockerfile`; `one/` and the runtime image build read from them.
- Restore `automated` on `app-of-apps` (`kubectl -n argocd patch application app-of-apps --type merge -p '{"spec":{"syncPolicy":{"automated":{"selfHeal":true,"prune":false}}}}'`) and flip `app-t3code.yaml` to automated.
- On the LXC `t3code.service` is stopped and disabled. Decide whether the LXC stays for SSH, Syncthing and the vault MCP (the pod reaches the vault through it) or those move too.
- Inside the pod, `t3 connect link --headless --base-dir /home/spyros/.t3` re-links T3 Connect. Parked LXC link secrets sit in `userdata/secrets-cloud-premerge`.
- Drop the `t3new.epaflix.com` Pi-hole record and `one/ingress.yaml` once nothing uses the name.

## Sunset result (2026-09-23)

Done early, two days into the bake, on request.

- The LXC was still reachable after cutover. The T3 desktop app kept launching a server on it over SSH, and both servers share environment id `efa9a9de-95da-40c1-bc19-b85690f16e68`. It held one thread from 2026-09-23 09:05 to 09:20 UTC+2, a question about the update button asked on the wrong box. Nothing else changed in its home after cutover except caches and repo checkouts. T3 Connect was left alone on the LXC because logging out there could revoke the shared environment link the pod uses.
- Vault: `syncthing/keepass` now serves the MCP (PR #1507). The LXC was removed as a Syncthing device after the hub reported it 100% in sync on `secrets-vault`.
- AKS: `~/.local/bin/aks-auth` in the pod home ran over SSH to the LXC. It now runs `aks-auth-central` locally, and `az` in the pod was already logged in.
- CT 100 destroyed with `purge=1&destroy-unreferenced-disks=1`: disk `local-raid:vm-100-disk-0` removed, vmid dropped from backup job `backup-ef3c2d49-5f5a`, about 110G freed on `local-raid`. The last PBS backup is `pbs-backup-local:backup/ct/100/2026-09-22T23:00:14Z`. It could not be marked protected because the PBS datastore is full (`No space left on device`), so prune will eventually remove it.
- Pi-hole: removed `t3code-ssh.epaflix.com` from `10-epaflix.conf` and the `t3env0`/`t3env1` host records, then restarted `pihole-FTL`. `t3new.epaflix.com` was already gone, so `one/ingress.yaml` went too.
- t3env: the ArgoCD app `t3code-env` and its resources are deleted, as are the PVCs `remote-pi/home-t3env-0` and `home-t3env-1` (40Gi). The copies of those homes in `t3code/home-t3env-*` stay; the pod mounts them.
- Repo: LXC provisioning (`1-proxmox/t3code`, `provision.sh`, `update.sh`, the wizards, the guest-only `files/` scripts) and the t3env manifests are removed. `env/` keeps only what the runtime image and its CI use.
- ArgoCD: the `t3code` app is automated (selfHeal, prune). The namespace and the three home PVCs carry `Prune=false,Delete=false`, so neither a prune nor an app delete can remove agent history. The migration tooling in this directory was deleted; this README stays as the record.

## Known gaps

- `one/tools` and most of `one/files` are byte copies of `env/` inputs because kustomize cannot read above its root. `one/files/entrypoint.sh` and `one/files/searxng-mcp.py` are this overlay's own. `one/tools/sync-shared.sh --check` catches drift; the `build-t3-runtime` workflow runs it.
