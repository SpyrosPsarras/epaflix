#!/usr/bin/env bash
set -euo pipefail

# Refuses a commit whose staged additions introduce a credential-store value
# byte-for-byte (#978). The SOPS store is the only place a hostname, IP,
# username or password may live; a value surfacing in a tracked file is the
# drift class this repo keeps hitting (#782, #803, #977, #978), so the audit
# PR #977 ran once now runs on every commit, repo-wide.
#
# Values already present in the tracked tree at HEAD are allowed: they are
# published, and re-flagging them on every touch would block routine commits
# (the store carries LAN IPs that deployment manifests legitimately repeat).
# The guard stops new leaks. Values already at HEAD are computed with one
# git grep over everything except *.enc.yaml.
#
# Values are matched but never printed: findings name the store key and the
# staged file:line only. Values shorter than 8 characters (ports, "true") are
# not scanned; block-scalar values are scanned line-by-line. Skipped, not
# failed, when sops or the age key is unavailable, so worktrees without store
# access can still commit; on any machine that can decrypt the store this is
# a hard stop. Encrypted files are skipped as scan targets: staged ciphertext
# can contain a value-shaped substring by chance.
#
# Test overrides: STORE, KEY_FILE, and GIT_BIN (defaults to git) so the suite
# can point them at fixtures.

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
STORE="${STORE:-$repo_root/.github/instructions/secrets.enc.yaml}"
KEY_FILE="${KEY_FILE:-$HOME/.config/sops/age/k3s-cluster.txt}"
GIT_BIN="${GIT_BIN:-git}"

if ! command -v sops >/dev/null 2>&1; then
  echo "SKIP: sops is not on PATH; cannot compare staged files against the store."
  exit 0
fi
if [ ! -f "$KEY_FILE" ]; then
  echo "SKIP: no age key at $KEY_FILE; cannot decrypt the store to compare."
  exit 0
fi
if [ ! -f "$STORE" ]; then
  echo "SKIP: no credential store at $STORE; nothing to compare against."
  exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
umask 077

if ! SOPS_AGE_KEY_FILE="$KEY_FILE" sops -d "$STORE" > "$work/store.yaml" 2>/dev/null; then
  echo "SKIP: the store did not decrypt with the available key; guard cannot run."
  exit 0
fi

"$GIT_BIN" diff --cached --unified=0 --no-color --no-ext-diff > "$work/staged.diff" 2>/dev/null || true
if [ ! -s "$work/staged.diff" ]; then
  exit 0
fi

python3 - "$work/store.yaml" "$work/staged.diff" "$work/values.txt" <<'PYEOF'
import os
import subprocess
import sys

MIN_LEN = 8
GIT_BIN = os.environ.get("GIT_BIN", "git")


def unquote(s):
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def store_values(path):
    values = {}
    current_key = None
    block_lines = None

    def flush_block():
        if current_key and block_lines is not None:
            for raw in block_lines:
                content = raw.strip()
                if len(content) >= MIN_LEN:
                    values.setdefault(current_key, []).append(content)

    with open(path) as f:
        for line in f:
            body = line.rstrip("\n")
            if current_key is not None:
                if body == "" or body[:1] in (" ", "\t"):
                    if block_lines is not None:
                        block_lines.append(body)
                    continue
                flush_block()
                current_key = None
                block_lines = None
            stripped = body.strip()
            if not stripped or stripped.startswith("#") or stripped == "---":
                continue
            if body[:1] in (" ", "\t") or stripped.startswith("- "):
                continue
            key, sep, rest = body.partition(":")
            if not sep:
                continue
            rest = rest.strip()
            if rest in ("|", "|-", "|+", ">", ">-", ">+"):
                current_key = key.strip()
                block_lines = []
            elif rest == "":
                pass
            else:
                value = unquote(rest)
                if len(value) >= MIN_LEN:
                    values.setdefault(key.strip(), []).append(value)
    flush_block()
    return values


def staged_additions(path):
    findings = []
    current_file = None
    new_line = 0
    with open(path) as f:
        for line in f:
            body = line.rstrip("\n")
            if body.startswith("+++ b/"):
                current_file = body[6:]
            elif body.startswith("+++ "):
                current_file = None
            elif body.startswith("@@"):
                parts = body.split()
                if len(parts) >= 3 and parts[2].startswith("+"):
                    span = parts[2][1:].split(",")
                    new_line = int(span[0])
            elif body.startswith("+") and current_file is not None:
                findings.append((current_file, new_line, body[1:]))
                new_line += 1
            elif body.startswith("-"):
                pass
            elif body.startswith(" ") and current_file is not None:
                new_line += 1
    return findings


values = store_values(sys.argv[1])
if not values:
    sys.exit(0)

candidates = list(dict.fromkeys(
    (key, cand) for key, cands in values.items() for cand in cands
))
with open(sys.argv[3], "w") as f:
    f.write("\n".join(cand for _, cand in candidates) + "\n")
try:
    baseline = subprocess.run(
        [GIT_BIN, "grep", "-F", "-h", "-f", sys.argv[3], "HEAD", "--",
         ":(exclude)*.enc.yaml", ":(exclude)*.enc.yml"],
        capture_output=True, text=True, errors="replace",
    ).stdout.splitlines()
except OSError:
    baseline = []

violations = []
for path, lineno, text in staged_additions(sys.argv[2]):
    if path.endswith(".enc.yaml") or path.endswith(".enc.yml"):
        continue
    for key, cand in candidates:
        if cand in text:
            if any(cand in line for line in baseline):
                continue
            violations.append((path, lineno, key))

if violations:
    print("ERROR: staged additions introduce credential-store values (#978):")
    for path, lineno, key in violations:
        print(f"  {path}:{lineno} matches the value of store key {key}")
    print("       Move the value into the SOPS store and reference the key")
    print("       name instead; the commit is refused.")
    sys.exit(1)
PYEOF
