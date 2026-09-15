#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
script="$here/collapse-duplicate-email-keys.sh"
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
SECRET_ALERT_PASS="alert-pass-123456"
SECRET_AUTH_PASS="auth-pass-654321"

mkdir -p "$tmp/bin"
cat >"$tmp/bin/sops" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
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
    {
      echo "__FAKE_SOPS_CIPHERTEXT__"
      cat "$file"
      echo "__END_FAKE_SOPS_CIPHERTEXT__"
      printf 'sops:\n    age:\n        - recipient: %s\n' \
        "${SOPS_FAKE_RECIPIENT:-age1fakefakefakefakefakefake}"
    }
    ;;
  *)
    if grep -q '^__FAKE_SOPS_CIPHERTEXT__$' "$file"; then
      sed '/^__FAKE_SOPS_CIPHERTEXT__$/d;/^__END_FAKE_SOPS_CIPHERTEXT__$/d' "$file" |
        sed '/^sops:$/,$d'
    else
      sed '/^sops:$/,$d' "$file"
    fi
    ;;
esac
FAKE
chmod +x "$tmp/bin/sops"

init_repo() {
  rm -rf "$tmp/repo"
  mkdir -p "$tmp/repo"
  git -C "$tmp/repo" init -q
  git -C "$tmp/repo" -c user.email=test@test -c user.name=test commit -q --allow-empty -m init
  echo fake-age-key >"$tmp/key.txt"
}

write_store() {
  cat >"$tmp/repo/store.enc.yaml"
}

run_script() {
  (cd "$tmp/repo" && PATH="$tmp/bin:$PATH" KEY_FILE="$tmp/key.txt" \
    STORE="$tmp/repo/store.enc.yaml" bash "$script" "$@")
}

assert_output_has() {
  if grep -qF "$1" "$output"; then
    pass "$2"
  else
    fail "$2 (missing: $1)"
  fi
}

assert_output_lacks() {
  if grep -q "$1" "$output"; then
    fail "$2 (leaked: $1)"
  else
    pass "$2"
  fi
}

assert_line_count() {
  local file=$1 pattern=$2 want=$3 name=$4
  local got
  got="$(grep -c "^$pattern" "$file" || true)"
  if [ "$got" = "$want" ]; then
    pass "$name"
  else
    fail "$name (expected $want of ^$pattern, got $got)"
  fi
}

init_repo
write_store <<EOF
truenas_admin_password: supersecret42-value
alert_email_username: alerts@example.test
alert_email_password: $SECRET_ALERT_PASS
alert_email_hostname: $SECRET_HOST
alert_email_IMAP_port: '993'
alert_email_SMTP_port: '587'
alert_email_SMTP_SSL: 'true'
alert_email_SMTP_SSL_encryption: STARTTLS
auth_email_username: auth@example.test
auth_email_password: $SECRET_AUTH_PASS
auth_email_hostname: $SECRET_HOST
auth_email_IMAP_port: '993'
auth_email_SMTP_port: '587'
auth_email_SMTP_SSL: 'true'
auth_email_SMTP_SSL_encryption: STARTTLS
last_key: keepme
sops:
    age:
        - recipient: age1fakefakefakefakefakefake
EOF
cp "$tmp/repo/store.enc.yaml" "$tmp/store.before"

if run_script >"$output" 2>&1; then
  pass "dry-run exits zero"
else
  fail "dry-run exits zero"
fi
assert_output_has "mail_relay_hostname <- alert_email_hostname + auth_email_hostname" \
  "dry-run reports the hostname collapse"
assert_output_has "10 key(s) collapse into 5 shared mail_relay_* key(s)" \
  "dry-run reports five shared keys from ten duplicates"
if cmp -s "$tmp/repo/store.enc.yaml" "$tmp/store.before"; then
  pass "dry-run leaves the store untouched"
else
  fail "dry-run leaves the store untouched"
fi

if run_script --apply >"$output" 2>&1; then
  pass "apply exits zero"
else
  fail "apply exits zero"
fi
assert_output_has "ROUND_TRIP_OK" "apply round-trip verified"
assert_output_has "APPLIED" "apply reports success"

assert_line_count "$tmp/repo/store.enc.yaml" "mail_relay_hostname:" 1 \
  "shared hostname key exists exactly once"
assert_line_count "$tmp/repo/store.enc.yaml" "mail_relay_SMTP_port:" 1 \
  "shared SMTP port key exists exactly once"
assert_line_count "$tmp/repo/store.enc.yaml" "mail_relay_IMAP_port:" 1 \
  "shared IMAP port key exists exactly once"
assert_line_count "$tmp/repo/store.enc.yaml" "alert_email_hostname:" 0 \
  "per-mailbox hostname keys are gone (alert)"
assert_line_count "$tmp/repo/store.enc.yaml" "auth_email_hostname:" 0 \
  "per-mailbox hostname keys are gone (auth)"

grep -q "^alert_email_username: alerts@example.test$" "$tmp/repo/store.enc.yaml" &&
  pass "alert username preserved verbatim" ||
  fail "alert username preserved verbatim"
grep -q "^alert_email_password: $SECRET_ALERT_PASS$" "$tmp/repo/store.enc.yaml" &&
  pass "alert password preserved verbatim" ||
  fail "alert password preserved verbatim"
grep -q "^auth_email_username: auth@example.test$" "$tmp/repo/store.enc.yaml" &&
  pass "auth username preserved verbatim" ||
  fail "auth username preserved verbatim"
grep -q "^auth_email_password: $SECRET_AUTH_PASS$" "$tmp/repo/store.enc.yaml" &&
  pass "auth password preserved verbatim" ||
  fail "auth password preserved verbatim"
grep -q "^truenas_admin_password: supersecret42-value$" "$tmp/repo/store.enc.yaml" &&
  pass "unrelated keys preserved verbatim" ||
  fail "unrelated keys preserved verbatim"
grep -q "^last_key: keepme$" "$tmp/repo/store.enc.yaml" &&
  pass "trailing keys preserved" ||
  fail "trailing keys preserved"

host_line="$(grep -n "^mail_relay_hostname:" "$tmp/repo/store.enc.yaml" | cut -d: -f1)"
user_line="$(grep -n "^alert_email_username:" "$tmp/repo/store.enc.yaml" | cut -d: -f1)"
end_line="$(grep -n "^last_key:" "$tmp/repo/store.enc.yaml" | cut -d: -f1)"
if [ -n "$host_line" ] && [ -n "$user_line" ] && [ -n "$end_line" ] &&
  [ "$host_line" -lt "$user_line" ] && [ "$user_line" -lt "$end_line" ]; then
  pass "shared block sits where the email keys were"
else
  fail "shared block sits where the email keys were"
fi

assert_output_lacks "$SECRET_HOST" "apply output never prints the hostname"
assert_output_lacks "$SECRET_ALERT_PASS" "apply output never prints the alert password"
assert_output_lacks "$SECRET_AUTH_PASS" "apply output never prints the auth password"

cp "$tmp/repo/store.enc.yaml" "$tmp/store.applied"
if run_script --apply >"$output" 2>&1; then
  pass "second apply exits zero"
else
  fail "second apply exits zero"
fi
assert_output_has "nothing to do" "second apply is a no-op"
if cmp -s "$tmp/repo/store.enc.yaml" "$tmp/store.applied"; then
  pass "second apply leaves the store unchanged"
else
  fail "second apply leaves the store unchanged"
fi

init_repo
write_store <<EOF
alert_email_SMTP_port: '587'
auth_email_SMTP_port: '2587'
sops:
    age:
        - recipient: age1fakefakefakefakefakefake
EOF
if run_script --apply >"$output" 2>&1; then
  pass "diverged values apply cleanly"
else
  fail "diverged values apply cleanly"
fi
assert_output_has "kept per-mailbox" "diverged values reported as kept"
assert_line_count "$tmp/repo/store.enc.yaml" "alert_email_SMTP_port:" 1 \
  "diverged alert key kept"
assert_line_count "$tmp/repo/store.enc.yaml" "auth_email_SMTP_port:" 1 \
  "diverged auth key kept"
assert_line_count "$tmp/repo/store.enc.yaml" "mail_relay_SMTP_port:" 0 \
  "no shared key for diverged values"

init_repo
write_store <<EOF
only: flat
sops:
    age:
        - recipient: age1fakefakefakefakefakefake
EOF
if run_script >"$output" 2>&1; then
  pass "store without email keys exits zero"
else
  fail "store without email keys exits zero"
fi
assert_output_has "nothing to do: no alert_email_" "store without email keys is a no-op"

init_repo
write_store <<EOF
key:
  nested: value
sops:
    age:
        - recipient: age1fakefakefakefakefakefake
EOF
if run_script >"$output" 2>&1; then
  fail "non-flat store is refused"
else
  pass "non-flat store is refused"
fi
assert_output_has "not a flat key: value entry" "non-flat store names the layout problem"

if run_script --apply >"$output" 2>&1; then
  fail "non-flat store is refused in apply mode"
else
  pass "non-flat store is refused in apply mode"
fi

init_repo
write_store <<'EOF'
truenas_admin_password: supersecret42-value
sops:
    age:
        - recipient: age1fakefakefakefakefakefake
EOF
if run_script --apply >"$output" 2>&1; then
  pass "apply without email keys exits zero"
else
  fail "apply without email keys exits zero"
fi
assert_output_has "nothing to do" "apply without email keys is a no-op"

init_repo
write_store <<'EOF'
alert_email_hostname: smtp.relay-example.test
EOF
if (cd "$tmp/repo" && PATH="$tmp/bin:$PATH" KEY_FILE="$tmp/absent-key.txt" \
  STORE="$tmp/repo/store.enc.yaml" bash "$script") >"$output" 2>&1; then
  fail "missing key file is refused"
else
  pass "missing key file is refused"
fi

printf '\n%s passed, %s failed\n' "$pass_count" "$fail_count"
[ "$fail_count" -eq 0 ]
