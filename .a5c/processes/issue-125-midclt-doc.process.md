# Process: issue-125-midclt-doc

**Goal:** Close issue #125 — document the TrueNAS 25.10.0.1 `midclt -j` job-method post-completion crash (`TypeError: unhashable type: 'dict'`, non-zero exit despite server-side success) and its workaround in `.github/instructions/truenas.instructions.md`, landed via branch+PR per the Epaflix merge policy.

**Methodology:** Evolutionary / docs-as-code — small reversible doc-only increment with an independent verification gate.

## Phases

1. **prep-branch** (shell) — branch `docs-midclt-job-poll-caveat-125` off `origin/main`.
2. **author-doc** (agent) — add a runbook subsection covering: the crash + non-zero exit on success; orphaned-dataset / lost-passphrase consequence + print-before-call mitigation; no `@file` payload (inline positional JSON only); verify real outcome via `zfs get`/`zfs list` not the exit code. Placeholders only.
3. **verify-doc** (agent) — independent check that all four facts are present, markdown is sound, and no real secret leaked. Loops back to author (max 3 attempts) on failure.
4. **owner approval** (breakpoint) — single gate before touching the remote, given low breakpoint tolerance.
5. **open-and-merge-pr** (shell) — stage **only the doc file**, commit (`Closes #125`), push, open PR, rebase onto `origin/main` + `--force-with-lease`, wait for the `validate` check, `gh pr merge --merge --delete-branch`.

## Guardrails honored
- Branch+PR + rebase + `--merge` (semi-linear merge policy).
- `git add` scoped to the doc file only — never stages `.a5c/` scaffolding.
- No secrets: placeholders only; verify step audits for leaks; pre-commit SOPS hook unaffected (no `kind: Secret`).
- Issue itself is the follow-up tracker; upstream-bug tracking left optional per the issue text.
