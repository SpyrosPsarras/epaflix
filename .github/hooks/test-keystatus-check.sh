#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
script="$repo_root/0-truenas/scripts/keystatus-check.sh"
tmp="$(mktemp -d)"
output="$(mktemp)"
trap 'rm -rf "$tmp"; rm -f "$output"' EXIT

stub="$tmp/stub-bin"
mkdir -p "$stub"
cat >"$stub/zfs" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$@" >>"$ZFS_LOG"
if [ "${STUB_ZFS_FAIL:-0}" = "1" ]; then
  echo "stub zfs: cannot open dataset" >&2
  exit 1
fi
printf '%s\n' "${STUB_KEYSTATUS:-available}"
SH
cat >"$stub/curl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
printf -- '---\n' >>"$NTFY_LOG"
printf '%s\n' "$@" >>"$NTFY_LOG"
SH
chmod +x "$stub/zfs" "$stub/curl"

state_file="$tmp/keystatus-check.failing"
ntfy_log="$tmp/ntfy.log"
zfs_log="$tmp/zfs.log"

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

records() {
  grep -c '^---$' "$ntfy_log" || true
}

run_check() {
  : >"$ntfy_log"
  : >"$zfs_log"
  NTFY_LOG="$ntfy_log" ZFS_LOG="$zfs_log" STUB_KEYSTATUS="$1" STUB_ZFS_FAIL="${2:-0}" \
    PATH="$stub:$PATH" \
    STATE_FILE="$state_file" \
    bash "$script" >"$output" 2>&1
}

rm -f "$state_file"
rc=0; run_check unavailable || rc=$?
if [ "$rc" -eq 1 ] \
  && [ "$(records)" -eq 1 ] \
  && grep -q 'Title: TrueNAS apps/encrypted-backups is LOCKED' "$ntfy_log" \
  && grep -q 'Priority: high' "$ntfy_log" \
  && grep -q 'zfs load-key' "$ntfy_log" \
  && [ -f "$state_file" ]; then
  pass "locked dataset with no state file notifies once at high priority and leaves the state file"
else
  fail "locked dataset with no state file notifies once at high priority and leaves the state file (rc=$rc, records=$(records))"
fi

rc=0; run_check unavailable || rc=$?
if [ "$rc" -eq 1 ] && [ "$(records)" -eq 0 ] && [ -f "$state_file" ]; then
  pass "locked dataset with the state file already present stays silent"
else
  fail "locked dataset with the state file already present stays silent (rc=$rc, records=$(records))"
fi

rc=0; run_check available || rc=$?
if [ "$rc" -eq 0 ] \
  && [ "$(records)" -eq 1 ] \
  && grep -q 'Title: TrueNAS apps/encrypted-backups unlocked' "$ntfy_log" \
  && [ ! -e "$state_file" ]; then
  pass "unlocked dataset clears the state file and sends the recovery notification"
else
  fail "unlocked dataset clears the state file and sends the recovery notification (rc=$rc, records=$(records))"
fi

rc=0; run_check available || rc=$?
if [ "$rc" -eq 0 ] && [ "$(records)" -eq 0 ]; then
  pass "unlocked dataset with no state file sends nothing"
else
  fail "unlocked dataset with no state file sends nothing (rc=$rc, records=$(records))"
fi

rm -f "$state_file"
rc=0; run_check "" 1 || rc=$?
if [ "$rc" -eq 1 ] \
  && [ "$(records)" -eq 1 ] \
  && grep -q 'cannot read keystatus' "$ntfy_log" \
  && [ -f "$state_file" ]; then
  pass "zfs failure notifies that the check itself is broken and leaves the state file"
else
  fail "zfs failure notifies that the check itself is broken and leaves the state file (rc=$rc, records=$(records))"
fi

rm -f "$state_file"
rc=0; run_check unavailable || rc=$?
if grep -q '^https://ntfy.epaflix.com/truenas-alerts$' "$ntfy_log"; then
  pass "notifications post to the truenas-alerts ntfy topic"
else
  fail "notifications post to the truenas-alerts ntfy topic"
fi

if [ "$(xargs <"$zfs_log")" = "get -H -o value keystatus apps/encrypted-backups" ]; then
  pass "script queries keystatus of apps/encrypted-backups via zfs get"
else
  fail "script queries keystatus of apps/encrypted-backups via zfs get (got: $(xargs <"$zfs_log"))"
fi

if [ -x "$script" ]; then
  pass "keystatus-check.sh is executable for cron"
else
  fail "keystatus-check.sh is executable for cron"
fi

printf '%s\n' "1..$((pass_count + fail_count))"
if [ "$fail_count" -ne 0 ]; then
  printf '%s fixture test(s) failed\n' "$fail_count" >&2
  exit 1
fi
printf 'All %s fixture tests passed.\n' "$pass_count"
