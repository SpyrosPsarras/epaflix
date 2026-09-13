#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
script="$repo_root/0-truenas/scripts/pool-capacity-forecast.py"
tmp="$(mktemp -d)"
output="$(mktemp)"
trap 'rm -rf "$tmp"; rm -f "$output"' EXIT

stub="$tmp/stub-bin"
mkdir -p "$stub"
cat >"$stub/zpool" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$ZPOOL_LOG"
if [ "${STUB_ZPOOL_FAIL:-0}" = "1" ]; then
  echo "stub zpool: cannot open pool" >&2
  exit 1
fi
GiB=$((1024 * 1024 * 1024))
size=$((20000 * GiB))
alloc=$((${STUB_ALLOC_GIB:-17980} * GiB))
printf 'pool1\t%s\t%s\t%s\t12\n' "$size" "$alloc" "$((size - alloc))"
SH
chmod +x "$stub/zpool"

history="$tmp/history.jsonl"
zpool_log="$tmp/zpool.log"

pass_count=0
fail_count=0

pass() {
  pass_count=$((pass_count + 1))
  printf 'ok - %s\n' "$1"
}

fail() {
  fail_count=$((fail_count + 1))
  printf 'not ok - %s\n' "$1" >&2
  if [ -s "$output" ]; then
    sed 's/^/  /' "$output" >&2
  fi
}

run_check() {
  local now="$1"
  shift
  : >"$zpool_log"
  ZPOOL_LOG="$zpool_log" POOLCAP_NOW="$now" PATH="$stub:$PATH" \
    python3 "$script" --pool pool1 --history "$history" "$@" >"$output" 2>&1
}

# Fixture arithmetic (all GiB, 10 GiB/day growth, 20000 GiB pool):
#   2026-06-25 alloc 17900, 2026-07-02 alloc 17970, now 2026-07-03 alloc 17980.
#   90% at 2026-07-05 (2 days out), 95% at 2026-10-13 (102 days out), full at 2027-01-21.
seed_history() {
  GiB=$((1024 * 1024 * 1024))
  cat >"$history" <<EOF
{"ts": "2026-06-25T04:00:00", "pool": "pool1", "size": $((20000 * GiB)), "alloc": $((17900 * GiB)), "free": $((2100 * GiB)), "frag": 11}
{"ts": "2026-07-02T04:00:00", "pool": "pool1", "size": $((20000 * GiB)), "alloc": $((17970 * GiB)), "free": $((2030 * GiB)), "frag": 12}
EOF
}

seed_history_declining() {
  GiB=$((1024 * 1024 * 1024))
  cat >"$history" <<EOF
{"ts": "2026-06-25T04:00:00", "pool": "pool1", "size": $((20000 * GiB)), "alloc": $((17900 * GiB)), "free": $((2100 * GiB)), "frag": 11}
{"ts": "2026-07-02T04:00:00", "pool": "pool1", "size": $((20000 * GiB)), "alloc": $((17830 * GiB)), "free": $((2170 * GiB)), "frag": 11}
EOF
}

seed_history
rc=0; run_check "2026-07-03T04:00:00" || rc=$?
if [ "$rc" -eq 1 ] \
  && grep -q 'pool1: 17980.0 GiB used of 20000.0 GiB (89.9%), frag 12%' "$output" \
  && grep -q 'weekly net growth: 70.0 GiB' "$output" \
  && grep -q 'forecast 90% used: 2026-07-05 (2 days)' "$output" \
  && grep -q 'forecast 95% used: 2026-10-13 (102 days)' "$output" \
  && grep -q 'pool full 2027-01-21' "$output" \
  && grep -q 'reclaim 1980.0 GiB now, then delete 70.0 GiB/week' "$output"; then
  pass "growing pool forecasts threshold dates and exits 1 inside the 180d horizon"
else
  fail "growing pool forecasts threshold dates and exits 1 inside the 180d horizon (rc=$rc)"
fi

seed_history
rc=0; run_check "2026-07-03T04:00:00" --horizon-days 90 || rc=$?
if [ "$rc" -eq 0 ] && grep -q 'forecast 95% used: 2026-10-13 (102 days)' "$output"; then
  pass "same forecast exits 0 when the horizon is tighter than the runway"
else
  fail "same forecast exits 0 when the horizon is tighter than the runway (rc=$rc)"
fi

seed_history_declining
rc=0; STUB_ALLOC_GIB=17820 run_check "2026-07-03T04:00:00" || rc=$?
if [ "$rc" -eq 0 ] \
  && grep -q 'weekly net growth: freeing 70.0 GiB' "$output" \
  && ! grep -Eq 'forecast (90|95)% used: [0-9]' "$output" \
  && ! grep -Eq 'pool full [0-9]' "$output"; then
  pass "shrinking pool reports the free rate and never forecasts a full date"
else
  fail "shrinking pool reports the free rate and never forecasts a full date (rc=$rc)"
fi

: >"$history"
rc=0; run_check "2026-07-03T04:00:00" || rc=$?
if [ "$rc" -eq 0 ] \
  && grep -q 'history too short' "$output" \
  && [ "$(wc -l <"$history")" -eq 1 ]; then
  pass "first run records the measurement, defers the forecast, exits 0"
else
  fail "first run records the measurement, defers the forecast, exits 0 (rc=$rc)"
fi

rc=0; run_check "2026-07-03T04:00:00" || rc=$?
if [ "$rc" -eq 0 ] && [ "$(wc -l <"$history")" -eq 1 ]; then
  pass "same-day rerun replaces the row instead of doubling it"
else
  fail "same-day rerun replaces the row instead of doubling it (rc=$rc)"
fi

seed_history
before="$(wc -l <"$history")"
rc=0; run_check "2026-07-03T04:00:00" || rc=$?
if [ "$(wc -l <"$history")" -eq "$((before + 1))" ] \
  && [ "$(tail -1 "$history")" = '{"ts": "2026-07-03T04:00:00", "pool": "pool1", "size": 21474836480000, "alloc": 19305877995520, "free": 2168958484480, "frag": 12}' ]; then
  pass "each run appends one row with the live zpool numbers"
else
  fail "each run appends one row with the live zpool numbers (rows: $(wc -l <"$history"), want $((before + 1)))"
fi

seed_history
rows_before="$(wc -l <"$history")"
rc=0; STUB_ZPOOL_FAIL=1 run_check "2026-07-03T04:00:00" || rc=$?
if [ "$rc" -eq 2 ] && [ "$(wc -l <"$history")" -eq "$rows_before" ]; then
  pass "zpool failure exits 2 without touching the history"
else
  fail "zpool failure exits 2 without touching the history (rc=$rc)"
fi

GiB=$((1024 * 1024 * 1024))
cat >"$history" <<EOF
{"ts": "2026-06-25T04:00:00", "pool": "apps", "size": $((150 * GiB)), "alloc": $((100 * GiB)), "free": $((50 * GiB)), "frag": 5}
{"ts": "2026-06-25T04:00:00", "pool": "pool1", "size": $((20000 * GiB)), "alloc": $((17900 * GiB)), "free": $((2100 * GiB)), "frag": 11}
{"ts": "2026-07-02T04:00:00", "pool": "pool1", "size": $((20000 * GiB)), "alloc": $((17970 * GiB)), "free": $((2030 * GiB)), "frag": 12}
EOF
rc=0; run_check "2026-07-03T04:00:00" || rc=$?
if [ "$rc" -eq 1 ] \
  && grep -q '"pool": "apps"' "$history" \
  && grep -q '"ts": "2026-07-03T04:00:00", "pool": "pool1"' "$history"; then
  pass "other pools' history rows survive the same-day rewrite"
else
  fail "other pools' history rows survive the same-day rewrite (rc=$rc)"
fi

seed_history
rc=0; run_check "2026-07-03T04:00:00" || rc=$?
if [ "$(xargs <"$zpool_log")" = "list -Hp -o name,size,alloc,free,frag pool1" ]; then
  pass "script reads the pool via zpool list -Hp"
else
  fail "script reads the pool via zpool list -Hp (got: $(xargs <"$zpool_log"))"
fi

if [ -x "$script" ]; then
  pass "pool-capacity-forecast.py is executable for cron"
else
  fail "pool-capacity-forecast.py is executable for cron"
fi

printf '%s\n' "1..$((pass_count + fail_count))"
if [ "$fail_count" -ne 0 ]; then
  printf '%s fixture test(s) failed\n' "$fail_count" >&2
  exit 1
fi
printf 'All %s fixture tests passed.\n' "$pass_count"
