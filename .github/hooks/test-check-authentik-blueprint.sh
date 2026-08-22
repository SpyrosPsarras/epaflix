#!/usr/bin/env bash
# Fixture suite for check-authentik-blueprint.sh / check_authentik_blueprint.py
# (#883, #876, #940).
#
# Every fixture is a synthetic PLAINTEXT Secret document, so this suite needs no
# age key and runs in CI - the same split ci.yml already uses for
# test-check-sops-encrypted.sh. The real blueprint check cannot run in CI at all,
# because the payload is SOPS-encrypted and CI has no key.
#
# Named test-*, not check-*, so the pre-commit dispatcher does NOT pick it up.
#
# Every case here demonstrates the checker REJECTING a specific bad input, not
# merely passing on a good one. Case "leak control" is the important one: it
# proves mechanically that the YAML error path cannot echo Secret content, since
# str(yaml.YAMLError) was measured to embed the offending source line.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
checker="$repo_root/.github/hooks/check_authentik_blueprint.py"
hook="$repo_root/.github/hooks/check-authentik-blueprint.sh"
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
    # Fixture diagnostics only: every fixture value in this file is synthetic.
    sed 's/^/  /' "$output" >&2
  fi
}

run_checker() {
  : >"$output"
  python3 "$checker" --path "fixture/$1" <"$tmp/$1" >"$output" 2>&1
}

expect_pass() {
  if run_checker "$2"; then
    pass "$1"
  else
    fail "$1"
  fi
}

expect_fail() {
  if run_checker "$2"; then
    fail "$1"
  else
    pass "$1"
  fi
}

if ! python3 -c 'import yaml' >/dev/null 2>&1; then
  echo "ERROR: fixture suite requires PyYAML (.github/hooks/requirements.txt)" >&2
  exit 1
fi

# 1. Clean payload: a resolvable !KeyOf, a well-shaped !Find, an inert tag, and
#    an identifiers-only absent entry.
cat >"$tmp/clean.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: fixture-blueprint
stringData:
  fixture-blueprint.yaml: |
    version: 1
    metadata:
      name: fixture
    entries:
    - model: authentik_core.group
      id: fixture-group-a
      identifiers:
        name: Fixture Group A
    - model: authentik_core.user
      id: fixture-user
      identifiers:
        username: fixture-user
      attrs:
        name: !Format ["%s", "fixture"]
        groups:
        - !KeyOf fixture-group-a
        - !Find [authentik_core.group, [name, Fixture Group A]]
    - model: authentik_core.group
      id: fixture-group-retired
      state: absent
      identifiers:
        name: Fixture Group Retired
YAML
expect_pass "clean payload passes (resolvable !KeyOf, !Find, absent identifiers-only entry)" clean.yaml

# 2. #876 REPLAY: an entry inserted at the wrong indent inside the preceding
#    entry's mapping, so the parser hits a '-' where the block should end -
#    the exact ParserError #883 quotes. The offending line carries the canary
#    used by the leak control below, which only works because the canary sits
#    on the line the error mark points at.
cat >"$tmp/876-replay.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: fixture-blueprint
stringData:
  fixture-blueprint.yaml: |
    version: 1
    entries:
    - model: authentik_core.group
      id: fixture-group-a
      - model: CANARY-MUST-NOT-PRINT
        id: fixture-group-b
YAML
expect_fail "#876 replay: misindented entry item is rejected (expected <block end>, but found '-')" 876-replay.yaml

# 3. #940 REPLAY: valid YAML, !KeyOf pointing at a state:absent entry. This is
#    the control that proves layer 2 is not redundant with layer 1 - layer 1
#    parses this payload cleanly.
cat >"$tmp/940-replay.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: fixture-blueprint
stringData:
  fixture-blueprint.yaml: |
    version: 1
    entries:
    - model: authentik_core.group
      id: fixture-group-retired
      state: absent
      identifiers:
        name: Fixture Group Retired
    - model: authentik_core.user
      id: fixture-user
      identifiers:
        username: fixture-user
      attrs:
        groups:
        - !KeyOf fixture-group-retired
YAML
expect_fail "#940 replay: !KeyOf at a state:absent entry is rejected" 940-replay.yaml

# 3b. Same payload must parse clean under layer 1 alone, or case 3 proves
#     nothing about layer 2 being necessary.
: >"$output"
if python3 - "$tmp/940-replay.yaml" >"$output" 2>&1 <<'PY'
import sys, yaml
class L(yaml.SafeLoader): pass
for t in ('!Find','!KeyOf','!Context','!Format','!If','!Env','!Enumerate','!Value','!Index','!Condition'):
    L.add_constructor(t, lambda l, n: None)
doc = yaml.safe_load(open(sys.argv[1]))
payload = doc['stringData']['fixture-blueprint.yaml']
assert yaml.load(payload, Loader=L)['entries']
print("layer-1-only load succeeded, so layer 2 is what rejects this")
PY
then
  pass "layer 1 alone parses the #940 replay cleanly (so layer 2 is load-bearing)"
else
  fail "layer 1 alone parses the #940 replay cleanly (so layer 2 is load-bearing)"
fi

# 4. !KeyOf at an id no entry declares.
cat >"$tmp/dangling-keyof.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: fixture-blueprint
stringData:
  fixture-blueprint.yaml: |
    version: 1
    entries:
    - model: authentik_core.user
      id: fixture-user
      identifiers:
        username: fixture-user
      attrs:
        groups:
        - !KeyOf fixture-group-never-declared
YAML
expect_fail "!KeyOf at an id no entry declares is rejected" dangling-keyof.yaml

# 5. state:absent entry carrying attrs (the shape that silently skipped a
#    delete for 2 days, #940).
cat >"$tmp/absent-with-attrs.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: fixture-blueprint
stringData:
  fixture-blueprint.yaml: |
    version: 1
    entries:
    - model: authentik_core.group
      id: fixture-group-retired
      state: absent
      identifiers:
        name: Fixture Group Retired
      attrs:
        is_superuser: false
YAML
expect_fail "state:absent entry carrying attrs is rejected" absent-with-attrs.yaml

# 6. Well-shaped !Find whose (model, attr, value) matches a sibling entry that
#    is declared absent.
cat >"$tmp/find-absent-sibling.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: fixture-blueprint
stringData:
  fixture-blueprint.yaml: |
    version: 1
    entries:
    - model: authentik_core.group
      id: fixture-group-retired
      state: absent
      identifiers:
        name: Fixture Group Retired
    - model: authentik_core.user
      id: fixture-user
      identifiers:
        username: fixture-user
      attrs:
        groups:
        - !Find [authentik_core.group, [name, Fixture Group Retired]]
YAML
expect_fail "!Find matching a state:absent sibling entry is rejected" find-absent-sibling.yaml

# 7. !Find with the wrong shape - a 3-element list, and a bare scalar.
cat >"$tmp/find-three-elements.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: fixture-blueprint
stringData:
  fixture-blueprint.yaml: |
    version: 1
    entries:
    - model: authentik_core.user
      id: fixture-user
      identifiers:
        username: fixture-user
      attrs:
        groups:
        - !Find [authentik_core.group, [name, Fixture Group A], extra]
YAML
expect_fail "!Find as a 3-element list is rejected" find-three-elements.yaml

cat >"$tmp/find-scalar.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: fixture-blueprint
stringData:
  fixture-blueprint.yaml: |
    version: 1
    entries:
    - model: authentik_core.user
      id: fixture-user
      identifiers:
        username: fixture-user
      attrs:
        groups: !Find fixture-group-a
YAML
expect_fail "!Find as a bare scalar is rejected" find-scalar.yaml

# 8. A tag outside the ten registered ones. Deliberate: a new Authentik tag has
#    to be added to the checker on purpose, not silently ignored.
cat >"$tmp/unregistered-tag.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: fixture-blueprint
stringData:
  fixture-blueprint.yaml: |
    version: 1
    entries:
    - model: authentik_core.user
      id: fixture-user
      identifiers:
        username: fixture-user
      attrs:
        name: !Nope fixture
YAML
expect_fail "an unregistered tag is rejected" unregistered-tag.yaml

# 9. `entries:` present but empty.
cat >"$tmp/empty-entries.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: fixture-blueprint
stringData:
  fixture-blueprint.yaml: |
    version: 1
    entries: []
YAML
expect_fail "an empty entries list is rejected" empty-entries.yaml

# 10. LEAK CONTROL. The #876 fixture's broken line carries the scalar
#     CANARY-MUST-NOT-PRINT. str(yaml.YAMLError) embeds the offending source
#     line, which in production is decrypted Secret content, so the checker
#     must report e.problem and the mark only. If the canary shows up here, the
#     checker is echoing Secret material into the transcript (the #602 class).
run_checker 876-replay.yaml || true
if grep -qF -- 'CANARY-MUST-NOT-PRINT' "$output"; then
  fail "leak control: YAML error path echoed the offending source line"
else
  pass "leak control: YAML error path prints no payload content (canary absent)"
fi

# 11. Wrapper-level fail-closed: a staged *blueprint*.enc.yaml plus a sops that
#     exits non-zero (a locked KeePassXC, a missing age key) must FAIL the
#     check, not skip it. If this ever passes, the whole gate is theatre.
stub="$tmp/stub-bin"
mkdir -p "$stub"
printf '#!/usr/bin/env bash\necho "sops: cannot decrypt (fixture stub)" >&2\nexit 1\n' \
  >"$stub/sops"
chmod +x "$stub/sops"

repo="$tmp/repo"
mkdir -p "$repo/2-k3s/07.authentik-deployment"
git -C "$repo" init -q
git -C "$repo" config user.name "Synthetic Fixture"
git -C "$repo" config user.email "fixture.invalid@example.invalid"
cp "$tmp/clean.yaml" "$repo/2-k3s/07.authentik-deployment/fixture-blueprint.enc.yaml"
git -C "$repo" add -A

: >"$output"
if (cd "$repo" && PATH="$stub:$PATH" bash "$hook") >"$output" 2>&1; then
  fail "wrapper fails closed when sops exits non-zero"
else
  pass "wrapper fails closed when sops exits non-zero"
fi

# 12. Wrapper happy path with the same seam: a sops stub that emits the clean
#     fixture. Proves the wrapper is not simply always-failing, and that it
#     reads the STAGED blob rather than the worktree file.
printf '#!/usr/bin/env bash\ncat\n' >"$stub/sops"
chmod +x "$stub/sops"
: >"$output"
if (cd "$repo" && PATH="$stub:$PATH" bash "$hook") >"$output" 2>&1; then
  pass "wrapper passes a clean staged blueprint payload"
else
  fail "wrapper passes a clean staged blueprint payload"
fi

# 13. Nothing staged that matches the convention: the wrapper is a no-op.
git -C "$repo" commit -q --no-verify -m "fixture baseline"
: >"$output"
if (cd "$repo" && PATH="$stub:$PATH" bash "$hook") >"$output" 2>&1 && [ ! -s "$output" ]; then
  pass "no staged *blueprint*.enc.yaml is a silent no-op"
else
  fail "no staged *blueprint*.enc.yaml is a silent no-op"
fi

# The dispatcher runs `check-*.sh` only, so a gate named anything else silently
# never runs - and a fixture suite named check-* would run on every commit.
discovered="$(cd "$repo_root/.github/hooks" && echo check-*.sh)"
if printf '%s\n' "$discovered" | grep -qw 'check-authentik-blueprint.sh' \
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
