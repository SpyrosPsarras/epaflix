#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
checker="$repo_root/2-k3s/07.authentik-deployment/files/check_authentik_blueprint.py"
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

run_checker 876-replay.yaml || true
if grep -qF -- 'CANARY-MUST-NOT-PRINT' "$output"; then
  fail "leak control: YAML error path echoed the offending source line"
else
  pass "leak control: YAML error path prints no payload content (canary absent)"
fi

python3 - "$tmp/clean.yaml" "$tmp/api-shape.yaml" <<'PY'
import base64, json, sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
payload = doc["stringData"]["fixture-blueprint.yaml"]
api = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"name": "fixture-blueprint", "namespace": "fixture-ns"},
    "type": "Opaque",
    "data": {
        "fixture-blueprint.yaml": base64.b64encode(payload.encode()).decode(),
    },
}
open(sys.argv[2], "w").write(json.dumps(api))
PY
expect_pass "kube-API Secret shape (base64 data, what the CronJob pipes in) is checked" api-shape.yaml

python3 - "$tmp/940-replay.yaml" "$tmp/api-shape-broken.yaml" <<'PY'
import base64, json, sys, yaml
doc = yaml.safe_load(open(sys.argv[1]))
payload = doc["stringData"]["fixture-blueprint.yaml"]
api = {
    "apiVersion": "v1",
    "kind": "Secret",
    "metadata": {"name": "fixture-blueprint", "namespace": "fixture-ns"},
    "type": "Opaque",
    "data": {
        "fixture-blueprint.yaml": base64.b64encode(payload.encode()).decode(),
    },
}
open(sys.argv[2], "w").write(json.dumps(api))
PY
expect_fail "kube-API Secret shape with the #940 replay inside is rejected" api-shape-broken.yaml

cat >"$tmp/no-payload.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: fixture-blueprint
stringData:
  not-a-blueprint.txt: |
    version: 1
YAML
expect_fail "a Secret with no .yaml/.yml payload key is refused, not passed vacuously" no-payload.yaml

discovered="$(cd "$repo_root/.github/hooks" && echo check-*.sh)"
if ! printf '%s\n' "$discovered" | grep -q 'blueprint'; then
  pass "no blueprint check in the pre-commit dispatcher glob (commit path is KeePassXC-free)"
else
  fail "no blueprint check in the pre-commit dispatcher glob (commit path is KeePassXC-free)"
fi

kustomization="$repo_root/2-k3s/07.authentik-deployment/kustomization.yaml"
if grep -qF 'files/check_authentik_blueprint.py' "$kustomization"; then
  pass "the CronJob ConfigMap is generated from the same file this suite runs"
else
  fail "the CronJob ConfigMap is generated from the same file this suite runs"
fi

printf '%s\n' "1..$((pass_count + fail_count))"
if [ "$fail_count" -ne 0 ]; then
  printf '%s fixture test(s) failed\n' "$fail_count" >&2
  exit 1
fi
printf 'All %s fixture tests passed.\n' "$pass_count"
