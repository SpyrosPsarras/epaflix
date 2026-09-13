#!/usr/bin/env bash
# Render config.template.yaml into two sops secrets, plaintext never on disk
# and never in a process argv:
#   cliproxy-free/config.enc.yaml                                    (this instance's config)
#   ../08.servarr/_shared/secrets/lingarr-cliproxy-api-key.enc.yaml  (lingarr's copy of the client key)
#
# Inputs (env only):
#   OPENROUTER_API_KEY  required. Reuse the main instance's credential from
#                       cliproxy/cliproxy-secrets.enc.yaml (stringData.openrouter-api-key).
#   LINGARR_API_KEY     optional. Omitted = generate a new one (rotation).
#
# Both outputs are encrypted to temp files first; the destinations are replaced
# only after BOTH encryptions succeeded, so a failure anywhere leaves the two
# committed secrets untouched and still agreeing on the key. Prints the two
# output paths, nothing else.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
config_out="$here/config.enc.yaml"
lingarr_out="$here/../../08.servarr/_shared/secrets/lingarr-cliproxy-api-key.enc.yaml"

: "${OPENROUTER_API_KEY:?set OPENROUTER_API_KEY}"
# Shape must satisfy the lingarr boot guard (reconcile-ai-provider.sql: ^omp-[A-Za-z0-9._-]+$).
export LINGARR_API_KEY="${LINGARR_API_KEY:-omp-lingarr-$(openssl rand -hex 24)}"
export TEMPLATE="$here/config.template.yaml"

config_tmp=$(mktemp "$config_out.XXXXXX")
lingarr_tmp=$(mktemp "$lingarr_out.XXXXXX")
trap 'rm -f "$config_tmp" "$lingarr_tmp"' EXIT

# Python reads both secrets from its environment, validates their shape (which
# doubles as YAML injection defence: the values land inside double quotes) and
# emits the Secret manifest on stdout for sops.
python3 - <<'PY' | sops -e --filename-override "$config_out" /dev/stdin > "$config_tmp"
import os, re, sys, yaml
ork, lk = os.environ["OPENROUTER_API_KEY"], os.environ["LINGARR_API_KEY"]
if not re.fullmatch(r"sk-or-v1-[A-Za-z0-9._-]+", ork): sys.exit("OPENROUTER_API_KEY is not sk-or-v1- shaped")
if not re.fullmatch(r"omp-[A-Za-z0-9._-]+", lk): sys.exit("LINGARR_API_KEY is not omp- shaped")
cfg = open(os.environ["TEMPLATE"]).read().replace("__LINGARR_API_KEY__", lk).replace("__OPENROUTER_API_KEY__", ork)
yaml.safe_load(cfg)  # must still parse
print(yaml.safe_dump({
    "apiVersion": "v1", "kind": "Secret",
    "metadata": {"name": "cliproxy-free-config", "namespace": "remote-pi",
                 "labels": {"app.kubernetes.io/name": "cliproxy-free", "app.kubernetes.io/component": "proxy"},
                 "annotations": {"argocd.argoproj.io/sync-wave": "-2"}},
    "type": "Opaque",
    "stringData": {"config.yaml": cfg},
}, sort_keys=False), end="")
PY

python3 - <<'PY' | sops -e --filename-override "$lingarr_out" /dev/stdin > "$lingarr_tmp"
import os, yaml
print(yaml.safe_dump({
    "apiVersion": "v1", "kind": "Secret",
    "metadata": {"name": "lingarr-cliproxy-api-key", "namespace": "servarr"},
    "type": "Opaque",
    "stringData": {"omp-api-key": os.environ["LINGARR_API_KEY"]},
}, sort_keys=False), end="")
PY

# Both succeeded (set -e and pipefail would have exited above otherwise).
mv -f "$config_tmp" "$config_out"
mv -f "$lingarr_tmp" "$lingarr_out"
echo "$config_out"
echo "$lingarr_out"
