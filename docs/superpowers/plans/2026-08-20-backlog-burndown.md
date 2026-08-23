# Backlog burndown - plan

Written 2026-08-20. Covers 92 open issues and 9 open PRs on `SpyrosPsarras/epaflix`.

## What triggered this

The ask was "fix all my github issues". This has been attempted once already and
the attempt is worth reading before starting again, because it measured things
that change what the goal should be.

## The prior attempt

Automated run `01KZK3CKNPY463MAC9707DX69V`, process
`close-all-open-issues.js`, paused 2026-08-10 for a host reboot.

It cannot be resumed: the run journal that made it resumable is gone, and the
orchestrator that produced it has since been retired (2026-08-23). What
survives is the process definition and `close-all-open-issues.inputs.json` with
its 12 cluster definitions — both archived out of the repo to
`~/.a5c-archive-epaflix/processes/` — plus
`artifacts/close-all-issues/ledger.json` and about 50 working files under
`artifacts/close-all-issues/`. That is enough to restart without
re-deriving anything. It is not enough to `run:iterate`.

Three of its findings matter more than its output.

**The count goes up when you work the backlog.** The run closed 8 issues and
filed roughly 23. Open count went 69 to 84 during the run and stands at 92
today. Measured across the whole window since 2026-08-09: 35 issues closed, 60
opened, net +25. This is not the run failing. Honest investigation of one issue
finds defects that were already there and unrecorded, and the repo rule is to
file every one of them. The generation rate exceeds the closure rate, and it
will keep doing so until the genuinely broken subsystems are actually fixed
rather than surveyed.

**About 36 issues structurally cannot be closed by an agent.** The prior run's
lane census, re-measured today against current labels:

| Lane | Count | Who can close it |
|---|---|---|
| `needs-decision` | 30 | owner decides, agent cannot |
| `agent-gated` | 13 | agent works it, owner approves one gate |
| `needs-hands` | 8 | owner acts - physical, third-party UI, or a window |
| `blocked-external` | 4 | upstream, nobody here |
| unlabelled / agent-now | ~37 | agent, unattended |

Floor with no owner involvement is roughly 55 open. So "zero open issues" is not
a reachable target and tracking progress by open count will read as failure even
on a good week. The prior run proposed a better metric and I agree with it:
**drive the agent-now lane to empty, and convert `needs-decision` from 30 vague
threads into decided answers.**

**Same-file issues must share a PR.** #908, #909, #910, #912 and #913 all edit
the same observability YAML. As five sequential PRs under this repo's
semi-linear merge policy, with mandatory rebase and a strict up-to-date check,
each merge invalidates the other four. They are one PR. This is the reason the
unit of work below is a cluster and not an issue.

## Decisions taken

Asked and answered on 2026-08-20:

- Triage to a real backlog before burning it down.
- Work cluster by cluster, not issue by issue.
- Chart a wayfinder map per decision-heavy cluster.
- Serial execution, one PR at a time.

The last two interact with the same-file finding above, so to be explicit:
**one PR at a time means one PR per cluster, not one PR per issue.** Serial at
the issue level would deadlock the observability five against each other.

This also settles the two questions the prior run asked and never got answers
to. Question 1 (throughput) is answered by cluster grouping. Question 2
(definition of done) is answered by the wayfinder maps, which do more than
rewrite the `needs-decision` issues into crisp questions - they order them and
record the answers.

## What already exists and should not be rebuilt

**#922 is a finished wayfinder map.** "Wayfinder map: TrueNAS host and GPU
observability, and where ntfy lives" is charted to a standard well above what a
fresh charting session would produce. Destination, notes with 12 read-first file
references, a mermaid graph, 9 tickets, 7 recorded decisions, 2 fog patches, 3
out-of-scope entries, and dropped blocking edges with the reasoning for each
drop. Its 9 tickets (#904, #914 through #921) are open and unclaimed. It has
never been worked.

Two defects in it, both cosmetic and both worth fixing before a session runs
against it. It does not carry the `wayfinder:map` label, and its tickets carry
no `wayfinder:<type>` labels, so no frontier query finds them. The labels all
exist in the repo already.

**#498 is the proven pattern.** One completed wayfinder map, closed, 10 tickets
across grilling, research, prototype and task. The convention works here.

**Investigations already banked.** Do not re-derive these:

| File | What it holds |
|---|---|
| `artifacts/close-all-issues/handoff/handoff-779.md` | #779 investigation complete. Fix, doc and PR remain. |
| `artifacts/close-all-issues/adopt-779.md` | Headline: the sweep command in #779 returns zero hits and is a false negative. `kubectl get -o json` strips `managedFields`. |
| `artifacts/close-all-issues/adopt-902.md` | #902 GPU fix already applied live 2026-08-09, never committed. Scripts and a README patch are sitting there waiting for a PR. |
| `artifacts/close-all-issues/comment-541.md` | #541 measured. The homarr OIDC slug collides with the #284 forward-auth application. |
| `artifacts/close-all-issues/rd-newissue.md` | #31 and #957 root cause. The fine-grained PAT never got `Issues: write`. |

## Loose ends to clear first

Five files have been uncommitted in the working tree since before 2026-08-10 and
survived a branch switch back to `main`:

```
 M 0-truenas/README.md
 M 2-k3s/13.odysseus/ntfy.yaml
?? airvpn-auto-finder.md
?? issues-list.txt
?? issues-nonairvpn.md
```

The two modified files look like hand edits and both sit in the path of planned
work. `ntfy.yaml` is the file #914 and #915 decide the shape of, and
`0-truenas/README.md` is what the parked #902 patch applies to. Resolve these
before any branch work, or the first rebase will surface them at the worst time.
The three untracked root files belong under `artifacts/`.

## Phases

### Phase 0 - drain the PRs

One sitting. Nothing here needs a decision.

| PR | Action |
|---|---|
| #847, #848, #928, #987, #1019 | Renovate, all CLEAN. Rebase onto `origin/main`, `push --force-with-lease`, wait for `validate`, `gh pr merge --merge`. Serial, one at a time. |
| #977 | Real work, stranded 10 days, `docs(secrets)` +376/-183. Rebase, re-verify, merge. Belongs to the secrets cluster. |
| #958 | Real work, stranded 10 days, prowlarr key resync. Pairs with #960 which is its own post-merge verification issue. |
| #819 | Blocked on #818. Do not merge. Park until the spec-system decision lands. |
| #604 | Draft, stale, targets `v1.36.2+k3s1` while stable moved on. Belongs to the maintenance window, not to Phase 0. |

Also check every merged PR from this run's lineage for unticked test-plan boxes.
`CLAUDE.md` records 24 merged PRs that accumulated exactly this, and the rule is
that an unchecked post-merge box with no tracking issue is a lost follow-up. The
true backlog is larger than 92 by however many of those are still open.

### Phase 1 - triage to a real backlog

One session, read-only against the tracker except for closes and labels.

26 issues have not been touched since 2026-08-03 to 08-05. Some are no longer
true. #726 is a post-soak flip whose soak has long elapsed. #471 and #482 are
`blocked-external` findings that may have resolved upstream. Verify each against
live state, then close or refresh.

The repo rule applies with no exceptions: never close a soak or flip issue on
"soak elapsed". Paste the literal current value, from both the manifest and
`kubectl --context epaflix get application ... -o jsonpath`. #50 was closed as
done while the manifest still said `prune: false`, and #551 exists because of
it.

Output of this phase is a labelled backlog where every issue has a lane and a
cluster, plus the refreshed cluster map below committed as fact.

### Phase 2 - chart the maps

Only for clusters with real fog. A cluster of diagnosed bugs with known fixes
needs a branch, not a map.

Needs a map: edge exposure, secrets and recovery, PBS and Proxmox, pool1
capacity, and the arr pipeline. The arr pipeline needs one mainly to decompose
itself, because 20-odd issues is too many to hold in one session.

Already has a map: TrueNAS, GPU and ntfy. That is #922. Label it and work it.

Does not need a map: alerting rules, promtail, ArgoCD sync correctness, renovate,
tooling. These are measured bugs with obvious fixes.

### Phase 3 - burn down cluster by cluster

One cluster, one branch, one PR, merged before the next starts.

Ordering below is risk-weighted, not oldest-first. I am deliberately breaking
the prior run's oldest-first rule, because oldest-first is what left an origin
server answering port 443 to the whole internet while agents fixed subtitle
counts.

1. **Edge exposure.** #547, #549, #959, #966, #937. The origin IP is leaked via
   a wg-hop DNS record and the origin accepts direct 80/443 from any internet
   host, so Cloudflare currently provides nothing. Everything else on this list
   is less urgent than that. Map first, then fix.
2. **Secrets and recovery.** #580, #778, #801, #802, #803, #782, #963, #972,
   #978, #979, #982, plus PR #977. The only reachable age private key lives
   inside the cluster it is meant to help recover. That is an unrecoverable-bad-day
   shape and it has been open since 2026-08-01.
3. **Alerting rules.** #908, #909, #910, #912, #913 as ONE PR. Then #970, #980,
   #781. Do this third because until alerting is honest, every later phase is
   unverifiable. Today the mailbox only ever gets bad news, job failures are
   amplified about 8x, and one critical alert can never resolve because it
   matches Loki's own ruler logs.
4. **Promtail and logs.** #911, #824, #1006. Roughly 14k credential-shaped lines
   a week still reach Traefik's stdout.
5. **TrueNAS, GPU and ntfy.** Work map #922 to completion, one ticket per
   session. Then adopt the parked #902 work. Nine tickets, so this is nine or
   ten sessions and the largest single time commitment on this list.
6. **ArgoCD sync correctness.** #1010, #779, #715, #726. Sync reports Succeeded
   while silently skipping an OutOfSync ConfigMap, and Secrets report Synced
   while a legacy helm field manager hides stale keys. Both undermine trust in
   every fix landed above, which is why they come before the easy remainder.
   #779 is investigation-complete, so it is cheap.
7. **Maintenance window.** #329, #413 with PR #604, #718, #583. Batch these into
   one window. #329 power-cycles masters and swarm nodes to `cpu: host`, #413
   starts a 7-node k3s roll, #718 restarts PBS daemons, #583 brings the swarm
   back. Doing them separately means draining the fleet three or four times.
   Note that when #413 merges, ArgoCD `selfHeal` starts the roll immediately,
   and Traefik is a single replica pinned to worker 62 by an RWO PVC, so every
   `*.epaflix.com` hostname including the ArgoCD UI goes down while 62 drains.
   Supervised only.
8. **PBS and Proxmox.** #564, #585, #717, #719, #763. Four decisions, so map
   first. The single disk backing PBS on takaros means a disk failure loses
   every guest restore point.
9. **pool1 capacity.** #609, #805, #806, #807, #968, #969, #699. Pool at 82%
   with about 508 GB reclaimable, but #609 says root-cause the 27.6% path-match
   gap before any bulk delete, and the repo rule says grep open issues for a
   dataset name before destroying it. #515 destroyed a rollback target an open
   issue still named.
10. **The arr pipeline.** Roughly 20 issues across sonarr queue blindness,
    indexers, key rotation, content triage, lingarr and the rename. Map to
    decompose, then sub-clusters. Largest cluster, lowest blast radius, so it
    goes last.
11. **Renovate and tooling.** #31, #595, #957, #818, #971, #973, #997, #1013,
    #565. Mostly small. #957 unblocks #31 with a token permission change.

## Refreshed cluster map

The prior run's 12 clusters, with closed members dropped:

| Cluster | Open members |
|---|---|
| C1 sonarr blind queue | #705 #834 #837 |
| C2 avistaz korean | #471 #477 |
| C3 cleanuparr | #482 #871 |
| C4 age key recovery | #580 #782 #801 #802 #803 |
| C5 pool1 capacity | #609 #805 #806 #807 |
| C6 pbs backup | #564 #585 #717 #718 #719 #763 |
| C7 dns edge | #547 #549 |
| C8 swarm down | #329 #583 |
| C9 authentik forwardauth | #541 #883 |
| C10 reloader argocd | #715 #726 #779 |
| C11 lingarr | empty, all closed |
| C12 secret scan ci | #824 #856 |

59 open issues fall outside those, almost all filed after 2026-08-09. New
clusters for them:

| Cluster | Members |
|---|---|
| C13 alerting rules | #908 #909 #910 #912 #913 #970 #980 #781 |
| C14 promtail and logs | #911 #1006 |
| C15 truenas gpu and ntfy (map #922) | #902 #904 #914 #915 #916 #917 #918 #919 #920 #921 #922 |
| C16 secrets store and docs | #963 #972 #978 #979 #982 |
| C17 edge exposure | #937 #959 #966 |
| C18 arr key rotation | #873 #960 #975 #976 |
| C19 arr content triage | #520 #526 #968 #969 #699 #1029 #1030 #1032 |
| C20 vpn | #817 #1031 |
| C21 rename newtarr to seekarr | #473 #955 |
| C22 lingarr requeue | #926 #961 |
| C23 renovate | #31 #595 #957 |
| C24 argocd sync | #1010 |
| C25 tooling and meta | #565 #818 #971 #973 #997 #1013 |
| C26 k3s bump window | #413 |

## Standing rules that bit the last run

Carried forward verbatim, because each one cost something:

- Always `--context epaflix` on every `kubectl` and `helm` call (#856). A
  homelab session landed on a work cluster context once already.
- Never fetch a whole Secret and never `-o yaml`/`-o json` a Secret. One key at
  a time via `sops -d --extract`, never printed. Three separate leaks (#602,
  #712, #740) came from ignoring this, and #602 forced a token rotation.
- The ledger lies if a subagent writes it. Two entries in the prior run claimed
  the issue was open and the PR unmerged when both had landed. Verify every
  ledger row against `gh` before trusting it.
- Reading an API can burn a credential. Verifying #520 meant calling
  `GET /api/v1/settings/sonarr`, that response embeds `apiKey`, and the live
  sonarr key had to be rotated. Check what a response body contains before
  calling it.

## Success criteria

Not open count. Three things instead:

1. The agent-now lane is empty.
2. Every `needs-decision` issue is either decided and closed, or is a labelled
   ticket on a wayfinder map with a recorded position.
3. No merged PR carries an unchecked test-plan box without a tracking issue.

---

# Execution log

## Phase 0, 2026-08-20 - complete

9 open PRs to 2. Merged #1019, #987, #848, #847, #928, #977. Closed #958 as superseded by #1021. Closed #960, #803, #782. Filed #1033, #1034. Remaining open PRs are #819 (blocked on #818) and #604 (maintenance window).

Three things learned that change how later phases should work:

- **`validate` proves less than it looks like.** It runs `helm template` with default values and skips `kustomize build` for any directory containing helmCharts or ksops generators, which excludes traefik, observability, argocd and servarr. A green check on a chart bump does not prove the chart renders against this repo's own values file. Render manually before merging any chart bump.
- **Stranded PRs cause duplicated work.** #958 sat 9 days and the same defect was independently rediagnosed and fixed by #1021. Merge or close, do not leave correct work open.
- **Loki chart 7.3.0 declares `appVersion: 3.6.12` but pins `loki.image.tag: 3.6.11`.** The bump changes nothing. Expect more of these.

## Phase 1, 2026-08-20 - partial

Re-measured 10 of the 26 stale issues against live state.

**Nothing was closeable.** Not one of the ten had rotted into invalidity. The stale tail is stale because nobody worked it, not because it stopped being true. That kills the assumption behind Phase 1 in the original plan, which expected triage to shrink 92 to 55-65. It will not. Budget the effort into Phase 3 instead.

| Issue | Verdict |
|---|---|
| #715 | Still open. No reloader annotation on any consumer of `argocd-redis` or `grafana-admin-secret`. |
| #726 | Still open. `syncPolicy: {}` in git, no `selfHeal`. Relabelled `blocked-external` to `agent-gated` - the soak elapsed and nothing external blocks it. |
| #549 | Still open, and easy to mis-verify. See below. |
| #583 | Premise changed. VMs running, Swarm formed, all three services at 0/1. |
| #718 | Still open and worse. Installed moved `4.2.4-1` to `4.2.5-1`; daemons still `4.1.4`. |
| #699 | Still open. Export and dataset both present. |
| #719 | Still open. No `thin_pool_autoextend` configured at all; `vm-1031-disk-1` at 99.98%. |
| #477 | Substantially changed. AvistaZ enabled at priority 5 and not in backoff. 1337x now covered by #1023. TorrentGalaxyClone no longer exists in Prowlarr. |
| #807 | Confirmed and quantified. 546 GB, six months old, in the pool root dataset. |
| #565 | Still open. Dataset unlocked today, but the alert it asks for does not exist. |

### The escalation

**pool1 is at 88%.** #609 filed this area at 82% and #805 is written around an 85% reclaim that never happened. Six points the wrong way while the issues sat. The cheapest single reclaim is #807's 546 GB, which would return the pool to roughly 85.5%.

That reorders Phase 3. Storage was position 9 on the assumption it was a slow clock. At 88% and rising it is not.

### The verification trap worth remembering

#549 looks gated if probed at `192.168.10.101`, returning a 302 to Authentik. `searxng.epaflix.com` resolves to `192.168.10.102`, where it returns an unauthenticated `200`. The `.101` route is real and no client reaches it by name. PR #934 made this exact mistake on `qbittorrent.epaflix.com`. Always pin to the IP in `10-epaflix.conf`, never to `.101`.

### Still unverified

16 of the 26: #585, #580, #564, #526, #473, #471, #482, #705, #763, #778, #781, #802, #801, #805, #806, #717.

## Owner decisions, 2026-08-20

**The Clonezilla images are a permanent keep.** `/mnt/pool1/dapc-backup`, 546 GB, never delete. Recorded on #807. This removes the single largest reclaim candidate from the capacity plan, so #805 becomes a buy-capacity decision rather than a housekeeping sequence.

**SearXNG auth split is confirmed as-built and #549 is closed.** External through Authentik, LAN without it, which is exactly what the three IngressRoutes already do. The issue's "fully open" premise came from probing the LAN entry point. No code changed.

## Revised ordering

Phase 1 does not continue as its own phase. Nothing in the stale tail was closeable, so a dedicated triage sweep buys nothing. The remaining 16 unverified stale issues get verified as their cluster is worked, not up front.

Storage moves from position 9 to position 3. pool1 is at 88%, it moved 6 points the wrong way while unattended, and the biggest reclaim candidate is now permanently committed. That makes it a capacity decision with a real deadline rather than a slow clock.

1. Edge exposure - #547, #959, #966, #937. #547 matters more after the #549 finding: Traefik enforcing Authentik on the origin is now known to be load-bearing rather than defence in depth, because it is the only thing gating SearXNG for anyone who reaches the origin IP around Cloudflare.
2. Secrets and age-key recovery - #580, #801, #802, #963, #972, #978, #979, #982.
3. Storage capacity - #805 first as a decision, then #609, #969, #968, #699, #807. Needs a map.
4. Alerting rules - #908, #909, #910, #912, #913 as ONE PR, then #970, #980, #781.
5. Promtail and logs - #911, #824, #1006.
6. TrueNAS, GPU and ntfy - work map #922, nine tickets. Adopt the parked #902 prose; hold the scripts for #919 and #916.
7. ArgoCD sync correctness - #1010, #1033, #1034, #779, #715, #726.
8. Maintenance window - #329, #413 with #604, #718, #583 batched into one.
9. PBS and Proxmox - #564, #585, #717, #719, #763. Needs a map.
10. The arr pipeline. Needs a map to decompose.
11. Renovate and tooling - #31, #595, #957, #818, #971, #973, #997, #1013, #565.

## Open process risk

Eight concurrent agent sessions were writing this repo during Phase 0 and Phase 1, plus one automated run adding tooling and editing instruction files mid-session. A serial one-PR-at-a-time plan assumes one writer. It does not hold today, and the five orphaned working-tree files this session cleaned up are what that looks like after a few weeks. Resolve the writer question before Phase 3, or expect the same mess again.
