#!/usr/bin/env bash
set -euo pipefail

# Printable offline copy of the SOPS age key (#801).
#
# Copies that exist today, all estate-dependent:
#   1. workstation ~/.config/sops/age/k3s-cluster.txt
#   2. in-cluster Secret argocd/sops-age
#   3. TrueNAS /mnt/apps/encrypted-backups/sops-age-backup/ — on a dataset that
#      comes up locked, and its unlock passphrase lives INSIDE the SOPS store
#      this key decrypts. Lose copies 1+2 and copy 3 is unreachable.
#
# This script writes a printable HTML page (key text, SHA256, QR, restore
# steps) into git-ignored artifacts/. Printed and stored off-site, the paper
# copy is the one that depends on nothing in the estate being up.
#
# The key never reaches stdout or any command's argv: it is embedded in the
# HTML by redirection, handed to qrencode via `-r` file input, and the finished
# page is verified by diffing its key block against the key file.
#
# Regenerate after every key rotation — rotation is now a recurring task
# because the store's ciphertext is public (the repo is public).

cd "$(git rev-parse --show-toplevel)"

KEY_FILE="${KEY_FILE:-$HOME/.config/sops/age/k3s-cluster.txt}"
STORE="${STORE:-.github/instructions/secrets.enc.yaml}"
OUT="${OUT:-artifacts/age-key-offline-copy.html}"

if [ ! -f "$KEY_FILE" ]; then
  echo "ERROR: key file not found at $KEY_FILE" >&2
  exit 1
fi

if ! grep -q 'AGE-SECRET-KEY-1' "$KEY_FILE"; then
  echo "ERROR: $KEY_FILE does not look like an age key file (no AGE-SECRET-KEY-1 line)." >&2
  echo "       Refusing to make a paper copy of the wrong file." >&2
  exit 1
fi

if ! SOPS_AGE_KEY_FILE="$KEY_FILE" sops -d "$STORE" >/dev/null 2>&1; then
  echo "ERROR: $KEY_FILE failed to decrypt $STORE." >&2
  echo "       A paper copy of a key that cannot read the store is worthless." >&2
  exit 1
fi

key_hash="$(sha256sum "$KEY_FILE" | cut -d' ' -f1)"
generated="$(date -u +%Y-%m-%d)"

umask 077
mkdir -p "$(dirname "$OUT")"
# A partially-written page still bears the key; it must not survive a failed run.
trap 'rm -f "${qr_svg:-}" "$OUT"' EXIT

qr_block=""
if command -v qrencode >/dev/null 2>&1; then
  qr_svg="$(mktemp)"
  qrencode -t SVG -m 2 -r "$KEY_FILE" -o "$qr_svg"
  qr_block="<p>Scan to restore the file exactly (phone camera, no estate dependency):</p>
$(cat "$qr_svg")"
else
  qr_block="<p class='warn'>No QR on this page: qrencode is not installed. The key text below
must be typed by hand. Install qrencode and re-run for a scannable copy:
<code>sudo apt install qrencode</code></p>"
fi

{
  cat <<'HTML'
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>epaflix age key - offline copy</title>
<style>
  body { font-family: monospace; margin: 2em; }
  pre { border: 1px solid #000; padding: 1em; font-size: 1.2em; white-space: pre-wrap; word-break: break-all; }
  svg { width: 300px; height: 300px; }
  .warn { color: #b00; }
  @media print { .noprint { display: none; } }
</style>
</head>
<body>
<h1>epaflix SOPS age key - offline paper copy</h1>
HTML
  echo "<p>Generated $generated UTC. Key file SHA256: <code>$key_hash</code></p>"
  cat <<'HTML'
<p>This key decrypts every SOPS-encrypted file in the epaflix repo, including
<code>.github/instructions/secrets.enc.yaml</code>, which holds the TrueNAS
<code>apps/encrypted-backups</code> unlock passphrase and every other estate
credential. Store this page somewhere a total loss of the homelab cannot
touch: not on TrueNAS, not in the cluster, not only inside the repo clone.</p>
<p>Restore:</p>
<ol>
  <li>Scan the QR (or copy the text) into <code>~/.config/sops/age/k3s-cluster.txt</code>, mode 600.</li>
  <li><code>sha256sum</code> the file and compare against the hash above.</li>
  <li>From the repo: <code>SOPS_AGE_KEY_FILE=~/.config/sops/age/k3s-cluster.txt sops -d .github/instructions/secrets.enc.yaml &gt;/dev/null &amp;&amp; echo OK</code></li>
  <li>Re-derive the other copies: in-cluster Secret <code>argocd/sops-age</code>, TrueNAS mirror on <code>apps/encrypted-backups</code>.</li>
</ol>
HTML
  echo "$qr_block"
  echo '<pre>'
  cat "$KEY_FILE"
  echo '</pre>'
  echo '</body></html>'
} > "$OUT"

chmod 600 "$OUT"

if ! sed -n '/^<pre>$/,/^<\/pre>$/p' "$OUT" | sed '1d;$d' | diff -q - "$KEY_FILE" >/dev/null; then
  echo "ERROR: generated page failed verification (key text round-trip) and was deleted." >&2
  exit 1
fi
trap - EXIT
rm -f "${qr_svg:-}"

echo "Wrote $OUT (mode 600, git-ignored)."
echo "  verified: key decrypts $STORE"
echo "  key file sha256: $key_hash"
echo
echo "Next (owner, hands-on): print the page, store the paper off-estate, scan the"
echo "QR once to prove it restores, then re-run this script after every key rotation."
