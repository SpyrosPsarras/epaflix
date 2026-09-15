#!/usr/bin/env bash
# Brings the guest to versions.env and env/tools/package.json. Run as root.
# A stamp of the last applied pins makes polling a no-op
# unless Renovate changed a pin. Restarts t3code.service when t3 changed.
set -euo pipefail

DIR=$(cd "$(dirname "$0")" && pwd)
T3_USER=${T3_USER:-spyros}
STAMP=/var/lib/t3code/versions.applied
PKG=$DIR/env/tools/package.json
. "$DIR/versions.env"
npm_pin() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["dependencies"][sys.argv[2]])' "$PKG" "$1"; }
T3_VERSION=$(npm_pin t3)
CLAUDE_CODE_VERSION=$(npm_pin @anthropic-ai/claude-code)
OPENCODE_VERSION=$(npm_pin opencode-ai)
CODEX_VERSION=$(npm_pin @openai/codex)
# Preserve KEY=value stamps so previous T3 versions still parse after migration.
pins() {
  cat "$DIR/versions.env"
  printf 'OS_PACKAGES_SHA256=%s\n' "$(sha256sum "$DIR/env/tools/os-packages.txt" | cut -d ' ' -f 1)"
  printf 'T3_VERSION=%s\nCLAUDE_CODE_VERSION=%s\nOPENCODE_VERSION=%s\nCODEX_VERSION=%s\n' \
    "$T3_VERSION" "$CLAUDE_CODE_VERSION" "$OPENCODE_VERSION" "$CODEX_VERSION"
}

# Installs the current OpenCode catalog hook and preserves user configuration.
# Run before the version stamp check so hook updates do not require a tool bump.
# The hook fetches subscription-scoped models when OpenCode loads a workspace.
if [[ -f /etc/t3code/t3code.env ]] && id "$T3_USER" >/dev/null 2>&1; then
  sudo -u "$T3_USER" -H python3 "$DIR/files/oc-config.py" \
    || echo "OpenCode configuration refresh failed - keeping last-known opencode.json" >&2
fi

if [[ -x /opt/keepass-mcp/bin/python ]]; then
  bash "$DIR/files/setup-search.sh"
fi

if [[ -f $STAMP ]] && cmp -s "$STAMP" <(pins); then
  echo "t3code already at pinned versions"
  exit 0
fi
prev_t3=$(sed -n 's/^T3_VERSION=\([^ ]*\).*/\1/p' "$STAMP" 2>/dev/null || true)

npm install -g "t3@$T3_VERSION" "@anthropic-ai/claude-code@$CLAUDE_CODE_VERSION" "opencode-ai@$OPENCODE_VERSION" "@openai/codex@$CODEX_VERSION"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
T3_VERSIONS_FILE="$DIR/versions.env" bash "$DIR/env/tools/install-cluster-tools.sh"

apt-get update -q >/dev/null
DEBIAN_FRONTEND=noninteractive xargs -a "$DIR/env/tools/os-packages.txt" apt-get install -y -q kubectl >/dev/null

if [[ $prev_t3 != "$T3_VERSION" ]] && id "$T3_USER" >/dev/null 2>&1; then
  sudo -u "$T3_USER" -H env XDG_RUNTIME_DIR="/run/user/$(id -u "$T3_USER")" \
    t3 service update
  echo "t3 ${prev_t3:-none} -> $T3_VERSION, service updated"
fi

# A failed service update must be retried rather than stamped as applied.
install -d -m 0755 /var/lib/t3code
pins >"$tmp/stamp"
install -m 0644 "$tmp/stamp" "$STAMP"

echo "t3code at: t3=$T3_VERSION claude=$CLAUDE_CODE_VERSION opencode=$OPENCODE_VERSION codex=$CODEX_VERSION helm=$HELM_VERSION kustomize=$KUSTOMIZE_VERSION argocd=$ARGOCD_VERSION sops=$SOPS_VERSION"
