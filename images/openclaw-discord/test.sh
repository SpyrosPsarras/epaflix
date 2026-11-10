#!/usr/bin/env bash
set -euo pipefail
IMAGE="${1:-openclaw-discord:dev}"

docker run --rm -i --entrypoint bash "$IMAGE" -s <<'CHECKS'
set -euo pipefail
fail() { echo "FAIL: $*"; exit 1; }

echo "== 1. gh, git, curl and node are on PATH, python3 makes venvs, and the image runs as UID 1000 =="
for c in gh git curl node; do command -v "$c" || fail "$c not on PATH"; done
gh --version
[ "$(id -u)" = 1000 ] || fail "runs as UID $(id -u), not 1000"
python3 -m venv /tmp/venv-check && /tmp/venv-check/bin/pip --version || fail "python3 -m venv cannot create a venv with pip"
echo ok

echo "== 2. the plugins resolve openclaw from /app =="
plugins=/opt/openclaw-plugins/node_modules
[ "$(readlink "$plugins/openclaw")" = /app ] || fail "$plugins/openclaw is not a symlink to /app"
echo ok

HOME=$(mktemp -d)
export HOME OPENCLAW_CONFIG_PATH="$HOME/openclaw.json" OPENCLAW_CONFIG_READONLY=1
config() {
  node -e '
const c = {
  gateway: { mode: "local", bind: "loopback", controlUi: { enabled: false } },
  models: { providers: { cliproxy: {
    baseUrl: "http://127.0.0.1:1", api: "anthropic-messages", apiKey: "dummy-key",
    models: [{ id: "claude/claude-haiku-5-5", name: "Haiku", input: ["text"] }],
  } } },
  agents: { defaults: { model: { primary: "cliproxy/claude/claude-haiku-5-5" }, sandbox: { mode: "off" } } },
  tools: { web: { search: { provider: "searxng" } } },
  plugins: {
    load: { paths: [
      "/opt/openclaw-plugins/node_modules/@openclaw/discord",
      "/opt/openclaw-plugins/node_modules/@openclaw/searxng-plugin",
    ] },
    entries: { searxng: { config: { webSearch: { baseUrl: "http://127.0.0.1:1" } } } },
  },
  channels: { discord: {
    enabled: true, token: "${DISCORD_BOT_TOKEN}", dmPolicy: "disabled", groupPolicy: "allowlist",
    guilds: { "100000000000000001": { channels: { "100000000000000002": {
      enabled: true, requireMention: false, roles: ["100000000000000003"],
    } } } },
  } },
};
const variant = process.argv[1];
if (variant === "unknown-key") c.channels.discord.notARealKey = true;
if (variant === "allow") c.plugins.allow = ["discord", "searxng"];
require("fs").writeFileSync(process.env.OPENCLAW_CONFIG_PATH, JSON.stringify(c));
' "$1"
}
validate() { config "$1"; openclaw config validate --json 2>/dev/null || true; }

echo "== 3. the sample config validates with both plugins on plugins.load.paths =="
out=$(validate base)
echo "$out" | grep -q '"valid": *true' || fail "sample config is not valid: $out"
echo ok

echo "== 4. an unknown channels.discord key is rejected, so the Discord plugin loaded =="
out=$(validate unknown-key)
echo "$out" | grep -q '"valid": *false' || fail "unknown key was accepted: $out"
echo "$out" | grep -q 'notARealKey' || fail "rejection does not name the unknown Discord key: $out"
echo ok

echo "== 5. plugins.allow [discord, searxng] still validates =="
out=$(validate allow)
echo "$out" | grep -q '"valid": *true' || fail "allowlisted config is not valid: $out"
echo ok

echo "== 6. Discord starts from the load path: a dummy token ends in auth failure, not a trust refusal =="
config allow
openclaw plugins inspect discord --json > /tmp/inspect.json 2>/dev/null
grep -q '"status": *"loaded"' /tmp/inspect.json || fail "discord plugin did not load"
openclaw plugins inspect searxng --json > /tmp/inspect-searxng.json 2>/dev/null
grep -q '"status": *"loaded"' /tmp/inspect-searxng.json || fail "searxng plugin did not load"
DISCORD_BOT_TOKEN=dummy openclaw gateway run --port 18789 --auth none > /tmp/gateway.log 2>&1 &
gateway=$!
for _ in $(seq 1 120); do
  grep -q "Failed to resolve Discord application id: .*401" /tmp/gateway.log && break
  sleep 1
done
kill "$gateway" 2>/dev/null || true
grep -a "\[discord\]" /tmp/gateway.log || true
grep -q "Failed to resolve Discord application id: .*401" /tmp/gateway.log || { tail -40 /tmp/gateway.log; fail "no Discord auth failure within 120 s"; }
if grep -aEi "only available for trusted plugins|trust-refused|PLUGIN_TRUST_REFUSED|ingress queue is unavailable|ingressUnavailable" /tmp/gateway.log; then
  fail "Discord channel was refused for plugin trust"
fi
echo ok

echo "== 7. render.mjs fills the Discord IDs and copies AGENTS.md =="
mkdir -p /tmp/render/config
echo '{"guilds": {"__DISCORD_GUILD_ID__": {"__DISCORD_CHANNEL_ID__": "${DISCORD_BOT_TOKEN}"}}}' > /tmp/render/config/openclaw.json
echo rules > /tmp/render/config/AGENTS.md
DISCORD_GUILD_ID=100000000000000001 DISCORD_CHANNEL_ID=100000000000000002 DISCORD_BOT_TOKEN=dummy \
  node /opt/openclaw-discord/render.mjs /tmp/render/config /tmp/render/state
grep -q '"100000000000000001": {"100000000000000002": "${DISCORD_BOT_TOKEN}"}' /tmp/render/state/openclaw.json || fail "render.mjs output: $(cat /tmp/render/state/openclaw.json)"
[ "$(cat /tmp/render/state/workspace/AGENTS.md)" = rules ] || fail "render.mjs did not copy AGENTS.md"
echo ok

echo "ALL CHECKS PASSED"
CHECKS
