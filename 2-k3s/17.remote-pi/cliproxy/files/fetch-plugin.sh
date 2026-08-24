#!/bin/sh
# Fetch one CLIProxyAPI plugin release into the plugins emptyDir before the proxy
# starts. Used by both plugin initContainers. See #858.
#
# Usage: sh fetch-plugin.sh <id> <version> <sha256> <zip-url>
#
# The four values live in the initContainer args in cliproxy/deployment.yaml,
# which is therefore the ONE place a version is pinned. There is no default and
# no fallback: a missing argument is a hard failure, because a silently skipped
# plugin looks identical to a working one until someone notices a model or an
# endpoint is missing.
#
# Why an initContainer at all: the proxy's root filesystem is read-only (#862)
# and there is no PVC, so the management panel's "Plugins -> Store" install
# button has nowhere durable to write. Git owns the version instead, and the
# plugins are re-fetched on every pod start.
#
# The .so is written as <id>-v<version>.so. The filename does not decide the
# plugin id - the host reads that from the plugin's own metadata, measured on
# v7.2.140 by loading the Copilot release under a version-suffixed name and
# getting `plugin_id=cliproxyapi-copilot` back. Keeping the version in the
# filename is what makes the "already present" check and the stale-.so prune
# below possible.
#
# Fails CLOSED on a checksum mismatch and OPEN on an unreachable GitHub: a
# tampered artifact must never load, but a GitHub outage must not take the proxy
# down for the sake of a plugin. The cost of failing open is a silent absence, so
# each caller's README section says what disappears.
set -eu

if [ "$#" -ne 4 ]; then
  echo "ERROR: usage: fetch-plugin.sh <id> <version> <sha256> <zip-url>" >&2
  exit 1
fi

ID=$1
VERSION=$2
SHA256=$3
URL=$4

TARGET="/plugins/${ID}-v${VERSION}.so"
ZIP="/plugins/.${ID}.zip"

if [ -f "$TARGET" ]; then
  echo "${ID} v${VERSION} already present"
  exit 0
fi

if ! wget -q -T 30 -O "$ZIP" "$URL"; then
  echo "WARNING: could not download ${ID} v${VERSION} from GitHub."
  echo "WARNING: starting the proxy without it."
  rm -f "$ZIP"
  exit 0
fi

if ! echo "${SHA256}  ${ZIP}" | sha256sum -c -; then
  echo "ERROR: ${ID} v${VERSION} checksum mismatch - refusing to install"
  rm -f "$ZIP"
  exit 1
fi

# The host prunes old plugin files only once per process start; on an emptyDir
# there is nothing to prune anyway, but a stale .so from a previous version in
# the same pod lifetime would win if it sorted higher.
rm -f "/plugins/${ID}-v"*.so
# Every release here ships the library as <id>.so at the root of the zip.
unzip -oq "$ZIP" "${ID}.so" -d /plugins
mv "/plugins/${ID}.so" "$TARGET"
rm -f "$ZIP"

ls -l "$TARGET"
