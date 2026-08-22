#!/usr/bin/env bash
# Pre-commit hook: parse and resolve the Authentik blueprint payload nested
# inside a staged `*blueprint*.enc.yaml` Secret (#883, #876, #940).
#
# The name matches the `check-*.sh` glob in run-pre-commit.sh, so the
# dispatcher discovers it with no installer change.
#
# WHY A HOOK AND NOT CI. #883 asks "a CI step ... or the check moves to the
# pre-commit hook, which already has [the key]. Decide which." CI structurally
# cannot: it has no age key, as .github/workflows/ci.yml says twice - "full
# render needs the age key, withheld" (Kustomize build step) and "No age key
# needed: schema enforcement happens before any ksops decryption" (Helm pins
# step). The payload lives inside a SOPS-encrypted Secret, so CI cannot see it
# at all. CI runs the fixture suite instead (synthetic plaintext, no key), the
# same split ci.yml already uses for test-check-sops-encrypted.sh.
#
# Discovery is by filename: a blueprint Secret NOT named `*blueprint*.enc.yaml`
# is invisible to this check. Documented in
# 2-k3s/07.authentik-deployment/README.md.
#
# `set -euo pipefail` is the fail-closed contract: if sops cannot decrypt (a
# locked KeePassXC, a missing age key) the commit FAILS rather than skipping a
# check that then looks green. The plaintext is streamed from the index through
# sops into the checker - it never touches disk and is never echoed.
set -euo pipefail

self="${BASH_SOURCE[0]}"
self="$(readlink -f "$self" 2>/dev/null || echo "$self")"
here="$(cd "$(dirname "$self")" && pwd)"

staged="$(git diff --cached --name-only --diff-filter=ACMR \
  | grep -E '(^|/)[^/]*blueprint[^/]*\.enc\.yaml$' || true)"

if [ -z "$staged" ]; then
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import yaml' >/dev/null 2>&1; then
  echo "ERROR: check-authentik-blueprint requires python3 and PyYAML" >&2
  echo "       Install from .github/hooks/requirements.txt, then retry." >&2
  exit 1
fi

if ! command -v sops >/dev/null 2>&1; then
  echo "ERROR: check-authentik-blueprint requires sops to read the staged payload." >&2
  echo "       See .github/instructions/sops.instructions.md." >&2
  exit 1
fi

fail=0
while IFS= read -r path; do
  [ -n "$path" ] || continue
  echo "checking blueprint payload: $path"
  if ! git cat-file blob ":$path" \
    | sops -d --input-type yaml --output-type yaml /dev/stdin \
    | python3 "$here/check_authentik_blueprint.py" --path "$path"; then
    fail=1
    echo "       If the failure above is a decryption error, the age key is not" >&2
    echo "       available (KeePassXC locked?). This check fails closed on" >&2
    echo "       purpose - see .github/instructions/sops.instructions.md." >&2
  fi
done <<<"$staged"

exit $fail
