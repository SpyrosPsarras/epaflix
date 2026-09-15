#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
hook="$repo_root/.github/hooks/check-no-plaintext-store-values.sh"
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

SECRET_HOST="smtp.relay-example.test"
SECRET_PASS="supersecret42-value"

store_plaintext="$tmp/store-plain.yaml"
cat >"$store_plaintext" <<EOF
truenas_admin_password: $SECRET_PASS
alert_email_hostname: $SECRET_HOST
alert_email_SMTP_port: '587'
auth_email_username: auth@example.test
ssh_deploy_key: |-
  -----BEGIN OPENSSH PRIVATE KEY-----
  b3BlbnNzaC1rZXktdjEAAAAABGxvY2FsaG9zdHJ1bg
  -----END OPENSSH PRIVATE KEY-----
EOF

mkdir -p "$tmp/bin"
cat >"$tmp/bin/sops" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
if [ "${SOPS_FAKE_FAIL:-0}" = "1" ]; then
  echo "fake sops: decryption failure" >&2
  exit 1
fi
mode=""
file=""
while [ $# -gt 0 ]; do
  case "$1" in
    -d|--decrypt) mode=d ;;
    -e|--encrypt) mode=e ;;
    --age|--config|--filename-override|--input-type|--output-type|--output)
      shift
      ;;
    -*) ;;
    *) file="$1" ;;
  esac
  shift
done
[ -n "$file" ] || { echo "fake sops: no file argument" >&2; exit 2; }
case "$mode" in
  e)
    { echo "__FAKE_SOPS_CIPHERTEXT__"; cat "$file"; echo "__END_FAKE_SOPS_CIPHERTEXT__"; }
    ;;
  *)
    if grep -q '^__FAKE_SOPS_CIPHERTEXT__$' "$file"; then
      sed '/^__FAKE_SOPS_CIPHERTEXT__$/d;/^__END_FAKE_SOPS_CIPHERTEXT__$/d' "$file"
    else
      cat "$file"
    fi
    ;;
esac
FAKE
chmod +x "$tmp/bin/sops"

reset_repo() {
  rm -rf "$tmp/repo"
  mkdir -p "$tmp/repo"
  git -C "$tmp/repo" init -q
  git -C "$tmp/repo" -c user.email=test@test -c user.name=test commit -q --allow-empty -m init
  cp "$store_plaintext" "$tmp/repo/store.enc.yaml"
  git -C "$tmp/repo" add store.enc.yaml
  git -C "$tmp/repo" -c user.email=test@test -c user.name=test commit -q -m "seed the store"
  echo fake-age-key >"$tmp/key.txt"
}

reset_staged() {
  git -C "$tmp/repo" reset -q --hard HEAD
  git -C "$tmp/repo" clean -fdq
}

run_hook() {
  (cd "$tmp/repo" && PATH="$tmp/bin:$PATH" KEY_FILE="$tmp/key.txt" \
    STORE="$tmp/repo/store.enc.yaml" bash "$hook")
}

stage_content() {
  local path=$1
  mkdir -p "$tmp/repo/$(dirname "$path")"
  cat >"$tmp/repo/$path"
  git -C "$tmp/repo" add "$path"
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

reset_repo

expect_pass "nothing staged passes"

stage_content notes.txt <<'EOF'
Everything here is boring and refers to key names only.
EOF
expect_pass "clean staged file passes"
reset_staged

stage_content runbook.txt <<EOF
Mail is delivered by $SECRET_HOST on port 587.
EOF
expect_fail "staged file with a store value is refused"
grep -q "alert_email_hostname" "$output" && pass "finding names the store key" || fail "finding names the store key"
if grep -q "$SECRET_HOST" "$output"; then
  fail "finding never prints the value"
else
  pass "finding never prints the value"
fi
reset_staged

stage_content docs/guide.md <<EOF
# Guide

\`\`\`
host: $SECRET_HOST
\`\`\`
EOF
expect_fail "value inside a fenced block is still caught"
reset_staged

stage_content deploy.sh <<EOF
install-key <<'KEY'
b3BlbnNzaC1rZXktdjEAAAAABGxvY2FsaG9zdHJ1bg
KEY
EOF
expect_fail "block-scalar store values are scanned line-by-line"
grep -q "ssh_deploy_key" "$output" && pass "block-scalar finding names the store key" || fail "block-scalar finding names the store key"
reset_staged

stage_content published.md <<EOF
The relay $SECRET_HOST is documented here and committed at HEAD.
EOF
git -C "$tmp/repo" -c user.email=test@test -c user.name=test commit -q -m "publish a value at HEAD"
reset_staged
stage_content newfile.txt <<EOF
Referencing $SECRET_HOST again after it was published.
EOF
expect_pass "a value already published at HEAD is allowed"
reset_staged

stage_content payload.enc.yaml <<EOF
data: some-base64-that-happens-to-contain-$SECRET_HOST-inside
EOF
expect_pass "ciphertext files are skipped (.enc.yaml)"
reset_staged

stage_content ports.txt <<'EOF'
SMTP uses 587 and IMAP uses 993.
EOF
expect_pass "values shorter than 8 characters are not scanned"
reset_staged

stage_content leaked.txt <<EOF
Mail is delivered by $SECRET_HOST on port 587.
EOF
git -C "$tmp/repo" -c user.email=test@test -c user.name=test commit -q -m "seed a committed leak"
reset_staged
stage_content leaked.txt <<'EOF'
The relay hostname now lives only in the store.
EOF
expect_pass "removed lines are not scanned"
reset_staged

: >"$output"
if (cd "$tmp/repo" && KEY_FILE="$tmp/absent-key.txt" bash "$hook") >"$output" 2>&1; then
  if grep -q "SKIP" "$output"; then
    pass "missing age key skips the guard"
  else
    fail "missing age key skips the guard (no SKIP line)"
  fi
else
  fail "missing age key skips the guard (hook failed)"
fi

: >"$output"
if (cd "$tmp/repo" && SOPS_FAKE_FAIL=1 PATH="$tmp/bin:$PATH" bash "$hook") >"$output" 2>&1; then
  if grep -q "SKIP" "$output"; then
    pass "undecryptable store skips the guard"
  else
    fail "undecryptable store skips the guard (no SKIP line)"
  fi
else
  fail "undecryptable store skips the guard (hook failed)"
fi

: >"$output"
if (cd "$tmp/repo" && PATH="/usr/bin:/bin" bash "$hook") >"$output" 2>&1; then
  if grep -q "SKIP" "$output"; then
    pass "sops missing from PATH skips the guard"
  else
    fail "sops missing from PATH skips the guard (no SKIP line)"
  fi
else
  fail "sops missing from PATH skips the guard"
fi

printf '\n%s passed, %s failed\n' "$pass_count" "$fail_count"
[ "$fail_count" -eq 0 ]
