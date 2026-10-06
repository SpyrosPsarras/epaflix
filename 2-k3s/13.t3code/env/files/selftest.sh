#!/usr/bin/env bash
# Self-check for the t3env scripts without starting T3. Runs locally or in
# the pod. Exercises: credential helper host filtering, entrypoint home
# seeding is idempotent and runs the backup, installer and plugin check,
# lock pins match package.json.
set -euo pipefail
DIR=$(cd "$(dirname "$0")" && pwd)
HELPER=$DIR/git-credential-github.sh
fail() { echo "FAIL: $*" >&2; exit 1; }

export GITHUB_TOKEN=tok-selftest
ask() { printf 'protocol=%s\nhost=%s\n\n' "$2" "$3" | bash "$HELPER" "$1"; }
[[ $(ask get https github.com) == $'username=x-access-token\npassword=tok-selftest' ]] || fail "github.com get must return the token"
[[ -z $(ask get https gitlab.com) ]] || fail "other hosts must get nothing"
[[ -z $(ask get https evil.github.com) ]] || fail "subdomains must get nothing"
[[ -z $(ask get http github.com) ]] || fail "plain http must get nothing"
[[ -z $(ask store https github.com) ]] || fail "store must be a no-op"
[[ -z $(ask erase https github.com) ]] || fail "erase must be a no-op"
[[ -z $(GITHUB_TOKEN='' ask get https github.com) ]] || fail "empty token must return nothing"
echo "ok: credential helper filters host/protocol/operation"

# Entrypoint up to (not including) `exec t3 serve`, against a scratch HOME
# with a local bare repo so nothing leaves the machine.
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
git init -q --bare "$tmp/remote.git"
export HOME=$tmp/home ANTHROPIC_BASE_URL=http://cliproxy.test ANTHROPIC_AUTH_TOKEN=unused
# The OpenCode 1 backup follows these; set, they would point it at the real store.
unset XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME T3CODE_HOME
export T3_PROJECT_REPO=$tmp/remote.git
mkdir -p "$tmp/scripts"
cp "$HELPER" "$DIR/../../files/cliproxy-models.js" "$DIR/../../one/files/"{jev-auto.js,jev-guard.js,opencode-compat.js,jev-checks.md,opencode-v1-backup.py} "$tmp/scripts/"
# The cc-safety-net installer and the plugin check need the image (tools/smoke.sh runs them); here they only log their arguments.
echo 'import sys; open(sys.argv[0] + ".calls", "a").write(" ".join(sys.argv[1:]) + "\n")' >"$tmp/scripts/cc-safety-net-install.py"
echo 'import { appendFileSync } from "node:fs"; appendFileSync(process.argv[1] + ".calls", process.argv.slice(2).join(" ") + "\n")' >"$tmp/scripts/opencode-plugin-check.mjs"
sed -e "s|/scripts/|$tmp/scripts/|g" -e "s|/var/run/secrets/kubernetes.io/serviceaccount/token|$tmp/no-sa-token|g" \
  -e '/^echo "t3env:/,$d' "$DIR/entrypoint.sh" >"$tmp/entrypoint-noexec.sh"
run() { bash "$tmp/entrypoint-noexec.sh"; }
run
[[ -d $HOME/projects/remote/.git ]] || fail "project not cloned"
[[ $(git config --global credential.helper) == "$tmp/scripts/git-credential-github.sh" ]] || fail "helper not configured"
python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["enabled_providers"] == ["cliproxy", "jev-auto"]' "$HOME/.config/opencode/opencode.json" || fail "opencode.json invalid or Jev Auto not allowed"
python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["plugin"] == ["superpowers@git+https://github.com/obra/superpowers.git"]' "$HOME/.config/opencode/opencode.json" || fail "superpowers not registered"
[[ $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["state"])' "$HOME/.local/share/opencode/opencode.db.v1-backup.marker") == no-v1 ]] || fail "OpenCode 1 backup did not run"
grep -q -- "--config $HOME/.config/opencode/opencode.json" "$tmp/scripts/cc-safety-net-install.py.calls" || fail "cc-safety-net installer not run"
[[ $(<"$tmp/scripts/opencode-plugin-check.mjs.calls") == "/tools/node_modules/@opencode/cli/bin/opencode.exe $HOME/.config/opencode cliproxy-models jev-auto jev-guard opencode-compat cc-safety-net superpowers" ]] || fail "plugin check not run with the six plugin IDs"
for plugin in cliproxy-models jev-auto jev-guard opencode-compat; do [[ -f $HOME/.config/opencode/plugins/$plugin.js ]] || fail "plugin $plugin not installed"; done
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert "http://cliproxy.test/v1" in d["providerInstances"]["codex"]["config"]["launchArgs"]' "$HOME/.t3/userdata/settings.json" || fail "settings.json invalid"
python3 - "$HOME/.t3/userdata/settings.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert not d["providers"]["opencode"].get("serverUrl"), "fresh legacy provider must use managed OpenCode"
assert not d["providerInstances"]["opencode"].get("config", {}).get("serverUrl"), "fresh instance must use managed OpenCode"
PY
[[ $(stat -c %a "$HOME/.t3/userdata/settings.json") == 600 ]] || fail "settings.json must be 0600"
echo '{"user":"edited"}' >"$HOME/.t3/userdata/settings.json"
echo '{"user":"edited"}' >"$HOME/.config/opencode/opencode.json"
chmod 0444 "$HOME/.config/opencode/plugins/cliproxy-models.js"
run
[[ $(<"$HOME/.t3/userdata/settings.json") == '{"user":"edited"}' ]] || fail "second run overwrote settings.json"
python3 -c 'import json,sys; assert json.load(open(sys.argv[1])) == {"user": "edited", "plugin": ["superpowers@git+https://github.com/obra/superpowers.git"]}' "$HOME/.config/opencode/opencode.json" || fail "second run overwrote opencode.json"
echo "ok: entrypoint seeds once and keeps user edits"
[[ $(stat -c %a "$HOME/.config/opencode/plugins/cliproxy-models.js") == 644 ]] || fail "plugin copy must be writable after restart"

python3 - "$HOME/.t3/userdata/settings.json" <<'PY'
import json, sys
d = {
    "user": "edited",
    "providers": {"opencode": {"enabled": True, "serverUrl": "http://127.0.0.1:4096"}},
    "providerInstances": {
        "opencode": {"driver": "opencode", "config": {"serverUrl": "http://127.0.0.1:4096", "customModels": ["keep-me"]}},
        "renamed": {"driver": "opencode", "config": {"serverUrl": "http://127.0.0.1:4096"}},
        "external": {"driver": "opencode", "config": {"serverUrl": "https://custom.example"}},
        "codex": {"driver": "codex", "config": {"launchArgs": "keep-me"}},
    },
}
json.dump(d, open(sys.argv[1], "w"))
PY
run
python3 - "$HOME/.t3/userdata/settings.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
assert d["user"] == "edited"
assert d["providers"]["opencode"] == {"enabled": True}
assert d["providerInstances"]["opencode"]["config"] == {"customModels": ["keep-me"]}
assert d["providerInstances"]["renamed"]["config"] == {}
assert d["providerInstances"]["external"]["config"]["serverUrl"] == "https://custom.example"
assert d["providerInstances"]["codex"]["config"]["launchArgs"] == "keep-me"
PY
before=$(sha256sum "$HOME/.t3/userdata/settings.json")
run
[[ $(sha256sum "$HOME/.t3/userdata/settings.json") == "$before" ]] || fail "migration must be idempotent"
echo "ok: managed OpenCode migration preserves other settings and is idempotent"

# Lock root and resolved versions agree with package.json.
python3 - "$DIR/../tools/package.json" "$DIR/../tools/package-lock.json" <<'PY'
import json, sys
want = json.load(open(sys.argv[1]))["dependencies"]
lock = json.load(open(sys.argv[2]))["packages"]
assert lock[""]["dependencies"] == want, "lock root dependencies differ from package.json"
got = {n: lock[f"node_modules/{n}"]["version"] for n in want}
assert got == want, f"lock {got} != package.json {want}"
PY
echo "ok: package-lock.json matches package.json"
