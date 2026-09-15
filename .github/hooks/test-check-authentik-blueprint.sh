#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
checker="$repo_root/2-k3s/07.authentik-deployment/files/check_authentik_blueprint.py"
tmp="$(mktemp -d)"
output="$(mktemp)"
stub_pid=""
trap 'rm -rf "$tmp"; rm -f "$output"; [ -n "$stub_pid" ] && kill "$stub_pid" 2>/dev/null' EXIT

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
  local fixture="$1"
  shift
  : >"$output"
  python3 "$checker" --path "fixture/$fixture" "$@" <"$tmp/$fixture" >"$output" 2>&1
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

# ---- Layer 3: the server dry-run (#1103) ---------------------------------
#
# A stub HTTP server stands in for authentik's validate endpoint. The layer-3
# fixture payload carries its own ak-blueprint-check-token entry, whose key
# doubles as the leak canary: the stub echoes it back inside a failing log
# event and the response's attributes, and the checker output must never
# contain it.

cat >"$tmp/validate-stub.py" <<'PY'
"""Stub authentik validate endpoint; argv: mode, record-file, port."""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

MODE, RECORD, PORT = sys.argv[1], sys.argv[2], int(sys.argv[3])
CANARY = "FIXTURE-CHECK-TOKEN-0000000000000000000000000000000000000000"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        with open(RECORD, "wb") as fh:
            fh.write(f"{self.headers.get('Authorization', '')}\n".encode())
            fh.write(f"{self.headers.get('Content-Type', '')}\n".encode())
            fh.write(body)
        if MODE == "success":
            out, code = {"success": True, "imported": False, "logs": []}, 200
        elif MODE == "invalid":
            out, code = {
                "success": False,
                "imported": False,
                "logs": [
                    {
                        "timestamp": "2026-09-15T00:00:00Z",
                        "log_level": "warning",
                        "logger": "authentik.blueprints.v1.importer",
                        "event": "Entry invalid: Serializer errors "
                        "{'attrs': {'name': ['Fixture Group A is not "
                        f"valid']}} {CANARY}",
                        "attributes": {
                            "entry": {"attrs": {"key": CANARY}},
                            "error": {"detail": CANARY},
                        },
                    }
                ],
            }, 200
        elif MODE == "forbidden":
            out, code = {
                "detail": {
                    "fixture-user": "User lacks permission to create "
                    "authentik_core.user"
                }
            }, 403
        elif MODE == "notallowed":
            out, code = {"detail": 'Method "POST" not allowed.'}, 405
        elif MODE == "notfound":
            out, code = {"detail": "Not Found."}, 404
        else:
            out, code = {"detail": "boom"}, 500
        payload = json.dumps(out).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
PY

cat >"$tmp/clean-check.yaml" <<'YAML'
apiVersion: v1
kind: Secret
metadata:
  name: fixture-blueprint
stringData:
  fixture-blueprint.yaml: |
    version: 1
    metadata:
      name: fixture-check
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
        groups:
        - !KeyOf fixture-group-a
    - model: authentik_core.user
      id: fixture-check-user
      identifiers:
        username: ak-blueprint-check
      attrs:
        name: Blueprint Check
        type: service_account
        path: users/service-accounts
        is_active: true
    - model: authentik_core.token
      id: ak-blueprint-check-token
      identifiers:
        identifier: ak-blueprint-check-token
      attrs:
        user: !KeyOf fixture-check-user
        intent: api
        key: FIXTURE-CHECK-TOKEN-0000000000000000000000000000000000000000
        expiring: false
YAML

free_port() {
  python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}

stop_stub() {
  [ -n "$stub_pid" ] || return 0
  kill "$stub_pid" 2>/dev/null || true
  wait "$stub_pid" 2>/dev/null || true
  stub_pid=""
}

start_stub() { # $1=mode $2=record-file -> echoes the port
  local mode="$1" record="$2" port
  port="$(free_port)"
  # stdout/stderr must not leak into the command substitution that captures
  # the port: a background child holding the pipe open blocks $(start_stub).
  python3 "$tmp/validate-stub.py" "$mode" "$record" "$port" >/dev/null 2>&1 &
  stub_pid=$!
  local ready=0
  for _ in $(seq 1 50); do
    if python3 -c "import socket; socket.create_connection(('127.0.0.1', $port), timeout=0.2)" 2>/dev/null; then
      ready=1
      break
    fi
    sleep 0.1
  done
  if [ "$ready" -ne 1 ]; then
    stop_stub
    fail "stub ($mode) never became reachable - test infrastructure broken, not the checker"
    return 1
  fi
  echo "$port"
}

record="$tmp/stub-record.txt"
stub_port="$(start_stub success "$record")"
if run_checker clean-check.yaml \
    --validate-url "http://127.0.0.1:$stub_port/api/v3/managed/blueprints/validate/"; then
  pass "layer 3: a 200 success:true response passes the run"
else
  fail "layer 3: a 200 success:true response passes the run"
fi
if grep -qF "server dry-run: passed" "$output" \
    && grep -qF 'name="file"' "$record" \
    && grep -qF 'filename="fixture-blueprint.yaml"' "$record" \
    && grep -qF "version: 1" "$record" \
    && grep -qF "Bearer FIXTURE-CHECK-TOKEN" "$record"; then
  pass "layer 3: the payload and its bearer token are POSTed as multipart"
else
  fail "layer 3: the payload and its bearer token are POSTed as multipart"
fi
stop_stub

stub_port="$(start_stub invalid "$record")"
if run_checker clean-check.yaml \
    --validate-url "http://127.0.0.1:$stub_port/api/v3/managed/blueprints/validate/"; then
  fail "layer 3: a success:false response fails the run"
else
  pass "layer 3: a success:false response fails the run"
fi
if grep -qF "Serializer errors" "$output" \
    && ! grep -qF "FIXTURE-CHECK-TOKEN" "$output" \
    && ! grep -qF "Fixture Group A" "$output" \
    && ! grep -qF "attributes" "$output"; then
  pass "layer 3: failing log events are readable but every payload value is redacted"
else
  fail "layer 3: failing log events are readable but every payload value is redacted"
fi
stop_stub

stub_port="$(start_stub notallowed "$record")"
if run_checker clean-check.yaml \
    --validate-url "http://127.0.0.1:$stub_port/api/v3/managed/blueprints/validate/"; then
  pass "layer 3: a 405 from an authentik without the validate endpoint exits 0"
else
  fail "layer 3: a 405 from an authentik without the validate endpoint exits 0"
fi
if grep -qF "server dry-run inactive" "$output"; then
  pass "layer 3: the inactive note names the reason (stated gap, not silent)"
else
  fail "layer 3: the inactive note names the reason (stated gap, not silent)"
fi
stop_stub

# 404 is a violation, not a stated gap: an endpointless authentik answers 405
# (the route falls through to the detail path), so 404 means the URL is wrong.
stub_port="$(start_stub notfound "$record")"
if run_checker clean-check.yaml \
    --validate-url "http://127.0.0.1:$stub_port/api/v3/managed/blueprints/validate/"; then
  fail "layer 3: a 404 (wrong URL) fails the run instead of reporting inactive"
else
  pass "layer 3: a 404 (wrong URL) fails the run instead of reporting inactive"
fi
stop_stub

stub_port="$(start_stub forbidden "$record")"
if run_checker clean-check.yaml \
    --validate-url "http://127.0.0.1:$stub_port/api/v3/managed/blueprints/validate/"; then
  fail "layer 3: a 403 from check_blueprint_perms fails the run"
else
  pass "layer 3: a 403 from check_blueprint_perms fails the run"
fi
if grep -qF "authentik_core.user" "$output"; then
  pass "layer 3: the 403 detail names the model the credential lacks"
else
  fail "layer 3: the 403 detail names the model the credential lacks"
fi
stop_stub

stub_port="$(start_stub boom "$record")"
if run_checker clean-check.yaml \
    --validate-url "http://127.0.0.1:$stub_port/api/v3/managed/blueprints/validate/"; then
  fail "layer 3: an unexpected 500 fails the run"
else
  pass "layer 3: an unexpected 500 fails the run"
fi
stop_stub

# A dead port must fail the run: the server being unreachable at check time
# is signal, not noise.
dead_port="$(free_port)"
if run_checker clean-check.yaml \
    --validate-url "http://127.0.0.1:$dead_port/api/v3/managed/blueprints/validate/"; then
  fail "layer 3: an unreachable server fails the run"
else
  pass "layer 3: an unreachable server fails the run"
fi

# No check token in the payload -> layer 3 stays inactive without contacting
# the server. The stub answers 500, so any POST attempt would fail the run.
cat >"$tmp/no-token.yaml" <<'YAML'
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
      identifiers:
        name: Fixture Group A
YAML
stub_port="$(start_stub boom "$record")"
if run_checker no-token.yaml \
    --validate-url "http://127.0.0.1:$stub_port/api/v3/managed/blueprints/validate/"; then
  pass "layer 3: a payload without the check token exits 0 without contacting the server"
else
  fail "layer 3: a payload without the check token exits 0 without contacting the server"
fi
if grep -qF "server dry-run inactive" "$output" \
    && ! grep -qF "boom" "$record"; then
  pass "layer 3: the inactive note reports the missing credential"
else
  fail "layer 3: the inactive note reports the missing credential"
fi
stop_stub

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
