#!/bin/sh
# Fetch the pi-bridge CLIProxyAPI plugin into the plugins emptyDir before the
# proxy starts. See #858 and the README section "pi-bridge plugin".
#
# Why an initContainer at all: the proxy's root filesystem is read-only (#862)
# and there is no PVC, so the management panel's "Plugins -> Store" install
# button has nowhere durable to write. Git owns the version instead, and the
# plugin is re-fetched on every pod start.
#
# The VERSION here is the ONLY place the version is pinned. The config block in
# reconcile-config.psql deliberately sets no `store.version`: with exactly one
# .so present the host loads that one, so a bump is a one-line edit here.
# Verified on v7.2.127 - the plugin loads with no store.version in config.yaml.
#
# Fails CLOSED on a checksum mismatch and OPEN on an unreachable GitHub: a
# tampered artifact must never load, but a GitHub outage must not take the
# proxy itself down for the sake of an optional quota endpoint.
set -eu

VERSION=0.9.1
# sha256 of the release zip, from the release's own checksums.txt.
SHA256=7cfea7b47888c870acc8afca73d8f78cb8454559040c401a2aa08a870107db26

TARGET="/plugins/pi-bridge-v${VERSION}.so"
ZIP="/plugins/.pi-bridge.zip"
URL="https://github.com/abix5/pi-cliproxyapi-bridge/releases/download/v${VERSION}/pi-bridge_${VERSION}_linux_amd64.zip"

if [ -f "$TARGET" ]; then
  echo "pi-bridge v${VERSION} already present"
  exit 0
fi

if ! wget -q -T 30 -O "$ZIP" "$URL"; then
  echo "WARNING: could not download pi-bridge v${VERSION} from GitHub."
  echo "WARNING: starting the proxy without it - quota endpoints will 404."
  rm -f "$ZIP"
  exit 0
fi

if ! echo "${SHA256}  ${ZIP}" | sha256sum -c -; then
  echo "ERROR: pi-bridge v${VERSION} checksum mismatch - refusing to install"
  rm -f "$ZIP"
  exit 1
fi

# The host prunes old plugin files only once per process start; on an emptyDir
# there is nothing to prune anyway, but a stale .so from a previous version in
# the same pod lifetime would win if it sorted higher.
rm -f /plugins/pi-bridge-v*.so
unzip -oq "$ZIP" pi-bridge.so -d /plugins
mv /plugins/pi-bridge.so "$TARGET"
rm -f "$ZIP"

ls -l "$TARGET"
