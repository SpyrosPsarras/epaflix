#!/usr/bin/env bash
set -euo pipefail

# Fixture tests for print-age-key-backup.sh. The age key here is a synthetic
# fixture that only matches the script's format gate — it decrypts nothing.

script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/print-age-key-backup.sh"
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

FAKE_KEY="# created: 2026-09-05T00:00:00Z
# public key: age1testtesttesttesttesttesttesttesttesttesttesttesttes
AGE-SECRET-KEY-1testtesttesttesttesttesttesttesttesttesttesttesttest"

shim_dir="$tmp/bin"
mkdir -p "$shim_dir"
cat >"$shim_dir/sops" <<'SHIM'
#!/usr/bin/env bash
exit "${FAKE_SOPS_EXIT:-0}"
SHIM
chmod +x "$shim_dir/sops"

run_script() {
  KEY_FILE="$tmp/key.txt" STORE="$tmp/store.yaml" OUT="$tmp/out/page.html" \
    PATH="$shim_dir:$PATH" bash "$script" >"$output" 2>&1
}

setup_happy() {
  printf '%s\n' "$FAKE_KEY" >"$tmp/key.txt"
  printf 'synthetic: store\n' >"$tmp/store.yaml"
  rm -rf "$tmp/out"
}

# 1. Happy path: valid key format, sops stub succeeds.
setup_happy
if run_script; then
  pass "script exits 0 when sops verification passes"
else
  fail "script exits 0 when sops verification passes"
fi

# 2. Page exists, mode 600, holds the exact key text, hash, and restore steps.
if [ -f "$tmp/out/page.html" ] && [ "$(stat -c %a "$tmp/out/page.html")" = "600" ]; then
  pass "page written with mode 600"
else
  fail "page written with mode 600"
fi

if grep -qF "AGE-SECRET-KEY-1testtesttesttesttesttesttesttesttesttesttesttesttest" "$tmp/out/page.html"; then
  pass "page contains the key text"
else
  fail "page contains the key text"
fi

expected_hash="$(sha256sum "$tmp/key.txt" | cut -d' ' -f1)"
if grep -qF "$expected_hash" "$tmp/out/page.html"; then
  pass "page contains the key file sha256"
else
  fail "page contains the key file sha256"
fi

if grep -q 'sops -d .github/instructions/secrets.enc.yaml' "$tmp/out/page.html" \
  && grep -q 'argocd/sops-age' "$tmp/out/page.html"; then
  pass "page carries the restore steps"
else
  fail "page carries the restore steps"
fi

# 2b. The key must never reach the script's stdout or stderr.
if grep -q 'AGE-SECRET-KEY-1' "$output"; then
  fail "key text stays out of the script's output"
else
  pass "key text stays out of the script's output"
fi

# 3. No qrencode on PATH: page says so instead of embedding a QR.
if grep -q 'qrencode is not installed' "$tmp/out/page.html"; then
  pass "missing qrencode is stated on the page"
else
  fail "missing qrencode is stated on the page"
fi

# 4. qrencode shim on PATH: page embeds an inline SVG.
cat >"$shim_dir/qrencode" <<'SHIM'
#!/usr/bin/env bash
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; fi
  shift
done
printf '<svg>synthetic-qr</svg>' >"$out"
SHIM
chmod +x "$shim_dir/qrencode"
setup_happy
if run_script && grep -q '<svg>synthetic-qr</svg>' "$tmp/out/page.html"; then
  pass "qrencode on PATH embeds the QR svg"
else
  fail "qrencode on PATH embeds the QR svg"
fi

# 5. sops verification fails: script must fail and write nothing.
setup_happy
if KEY_FILE="$tmp/key.txt" STORE="$tmp/store.yaml" OUT="$tmp/out/page.html" \
  FAKE_SOPS_EXIT=1 PATH="$shim_dir:$PATH" bash "$script" >"$output" 2>&1; then
  fail "failing sops verification fails the script"
else
  pass "failing sops verification fails the script"
fi
if [ ! -e "$tmp/out/page.html" ]; then
  pass "no page is written when verification fails"
else
  fail "no page is written when verification fails"
fi

# 6. File that is not an age key: format gate fires before sops is called.
printf 'not an age key\n' >"$tmp/key.txt"
if KEY_FILE="$tmp/key.txt" STORE="$tmp/store.yaml" OUT="$tmp/out/page.html" \
  PATH="$shim_dir:$PATH" bash "$script" >"$output" 2>&1; then
  fail "non-age key file is rejected"
else
  pass "non-age key file is rejected"
fi

# 7. Missing key file.
setup_happy
if KEY_FILE="$tmp/missing.txt" STORE="$tmp/store.yaml" OUT="$tmp/out/page.html" \
  PATH="$shim_dir:$PATH" bash "$script" >"$output" 2>&1; then
  fail "missing key file is rejected"
else
  pass "missing key file is rejected"
fi

printf '%s\n' "1..$((pass_count + fail_count))"
if [ "$fail_count" -ne 0 ]; then
  printf '%s fixture test(s) failed\n' "$fail_count" >&2
  exit 1
fi
printf 'All %s fixture tests passed.\n' "$pass_count"
