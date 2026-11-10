#!/usr/bin/env bash
set -euo pipefail

runner="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/run-parallel.py"
fail() { echo "FAIL: $*" >&2; exit 1; }

out=$(printf '# comment\n\necho one\necho two\n' | python3 "$runner") || fail "passing commands exited non-zero"
grep -q '^2/2 passed$' <<<"$out" || fail "comments or blank lines were counted: $out"
grep -q '^one$' <<<"$out" && grep -q '^two$' <<<"$out" || fail "output was lost: $out"

if out=$(printf 'echo kept\necho broken; exit 3\n' | python3 "$runner"); then fail "a failing command passed"; fi
grep -q '^::error::exit 3 after .*: echo broken; exit 3$' <<<"$out" || fail "failure not reported: $out"
grep -q '^kept$' <<<"$out" || fail "other output lost on failure: $out"

if printf '# only a comment\n' | python3 "$runner" >/dev/null 2>&1; then fail "empty input passed"; fi

start=$SECONDS
if out=$(printf 'sleep 60 & echo started\n' | RUN_PARALLEL_TIMEOUT=2 python3 "$runner"); then fail "a hung command passed"; fi
(( SECONDS - start < 20 )) || fail "a child holding the output pipe was not killed"
grep -q '^::error::exit timeout ' <<<"$out" && grep -q '^killed after 2s$' <<<"$out" || fail "timeout not reported: $out"

echo "PASS run-parallel: output kept, empty input, failures and timeouts fail"
