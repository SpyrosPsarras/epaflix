#!/usr/bin/env bash
set -euo pipefail
SCRIPT=$(cd "$(dirname "$0")" && pwd)/pegaprox-auto-update.sh
stub=$(mktemp -d); trap 'rm -rf "$stub"' EXIT
mkdir "$stub/app"
fail() { echo "FAIL: $*" >&2; exit 1; }
checks=0

curl() {
  local endpoint
  case ${!#} in
    https://api.github.com/repos/PegaProx/project-pegaprox/releases/latest) endpoint=release ;;
    https://raw.githubusercontent.com/PegaProx/project-pegaprox/main/version.json) endpoint=main ;;
    *) return 99 ;;
  esac
  echo "$endpoint" >>"$TEST_DIR/requests"
  cat "$TEST_DIR/$endpoint.json"
  [[ $CURL_FAIL != "$endpoint" ]]
}
systemctl() {
  case $* in
    'is-active --quiet pegaprox.service') return "$SERVICE_STATUS" ;;
    'show -p MainPID --value pegaprox.service')
      if [[ -e $TEST_DIR/waited ]]; then echo "$SERVICE_PID_AFTER"; else echo "$SERVICE_PID_BEFORE"; fi ;;
    *) return 99 ;;
  esac
}
sleep() { [[ $* == 30 ]] && touch "$TEST_DIR/waited"; }
export -f curl systemctl sleep
cat >"$stub/app/update.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
touch "$TEST_DIR/updated"
[[ $PEGAPROX_BRANCH == main && $PWD == "$TEST_DIR/app" ]]
read -t 2 -n 1 -r reply
[[ $reply == y ]]
cp "$TEST_DIR/after.json" version.json
exit "$UPDATE_STATUS"
SH

reset_case() {
  printf '{"version":"1.0"}\n' >"$stub/app/version.json"
  printf '{"tag_name":"v1.4.1"}\n' >"$stub/release.json"
  printf '{"version":"1.4.1"}\n' | tee "$stub/main.json" >"$stub/after.json"
  rm -f "$stub/updated" "$stub/waited"
  : >"$stub/requests"
  export UPDATE_STATUS=0 SERVICE_STATUS=0 SERVICE_PID_BEFORE=123 SERVICE_PID_AFTER=123 CURL_FAIL=
}
run() {
  status=0
  TEST_DIR="$stub" PEGAPROX_BRANCH=unreleased timeout 5 bash -c \
    'source "$1"; update_pegaprox "$2"' bash "$SCRIPT" "$stub/app" >"$stub/output" 2>&1 || status=$?
  checks=$((checks + 1))
}
success() { [[ $status == 0 ]] || fail "$1 (exit $status): $(<"$stub/output")"; }
failure() { [[ $status != 0 && $status != 124 ]] || fail "$1 (exit $status)"; }
no_update() { [[ ! -e $stub/updated && ! -e $stub/waited ]] || fail "$1"; }

reset_case
printf '{"version":"1.4.1"}\n' >"$stub/app/version.json"
run
success 'current release must succeed'
no_update 'current release must not update'
[[ $(<"$stub/requests") == release ]] || fail 'current release must not fetch main'
grep -q 'already at release' "$stub/output" || fail 'current release must log its skip'

reset_case
printf '{"version":"1.5.0"}\n' >"$stub/main.json"
run
success 'unreleased main must skip successfully'
no_update 'unreleased main must not update'
grep -q 'main is not at release' "$stub/output" || fail 'main mismatch must log its skip'

reset_case
run
success 'matching main and a stable service must succeed'
[[ -e $stub/updated && -e $stub/waited ]] || fail 'update must run and wait for service stability'
[[ $(<"$stub/app/version.json") == '{"version":"1.4.1"}' ]] || fail 'updated version must match'

reset_case
printf '{"tag_name":"1.4.1"}\n' >"$stub/release.json"
run
success 'release tag without v must work'

for endpoint in release main; do
  reset_case; export CURL_FAIL=$endpoint
  run; failure "$endpoint network failure must fail even after valid JSON"
  no_update "$endpoint network failure must not update"
done

for file in app/version release main; do
  reset_case
  case $file in
    app/version) input='not json' ;;
    release) input='{"tag_name":"vv1.4.1"}' ;;
    main) input='{"version":42}' ;;
  esac
  printf '%s\n' "$input" >"$stub/$file.json"
  run; failure "invalid $file data must fail"
  no_update "invalid $file data must not update"
done

reset_case; export UPDATE_STATUS=17
run; failure 'updater failure must not be hidden by a matching version'
reset_case
printf '{"version":"1.0"}\n' >"$stub/after.json"
run; failure 'wrong installed version must fail after update'
reset_case; export SERVICE_STATUS=3
run; failure 'inactive service must fail after update'
reset_case; export SERVICE_PID_AFTER=456
run; failure 'service PID changing during the wait must fail even when active'
grep -q 'pegaprox.service restarted after update' "$stub/output" || fail 'changed PID must log a restart'
reset_case; export SERVICE_PID_BEFORE=0
run; failure 'missing main process immediately after update must fail even when active'
grep -q 'pegaprox.service has no main process after update' "$stub/output" || fail 'missing PID must log no main process'

echo "ok: $checks offline checks cover release gating, input failures, updater failures and service stability"
