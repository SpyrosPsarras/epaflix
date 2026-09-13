#!/usr/bin/env bash
# Self-check for the t3env scripts without starting T3. Runs locally or in
# the pod. Exercises: credential helper host filtering, entrypoint home
# seeding is idempotent, lock pins match versions.env.
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
export T3_PROJECT_REPO=$tmp/remote.git
mkdir -p "$tmp/scripts"
cp "$HELPER" "$DIR/../../files/cliproxy-models.js" "$tmp/scripts/"
sed -e "s|/scripts/|$tmp/scripts/|g" -e '/^echo "t3env:/,$d' "$DIR/entrypoint.sh" >"$tmp/entrypoint-noexec.sh"
run() { bash "$tmp/entrypoint-noexec.sh"; }
run
[[ -d $HOME/projects/remote/.git ]] || fail "project not cloned"
[[ $(git config --global credential.helper) == "$tmp/scripts/git-credential-github.sh" ]] || fail "helper not configured"
python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$HOME/.config/opencode/opencode.json" || fail "opencode.json invalid"
python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); assert "http://cliproxy.test/v1" in d["providerInstances"]["codex"]["config"]["launchArgs"]' "$HOME/.t3/userdata/settings.json" || fail "settings.json invalid"
[[ $(stat -c %a "$HOME/.t3/userdata/settings.json") == 600 ]] || fail "settings.json must be 0600"
echo '{"user":"edited"}' >"$HOME/.t3/userdata/settings.json"
echo '{"user":"edited"}' >"$HOME/.config/opencode/opencode.json"
chmod 0444 "$HOME/.config/opencode/plugins/cliproxy-models.js"
run
[[ $(<"$HOME/.t3/userdata/settings.json") == '{"user":"edited"}' ]] || fail "second run overwrote settings.json"
[[ $(<"$HOME/.config/opencode/opencode.json") == '{"user":"edited"}' ]] || fail "second run overwrote opencode.json"
echo "ok: entrypoint seeds once and keeps user edits"
[[ $(stat -c %a "$HOME/.config/opencode/plugins/cliproxy-models.js") == 644 ]] || fail "plugin copy must be writable after restart"

# Lock pins agree with versions.env.
# shellcheck source=2-k3s/13.t3code/versions.env
. "$DIR/../../versions.env"
python3 - "$DIR/../tools/package-lock.json" "$T3_VERSION" "$CLAUDE_CODE_VERSION" "$OPENCODE_VERSION" "$CODEX_VERSION" <<'PY'
import json, sys
lock = json.load(open(sys.argv[1]))["packages"]
want = dict(zip(["t3", "@anthropic-ai/claude-code", "opencode-ai", "@openai/codex"], sys.argv[2:]))
got = {n: lock[f"node_modules/{n}"]["version"] for n in want}
assert got == want, f"lock {got} != versions.env {want}; run env/tools/regen-lock.sh"
PY
echo "ok: package-lock.json matches versions.env"
