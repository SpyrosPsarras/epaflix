# Verification traps

Ways a verification check reports a **confident wrong answer** about a change
that was actually fine — or a change that was actually broken.

Read this before writing any check that renders this repo, diffs a branch, or
greps for evidence.

## The through-line

Every trap below fails toward **"nothing here"** rather than toward an error:

- A render that cannot decrypt reports missing manifests.
- A diff range that is empty reports an unchanged file.
- A truncated evidence bundle reports absent evidence.
- A regex that cannot match reports a clean repo.

None of them raise an exception. All of them produce a confident wrong answer,
and the wrong answer looks exactly like success.

**Design rule:** a check must distinguish "I looked and found nothing" from
"I could not look". If it cannot, it is not a check. Make the second case fail
loudly.

**Corollary:** "the command exited 0" is not evidence. Confirm the command was
capable of failing. A check that cannot fail has no information in it.

## The traps

### 1. `git diff --name-only` includes deletions

A loop over the changed-file list opens each path and parses it. After a
`git mv` or a delete, the source path is gone, so the loop reports
`PARSE FAIL ... No such file or directory` on a file that is correctly absent.

**Fix:** `git diff --name-only --diff-filter=d` to exclude deletions.

### 2. ksops renders decrypt outside the `sops` PATH wrapper

`kustomize build` on a ksops directory decrypts through the sops Go library, so
a PATH wrapper that supplies the age key never runs and `SOPS_AGE_KEY` is
absent. Renders fail with `Error getting data key: 0 successful groups
required`, and render assertions then report the new manifests as **ABSENT** —
indistinguishable from an unimplemented change.

**Fix:** one shared preamble used by every render step, which fails loudly when
the key is unavailable rather than letting renders fail quietly. See
`.github/instructions/sops.instructions.md`.

### 3. A heredoc's exit status is discarded

`set -uo pipefail` without `-e` means a heredoc exiting non-zero does not stop
the script. `(render assertions passed)` gets printed three lines below three
`FAIL` lines.

**Fix:** capture `$?` and branch on it. A check that always reports success is
not a check.

**Smell to grep for:** any `PYEOF`/heredoc terminator immediately followed by an
unconditional success `echo`.

### 4. An evidence bundle silently truncates

A reviewer prompt slices attached artifacts to a fixed length. A diff-first
bundle of 74,864 chars against a 60,000-char slice dropped the entire
verification block, leaving a reviewer holding a changed SOPS ciphertext line
with no decrypted before/after.

Which artifact gets dropped matters: **the diff is reconstructible from the
branch at any time; captured command output exists only in that bundle.**

**Fix:** verification output first and complete, diff truncated instead, and
announce the truncation with the command to read the rest.

### 5. Post-merge checks that diff `origin/main..HEAD`

Once merged, that range is empty, so "did the webhook FQDN change?" concludes it
did not — about a change that landed correctly.

**Fix:** detect the empty range and assert the current live or decrypted value
directly instead.

### 6. A multi-alternative pattern in a basic regex

`grep "a|b|c"` without `-E` searches for the **literal string** `a|b|c`. Zero
matches is guaranteed by construction, whatever the repo contains.

Seen 2026-08-23: `git ls-files -z | xargs -0 grep -nE -i "babysit|\.a5c|..."`
written *without* `-E` was offered as proof that no dangling references
remained. The conclusion happened to be true; the command proved nothing.

**Fix:** `grep -E` for alternation, or `grep -F` for one literal. When a search
is load-bearing evidence, also run it in a form you expect to **match** — a
pattern that finds nothing everywhere is not distinguishable from a broken
pattern.

## Mechanical, not advisory

Traps 1, 4 and 5 are boilerplate every new check re-derives. They can be removed
by construction rather than by remembering:

- a helper returning changed files excluding deletions;
- a bundler that budgets verification output ahead of diff;
- a diff-range helper that degrades to a live assertion when the range is empty.

Traps 3 and 6 suit a lint. Trap 2 needs the shared preamble in trap 2's fix.

## Origin

Traps 1-5 were recorded in #1096 from a 2026-08-22 automated run, where three of
them each cost a review attempt. The orchestrator that found them has since been
retired, but the traps are harness-agnostic, which is why they live here instead
of dying with it. Trap 6 was added when the same species of bug — failing toward
"nothing here" — appeared in the very session that retired that tooling.

Related: #1095 (planner arrays dying with a git-ignored run), #1035, #1088.
