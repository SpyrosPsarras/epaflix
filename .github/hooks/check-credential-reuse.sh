#!/usr/bin/env bash
set -euo pipefail

# Fails a commit when two credential-store keys hold the same value (#1077).
# root@pam on takaros and evanthoulaki shared the AirVPN account password,
# which is live in Secret servarr/airvpn-credentials, mounted into
# qbittorrent and vpn-picker. The overlap sat in the store for 17 days
# because every existing guard compared a value against tracked files or
# live cluster state; nothing hashed the store against itself. This check
# does that on every commit (run-pre-commit.sh runs every check-*.sh), which
# is also whatever runs after a rotation: change the store, commit, and the
# group either dissolves or fails.
#
# Scope: credential-class keys only. A key counts when its dotted name
# contains password, passphrase, secret, token, key or ssh (case
# insensitive). Hostnames, IPs, ports, usernames, endpoints and fingerprints
# are identifiers, not credentials, and are excluded on purpose: root == root
# as a username on two hypervisors is normal, two hosts sharing a root
# password is the original #745 incident.
#
# Rule: hash every credential-class value, group by sha256, and fail on any
# group spanning two or more keys unless its 12-char sha256 prefix is
# recorded in ACCEPTED_REUSE. Groups are printed with key names and prefixes;
# values are never printed. That is the same identification #977 and #1077
# used, so post-rotation verification can paste the prefixes verbatim.
# REPORT_KEYS names keys to print len+prefix for, the paste-into-the-issue
# half of a rotation.
#
# Accepted overlaps print as ACCEPTED so the recorded risk stays visible. An
# acceptance that no longer matches any store value warns instead of
# failing: the rotation landed, the entry is stale, remove it so the record
# stays true. An entry whose group changes shape (a third key joins, or the
# value on a recorded key changes) stops matching and fails like any other
# reuse.
#
# The default acceptance is the recorded #1077 risk, decided by the owner on
# 2026-08-21 ("record the risk, rotate later"): sha256 prefix d0ccf31122f7,
# shared by airvpn_password, proxmox-takaros_password and
# proxmox-evanthoulaki_password. Remove that entry when the hypervisor-side
# rotation lands; the issue records the sequencing (hypervisor side, not
# AirVPN).
#
# Skipped, not failed, when sops, the age key or the store is unavailable,
# same as check-no-plaintext-store-values.sh, so worktrees without store
# access can still commit. Block scalars hash as one value: two different
# SSH keys share their BEGIN/END armor lines, and line-by-line hashing would
# flag them as reused.
#
# Test overrides: STORE, KEY_FILE, ACCEPTED_REUSE and REPORT_KEYS.
# ACCEPTED_REUSE unset selects the recorded default; set but empty disables
# every acceptance.

repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
STORE="${STORE:-$repo_root/.github/instructions/secrets.enc.yaml}"
KEY_FILE="${KEY_FILE:-$HOME/.config/sops/age/k3s-cluster.txt}"

if ! command -v sops >/dev/null 2>&1; then
  echo "SKIP: sops is not on PATH; cannot hash the store for reuse checks."
  exit 0
fi
if [ ! -f "$KEY_FILE" ]; then
  echo "SKIP: no age key at $KEY_FILE; cannot decrypt the store for reuse checks."
  exit 0
fi
if [ ! -f "$STORE" ]; then
  echo "SKIP: no credential store at $STORE; nothing to hash."
  exit 0
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
umask 077

if ! SOPS_AGE_KEY_FILE="$KEY_FILE" sops -d "$STORE" > "$work/store.yaml" 2>/dev/null; then
  echo "SKIP: the store did not decrypt with the available key; reuse check cannot run."
  exit 0
fi

# Recorded accepted overlaps: 12-char sha256 prefixes, never values. The
# default applies only when ACCEPTED_REUSE is unset; set-but-empty disables
# every acceptance, which is how the fixture suite runs strict.
#
# d0ccf31122f7: root@pam on takaros and evanthoulaki shares the AirVPN
#   account password, live in Secret servarr/airvpn-credentials (#1077).
#   Owner decision 2026-08-21: record the risk, rotate later. Rotate the
#   hypervisor side first (see the issue for sequencing); remove this entry
#   when the overlap is gone.
# e7a1b07ac4c5: the shared lab password recorded in #778: the seven k3s
#   node passwords kept by decision (#745), the owner's four personal
#   machines, and smtp/tplink/wg-hop rotating per-credential at the owner's
#   pace. Stays accepted while the value lives; warns once the last member
#   rotates.
export ACCEPTED_REUSE="${ACCEPTED_REUSE-d0ccf31122f7 e7a1b07ac4c5}"
export REPORT_KEYS="${REPORT_KEYS:-}"

python3 - "$work/store.yaml" <<'PYEOF'
import hashlib
import os
import re
import sys

MIN_LEN = 8
PREFIX_LEN = 12
MARKERS = ("password", "passphrase", "secret", "token", "key", "ssh")


def is_credential(key):
    low = key.lower()
    return any(marker in low for marker in MARKERS)


def unquote(s):
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def credential_values(path):
    # Same YAML subset as check-no-plaintext-store-values.sh: flat scalars,
    # one nesting level, block scalars. A block scalar is kept as one value,
    # not one per line, so armor lines shared by two different SSH keys do
    # not read as reuse.
    values = {}
    parent = None
    current_key = None
    block_lines = None

    def add(key, raw):
        if not is_credential(key):
            return
        value = unquote(raw.strip())
        if len(value) >= MIN_LEN:
            values[key] = value

    def flush_block():
        nonlocal current_key, block_lines
        if current_key and block_lines is not None:
            add(current_key, "\n".join(block_lines))
        current_key = None
        block_lines = None

    with open(path) as f:
        for line in f:
            body = line.rstrip("\n")
            if current_key is not None:
                if body == "" or body[:1] in (" ", "\t"):
                    block_lines.append(body)
                    continue
                flush_block()
            stripped = body.strip()
            if not stripped or stripped.startswith("#") or stripped == "---":
                continue
            indented = body[:1] in (" ", "\t")
            key, sep, rest = stripped.partition(":")
            if not sep or stripped.startswith("- "):
                continue
            key = key.strip()
            rest = rest.strip()
            if indented:
                if parent:
                    if rest in ("|", "|-", "|+", ">", ">-", ">+"):
                        current_key = f"{parent}.{key}"
                        block_lines = []
                    elif rest:
                        add(f"{parent}.{key}", rest)
                continue
            if rest in ("|", "|-", "|+", ">", ">-", ">+"):
                current_key = key
                block_lines = []
                parent = None
            elif rest == "":
                parent = key
            else:
                add(key, rest)
                parent = None
    flush_block()
    return values


def prefix(value):
    return hashlib.sha256(value.encode()).hexdigest()[:PREFIX_LEN]


values = credential_values(sys.argv[1])

# sops -d exiting 0 must mean plaintext. When every credential value still
# reads as ENC[...] (a shimmed or broken sops passed the encrypted file
# through), hashing ciphertext would print a meaningless OK, so skip instead.
ciphertext = [k for k, v in values.items() if v.startswith("ENC[")]
if ciphertext and len(ciphertext) == len(values):
    print("SKIP: every credential value still reads as ENC[...] ciphertext; "
          "sops -d did not actually decrypt.")
    sys.exit(0)
if ciphertext:
    print("WARNING: ENC[...] ciphertext values were excluded from the reuse "
          "check; sops -d output is part-encrypted.", file=sys.stderr)
    values = {k: v for k, v in values.items() if k not in ciphertext}

for key in (k for k in re.split(r"[,\s]+", os.environ["REPORT_KEYS"]) if k):
    if key in values:
        print(f"REPORT {key} len={len(values[key])} sha256={prefix(values[key])}")
    else:
        print(f"REPORT {key} absent (not a credential-class store key)")

groups = {}
for key, value in values.items():
    groups.setdefault(prefix(value), []).append(key)

accepted = set()
for token in re.split(r"[,\s]+", os.environ["ACCEPTED_REUSE"]):
    token = token.strip().lower()
    if not token:
        continue
    if re.fullmatch(rf"[0-9a-f]{{{PREFIX_LEN}}}", token):
        accepted.add(token)
    else:
        print(f"WARNING: ACCEPTED_REUSE entry {token!r} is not a 12-char sha256 "
              f"prefix; ignoring it.", file=sys.stderr)

overlaps = {h: keys for h, keys in groups.items() if len(keys) > 1}
live_overlap_prefixes = {h[:PREFIX_LEN] for h in overlaps}

for stale in sorted(accepted - live_overlap_prefixes):
    print(f"WARNING: accepted overlap sha256:{stale} no longer matches any store overlap.")
    print(f"         The rotation likely landed; remove the entry from")
    print(f"         check-credential-reuse.sh so the recorded-risk list stays true.")

for h, keys in sorted(overlaps.items()):
    if h[:PREFIX_LEN] not in accepted:
        continue
    print(f"ACCEPTED sha256:{h[:PREFIX_LEN]} (len={len(values[keys[0]])}) "
          f"shared by {len(keys)} keys (recorded risk):")
    for key in sorted(keys):
        marker = " (hypervisor)" if key.startswith("proxmox-") else ""
        print(f"  {key}{marker}")

violations = {h: keys for h, keys in overlaps.items() if h[:PREFIX_LEN] not in accepted}
if violations:
    print("ERROR: credential reuse in the SOPS store (#1077):")
    for h, keys in sorted(violations.items()):
        print(f"  sha256:{h[:PREFIX_LEN]} (len={len(values[keys[0]])}) "
              f"is shared by {len(keys)} keys:")
        for key in sorted(keys):
            marker = " (hypervisor)" if key.startswith("proxmox-") else ""
            print(f"    {key}{marker}")
    print("  One readable copy grants everything the others guard; this is how")
    print("  root@pam on both hypervisors became readable from qbittorrent.")
    print("  Rotate one side so the values stop being equal, or record an")
    print("  owner-accepted risk via ACCEPTED_REUSE (12-char sha256 prefixes;")
    print("  see the header of check-credential-reuse.sh).")
    sys.exit(1)

print(f"OK: {len(values)} credential values hashed; no unaccepted reuse.")
PYEOF
