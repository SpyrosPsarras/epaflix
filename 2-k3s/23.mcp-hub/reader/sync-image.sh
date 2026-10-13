#!/usr/bin/env bash
set -euo pipefail
dir=$(cd "$(dirname "$0")" && pwd)
tag=$(bash "$dir/image-tag.sh")
python3 - "$dir/homelab/deployment.yaml" "$tag" <<'PY'
from pathlib import Path
import re
import sys
path = Path(sys.argv[1])
text, count = re.subn(r'image: ghcr\.io/spyrospsarras/vault-reader:[^\s]+',
                      'image: ghcr.io/spyrospsarras/vault-reader:inputs-' + sys.argv[2], path.read_text())
assert count == 1
path.write_text(text)
PY
