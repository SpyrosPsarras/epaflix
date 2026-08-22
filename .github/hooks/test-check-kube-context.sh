#!/usr/bin/env bash
# Fixture suite for check-kube-context.sh and install-kubeconfig-epaflix.sh
# (#856).
#
# Writes synthetic kubeconfigs to a temp dir and runs the real check with
# KUBECONFIG pointed at each one. Named test-*, not check-*, so the pre-commit
# dispatcher does NOT pick it up (same convention as test-check-sops-encrypted.sh).
#
# The non-epaflix fixture names are invented and resemble nothing real: the
# five withheld work context names appear nowhere in this repo. Case 6 is the
# leak control - it asserts the check's own output never contains a context
# name, which is the mechanical proof that the code path is name-agnostic and
# so generalises to the withheld names.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
hook="$repo_root/.github/hooks/check-kube-context.sh"
installer="$repo_root/.github/hooks/install-kubeconfig-epaflix.sh"
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

# $1 name, $2 kubeconfig fixture file
expect_pass() {
  : >"$output"
  if KUBECONFIG="$2" bash "$hook" >"$output" 2>&1; then
    pass "$1"
  else
    fail "$1"
  fi
}

expect_fail() {
  : >"$output"
  if KUBECONFIG="$2" bash "$hook" >"$output" 2>&1; then
    fail "$1"
  else
    pass "$1"
  fi
}

# A kubeconfig holding one entry per named context. Server URLs are loopback.
write_kubeconfig() {
  local path=$1
  shift
  {
    printf 'apiVersion: v1\nkind: Config\npreferences: {}\nclusters:\n'
    for name in "$@"; do
      printf -- '- name: %s-cluster\n  cluster:\n    server: https://127.0.0.1:6443\n' "$name"
    done
    printf 'users:\n'
    for name in "$@"; do
      printf -- '- name: %s-user\n  user:\n    token: not-a-real-token\n' "$name"
    done
    printf 'contexts:\n'
    for name in "$@"; do
      printf -- '- name: %s\n  context:\n    cluster: %s-cluster\n    user: %s-user\n' "$name" "$name" "$name"
    done
    printf 'current-context: %s\n' "$1"
  } >"$path"
}

if ! command -v kubectl >/dev/null 2>&1; then
  echo "SKIP: kubectl is not on PATH, so the kubeconfig cases cannot run." >&2
  echo "SKIP: this leaves check-kube-context.sh unexercised on this machine." >&2
fi

if command -v kubectl >/dev/null 2>&1; then
  write_kubeconfig "$tmp/only-epaflix" epaflix
  expect_pass "epaflix-only kubeconfig passes" "$tmp/only-epaflix"

  write_kubeconfig "$tmp/multi" epaflix other-cluster-a other-cluster-b
  expect_fail "epaflix plus two other contexts is refused" "$tmp/multi"

  write_kubeconfig "$tmp/single-other" other-cluster-a
  expect_fail "a single non-epaflix context is refused (allowlist, not count-only)" "$tmp/single-other"

  printf 'apiVersion: v1\nkind: Config\npreferences: {}\nclusters: []\nusers: []\ncontexts: []\n' \
    >"$tmp/no-contexts"
  expect_fail "a kubeconfig with zero contexts is refused" "$tmp/no-contexts"

  # LEAK CONTROL: the multi-context run must not name anything it rejected.
  : >"$output"
  KUBECONFIG="$tmp/multi" bash "$hook" >"$output" 2>&1 || true
  leaked=0
  for name in other-cluster-a other-cluster-b; do
    if grep -qF -- "$name" "$output"; then
      leaked=1
    fi
  done
  if [ "$leaked" -eq 0 ]; then
    pass "leak control: rejection output names no context (counts only)"
  else
    fail "leak control: rejection output named a context it rejected"
  fi
fi

# kubectl absent from PATH: the check must self-skip, loudly, and pass. Only
# shell builtins run on that path, so an emptied PATH is enough - bash itself
# is invoked by absolute path because PATH lookup is gone.
bash_bin="$(command -v bash)"
: >"$output"
if PATH=/nonexistent-for-fixture "$bash_bin" "$hook" >"$output" 2>&1; then
  if grep -q '^SKIP: kubectl is not on PATH' "$output"; then
    pass "kubectl absent from PATH passes with a printed SKIP line"
  else
    fail "kubectl absent from PATH passed but printed no SKIP line"
  fi
else
  fail "kubectl absent from PATH must pass, not fail"
fi

# The installer's product verification, with a stub kubectl so the fixture can
# hand it a deliberately bad product. Without this, "the generated kubeconfig
# holds exactly one context" is an untested claim.
stub="$tmp/stub-bin"
mkdir -p "$stub"
cat >"$stub/kubectl" <<'SH'
#!/usr/bin/env bash
# Fixture stub: just enough `kubectl config` surface for the installer. Source
# context names come from $FIXTURE_SOURCE_CONTEXTS; the "generated" kubeconfig
# is $FIXTURE_PRODUCT, whose `# ctx:`/`# cluster:`/`# user:` marker lines are
# what the installer's verification reads back.
set -euo pipefail
args="$*"
case "$args" in
  "config get-contexts -o name")
    if [ -n "${KUBECONFIG:-}" ] && [ -f "${KUBECONFIG:-}" ]; then
      sed -n 's/^# ctx: //p' "$KUBECONFIG"
    else
      cat "$FIXTURE_SOURCE_CONTEXTS"
    fi
    ;;
  "config view --minify --flatten --context epaflix")
    cat "$FIXTURE_PRODUCT"
    ;;
  *jsonpath*clusters*) sed -n 's/^# cluster: //p' "$KUBECONFIG" ;;
  *jsonpath*users*) sed -n 's/^# user: //p' "$KUBECONFIG" ;;
  *) echo "stub kubectl: unexpected args: $args" >&2; exit 64 ;;
esac
SH
chmod +x "$stub/kubectl"

repo="$tmp/repo"
mkdir -p "$repo"
git -C "$repo" init -q
product="$tmp/product"
generated="$repo/.kube/epaflix.kubeconfig"

run_installer() {
  : >"$output"
  (cd "$repo" \
    && PATH="$stub:$PATH" \
       FIXTURE_SOURCE_CONTEXTS="$tmp/source-contexts" \
       FIXTURE_PRODUCT="$product" \
       bash "$installer") >"$output" 2>&1
}

printf 'other-cluster-a\nother-cluster-b\n' >"$tmp/source-contexts"
printf '# ctx: epaflix\n# cluster: epaflix-cluster\n# user: epaflix-user\n' >"$product"
rm -f "$generated"
if run_installer; then
  fail "installer refuses a source kubeconfig with no epaflix context"
elif grep -q 'context epaflix not found' "$output" \
  && ! grep -qF -- 'other-cluster-a' "$output" \
  && ! grep -qF -- 'other-cluster-b' "$output"; then
  pass "installer refuses a source kubeconfig with no epaflix context, naming none of them"
else
  fail "installer refuses a source kubeconfig with no epaflix context, naming none of them"
fi

printf 'epaflix\nother-cluster-a\n' >"$tmp/source-contexts"
printf '# ctx: epaflix\n# ctx: other-cluster-a\n# cluster: epaflix-cluster\n# cluster: other-cluster-a-cluster\n# user: epaflix-user\n# user: other-cluster-a-user\n' >"$product"
rm -f "$generated"
if run_installer; then
  fail "installer rejects a generated kubeconfig holding more than epaflix"
elif [ ! -e "$generated" ]; then
  pass "installer rejects a generated kubeconfig holding more than epaflix and deletes it"
else
  fail "installer rejects a generated kubeconfig holding more than epaflix and deletes it"
fi

printf 'epaflix\nother-cluster-a\n' >"$tmp/source-contexts"
printf '# ctx: epaflix\n# cluster: epaflix-cluster\n# user: epaflix-user\n' >"$product"
rm -f "$generated"
if run_installer && [ -f "$generated" ] \
  && [ "$(stat -c '%a' "$generated")" = "600" ]; then
  pass "installer accepts a verified single-context product and writes it mode 600"
else
  fail "installer accepts a verified single-context product and writes it mode 600"
fi

# The dispatcher runs `check-*.sh` only. A check named anything else silently
# never runs, and a fixture suite named check-* would run on every commit.
discovered="$(cd "$repo_root/.github/hooks" && echo check-*.sh)"
if printf '%s\n' "$discovered" | grep -qw 'check-kube-context.sh' \
  && ! printf '%s\n' "$discovered" | grep -q 'test-'; then
  pass "pre-commit dispatcher glob discovers this check and not this suite"
else
  fail "pre-commit dispatcher glob discovers this check and not this suite"
fi

printf '%s\n' "1..$((pass_count + fail_count))"
if [ "$fail_count" -ne 0 ]; then
  printf '%s fixture test(s) failed\n' "$fail_count" >&2
  exit 1
fi
printf 'All %s fixture tests passed.\n' "$pass_count"
