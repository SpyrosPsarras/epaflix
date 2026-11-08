#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
guard="$repo_root/.github/hooks/kubectl-guard.sh"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/guard-bin" "$tmp/real-bin" "$tmp/elsewhere"
ln -s "$guard" "$tmp/guard-bin/kubectl"
printf '#!/usr/bin/env bash\necho "real kubectl $*"\n' >"$tmp/real-bin/kubectl"
chmod +x "$tmp/real-bin/kubectl"
git -C "$tmp/elsewhere" init -q
fixture_path="$tmp/guard-bin:$tmp/real-bin:$PATH"

pass_count=0
fail_count=0

check() {
  local name=$1 dir=$2 expect=$3
  shift 3
  local out rc=0
  out="$(cd "$dir" && PATH="$fixture_path" kubectl "$@" 2>&1)" || rc=$?
  if { [ "$expect" = run ] && [ "$rc" -eq 0 ] && [ "$out" = "real kubectl $*" ]; } ||
    { [ "$expect" = refuse ] && [ "$rc" -ne 0 ] && [[ $out == *"needs --context epaflix"* ]] && [[ $out != *other-cluster-a* ]]; }; then
    pass_count=$((pass_count + 1))
    printf 'ok - %s\n' "$name"
  else
    fail_count=$((fail_count + 1))
    printf 'not ok - %s (rc=%s)\n  %s\n' "$name" "$rc" "$out" >&2
  fi
}

check "no context in the repo is refused" "$repo_root" refuse get pods
check "--context epaflix runs" "$repo_root" run --context epaflix get pods
check "--context=epaflix after the verb runs" "$repo_root" run get pods --context=epaflix
check "another context is refused" "$repo_root" refuse --context other-cluster-a get pods
check "the last --context wins, as in kubectl" "$repo_root" refuse --context epaflix get pods --context other-cluster-a
check "--context after -- is the remote command's, not kubectl's" "$repo_root" refuse exec pod -- sh --context epaflix
check "config needs no context" "$repo_root" run config get-contexts -o name
check "a subdirectory of the repo is guarded too" "$repo_root/.github" refuse get pods
check "another repo passes through untouched" "$tmp/elsewhere" run get pods
check "a non-repo directory passes through untouched" "$tmp" run get pods

printf '%s\n' "1..$((pass_count + fail_count))"
if [ "$fail_count" -ne 0 ]; then
  printf '%s fixture test(s) failed\n' "$fail_count" >&2
  exit 1
fi
printf 'All %s fixture tests passed.\n' "$pass_count"
