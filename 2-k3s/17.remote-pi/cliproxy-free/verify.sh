#!/usr/bin/env bash
# Prove the free-only policy against the LIVE cliproxy-free service. Exit 1 on
# the first failed check.
#
# The client key never appears in any argv, env, or on this shell's stdout. One
# probe pod in remote-pi mounts the cliproxy-free-config secret read-only; the
# checks are a Python script piped to it over `kubectl exec -i`. Python reads
# the key from the mounted file, makes the calls with urllib, parses JSON
# properly, and prints only the asserted fields (key redacted).
#
#   ./verify.sh                 # all checks, one real OpenRouter call
#   ./verify.sh --no-upstream   # skip the call that reaches OpenRouter
set -euo pipefail

call_upstream=1; [[ "${1:-}" == "--no-upstream" ]] && call_upstream=0
pod="verify-free-$RANDOM"

cleanup() { kubectl -n remote-pi delete pod "$pod" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT

kubectl -n remote-pi apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $pod
  namespace: remote-pi
  labels: { app.kubernetes.io/name: cliproxy-free-verify }
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext: { runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000, seccompProfile: { type: RuntimeDefault } }
  containers:
    - name: probe
      image: docker.io/library/python:3.13-alpine
      command: ["sleep", "600"]
      securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } }
      volumeMounts: [{ name: cfg, mountPath: /cfg, readOnly: true }]
      resources: { requests: { cpu: 10m, memory: 32Mi }, limits: { memory: 128Mi } }
  volumes:
    - name: cfg
      secret: { secretName: cliproxy-free-config }
EOF
kubectl -n remote-pi wait --for=condition=Ready "pod/$pod" --timeout=120s >/dev/null

kubectl -n remote-pi exec -i "$pod" -- python3 - "$call_upstream" <<'PY'
import json, re, sys, urllib.request, urllib.error

FREE = "http://cliproxy-free.remote-pi.svc.cluster.local:8317"
MAIN = "http://cliproxy.remote-pi.svc.cluster.local:8317"
call_upstream = sys.argv[1] == "1"

# api-keys line shape is fixed by config.template.yaml: `  - "omp-..."`.
cfg = open("/cfg/config.yaml").read()
keys = re.findall(r'^\s*-\s*"(omp-[A-Za-z0-9._-]+)"\s*$', cfg, re.M)
assert len(keys) == 1, f"expected exactly one omp- api-key in config, found {len(keys)}"
KEY = keys[0]

def redact(s): return str(s).replace(KEY, "<key>")

def call(base, path, body=None, timeout=90):
    req = urllib.request.Request(base + path, method="POST" if body is not None else "GET",
                                 headers={"Authorization": f"Bearer {KEY}", "content-type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try: return e.code, json.loads(raw)
        except Exception: return e.code, {"raw": raw[:200].decode(errors="replace")}

def ok(msg): print("ok  ", redact(msg))
def fail(msg): print("FAIL", redact(msg)); sys.exit(1)

# 1. catalog: exactly one model, the free alias
st, models = call(FREE, "/v1/models")
ids = sorted(m["id"] for m in models.get("data", []))
if st != 200 or ids != ["or-free"]: fail(f"catalog -> {st} {ids}")
ok("catalog -> ['or-free'] only")

# 2. paid / prefixed / bare-upstream IDs refused before OpenRouter
for m in ["or-glm-5.3", "openrouter/or-free", "minimax/minimax-m3", "anthropic/claude-sonnet-4.5", "openrouter/free"]:
    st, body = call(FREE, "/v1/chat/completions", {"model": m, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1})
    code = (body.get("error") or {}).get("code")
    if st not in (400, 404) or code != "model_not_found": fail(f"reject model={m} -> {st} {body}")
    ok(f"reject model={m} -> {st} {code}")

# 3. the main instance does not know this key
st, body = call(MAIN, "/v1/models")
if st != 401: fail(f"main instance -> {st} {body}")
ok(f"main instance -> 401 {body}")

# 4. one translation-shaped call carrying an escape attempt: OpenRouter
#    `models` fallback array and `provider.order`. Both must be stripped by
#    payload.filter, the answering model must be :free, and the content must
#    be a non-empty translation.
if call_upstream:
    st, body = call(FREE, "/v1/chat/completions", {
        "model": "or-free",
        "models": ["anthropic/claude-sonnet-4.5"],
        "provider": {"order": ["anthropic"], "allow_fallbacks": False},
        "messages": [{"role": "user", "content": "Translate to Greek. Reply with the Greek translation only, no explanation: Good morning, how are you?"}],
        "max_tokens": 60})
    if st != 200: fail(f"upstream -> {st} {body}")
    answered = body.get("model", "")
    if not (answered.endswith(":free") or answered == "openrouter/free"): fail(f"upstream answered by non-free model {answered!r}")
    content = ((body.get("choices") or [{}])[0].get("message") or {}).get("content")
    if not isinstance(content, str) or not content.strip(): fail(f"empty content from {answered}: {body}")
    if not re.search(r"[\u0370-\u03FF]", content): fail(f"content has no Greek letters: {content!r}")
    ok(f"upstream answered by {answered}")
    ok(f"translation: {content.strip()[:120]!r}")

print("PASS")
PY
