#!/usr/bin/env bash
# Secret guard entry point.
#
# Default mode validates staged YAML blobs from Git's index.  --full-tree
# validates every tracked YAML blob for CI.  The structured checker accepts
# plaintext Secret templates only when sensitive keys contain exact approved
# placeholders and no scalar has a credential-like shape.  It validates each
# SOPS document's canonical AES-GCM envelopes and age metadata, so a stub
# `sops:` mapping cannot bless plaintext or another document.
#
# This is not a general secret scanner.  Entropy detection covers internal
# whitespace and printable Unicode but still has false negatives: a short
# low-entropy credential under a non-sensitive key can pass.  Sensitive scalar
# keys remain hard-gated even when their names end in name/ref/reference/id;
# only constrained structural references (including pve.yml's paired token
# identifier/value fields) are semantic identifiers. Keep templates
# content-classified and freely editable; do not replace this with
# per-file key allowlists.
#
# Wire up via .github/hooks/install-hooks.sh (one-shot).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import yaml' >/dev/null 2>&1; then
  echo "ERROR: check-sops-encrypted requires python3 and PyYAML" >&2
  exit 1
fi

exec python3 "$here/check_sops_encrypted.py" "$@"
