# Process: system-upgrade Plans onboard (Epaflix #74)

Onboard the parked K3s `system-upgrade` Plans and perform the first
maintainer-supervised rolling cluster upgrade.

## Decisions (owner-confirmed, revised 2026-05-31)
- **Layout A** — Plans in new `2-k3s/maintenance/system-upgrade/plans/` (own kustomization).
- **ALL Apps automated — NO manual sync.** New App `system-upgrade-plans` =
  `syncPolicy.automated{selfHeal:true,prune:false}` + ServerSideApply; the existing
  `system-upgrade-controller` App is **flipped manual→automated in the same PR**.
- **Channel `stable`**.
- **Merge IS the deploy.** Because the Plans App is automated/selfHeal, merging makes
  app-of-apps create + auto-sync it → controller applies the Plans → rolling upgrade fires.
  ONE hard deploy gate at MERGE (after an etcd snapshot). No separate manual-sync step.
- **homarr is disposable** — its 0-disruption PDB may be evicted/killed. `postgres-cluster-primary`
  0-disruption PDB is a noted, non-blocking risk (CNPG failover + force-drain skip60).
- **Accepted consequence:** future stable-channel bumps will auto-roll the fleet UNSUPERVISED;
  closeout opens a follow-up to document the SOP / add an alert / consider channel pinning.

## Phases
1. **analyze + design** (agent, read-only) → `GATE 1` approve revised design (architecture-change).
2. **author manifests** (agent) — plans/ kustomization + automated App + controller-App flip +
   apps/ wiring + delete parked file; local `kustomize build` + SOPS guard; one commit →
   `GATE 2a` review authored change.
3. **pre-deploy snapshot** (agent) — take fresh etcd snapshot + baseline versions/readiness.
4. `GATE 2` (destructive-git + deploy) approve push+PR+**MERGE = live rolling upgrade**.
5. **publish-merge** (agent) — rebase onto origin/main, PR, wait for `validate`, `gh pr merge --merge`.
6. **rollout-watch** (agent) — watch app-of-apps create+selfHeal-sync the App, then masters
   (1-at-a-time) → workers (2-at-a-time) through cordon→drain→upgrade→uncordon. Recovery gate
   on stall (lever: patch Plan `concurrency:0` to pause; etcd snapshot restore for a bricked master).
7. **post-upgrade verify** (agent, read-only) — all 7 nodes on target + Ready, workloads healthy.
8. **closeout** (agent) — close #74, tick PR test plan (edit body), open future-auto-upgrade SOP follow-up.

## Safety
- etcd snapshot taken BEFORE merge; K3s upgrades are one-way.
- Masters `concurrency:1` preserve etcd quorum; workers `concurrency:2` keep ≥2 up.
- Hold/abort at GATE 2 leaves the branch local + unpushed; #74 stays open.

## Refs
- Issue #74 (this), #44 (controller install, closed), #18.
- Template: `.a5c/processes/traefik-prune-flip.js`.
