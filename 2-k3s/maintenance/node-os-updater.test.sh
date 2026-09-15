#!/usr/bin/env bash
# Exercises safe_reboot from node-os-updater-cronjob.yaml with stubbed
# kubectl/ssh. No cluster state is touched.
set -euo pipefail
CRONJOB=$(cd "$(dirname "$0")" && pwd)/node-os-updater-cronjob.yaml
stub=$(mktemp -d); trap 'rm -rf "$stub"' EXIT
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

cat >"$stub/kubectl" <<'STUB'
#!/usr/bin/env bash
echo "kubectl $*" >>"$CALLS"
case "${SCENARIO}:${1} ${2}" in
  "drainfail:drain k3s-test") exit 1 ;;
  "waittimeout:wait --for=condition=Ready") [ "${READY_BACK:-0}" = "1" ] && exit 0 || exit 1 ;;
  "retry:uncordon k3s-test")
    if [ -z "${UNCORDON_ONCE:-}" ]; then UNCORDON_ONCE=1; exit 1; fi
    exit 0 ;;
  *) exit 0 ;;
esac
STUB
printf '#!/usr/bin/env bash\necho "ssh $*" >>"$CALLS"\n' >"$stub/ssh"
printf '#!/usr/bin/env bash\n' >"$stub/sleep"
chmod +x "$stub/kubectl" "$stub/ssh" "$stub/sleep"

awk '/^ *safe_reboot\(\) \{/{f=1} f{print} f&&/^ *\}$/{exit}' "$CRONJOB" >"$work/fn.sh"
grep -q 'safe_reboot()' "$work/fn.sh" || fail "safe_reboot not found in $CRONJOB"

run() { SCENARIO="$1" CALLS="$work/calls" PATH="$stub:$PATH" SSH_OPTS="-o TestOption=no" bash -c '
  set -u
  source "$0" >&2
  rc=0
  safe_reboot k3s-test 10.0.0.1 >&2 || rc=$?
  echo "$rc"' "$work/fn.sh"; }

count() { grep -cF "$1" "$work/calls" || true; }
scenario() { : >"$work/calls"; }

scenario happy
rc=$(run happy)
[ "$rc" = "0" ] || fail "happy must exit 0, got $rc"
[ "$(count 'kubectl uncordon')" = "1" ] || fail "happy must uncordon once"
[ "$(count 'sudo reboot')" = "1" ] || fail "happy must reboot"
[ "$(count 'node-os-updater-cordoned- ')" = "1" ] || fail "happy must remove its label"

scenario drainfail
rc=$(run drainfail)
[ "$rc" = "1" ] || fail "drain failure must be reported, got $rc"
[ "$(count 'sudo reboot')" = "0" ] || fail "drain failure must not reboot"
[ "$(count 'kubectl uncordon')" = "1" ] || fail "drain failure must still uncordon"
[ "$(count 'node-os-updater-cordoned- ')" = "1" ] || fail "drain failure must still remove its label"

scenario waittimeout
rc=$(run waittimeout)
[ "$rc" = "1" ] || fail "ready-wait timeout must be reported, got $rc"
[ "$(count 'sudo reboot')" = "1" ] || fail "wait timeout happens after the reboot"
[ "$(count 'kubectl uncordon')" = "1" ] || fail "wait timeout must still uncordon"

scenario retry
rc=$(run retry)
[ "$rc" = "0" ] || fail "single uncordon failure must be retried, got $rc"
[ "$(count 'kubectl uncordon')" = "2" ] || fail "retry path must uncordon twice"

echo "ok: safe_reboot uncordons on every exit path and reports drain/wait failures"
