#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
script="$repo_root/1-proxmox/pbs/datastore-usage-alert.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

stub="$tmp/stub-bin"
mkdir -p "$stub"
cat >"$stub/df" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [ "${STUB_DF_FAIL:-0}" = "1" ]; then
  echo "stub df: /mnt/VMs: No such file or directory" >&2
  exit 1
fi
total=1031959552   # 984 GiB in 1K blocks, the ext4 inside vm-1031-disk-1
pct="${DF_PCT:-80}"
used=$((total * pct / 100))
avail=$((total - used))
printf 'Filesystem 1024-blocks     Used Available Capacity Mounted on\n'
printf '/dev/mapper/pve--raid-vm--1031--disk--1 %d %d %d %d%% /mnt/VMs\n' \
  "$total" "$used" "$avail" "$pct"
SH
chmod +x "$stub/df"
cat >"$stub/findmnt" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [ "${STUB_FINDMNT_FAIL:-0}" = "1" ]; then
  exit 1
fi
echo /dev/mapper/pve--raid-vm--1031--disk--1
SH
chmod +x "$stub/findmnt"
cat >"$stub/curl" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
# one log line per post: multiline bodies are flattened
printf '%s\n' "${*//$'\n'/ | }" >>"$CURL_LOG"
if [ "${STUB_CURL_FAIL:-0}" = "1" ]; then
  echo "stub curl: connection refused" >&2
  exit 7
fi
SH
chmod +x "$stub/curl"

curl_log="$tmp/curl.log"
state="$tmp/state.level"
export CURL_LOG="$curl_log"

pass_count=0
fail_count=0

pass() {
  pass_count=$((pass_count + 1))
  printf 'ok - %s\n' "$1"
}

fail() {
  fail_count=$((fail_count + 1))
  printf 'not ok - %s\n' "$1" >&2
  if [ -s "$tmp/out" ]; then
    sed 's/^/  /' "$tmp/out" >&2
  fi
}

run_alert() {
  : >"$curl_log"
  local rc=0
  DF_PCT="${DF_PCT:-80}" WARN_PCT="${WARN_PCT:-85}" CRIT_PCT="${CRIT_PCT:-95}" \
    STATE_FILE="$state" CURL_LOG="$curl_log" \
    PATH="$stub:$PATH" bash "$script" >"$tmp/out" 2>&1 || rc=$?
  return "$rc"
}

curl_count() { wc -l <"$curl_log" | tr -d ' '; }
last_post()  { tail -1 "$curl_log"; }

rm -f "$state"
rc=0; run_alert || rc=$?
if [ "$rc" -eq 0 ] && [ "$(curl_count)" -eq 0 ] && [ ! -e "$state" ]; then
  pass "healthy datastore stays silent: exit 0, no ntfy post, no state file"
else
  fail "healthy datastore stays silent: exit 0, no ntfy post, no state file (rc=$rc, posts=$(curl_count))"
fi

rm -f "$state"
rc=0; DF_PCT=87 run_alert || rc=$?
post="$(last_post)"
if [ "$rc" -eq 1 ] && [ "$(curl_count)" -eq 1 ] \
  && grep -q "https://ntfy.epaflix.com/pve-backups" <<<"$post" \
  && grep -q "87%" <<<"$post" && grep -q "127 GiB free" <<<"$post" \
  && grep -q "VMs-NFS" <<<"$post" \
  && grep -q "Priority: high" <<<"$post"; then
  pass "85%+ posts a high-priority warn with percent, free space and datastore name"
else
  fail "85%+ posts a high-priority warn with percent, free space and datastore name (rc=$rc, post=$post)"
fi
if [ "$(cat "$state")" = "1" ]; then
  pass "warn level recorded in the state file"
else
  fail "warn level recorded in the state file (state=$(cat "$state" 2>/dev/null || echo missing))"
fi

rc=0; DF_PCT=87 run_alert || rc=$?
if [ "$rc" -eq 0 ] && [ "$(curl_count)" -eq 0 ]; then
  pass "same-level rerun stays silent and exits 0"
else
  fail "same-level rerun stays silent and exits 0 (rc=$rc, posts=$(curl_count))"
fi

rc=0; DF_PCT=96 run_alert || rc=$?
post="$(last_post)"
if [ "$rc" -eq 1 ] && [ "$(curl_count)" -eq 1 ] \
  && grep -q "Priority: urgent" <<<"$post"; then
  pass "95%+ escalates to an urgent critical post"
else
  fail "95%+ escalates to an urgent critical post (rc=$rc, post=$post)"
fi
if [ "$(cat "$state")" = "2" ]; then
  pass "critical level recorded in the state file"
else
  fail "critical level recorded in the state file (state=$(cat "$state" 2>/dev/null || echo missing))"
fi

rc=0; DF_PCT=96 run_alert || rc=$?
if [ "$rc" -eq 0 ] && [ "$(curl_count)" -eq 0 ]; then
  pass "sustained critical stays silent"
else
  fail "sustained critical stays silent (rc=$rc, posts=$(curl_count))"
fi

rc=0; DF_PCT=87 run_alert || rc=$?
if [ "$rc" -eq 0 ] && [ "$(curl_count)" -eq 0 ] && [ "$(cat "$state")" = "1" ]; then
  pass "critical to warn de-escalation stays silent and relaxes the state, so a re-peak re-pages"
else
  fail "critical to warn de-escalation stays silent and relaxes the state (rc=$rc, posts=$(curl_count), state=$(cat "$state" 2>/dev/null || echo missing))"
fi

rc=0; DF_PCT=96 run_alert || rc=$?
post="$(last_post)"
if [ "$rc" -eq 1 ] && [ "$(curl_count)" -eq 1 ] \
  && grep -q "Priority: urgent" <<<"$post" && [ "$(cat "$state")" = "2" ]; then
  pass "re-peak into critical after de-escalation pages again"
else
  fail "re-peak into critical after de-escalation pages again (rc=$rc, post=$post)"
fi

rc=0; DF_PCT=80 run_alert || rc=$?
post="$(last_post)"
if [ "$rc" -eq 1 ] && [ "$(curl_count)" -eq 1 ] \
  && grep -q "recovered" <<<"$post" && [ ! -e "$state" ]; then
  pass "drop below 85% posts a recovery and clears the state file"
else
  fail "drop below 85% posts a recovery and clears the state file (rc=$rc, post=$post)"
fi

rc=0; DF_PCT=80 run_alert || rc=$?
if [ "$rc" -eq 0 ] && [ "$(curl_count)" -eq 0 ]; then
  pass "healthy store after recovery stays silent"
else
  fail "healthy store after recovery stays silent (rc=$rc, posts=$(curl_count))"
fi

: >"$curl_log"
rc=0; DF_PCT=90 STUB_DF_FAIL=1 STATE_FILE="$state" PATH="$stub:$PATH" \
  bash "$script" >"$tmp/out" 2>&1 || rc=$?
if [ "$rc" -eq 1 ] && [ "$(curl_count)" -eq 1 ] \
  && grep -q "Priority: urgent" <<<"$(last_post)"; then
  pass "unmeasurable datastore posts an urgent alert"
else
  fail "unmeasurable datastore posts an urgent alert (rc=$rc)"
fi

: >"$curl_log"
rc=0; STUB_DF_FAIL=1 STATE_FILE="$state" PATH="$stub:$PATH" \
  bash "$script" >"$tmp/out" 2>&1 || rc=$?
if [ "$rc" -eq 1 ] && [ "$(curl_count)" -eq 1 ]; then
  pass "unmeasurable datastore notifies on every run, no state dedup"
else
  fail "unmeasurable datastore notifies on every run, no state dedup (rc=$rc)"
fi

: >"$curl_log"
rc=0; DF_PCT=35 STUB_FINDMNT_FAIL=1 STATE_FILE="$state" PATH="$stub:$PATH" \
  bash "$script" >"$tmp/out" 2>&1 || rc=$?
post="$(last_post)"
if [ "$rc" -eq 1 ] && [ "$(curl_count)" -eq 1 ] \
  && grep -q "Priority: urgent" <<<"$post" \
  && grep -q "not a mountpoint" <<<"$post"; then
  pass "unmounted datastore (df reads the root fs) posts an urgent alert naming the mountpoint"
else
  fail "unmounted datastore (df reads the root fs) posts an urgent alert (rc=$rc, post=$post)"
fi

: >"$curl_log"
rc=0; DF_PCT=35 STUB_FINDMNT_FAIL=1 STATE_FILE="$state" PATH="$stub:$PATH" \
  bash "$script" >"$tmp/out" 2>&1 || rc=$?
if [ "$rc" -eq 1 ] && [ "$(curl_count)" -eq 1 ]; then
  pass "unmounted datastore notifies on every run, no state dedup"
else
  fail "unmounted datastore notifies on every run, no state dedup (rc=$rc)"
fi

rm -f "$state"
rc=0; DF_PCT=87 STUB_CURL_FAIL=1 run_alert || rc=$?
if [ "$rc" -eq 2 ] && [ ! -e "$state" ]; then
  pass "failed ntfy delivery exits 2 and leaves no state, so the next run retries"
else
  fail "failed ntfy delivery exits 2 and leaves no state, so the next run retries (rc=$rc)"
fi

rm -f "$state"
rc=0; DF_PCT=25 WARN_PCT=10 CRIT_PCT=20 run_alert || rc=$?
post="$(last_post)"
if [ "$rc" -eq 1 ] && grep -q "Priority: urgent" <<<"$post"; then
  pass "thresholds are overridable through the environment"
else
  fail "thresholds are overridable through the environment (rc=$rc, post=$post)"
fi

if [ -x "$script" ]; then
  pass "datastore-usage-alert.sh is executable for cron"
else
  fail "datastore-usage-alert.sh is executable for cron"
fi

printf '%s\n' "1..$((pass_count + fail_count))"
if [ "$fail_count" -ne 0 ]; then
  printf '%s fixture test(s) failed\n' "$fail_count" >&2
  exit 1
fi
printf 'All %s fixture tests passed.\n' "$pass_count"
