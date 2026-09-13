#!/usr/bin/env bash
# Regenerates env/tools/package.json + package-lock.json from ../../versions.env
# so the init container can `npm ci` the exact same tree every start.
# Run after Renovate bumps versions.env (no network install, lock only).
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=2-k3s/13.t3code/versions.env
. "$DIR/../../versions.env"
cat >"$DIR/package.json" <<EOF
{
  "name": "t3env-tools",
  "private": true,
  "description": "Pinned CLI set for the t3env pods; regenerate with regen-lock.sh",
  "dependencies": {
    "t3": "$T3_VERSION",
    "@anthropic-ai/claude-code": "$CLAUDE_CODE_VERSION",
    "opencode-ai": "$OPENCODE_VERSION",
    "@openai/codex": "$CODEX_VERSION"
  }
}
EOF
(cd "$DIR" && npm install --package-lock-only --ignore-scripts --no-fund --no-audit)
echo "lock: $(wc -c <"$DIR/package-lock.json") bytes"
