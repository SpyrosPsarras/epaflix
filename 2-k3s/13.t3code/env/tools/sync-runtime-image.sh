#!/usr/bin/env bash
# Run after changing the shared inventory or image inputs, before committing.
set -euo pipefail
dir=$(cd "$(dirname "$0")" && pwd)
tag=$(bash "$dir/runtime-tag.sh")
python3 - "$dir/../statefulset.yaml" "$tag" <<'PY'
import pathlib, re, sys
path = pathlib.Path(sys.argv[1])
text, count = re.subn(
    r"image: ghcr\.io/spyrospsarras/t3-runtime:[^\s]+",
    "image: ghcr.io/spyrospsarras/t3-runtime:inputs-" + sys.argv[2],
    path.read_text(),
)
assert count == 1, "expected exactly one runtime image"
path.write_text(text)
PY
