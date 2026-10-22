#!/usr/bin/env bash
set -euo pipefail

SSH_DIR=$(getent passwd "$(id -u)" | cut -d: -f6)/.ssh
mkdir -m 0700 "$SSH_DIR" 2>/dev/null || chmod 0700 "$SSH_DIR"
mkdir -p -m 0700 "$HOME/.ssh"
