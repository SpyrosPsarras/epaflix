# Verification traps

Ways a verification check reports a **confident wrong answer** about a change
that was actually fine — or a change that was actually broken.

Read this before writing any check that renders this repo, diffs a branch, or
greps for evidence.

## The through-line

Trap 1 fails toward a loud false alarm. Every other trap below fails toward
**"nothing here"** rather than toward an error:

- A render that cannot decrypt reports missing manifests.
- A diff range that is empty reports an unchanged file.
- A truncated evidence bundle reports absent evidence.
- A regex that cannot match reports a clean repo.
- A filter the server dropped reports somebody else's history as yours.
- A guard that cannot reject anything reports a clean run.

None of those raise an exception. All of them produce a confident wrong answer,
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

### 7. A parameter the server ignores, read as an answer

A filter or sort passed to an API is a request, not a guarantee. When the
server drops it, the response is still a well-formed 200, and the caller reads
somebody else's data as an answer about the thing it asked for.

Seen 2026-08-24, looking up whether an *arr had already failed a download:
`GET /api/v3/history?downloadId=<hash>&pageSize=1&sortKey=date&sortDirection=descending`.
An empty `downloadId` returns `totalRecords 8462`, because Sonarr reads an empty
filter as no filter, so a torrent with no hash was about to be explained by an
arbitrary event belonging to some other download. The filter is also
case-sensitive, and it fails the other way: the same hash lowercased returns 0
records, which is indistinguishable from "this download has no history". Whether
the sort was honoured cannot be told from the response at all with `pageSize=1`,
and that one fails open, because a stale `downloadFailed` under a newer
`grabbed` then reads as a failed download.

**Fix:** make the server prove it did what was asked. Request one more row than
you need so ordering is checkable, and require every returned record to carry
the key you filtered on. That discipline was already in the sibling read in the
same file: `arr_download_ids()` compares `totalRecords` against the rows fetched
and refuses to run against a partial queue.

### 8. A guard nobody has watched fail

Seen 2026-08-24: the repair for trap 7 shipped with a live run in which both new
guards passed, which proved nothing about either. One of them was inert. It
compared `str(r.get("date", ""))` on both sides, so a response carrying no
`date` compared `"" < ""`, passed, and was trusted as newest-first. Unverifiable
resolved to verified, one layer below the defect it was fixing.

**Fix:** seed each guard with the input it exists to reject, and record the
abort. For that history lookup it meant asserting `SystemExit` on seven seeded
responses: a foreign key in record 0, a foreign key in record 1, a missing key,
reversed ordering, an exact date tie, a missing date and an empty date. The
fan-out cap got the same treatment, with a live run at a cap of 1 so its own
error appears in the log. Fixtures need it too. An assertion reading
`("a" * 40).upper().lower()` against the same string claimed to test case
handling and could not fail.

## Mechanical, not advisory

Traps 1, 4 and 5 are boilerplate every new check re-derives. They can be removed
by construction rather than by remembering:

- a helper returning changed files excluding deletions;
- a bundler that budgets verification output ahead of diff;
- a diff-range helper that degrades to a live assertion when the range is empty.

Traps 3 and 6 suit a lint. Trap 2 needs the shared preamble in trap 2's fix.

Traps 7 and 8 are review-shaped rather than lint-shaped. The cheap mechanical
part of trap 7 is a shared response-validation helper for *arr reads; the rest
is a question to ask in review, "has anyone seen this check fail?", which is
what caught both of them.

## Origin

Traps 1-5 were recorded in #1096 from a 2026-08-22 automated run, where three of
them each cost a review attempt. The orchestrator that found them has since been
retired, but the traps are harness-agnostic, which is why they live here instead
of dying with it. Trap 6 was added when the same species of bug — failing toward
"nothing here" — appeared in the very session that retired that tooling.

Related: #1095 (planner arrays dying with a git-ignored run), #1035, #1088.

Traps 7 and 8 were added on 2026-08-24 from PR #1134, where the review gate
blocked the same change three times. Two public retractions from the same week
sit behind them: Sonarr#8899, withdrawn in #705, and the first diagnosis comment
on #834, which sorted the two populations by memory versus disk and was
corrected by a later comment the same day.
