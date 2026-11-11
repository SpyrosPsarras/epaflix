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
url="" data=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -d|--data|--data-binary) data="$2"; shift ;;
    http*) url="$1" ;;
  esac
  shift
done
printf '%s\t%s\n' "$url" "$(jq -c . <<<"$data" 2>/dev/null || echo INVALID_JSON)" >>"$POST_LOG"
if [ "${STUB_CURL_FAIL:-0}" = "1" ]; then
  echo "stub curl: connection refused" >&2
  exit 7
fi
SH
chmod +x "$stub/zfs" "$stub/curl"

state_file="$tmp/keystatus-check.failing"
post_log="$tmp/post.log"
env_file="$tmp/discord-webhook.env"
printf '%s\n' 'DISCORD_WEBHOOK_URL=https://discord.test/api/webhooks/1/abc' 'DISCORD_ROLE_ID=42' >"$env_file"
export DISCORD_ENV_FILE="$env_file"
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
  wc -l <"$post_log" | tr -d ' '
}

post_json() {
  tail -1 "$post_log" | cut -f2-
}

post_is() {
  post_json | jq -e "$@" >/dev/null 2>&1
}

run_check() {
  : >"$post_log"
  : >"$zfs_log"
  POST_LOG="$post_log" ZFS_LOG="$zfs_log" STUB_KEYSTATUS="$1" STUB_ZFS_FAIL="${2:-0}" \
    PATH="$stub:$PATH" \
    STATE_FILE="$state_file" \
    bash "$script" >"$output" 2>&1
}

rm -f "$state_file"
rc=0; run_check unavailable || rc=$?
if [ "$rc" -eq 1 ] \
  && [ "$(records)" -eq 1 ] \
  && post_is '.content | startswith("<@&42> **TrueNAS mpara-tank-lan/encrypted-backups is LOCKED**\n")' \
  && post_is '.allowed_mentions.roles == ["42"]' \
  && post_is '.username == "TrueNAS"' \
  && post_is '.content | contains("zfs load-key")' \
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
  && post_is '.content | startswith("**TrueNAS mpara-tank-lan/encrypted-backups unlocked**\n")' \
  && post_is '.content | contains("<@&") | not' \
  && post_is '.username == "TrueNAS"' \
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
  && post_is '.content | startswith("<@&42> ") and contains("cannot read keystatus")' \
  && [ -f "$state_file" ]; then
  pass "zfs failure notifies that the check itself is broken and leaves the state file"
else
  fail "zfs failure notifies that the check itself is broken and leaves the state file (rc=$rc, records=$(records))"
fi

rm -f "$state_file"
rc=0; run_check unavailable || rc=$?
if [ "$(tail -1 "$post_log" | cut -f1)" = "https://discord.test/api/webhooks/1/abc" ]; then
  pass "notifications post to the Discord webhook from the env file"
else
  fail "notifications post to the Discord webhook from the env file"
fi

rm -f "$state_file"
rc=0; run_check $'bad"quote\\back\nslash' || rc=$?
if [ "$(records)" -eq 1 ] && post_is '.content | contains("bad\"quote\\back\nslash")'; then
  pass "quotes, backslashes and newlines in the message still make valid JSON"
else
  fail "quotes, backslashes and newlines in the message still make valid JSON (post=$(post_json))"
fi

rm -f "$state_file"
rc=0; DISCORD_ENV_FILE="$tmp/missing.env" run_check unavailable || rc=$?
if [ "$rc" -ne 0 ] && [ "$(records)" -eq 0 ] && [ ! -e "$state_file" ]; then
  pass "missing env file fails without posting or writing state"
else
  fail "missing env file fails without posting or writing state (rc=$rc, records=$(records))"
fi

printf 'DISCORD_WEBHOOK_URL=\nDISCORD_ROLE_ID=42\n' >"$tmp/empty.env"
rm -f "$state_file"
rc=0; DISCORD_ENV_FILE="$tmp/empty.env" run_check unavailable || rc=$?
if [ "$rc" -ne 0 ] && [ "$(records)" -eq 0 ] && [ ! -e "$state_file" ]; then
  pass "empty webhook URL fails without posting or writing state"
else
  fail "empty webhook URL fails without posting or writing state (rc=$rc, records=$(records))"
fi

rm -f "$state_file"
rc=0; STUB_CURL_FAIL=1 run_check unavailable || rc=$?
if [ "$rc" -ne 0 ] && [ ! -e "$state_file" ]; then
  pass "failed delivery leaves no state, so the next run retries"
else
  fail "failed delivery leaves no state, so the next run retries (rc=$rc)"
fi

if [ "$(xargs <"$zfs_log")" = "get -H -o value keystatus mpara-tank-lan/encrypted-backups" ]; then
  pass "script queries keystatus of mpara-tank-lan/encrypted-backups via zfs get"
else
  fail "script queries keystatus of mpara-tank-lan/encrypted-backups via zfs get (got: $(xargs <"$zfs_log"))"
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
