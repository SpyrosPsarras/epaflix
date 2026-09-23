#!/usr/bin/env bash
# Run after changing the shared inventory or image inputs, before committing.
# Rewrites the runtime image in the one/ StatefulSet.
set -euo pipefail
dir=$(cd "$(dirname "$0")" && pwd)
tag=$(bash "$dir/runtime-tag.sh")
python3 - "$tag" "$dir/../../one/statefulset.yaml" <<'PY'
import pathlib, re, sys
for arg in sys.argv[2:]:
    path = pathlib.Path(arg)
    text, count = re.subn(
        r"image: ghcr\.io/spyrospsarras/t3-runtime:[^\s]+",
        "image: ghcr.io/spyrospsarras/t3-runtime:inputs-" + sys.argv[1],
        path.read_text(),
    )
    assert count == 1, f"expected exactly one runtime image in {path}"
    path.write_text(text)
PY
