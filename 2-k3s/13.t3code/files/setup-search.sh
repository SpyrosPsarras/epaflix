#!/usr/bin/env bash
# Run as root after the KeePass MCP venv and provider CLIs are installed.
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
T3_USER=${T3_USER:-spyros}
install -m 0644 "$DIR/searxng_mcp.py" /opt/keepass-mcp/searxng_mcp.py
install -m 0755 "$DIR/searxng-mcp" /usr/local/bin/searxng-mcp
sudo -u "$T3_USER" -H python3 "$DIR/configure-search.py"
