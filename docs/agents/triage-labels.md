# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

## Orthogonal labels

These are **not** state roles. An issue carries one *alongside* its state role,
not instead of it.

| Label | Meaning |
| ----------------- | ------------------------------------------------------- |
| `blocked-time` | Evaluated and correct, but cannot start until a date or event |
| `blocked-external`| Waiting on a third party outside this repo's control |

`blocked-time` exists because the five canonical states cannot express "not yet
due". A soak window or an "after the next upgrade" gate is not `needs-triage`
(evaluation is done), not `needs-info` (nobody is waiting on a reporter), and
must not be `ready-for-agent` — an AFK agent would either stall or close it on
time elapsed, which `CLAUDE.md` forbids: a soak closes on the live value pasted,
never on the calendar.

**A `blocked-time` issue must state its gate** in a comment: the earliest date,
or the event that unblocks it, plus what to measure when it opens. A gate nobody
wrote down is indistinguishable from a stalled issue.

## Retired labels

`agent-gated`, `agent-now`, `needs-hands` and `needs-decision` came from a
previous agent setup and are **no longer authoritative**.

Measured 2026-08-23, of 87 open issues: `agent-gated` 26, `needs-hands` 8,
`agent-now` 0, `needs-decision` 0 — 34 open issues carry at least one. Re-measure
rather than trusting this snapshot:

```bash
gh issue list -R SpyrosPsarras/epaflix --state open --limit 500 --json number,labels \
 | jq '[.[] | select(.labels | map(.name) | any(. == "agent-gated" or . == "agent-now" or . == "needs-hands" or . == "needs-decision"))] | length'
```

Treat any issue carrying one as `needs-triage` regardless of what it says, and
remove the retired label when you re-triage that issue. Never apply one to
something new.

`blocked-external` and the `wayfinder:*` labels are **not** retired —
`blocked-external` is orthogonal to triage state, and `/wayfinder` owns its own
set.
