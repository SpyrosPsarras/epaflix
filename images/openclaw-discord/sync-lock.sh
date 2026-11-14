#!/usr/bin/env bash
# Renovate post-upgrade task: rebuild package-lock.json with the base image's npm, so the image build's lock check passes.
# shortcut: npm is pinned by hand to the base image's npm (12.2.2 in openclaw 2026.11.3); update it when a base image bump changes npm (the build's lock check fails loudly if missed).
# npm 12 counts the lock's registry tarball URLs as remote fetches; only the lock is written (--ignore-scripts).
set -euo pipefail
cd "$(dirname "$0")"
npx --yes npm@12.2.2 install --package-lock-only --ignore-scripts --no-audit --no-fund --allow-remote=all
