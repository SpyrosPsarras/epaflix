#!/usr/bin/env bash
# Run after changing the shared inventory or image inputs, before committing.
# Rewrites each runtime variant in its StatefulSet.
set -euo pipefail
dir=$(cd "$(dirname "$0")" && pwd)
for variant in mixed homelab; do
  overlay=one
  [[ $variant != homelab ]] || overlay=../13.t3code-homelab
  tag=$(bash "$dir/runtime-tag.sh" "$variant")
  prefix=inputs
  [[ $variant != homelab ]] || prefix=homelab-inputs
python3 - "$prefix-$tag" "$dir/../../$overlay/statefulset.yaml" <<'PY'
import pathlib, re, sys
for arg in sys.argv[2:]:
    path = pathlib.Path(arg)
    text, count = re.subn(
        r"image: ghcr\.io/spyrospsarras/t3-runtime:[^\s]+",
        "image: ghcr.io/spyrospsarras/t3-runtime:" + sys.argv[1],
        path.read_text(),
    )
    assert count == 1, f"expected exactly one runtime image in {path}"
    path.write_text(text)
PY
done
