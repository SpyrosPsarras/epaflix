#!/usr/bin/env bash
# Batch regression for lingarr's translation path. Replays the exact request
# LocalAiService.TranslateBatchWithStructuredOutput sends (upstream lingarr
# 28f6a19: system = live ai_prompt, user = JSON array of {position,line},
# response_format = strict json_schema) against the LIVE cliproxy-free, N times.
#
# RED if any run fails batch_verdict.verdict(): wrapper shape, exactly the 25
# requested positions, every line non-empty and Greek-dominant. A 200 with the
# wrong shape or language is what made lingarr log "Failed to parse JSON
# response" and keep the original lines, so that is the assertion, not HTTP 200.
# `python3 batch_verdict.py --selftest` exercises the validator offline.
#
# Same key handling as verify.sh: a probe pod mounts the config secret, the
# scripts go in over stdin, the key never leaves the pod.
#
#   ./batch-repro.sh          # 3 runs against alias or-free
#   ./batch-repro.sh 5        # more runs
#   ALIAS=cand-x ./batch-repro.sh 3   # a different alias (canary configs)
set -euo pipefail

N=${1-3}   # default only when no argument; an explicit "" is rejected below
[[ "$N" =~ ^[1-9][0-9]*$ ]] || { echo "run count must be a positive integer, got '$N'" >&2; exit 2; }
ALIAS=${ALIAS:-or-free}
BASE=${BASE:-http://cliproxy-free.remote-pi.svc.cluster.local:8317}
SECRET=${SECRET:-cliproxy-free-config}
here=$(cd "$(dirname "$0")" && pwd)
pod="batch-repro-$RANDOM"

cleanup() { kubectl -n remote-pi delete pod "$pod" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT

kubectl -n remote-pi apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $pod
  namespace: remote-pi
  labels: { app.kubernetes.io/name: cliproxy-free-batch-repro }
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext: { runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000, seccompProfile: { type: RuntimeDefault } }
  containers:
    - name: probe
      image: docker.io/library/python:3.13-alpine
      command: ["sleep", "900"]
      securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] } }
      volumeMounts: [{ name: cfg, mountPath: /cfg, readOnly: true }]
      resources: { requests: { cpu: 10m, memory: 32Mi }, limits: { memory: 128Mi } }
  volumes:
    - name: cfg
      secret: { secretName: $SECRET }
EOF
kubectl -n remote-pi wait --for=condition=Ready "pod/$pod" --timeout=120s >/dev/null

# batch_verdict.py first, then the runner: one stdin stream, no files written in the pod.
{ cat "$here/batch_verdict.py"; cat <<'PY'; } | kubectl -n remote-pi exec -i "$pod" -- python3 - "$BASE" "$ALIAS" "$N"

# ---- runner (appended after batch_verdict.py) ----
import time, urllib.request, urllib.error
BASE, ALIAS, N = sys.argv[1], sys.argv[2], int(sys.argv[3])
cfg = open("/cfg/config.yaml").read()
KEY = re.findall(r'"(omp-[A-Za-z0-9._-]+)"', cfg)[0]
# The answering model must be one of the configured upstreams: the zero-cost
# pool is the policy, and a stray paid model answering under the alias would
# be the exact failure this check exists to catch. Same two-level parse as
# verify.sh: provider entry names at two-space indent, model names at six.
pairs, provider, in_models = [], None, False
for line in cfg.splitlines():
    m = re.match(r'^ {2}- name: "([^"]+)"$', line)
    if m:
        provider, in_models = m.group(1), False
        continue
    if provider and line.strip() == "models:":
        in_models = True
        continue
    m = re.match(r'^ {6}- name: "([^"]+)"$', line)
    if m and provider and in_models:
        pairs.append((provider, m.group(1)))
UPSTREAMS = [model for _, model in pairs]
assert UPSTREAMS, f"no upstream models parsed from config (pairs={pairs})"

# The live ai_prompt (settings.ai_prompt, 2026-09-13) with {sourceLanguage}/{targetLanguage} filled.
SYSTEM = ("You are a professional subtitle translator. Translate the line from en to el. Output ONLY the translated text, "
          "in the target language script. No explanations, no transliterations, no parenthetical notes, no English, no commentary, "
          "no quotes. Preserve punctuation and tone. If it is a name or proper noun that should not be translated, keep it as-is. "
          "Reply with only the translated line.")
schema = {"type": "json_schema", "json_schema": {"name": "batch_translation_response", "strict": True, "schema": {
    "type": "object", "properties": {"translations": {"type": "array", "items": {"type": "object", "properties": {
        "position": {"type": "integer", "description": "Position number of the subtitle item"},
        "line": {"type": "string", "description": "Translated subtitle text"}},
        "required": ["position", "line"], "additionalProperties": False}}},
    "required": ["translations"], "additionalProperties": False}}}
body = {"model": ALIAS, "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(BATCH)}],
        "response_format": schema}

red = 0
for i in range(N):
    t0 = time.time()
    req = urllib.request.Request(BASE + "/v1/chat/completions", data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {KEY}", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r: st, resp = r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: st, resp = e.code, {"raw": e.read()[:160].decode(errors="replace")}
    except Exception as e: st, resp = -1, {"raw": repr(e)[:160]}
    dt = time.time() - t0
    model = resp.get("model", "?"); ch = (resp.get("choices") or [{}])[0]
    content = ((ch.get("message") or {}).get("content")) or ""
    v = verdict(content) if st == 200 else f"http-{st}:{resp.get('raw','')}"
    if model not in UPSTREAMS: v += " NON-FREE-MODEL"
    if v != "OK": red += 1
    sample = (f'| 50: {json.loads(content)["translations"][0]["line"]!r}' if v == "OK"
              else f"| {content[:80].replace(chr(10),' ')!r}")
    print(f"{ALIAS} run {i+1}: {st} {dt:5.1f}s model={model} finish={ch.get('finish_reason')} -> {v} {sample}".replace(KEY, "<key>"), flush=True)
print(f"RESULT {ALIAS}: {N-red}/{N} OK, {red} RED")
sys.exit(1 if red else 0)
PY
