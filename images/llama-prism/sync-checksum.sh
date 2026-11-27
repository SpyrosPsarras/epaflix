#!/usr/bin/env bash
# Renovate runs this after it bumps PRISM_RELEASE: it copies the release asset's
# SHA-256 from GitHub's release API into PRISM_SHA256.
set -euo pipefail
dockerfile="$(cd "$(dirname "$0")" && pwd)/Dockerfile"
python3 - "$dockerfile" "${LLAMA_PRISM_API:-https://api.github.com}" <<'PY'
import json, re, sys, urllib.request
path, api = sys.argv[1], sys.argv[2]
text = open(path).read()
release = re.search(r"^ARG PRISM_RELEASE=(prism-b\d+-[0-9a-f]+)$", text, re.M).group(1)
asset = f"llama-{release}-bin-linux-cuda-12.8-x64.tar.gz"
with urllib.request.urlopen(f"{api}/repos/PrismML-Eng/llama.cpp/releases/tags/{release}", timeout=30) as response:
    assets = json.load(response)["assets"]
digest = next(a["digest"] for a in assets if a["name"] == asset)
assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest), f"unexpected digest for {asset}: {digest}"
text, count = re.subn(r"^ARG PRISM_SHA256=.*$", "ARG PRISM_SHA256=" + digest.removeprefix("sha256:"), text, flags=re.M)
assert count == 1, "expected exactly one PRISM_SHA256"
open(path, "w").write(text)
PY
