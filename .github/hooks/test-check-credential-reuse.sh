#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
hook="$here/check-credential-reuse.sh"
tmp="$(mktemp -d)"
output="$(mktemp)"
trap 'rm -rf "$tmp"; rm -f "$output"' EXIT

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

SECRET_A="alpha-secret-42"
SECRET_B="bravo-secret-42"
SECRET_HV="charlie-secret-42"
SECRET_NESTED="delta-token-424242"

prefix_of() {
  printf '%s' "$1" | sha256sum | cut -c1-12
}

mkdir -p "$tmp/bin"
cat >"$tmp/bin/sops" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
if [ "${SOPS_FAKE_FAIL:-0}" = "1" ]; then
  echo "fake sops: decryption failure" >&2
  exit 1
fi
file=""
while [ $# -gt 0 ]; do
  case "$1" in
    -*) ;;
    *) file="$1" ;;
  esac
  shift
done
[ -n "$file" ] || { echo "fake sops: no file argument" >&2; exit 2; }
cat "$file"
FAKE
chmod +x "$tmp/bin/sops"

echo fake-age-key >"$tmp/key.txt"

write_store() {
  cat >"$tmp/store.yaml"
}

# Overrides travel as arguments, never as VAR=x function prefixes: bash does
# not specify whether those survive the call, so a value could leak into a
# later case or vanish before the case that needs it. Arg 1 is
# ACCEPTED_REUSE, arg 2 is REPORT_KEYS. Empty means none, so the hook's
# recorded default never leaks into a case either way.
run_hook() {
  local accepted="${1-}" report_keys="${2-}"
  (PATH="$tmp/bin:$PATH" STORE="$tmp/store.yaml" KEY_FILE="$tmp/key.txt" \
    ACCEPTED_REUSE="$accepted" REPORT_KEYS="$report_keys" \
    bash "$hook")
}

expect_pass() {
  local name=$1
  shift
  : >"$output"
  if run_hook "$@" >"$output" 2>&1; then
    pass "$name"
  else
    fail "$name"
  fi
}

expect_fail() {
  local name=$1
  shift
  : >"$output"
  if run_hook "$@" >"$output" 2>&1; then
    fail "$name"
  else
    pass "$name"
  fi
}

base_store() {
  write_store <<EOF
truenas_admin_username: root
proxmox-testhost_username: root
proxmox-testhost_ip: 192.168.1.10
proxmox-testhost_password: $SECRET_HV
workload_one_password: $SECRET_A
workload_two_password: $SECRET_B
some_bot:
    proxmox_token: $SECRET_NESTED
EOF
}

base_store
expect_pass "distinct credential values pass"
grep -q "OK:" "$output" && pass "clean run ends with an OK line" || fail "clean run ends with an OK line"

write_store <<EOF
workload_one_password: $SECRET_A
workload_two_password: $SECRET_A
EOF
expect_fail "two workload keys sharing a password fail"
grep -q "workload_one_password" "$output" && pass "finding names the first key" || fail "finding names the first key"
grep -q "workload_two_password" "$output" && pass "finding names the second key" || fail "finding names the second key"
grep -q "sha256:$(prefix_of "$SECRET_A")" "$output" && pass "finding prints the 12-char sha256 prefix" || fail "finding prints the 12-char sha256 prefix"
if grep -q "$SECRET_A" "$output"; then
  fail "finding never prints the value"
else
  pass "finding never prints the value"
fi

write_store <<EOF
truenas_admin_username: root
proxmox-testhost_username: root
proxmox-testhost_ip: 192.168.1.10
proxmox-testhost_password: $SECRET_HV
workload_one_password: $SECRET_A
workload_two_password: $SECRET_B
pbs_host: pbs.lan.test
some_bot:
    pbs_host: pbs.lan.test
    proxmox_token: $SECRET_NESTED
EOF
expect_pass "shared usernames, hosts and IPs are not credentials"

write_store <<EOF
proxmox-testhost_password: $SECRET_HV
airvpn_password: $SECRET_HV
EOF
expect_fail "a hypervisor password shared with a workload key fails (#1077 class)"
grep -q "(hypervisor)" "$output" && pass "hypervisor member is marked" || fail "hypervisor member is marked"

HV_PREFIX="$(prefix_of "$SECRET_HV")"
expect_pass "an accepted prefix passes and prints ACCEPTED" "$HV_PREFIX"
grep -q "ACCEPTED" "$output" && pass "accepted group prints the ACCEPTED line" || fail "accepted group prints the ACCEPTED line"
if grep -q "$SECRET_HV" "$output"; then
  fail "accepted line never prints the value"
else
  pass "accepted line never prints the value"
fi

write_store <<EOF
proxmox-testhost_password: $SECRET_HV
airvpn_password: $SECRET_HV
workload_one_password: $SECRET_A
workload_two_password: $SECRET_A
EOF
expect_fail "an accepted group and a violation coexist in one run" "$HV_PREFIX"
if grep -q "ACCEPTED sha256:$HV_PREFIX" "$output"; then
  pass "accepted group still prints when another group fails"
else
  fail "accepted group still prints when another group fails"
fi
if grep -A1 "ERROR" "$output" | grep -q "sha256:$(prefix_of "$SECRET_A")"; then
  pass "the unaccepted group is the one reported as ERROR"
else
  fail "the unaccepted group is the one reported as ERROR"
fi
if grep "sha256:$(prefix_of "$SECRET_A")" "$output" | grep -q "recorded risk"; then
  fail "unaccepted group never prints as ACCEPTED"
else
  pass "unaccepted group never prints as ACCEPTED"
fi

base_store
expect_pass "a stale acceptance warns but does not fail" "deadbeef1234"
grep -q "no longer matches any store overlap" "$output" && pass "stale acceptance warns" || fail "stale acceptance warns"

write_store <<EOF
workload_one_password: $SECRET_A
EOF
expect_pass "an acceptance whose value survives only as a singleton is stale" "$(prefix_of "$SECRET_A")"
grep -q "no longer matches any store overlap" "$output" && pass "singleton-surviving acceptance warns" || fail "singleton-surviving acceptance warns"

base_store
expect_pass "a malformed acceptance token is ignored" "zzz"
grep -q "not a 12-char sha256 prefix" "$output" && pass "malformed token warns" || fail "malformed token warns"

write_store <<EOF
workload_one_password: $SECRET_A
workload_two_password: $SECRET_A
workload_three_password: $SECRET_A
EOF
expect_fail "an accepted prefix that is not 12 chars is ignored" "$(prefix_of "$SECRET_A")_x"
grep -q "workload_three_password" "$output" && pass "the group still fails with all three keys" || fail "the group still fails with all three keys"

write_store <<EOF
workload_one_password: $SECRET_A
workload_two_password: $SECRET_B
some_bot:
    proxmox_token: $SECRET_NESTED
other_bot:
    api_key: $SECRET_NESTED
EOF
expect_fail "a nested token shared with a flat key fails"
grep -q "some_bot.proxmox_token" "$output" && pass "nested finding names the dotted key" || fail "nested finding names the dotted key"
grep -q "other_bot.api_key" "$output" && pass "nested finding names the sibling key" || fail "nested finding names the sibling key"

ARMOR="b3BlbnNzaC1rZXktdjEAAAAABGxvY2FsaG9zdA"
write_store <<EOF
ssh_deploy_key: |-
  -----BEGIN OPENSSH PRIVATE KEY-----
  $ARMOR-one
  -----END OPENSSH PRIVATE KEY-----
ssh_deploy_key_two: |-
  -----BEGIN OPENSSH PRIVATE KEY-----
  $ARMOR-one
  -----END OPENSSH PRIVATE KEY-----
EOF
expect_fail "two identical block-scalar keys fail"
grep -q "ssh_deploy_key_two" "$output" && pass "block-scalar finding names both keys" || fail "block-scalar finding names both keys"

write_store <<EOF
ssh_deploy_key: |-
  -----BEGIN OPENSSH PRIVATE KEY-----
  $ARMOR-one
  -----END OPENSSH PRIVATE KEY-----
ssh_deploy_key_two: |-
  -----BEGIN OPENSSH PRIVATE KEY-----
  $ARMOR-two
  -----END OPENSSH PRIVATE KEY-----
EOF
expect_pass "two different SSH keys sharing armor lines pass"

write_store <<EOF
workload_one_password: '1234567'
workload_two_password: '1234567'
EOF
expect_pass "values shorter than 8 characters are not grouped"

base_store
expect_pass "REPORT_KEYS prints len and prefix, never the value" "" \
  "proxmox-testhost_password workload_one_password missing_key"
grep -q "REPORT proxmox-testhost_password len=${#SECRET_HV} sha256=$(prefix_of "$SECRET_HV")" "$output" \
  && pass "REPORT line carries len and prefix" || fail "REPORT line carries len and prefix"
grep -q "REPORT missing_key absent" "$output" && pass "unknown REPORT key reports absent" || fail "unknown REPORT key reports absent"
if grep -qE "$SECRET_HV|$SECRET_A" "$output"; then
  fail "REPORT never prints the value"
else
  pass "REPORT never prints the value"
fi

: >"$output"
if (PATH="/usr/bin:/bin" STORE="$tmp/store.yaml" KEY_FILE="$tmp/key.txt" bash "$hook") >"$output" 2>&1; then
  if grep -q "SKIP" "$output"; then
    pass "sops missing from PATH skips the guard"
  else
    fail "sops missing from PATH skips the guard (no SKIP line)"
  fi
else
  fail "sops missing from PATH skips the guard"
fi

: >"$output"
if (PATH="$tmp/bin:$PATH" STORE="$tmp/store.yaml" KEY_FILE="$tmp/absent-key.txt" bash "$hook") >"$output" 2>&1; then
  if grep -q "SKIP" "$output"; then
    pass "missing age key skips the guard"
  else
    fail "missing age key skips the guard (no SKIP line)"
  fi
else
  fail "missing age key skips the guard"
fi

: >"$output"
if (SOPS_FAKE_FAIL=1 PATH="$tmp/bin:$PATH" STORE="$tmp/store.yaml" KEY_FILE="$tmp/key.txt" bash "$hook") >"$output" 2>&1; then
  if grep -q "SKIP" "$output"; then
    pass "undecryptable store skips the guard"
  else
    fail "undecryptable store skips the guard (no SKIP line)"
  fi
else
  fail "undecryptable store skips the guard"
fi

: >"$output"
if (PATH="$tmp/bin:$PATH" STORE="$tmp/absent-store.yaml" KEY_FILE="$tmp/key.txt" bash "$hook") >"$output" 2>&1; then
  if grep -q "SKIP" "$output"; then
    pass "missing store skips the guard"
  else
    fail "missing store skips the guard (no SKIP line)"
  fi
else
  fail "missing store skips the guard"
fi

write_store <<'EOF'
workload_one_password: ENC[AES256_GCM,data:c2VjcmV0,iv:x,tag:y,type:str]
workload_two_password: ENC[AES256_GCM,data:b3RoZXI=,iv:x,tag:y,type:str]
EOF
: >"$output"
if (PATH="$tmp/bin:$PATH" STORE="$tmp/store.yaml" KEY_FILE="$tmp/key.txt" bash "$hook") >"$output" 2>&1; then
  if grep -q "SKIP.*ciphertext" "$output"; then
    pass "a store that still reads as ciphertext skips instead of faking an OK"
  else
    fail "a store that still reads as ciphertext skips instead of faking an OK (no SKIP line)"
  fi
else
  fail "a store that still reads as ciphertext skips instead of faking an OK"
fi
if grep -q "OK:" "$output"; then
  fail "ciphertext run never prints OK"
else
  pass "ciphertext run never prints OK"
fi

printf '\n%s passed, %s failed\n' "$pass_count" "$fail_count"
[ "$fail_count" -eq 0 ]
