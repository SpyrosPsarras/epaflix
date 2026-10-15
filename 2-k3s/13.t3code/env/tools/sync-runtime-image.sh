#!/usr/bin/env bash
# Run after changing the shared inventory or image inputs, before committing.
set -euo pipefail
dir=$(cd "$(dirname "$0")" && pwd)
for variant in mixed homelab work; do
  overlay=one
  [[ $variant == mixed ]] || overlay=../13.t3code-$variant
  tag=$(bash "$dir/runtime-tag.sh" "$variant")
  prefix=inputs
  [[ $variant == mixed ]] || prefix=$variant-inputs
  paths=("$dir/../../$overlay/statefulset.yaml")
python3 - "$prefix-$tag" "${paths[@]}" <<'PY'
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
