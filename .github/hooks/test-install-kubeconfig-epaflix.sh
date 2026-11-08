#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
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

stub="$tmp/stub-bin"
mkdir -p "$stub"
cat >"$stub/kubectl" <<'SH'
#!/usr/bin/env bash
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
    && env -u KUBECONFIG \
       PATH="$stub:$PATH" \
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

printf '%s\n' "1..$((pass_count + fail_count))"
if [ "$fail_count" -ne 0 ]; then
  printf '%s fixture test(s) failed\n' "$fail_count" >&2
  exit 1
fi
printf 'All %s fixture tests passed.\n' "$pass_count"
