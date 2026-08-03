# Process: issue-136-huntarr-tidyup

**Goal:** Finish the deferred post-migration cleanup from #131 (huntarr→newtarr): remove two benign
on-disk remnants and verify the inert stale env, then close issue #136.

## What it does

| Phase | Kind | Live? | Summary |
|-------|------|-------|---------|
| 1. verify-safety | agent (read-only) | no | Prove newtarr soak is good (ArgoCD `servarr` Synced/Healthy, pod 1/1, JSON config intact) AND each delete target is exactly what #136 describes: worker-61 tarball exists; worker-65 dir is the orphan with **no live PVC bound**; report HUNTARR_* env state. |
| **GATE 1** | breakpoint | — | **Destructive approval (owner).** Approve / Tarball only / Orphan dir only / Abort. |
| 2. delete-remnants | agent | **yes** | `rm -f` the worker-61 tarball + `rm -rf` the worker-65 orphan dir over SSH. Re-verify paths immediately before each rm. newtarr pod untouched. |
| 3. verify-done | agent (read-only) | no | Confirm both targets gone, newtarr still healthy, report env outcome (cleared / will-clear-on-restart). |
| **GATE 2** | breakpoint | — | **Close-out approval (owner).** |
| 4. closeout | agent | yes (gh) | Comment outcome on issue #136, `gh issue close`, open a follow-up issue only if something is deferred. |

## Targets (from issue #136)
- **worker-61** (`192.168.10.61`): `/var/lib/rancher/k3s/storage/huntarr-config-backup-20260531-pre-delete.tgz` (~32 MB safety net).
- **worker-65** (`192.168.10.65`): orphan local-path dir `pvc-47b294c2..._servarr_huntarr-config` (pre-existing OLD-cluster PVC backing dir, no live PVC).
- **newtarr pod**: stale `HUNTARR_*` env — verification only, no forced restart.

## Safety
- No repo/manifest changes → **no PR**. Close-out is a gh issue comment + close.
- Low breakpoint tolerance, `alwaysBreakOn: destructive` → one mandatory gate before any rm, gated separately for the outward close-out.
- Orphan dir is deleted only if proven to have **no live PVC/PV bound**; default to not-safe otherwise.
