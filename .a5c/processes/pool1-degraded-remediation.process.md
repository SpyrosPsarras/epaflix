# Process: pool1-degraded-remediation

**Goal:** Answer issue #124's owner question — *"can you fix it?"* — for the DEGRADED non-redundant
TrueNAS `pool1` (2-HDD stripe, 332 WRITE errors, now hosting the SOPS master age-key backup), then
apply only the safe software remediation and report.

## Phases

1. **Investigate (read-only).** SSH TrueNAS. Capture `zpool status -v/-x`, `zpool events`, scrub
   history; map the flagged vdev GUID → physical HDD (device/model/serial); pull `smartctl -a` for
   both stripe members; scan `dmesg`. Classify root cause: transient cabling/controller (UDMA_CRC,
   zero reallocated) vs genuine disk degradation (reallocated/pending/uncorrectable, SMART FAILED).

2. **Assess fixability (read-only).** Adversarial review. Split into software-fixable-now
   (`zpool clear` + scrub + verify) vs hardware-required (replace HDD, add a mirror). Produce honest
   `fixable` verdict, exact ordered remediation commands, and follow-up recommendations.

3. **GATE 1 (owner, mandatory).** Live write to the pool holding the master-key backup. Options:
   Approve remediation / Report findings only / Abort.

4. **Remediate (live, approved scope only).** Run exactly the approved `zpool clear` (+ optional
   on-demand scrub). NO `zpool replace/offline/detach` or any disk/partition op. Report new state +
   whether errors return immediately (fast return ⇒ dying disk).

5. **Verify (read-only).** Confirm ONLINE vs still DEGRADED, whether errors climbed back, scrub
   progress. Sets fixedNow / durable / needsSoak.

6. **GATE 2 (owner).** Approve outward reporting. Options: Approve report + follow-ups / Comment only
   / Skip.

7. **Report.** Comment findings + outcome on #124; open hardware follow-up issues (replace failing
   HDD; add mirror so the master-key backup is on a redundant pool) using the repo
   Finding/Current-state/Desired-outcome/Notes shape, cross-linked to #124 and #57. No dupes; no
   secrets in comments.

## Guardrails

- **Read-only by default;** only `zpool clear` + `scrub` are live, gated by GATE 1.
- **Disk replacement / adding redundancy is hardware** — out of scope for autonomous execution,
  captured as follow-ups, never auto-run.
- **No lockout risk:** independent age-key copies (workstation + in-cluster KSOPS) exist.
- Breakpoint tolerance LOW → two owner gates (live storage write; outward git/issue mutation).
- Never print credentials/age keys in issue comments.
