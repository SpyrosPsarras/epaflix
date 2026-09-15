#!/usr/bin/env bash
# One-shot migration for #978: collapse the duplicated alert_email_* /
# auth_email_* relay keys in the SOPS credential store into one shared
# mail_relay_* set so the relay's hostname and transport settings cannot
# diverge again. Only username and password stay per mailbox, and any shared
# candidate whose two copies disagree is left per-mailbox and reported.
#
# Default is a dry-run report; --apply re-encrypts the store in place after a
# full round-trip check. Values are never printed: findings are key names and
# 12-char sha256 prefixes, the same identification #977 used. Needs the age
# key (copy 1 in .github/hooks/print-age-key-backup.sh: workstation
# ~/.config/sops/age/k3s-cluster.txt). Idempotent: on an already-collapsed
# store it is a no-op. python3 stdlib only; top-level entries are the flat
# key: value scalars the email keys live in, comments pass through verbatim,
# and nested subtrees ride along as opaque byte-preserved blocks — the
# round-trip check refuses to write if any of that comes back different.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

KEY_FILE="${KEY_FILE:-$HOME/.config/sops/age/k3s-cluster.txt}"
STORE="${STORE:-.github/instructions/secrets.enc.yaml}"
MODE=dry-run
for arg in "$@"; do
  case "$arg" in
    --apply) MODE=apply ;;
    --dry-run) MODE=dry-run ;;
    *) echo "usage: $0 [--apply|--dry-run]" >&2; exit 1 ;;
  esac
done

command -v sops >/dev/null 2>&1 || { echo "ERROR: sops is not on PATH." >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 is not on PATH." >&2; exit 1; }
[ -f "$KEY_FILE" ] || {
  echo "ERROR: key file not found at $KEY_FILE." >&2
  echo "       Restore copy 1 from the KeePassXC entry /sops-age-k3s-cluster" >&2
  echo "       (see .github/hooks/print-age-key-backup.sh), or point KEY_FILE" >&2
  echo "       at wherever the age key is materialized." >&2
  exit 1
}
[ -f "$STORE" ] || { echo "ERROR: store not found at $STORE." >&2; exit 1; }

export SOPS_AGE_KEY_FILE="$KEY_FILE"
umask 077
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

sops -d "$STORE" > "$work/plain.yaml"

build_status=0
python3 - "$work/plain.yaml" "$work/new.yaml" "$work/plan.json" <<'PYEOF' || build_status=$?
import hashlib
import json
import sys

ALERT = "alert_email_"
AUTH = "auth_email_"
SHARED = "mail_relay_"
PER_MAILBOX = {"username", "password"}


class Abort(Exception):
    pass


def unquote(s):
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def parse_flat(path):
    items = []
    current = None
    with open(path) as f:
        text = f.read()
    for lineno, line in enumerate(text.splitlines(keepends=True), 1):
        body = line.rstrip("\n")
        if current is not None:
            if body == "" or body[:1] in (" ", "\t"):
                if current.get("block") or current.get("subtree"):
                    current["lines"].append(line)
                    continue
                if body == "":
                    current = None
                    items.append({"kind": "raw", "lines": [line]})
                    continue
                raise Abort(f'line {lineno}: unexpected indentation after '
                            f'"{current["key"]}"')
            current = None
        stripped = body.strip()
        if not stripped or stripped.startswith("#") or stripped == "---":
            items.append({"kind": "raw", "lines": [line]})
            continue
        if body[:1] in (" ", "\t") or stripped.startswith("- "):
            raise Abort(f"line {lineno}: not a flat key: value entry")
        key, sep, rest = body.partition(":")
        if not sep:
            raise Abort(f"line {lineno}: not a key: value entry")
        key = key.strip()
        if any(i["kind"] == "entry" and i["key"] == key for i in items):
            raise Abort(f"duplicate key {key}")
        rest = rest.strip()
        if rest in ("|", "|-", "|+", ">", ">-", ">+"):
            current = {"kind": "entry", "key": key, "lines": [line],
                       "scalar": None, "block": True}
        elif rest == "":
            current = {"kind": "entry", "key": key, "lines": [line],
                       "scalar": None, "subtree": True}
        else:
            current = {"kind": "entry", "key": key, "lines": [line],
                       "scalar": unquote(rest)}
        items.append(current)
    return items


def same_value(a, b):
    if a["scalar"] is not None and b["scalar"] is not None:
        return a["scalar"] == b["scalar"]
    return a["lines"] == b["lines"]


def fingerprint(entry):
    raw = entry["scalar"] if entry["scalar"] is not None else "".join(entry["lines"])
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


try:
    items = parse_flat(sys.argv[1])
    keys = [i["key"] for i in items if i["kind"] == "entry"]
    data = {i["key"]: i for i in items if i["kind"] == "entry"}

    alert_keys = [k for k in keys if k.startswith(ALERT)]
    auth_keys = [k for k in keys if k.startswith(AUTH)]
    if not alert_keys and not auth_keys:
        print("nothing to do: no alert_email_* / auth_email_* keys in the store")
        raise SystemExit(3)

    plan = {}
    report = []
    shared_order = []

    for akey in alert_keys:
        sfx = akey[len(ALERT):]
        auth_key = AUTH + sfx
        if sfx in PER_MAILBOX or auth_key not in data:
            plan[akey] = akey
            continue
        if same_value(data[akey], data[auth_key]):
            new_key = SHARED + sfx
            plan[akey] = new_key
            plan[auth_key] = new_key
            if sfx not in shared_order:
                shared_order.append(sfx)
                report.append(f"  {new_key} <- {akey} + {auth_key} "
                              f"(agree, sha256:{fingerprint(data[akey])})")
        else:
            plan[akey] = akey
            plan[auth_key] = auth_key
            report.append(f"  {akey} / {auth_key} kept per-mailbox "
                          f"(copies differ: sha256:{fingerprint(data[akey])} "
                          f"vs sha256:{fingerprint(data[auth_key])})")
    for gkey in auth_keys:
        if gkey not in plan:
            plan[gkey] = gkey

    moved = [k for k in plan if plan[k] != k]
    if not moved:
        print("nothing to do: no duplicated relay keys to collapse")
        print("\n".join(report))
        raise SystemExit(3)

    value_of = {}
    for k in keys:
        if k in plan:
            value_of.setdefault(plan[k], data[k])

    def entry_for(target):
        source = value_of[target]
        if source["key"] == target:
            return source
        head = source["lines"][0].replace(source["key"] + ":", target + ":", 1)
        return {"kind": "entry", "key": target, "lines": [head] + source["lines"][1:],
                "scalar": source["scalar"]}

    block = [SHARED + sfx for sfx in shared_order]
    block += [k for k in keys if k in plan and plan[k] == k]

    first = next(idx for idx, i in enumerate(items)
                 if i["kind"] == "entry" and i["key"] in plan)
    new_items = []
    for idx, item in enumerate(items):
        if idx == first:
            new_items.extend(entry_for(t) for t in block)
        if item["kind"] == "raw" or item["key"] not in plan:
            new_items.append(item)

    seen = set()
    for item in new_items:
        if item["kind"] != "entry":
            continue
        if item["key"] in seen:
            raise Abort(f"key {item['key']} would appear twice in the new store")
        seen.add(item["key"])

    with open(sys.argv[2], "w") as f:
        for item in new_items:
            f.write("".join(item["lines"]))

    with open(sys.argv[3], "w") as f:
        json.dump(plan, f)

    print("collapse plan:")
    print("\n".join(report))
    kept = sorted(k for k in plan if plan[k] == k)
    if kept:
        print(f"  kept per-mailbox: {', '.join(kept)}")
    print(f"  {len(moved)} key(s) collapse into {len(shared_order)} "
          f"shared mail_relay_* key(s)")
except SystemExit:
    raise
except Abort as exc:
    print(f"ERROR: store layout not understood ({exc}); nothing written.")
    raise SystemExit(1)
except Exception as exc:
    print(f"ERROR: migration aborted while building the new store "
          f"({type(exc).__name__}).")
    raise SystemExit(1)
PYEOF

if [ "$build_status" -eq 3 ]; then
  echo "Store untouched: nothing to collapse."
  exit 0
elif [ "$build_status" -ne 0 ]; then
  exit "$build_status"
fi

if [ "$MODE" = dry-run ]; then
  echo "DRY-RUN: store untouched. Re-run with --apply to write it."
  exit 0
fi

sops --encrypt --filename-override "$STORE" "$work/new.yaml" > "$work/new.enc.yaml"

python3 - "$work/new.enc.yaml" <<'PYEOF' || exit 1
import sys

in_sops = False
bad = 0
with open(sys.argv[1]) as f:
    for lineno, line in enumerate(f, 1):
        body = line.rstrip("\n")
        if body.strip() == "sops:":
            in_sops = True
            continue
        if in_sops:
            continue
        stripped = body.strip()
        if not stripped or stripped.startswith("#") or stripped == "---":
            continue
        key, sep, rest = stripped.partition(":")
        if not sep:
            continue
        rest = rest.strip()
        if rest == "":
            continue
        if rest in ("|", "|-", "|+", ">", ">-", ">+") or not rest.startswith("ENC["):
            bad += 1
            print(f"  line {lineno} ({key.strip()}): value not ciphertext")
if bad:
    print(f"ERROR: {bad} value(s) in the re-encrypted store are not ciphertext;")
    print("       the active .sops.yaml rule did not encrypt the values")
    print("       (e.g. an absolute STORE path matching the wrong rule).")
    print("       Nothing written.")
    raise SystemExit(1)
print("CIPHERTEXT_SHAPE_OK")
PYEOF

sops -d "$work/new.enc.yaml" > "$work/rt.yaml"

python3 - "$work/plain.yaml" "$work/rt.yaml" "$work/plan.json" <<'PYEOF' || exit 1
import json
import sys


class Abort(Exception):
    pass


def unquote(s):
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def parse_flat(path):
    items = []
    current = None
    with open(path) as f:
        text = f.read()
    for lineno, line in enumerate(text.splitlines(keepends=True), 1):
        body = line.rstrip("\n")
        if current is not None:
            if body == "" or body[:1] in (" ", "\t"):
                if current.get("block") or current.get("subtree"):
                    current["lines"].append(line)
                    continue
                if body == "":
                    current = None
                    items.append({"kind": "raw", "lines": [line]})
                    continue
                raise Abort(f'line {lineno}: unexpected indentation after '
                            f'"{current["key"]}"')
            current = None
        stripped = body.strip()
        if not stripped or stripped.startswith("#") or stripped == "---":
            items.append({"kind": "raw", "lines": [line]})
            continue
        if body[:1] in (" ", "\t") or stripped.startswith("- "):
            raise Abort(f"line {lineno}: not a flat key: value entry")
        key, sep, rest = body.partition(":")
        if not sep:
            raise Abort(f"line {lineno}: not a key: value entry")
        key = key.strip()
        if any(i["kind"] == "entry" and i["key"] == key for i in items):
            raise Abort(f"duplicate key {key}")
        rest = rest.strip()
        if rest in ("|", "|-", "|+", ">", ">-", ">+"):
            current = {"kind": "entry", "key": key, "lines": [line],
                       "scalar": None, "block": True}
        elif rest == "":
            current = {"kind": "entry", "key": key, "lines": [line],
                       "scalar": None, "subtree": True}
        else:
            current = {"kind": "entry", "key": key, "lines": [line],
                       "scalar": unquote(rest)}
        items.append(current)
    return items


def value_key(entry):
    return entry["scalar"] if entry["scalar"] is not None else "".join(entry["lines"])


try:
    with open(sys.argv[3]) as f:
        plan = json.load(f)
    old = {i["key"]: i for i in parse_flat(sys.argv[1]) if i["kind"] == "entry"}
    new = {i["key"]: i for i in parse_flat(sys.argv[2]) if i["kind"] == "entry"}
    for old_key, new_key in plan.items():
        if old_key not in old:
            raise Abort(f"plan key {old_key} missing from the original store")
        if new_key not in new:
            raise Abort(f"plan key {new_key} missing from the new store")
        if value_key(old[old_key]) != value_key(new[new_key]):
            raise Abort(f"round-trip mismatch at {old_key} -> {new_key}")
    for key, entry in old.items():
        if key in plan:
            continue
        if key not in new or value_key(entry) != value_key(new[key]):
            raise Abort(f"round-trip changed unrelated key {key}")
    expected = set(plan.values()) | (set(old) - set(plan))
    if set(new) != expected:
        raise Abort("new store has unexpected key changes")
    print("ROUND_TRIP_OK")
except SystemExit:
    raise
except Abort as exc:
    print(f"ERROR: {exc}; store not written.")
    raise SystemExit(1)
except Exception as exc:
    print(f"ERROR: verification failed ({type(exc).__name__}); store not written.")
    raise SystemExit(1)
PYEOF

cp "$work/new.enc.yaml" "$STORE"
chmod 644 "$STORE"
sops -d "$STORE" > "$work/final.yaml"
cmp -s "$work/final.yaml" "$work/rt.yaml" || {
  echo "ERROR: post-write verification failed; compare $STORE against git HEAD." >&2
  exit 1
}
echo "APPLIED: $STORE rewritten and verified. Commit it together with any key-name references."
