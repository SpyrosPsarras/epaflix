#!/bin/sh
set -eu

if [ "$#" -ne 4 ]; then
  echo "ERROR: usage: fetch-plugin.sh <id> <version> <sha256> <owner/repo>" >&2
  exit 1
fi

ID=$1
VERSION=$2
SHA256=$3
REPO=$4

URL="https://github.com/${REPO}/releases/download/v${VERSION}/${ID}_${VERSION}_linux_amd64.zip"
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

rm -f "/plugins/${ID}-v"*.so
unzip -oq "$ZIP" "${ID}.so" -d /plugins
mv "/plugins/${ID}.so" "$TARGET"
rm -f "$ZIP"

ls -l "$TARGET"
