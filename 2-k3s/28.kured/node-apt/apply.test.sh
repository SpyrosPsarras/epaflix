#!/usr/bin/env bash
# Checks the unattended-upgrades drop-in and apply.sh with stubbed kubectl/ssh.
# No node is touched.
set -euo pipefail
dir=$(cd "$(dirname "$0")" && pwd)
conf="$dir/52unattended-upgrades-homelab"
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

mkdir "$work/none"
printf 'Dir::Etc::parts "%s/none";\nDir::Etc::main "%s/none";\n' "$work" "$work" >"$work/apt.conf"
dump=$(APT_CONFIG="$work/apt.conf" apt-config -c "$conf" dump)
grep -qF 'Unattended-Upgrade::Allowed-Origins:: "${distro_id}:${distro_codename}-updates";' <<<"$dump" \
    || fail "the drop-in must allow the -updates pocket"
grep -qF 'Unattended-Upgrade::Remove-Unused-Dependencies "true";' <<<"$dump" \
    || fail "the drop-in must autoremove unused dependencies like the old job did"

mkdir "$work/bin"
cat >"$work/bin/kubectl" <<'STUB'
#!/usr/bin/env bash
printf 'node/k3s-a\nnode/k3s-b\n'
STUB
cat >"$work/bin/ssh" <<'STUB'
#!/usr/bin/env bash
node=$1; shift
cat >"$WORK/$node.conf"
echo "$node $*" >>"$WORK/calls"
[ "${BROKEN:-}" = "$node" ] && exit 0
cat "$WORK/$node.conf" | sed -n 's/^\t\(".*-updates";\)$/Unattended-Upgrade::Allowed-Origins:: \1/p'
STUB
chmod +x "$work/bin/"*

run() { WORK="$work" PATH="$work/bin:$PATH" BROKEN="${1:-}" bash "$dir/apply.sh" >"$work/out" 2>&1; }

run || fail "apply.sh failed on healthy nodes: $(cat "$work/out")"
for n in k3s-a k3s-b; do
    cmp -s "$conf" "$work/$n.conf" || fail "$n did not receive the drop-in"
done
grep -q '/etc/apt/apt.conf.d/52unattended-upgrades-homelab' "$work/calls" \
    || fail "apply.sh must install to /etc/apt/apt.conf.d/52unattended-upgrades-homelab"

: >"$work/calls"
if run k3s-a; then fail "apply.sh must fail when a node does not report the -updates origin"; fi
grep -q '^k3s-b ' "$work/calls" || fail "one bad node must not stop the rest"
grep -q 'k3s-a' "$work/out" || fail "the failing node must be named"

echo "node-apt tests OK"
