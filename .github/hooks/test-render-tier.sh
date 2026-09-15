#!/usr/bin/env bash
# Fixture suite for render-tier.sh. A stub kustomize stands in for the ksops
# plugin so no SOPS key or cluster is needed.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
script="$repo_root/.github/hooks/render-tier.sh"
tmp="$(mktemp -d)"
cleanup() { rm -rf "$tmp"; }
trap cleanup EXIT

tier="$tmp/tier"
mkdir -p "$tier"
printf 'resources: []\n' >"$tier/kustomization.yaml"

# Stub kustomize: ignores its arguments, prints one Secret carrying a relay
# password, unless FAKE_RENDER_FAIL is set.
mkdir -p "$tmp/bin"
cat >"$tmp/bin/kustomize" <<'EOF'
#!/usr/bin/env bash
if [[ ${FAKE_RENDER_FAIL:-0} == 1 ]]; then
  echo "stub kustomize failure" >&2
  exit 3
fi
cat <<'RENDER'
apiVersion: v1
kind: Secret
metadata:
  name: relay
type: Opaque
stringData:
  smtp_auth_password: hunter2
RENDER
EOF
chmod +x "$tmp/bin/kustomize"

output="$tmp/output"
evidence="$tmp/evidence"
mkdir -p "$evidence"
export PATH="$tmp/bin:$PATH"

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

run() { bash "$script" "$@" >"$output" 2>&1; }

# A check that records the render file, its mode and its content, then reports
# a verdict through its exit status.
recording_check() {
  printf '%s' "$RENDER_FILE" >"$evidence/render_path"
  stat -c %a "$RENDER_FILE" >"$evidence/render_mode"
  cp "$RENDER_FILE" "$evidence/render_copy"
  return "${CHECK_RC:-0}"
}
export -f recording_check
export evidence

rc=0; run "$tier" || rc=$?
if [ "$rc" -eq 0 ] \
  && grep -q '1 Secret(s) decrypted into the render of' "$output" \
  && grep -q 'credential-bearing and is now shredded' "$output" \
  && ! grep -q 'hunter2' "$output"; then
  pass "default check counts the decrypted Secrets and never prints a value"
else
  fail "default check counts the decrypted Secrets and never prints a value (rc=$rc)"
fi

rc=0; CHECK_RC=0 bash -c '"$1" "$2" recording_check' _ "$script" "$tier" >"$output" 2>&1 || rc=$?
render_path="$(cat "$evidence/render_path" 2>/dev/null || true)"
if [ "$rc" -eq 0 ] \
  && [ -n "$render_path" ] \
  && [ "$(cat "$evidence/render_mode")" = "600" ] \
  && grep -q 'smtp_auth_password: hunter2' "$evidence/render_copy" \
  && [ ! -e "$render_path" ]; then
  pass "the render file is private, visible to the check, and shredded on success"
else
  fail "the render file is private, visible to the check, and shredded on success (rc=$rc)"
fi

rc=0; CHECK_RC=7 bash -c '"$1" "$2" recording_check' _ "$script" "$tier" >"$output" 2>&1 || rc=$?
render_path="$(cat "$evidence/render_path" 2>/dev/null || true)"
if [ "$rc" -eq 7 ] \
  && [ -n "$render_path" ] \
  && [ ! -e "$render_path" ]; then
  pass "a failing check exits with its status and still shreds the render"
else
  fail "a failing check exits with its status and still shreds the render (rc=$rc)"
fi

rc=0; FAKE_RENDER_FAIL=1 run "$tier" || rc=$?
if [ "$rc" -eq 3 ] && grep -q 'stub kustomize failure' "$output"; then
  pass "a failed render surfaces the kustomize error and exits nonzero"
else
  fail "a failed render surfaces the kustomize error and exits nonzero (rc=$rc)"
fi

# For a render failure nothing in-process can capture the render path, so a
# stub shred proves the EXIT trap hands the render file to shred.
cat >"$tmp/bin/shred" <<'EOF'
#!/usr/bin/env bash
for f in "$@"; do
  [[ $f == -* ]] && continue
  printf '%s\n' "$f" >>"$SHRED_LOG"
  rm -f -- "$f"
done
EOF
chmod +x "$tmp/bin/shred"
rc=0; FAKE_RENDER_FAIL=1 SHRED_LOG="$tmp/shred.log" run "$tier" || rc=$?
if [ "$rc" -eq 3 ] \
  && [ "$(wc -l <"$tmp/shred.log")" = "1" ] \
  && [[ "$(cat "$tmp/shred.log")" == /* ]] \
  && [ ! -e "$(cat "$tmp/shred.log")" ]; then
  pass "a failed render still hands the render file to shred on exit"
else
  fail "a failed render still hands the render file to shred on exit (rc=$rc)"
fi

rc=0; run "$tmp/no-such-tier" || rc=$?
if [ "$rc" -eq 2 ] && grep -q 'not a directory:' "$output"; then
  pass "a missing tier directory exits 2 before rendering"
else
  fail "a missing tier directory exits 2 before rendering (rc=$rc)"
fi

printf '%s\n' "1..$((pass_count + fail_count))"
if [ "$fail_count" -ne 0 ]; then
  printf '%s fixture test(s) failed\n' "$fail_count" >&2
  exit 1
fi
printf 'All %s fixture tests passed.\n' "$pass_count"
