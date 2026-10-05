#!/usr/bin/env bash
# Runs inside the t3code pod with image-installed CLIs in /tools. This script
# seeds the persisted HOME on the PVC, migrates legacy provider settings,
# and starts T3 without opening a browser.
#
# Required env: ANTHROPIC_BASE_URL, ANTHROPIC_AUTH_TOKEN, HOME (PVC mount).
# Optional env: GITHUB_TOKEN (git push over https), T3_PROJECT_REPO,
# T3_PROJECT_DIR (checkout path; the merged t3code server keeps $HOME/epaflix).
set -euo pipefail

T3_PROJECT_REPO=${T3_PROJECT_REPO:-https://github.com/SpyrosPsarras/epaflix.git}
PROJECT_DIR=${T3_PROJECT_DIR:-$HOME/projects/$(basename "$T3_PROJECT_REPO" .git)}
export PATH=/tools/node_modules/.bin:$PATH

# github.com-only credential helper (files/git-credential-github.sh), set
# before the clone so a private remote works on first boot. The token stays
# in the process env; the helper prints it only to git, only for github.com.
mkdir -p "$(dirname "$PROJECT_DIR")"
git config --global credential.helper /scripts/git-credential-github.sh

# ssh expands ~ from /etc/passwd (/home/node), not $HOME, so its config goes
# there. It is rewritten on every start.
SSH_DIR=$(getent passwd "$(id -u)" | cut -d: -f6)/.ssh
mkdir -m 0700 "$SSH_DIR" 2>/dev/null || chmod 0700 "$SSH_DIR"
{
  # Homelab hosts (generated from the .lan DNS records, 1-proxmox/ssh). First,
  # so no Host block scopes the Include. They include
  # ~/.ssh/homelab-identity.conf: the key comes from SSH_AUTH_SOCK (the vault
  # agent below); host keys persist on the PVC and a new host's key is saved on
  # first connect, then must never change.
  printf 'Include /scripts/homelab-ssh.conf\n\n'
  # git@github.com: remotes (the Davidhorn checkouts).
  if [[ -r /run/t3-github-ssh/identity ]]; then
    cat <<'EOF'
Host github.com
  User git
  IdentityFile /run/t3-github-ssh/identity
  IdentitiesOnly yes
  UserKnownHostsFile /run/t3-github-ssh/known_hosts
  StrictHostKeyChecking yes
EOF
  fi
} >"$SSH_DIR/config"
# ssh does not create the directory of a UserKnownHostsFile; a fresh PVC has none.
mkdir -p -m 0700 "$HOME/.ssh"
# OpenSSH 9.2 expands ~ in Include from $HOME (the PVC), not the passwd home,
# so write the file to both.
for d in "$SSH_DIR" "$HOME/.ssh"; do
  printf 'UserKnownHostsFile %s/.ssh/known_hosts\nStrictHostKeyChecking accept-new\n' "$HOME" >"$d/homelab-identity.conf"
  chmod 0600 "$d/homelab-identity.conf"
done
chmod 0600 "$SSH_DIR/config"

# Homelab SSH keys from Vaultwarden (files/vault-ssh-agent.py). T3 and its
# agents get only the agent socket; the vault login stays in /run/t3-vaultwarden
# and bw's data dir in the memory-backed /run/t3-bw, both denied by
# cc-safety-net (ssh-policy.py). Keys load once per container start.
if [[ -s /run/t3-vaultwarden/password ]]; then
  if python3 /scripts/vault-ssh-agent.py /run/t3-vaultwarden /tmp/t3-ssh-agent/agent.sock /run/t3-bw; then
    export SSH_AUTH_SOCK=/tmp/t3-ssh-agent/agent.sock
  else
    echo "t3env: vault SSH keys not loaded; SSH falls back to key files" >&2
  fi
else
  echo "t3env: no Vaultwarden login in t3code-vaultwarden; vault SSH keys skipped" >&2
fi

# `ssh t3code` (t3code-ssh Service). authorized_keys goes to the passwd home:
# StrictModes rejects the PVC home (mode 2777). Sessions do not get T3's env tokens.
SSHD_DIR=$HOME/.ssh/sshd
if [[ -r /scripts/sshd-authorized-keys && -x /usr/sbin/sshd ]] &&
  mkdir -p -m 0700 "$SSHD_DIR" &&
  { [[ -s $SSHD_DIR/ssh_host_ed25519_key ]] ||
    ssh-keygen -q -t ed25519 -N '' -C t3code -f "$SSHD_DIR/ssh_host_ed25519_key"; } &&
  install -m 0600 /scripts/sshd-authorized-keys "$SSH_DIR/authorized_keys" &&
  cat >"$SSHD_DIR/sshd_config" <<EOF
Port 2222
HostKey $SSHD_DIR/ssh_host_ed25519_key
PidFile none
UsePAM no
PasswordAuthentication no
KbdInteractiveAuthentication no
AllowAgentForwarding no
AllowTcpForwarding local
PermitOpen 127.0.0.1:3773 localhost:3773
X11Forwarding no
PermitTunnel no
PrintMotd no
SetEnv HOME=$HOME SSH_AUTH_SOCK=/tmp/t3-ssh-agent/agent.sock PATH=/tools/node_modules/.bin:/usr/local/bin:/usr/bin:/bin
EOF
then
  if /usr/sbin/sshd -t -f "$SSHD_DIR/sshd_config"; then
    ( while :; do /usr/sbin/sshd -D -e -f "$SSHD_DIR/sshd_config" || :; sleep 5; done ) &
  else
    echo "t3env: sshd config invalid; ssh t3code unavailable" >&2
  fi
else
  echo "t3env: sshd not set up; ssh t3code unavailable" >&2
fi
# /etc/profile resets PATH in login shells (`ssh t3code`), dropping the image CLIs.
printf '%s\n' 'PATH=/tools/node_modules/.bin:$PATH' '[ -r "$HOME/.profile" ] && . "$HOME/.profile"' \
  >"$HOME/.bash_profile"

# Same remote in both environments so T3 groups them as one project.
if [[ ! -d $PROJECT_DIR/.git ]]; then
  git clone -q "$T3_PROJECT_REPO" "$PROJECT_DIR"
fi

# OpenCode: cliproxy provider with env references, written once.
OC_DIR=$HOME/.config/opencode
mkdir -p "$OC_DIR/plugins"
# Replace read-only copies from earlier starts and keep the destination writable.
install -m 0644 /scripts/cliproxy-models.js "$OC_DIR/plugins/cliproxy-models.js"
# Retired shadow logger: the copy on the PVC would keep loading otherwise.
# Its jev-shadow.jsonl records stay in ~/.local/state/opencode.
rm -f "$OC_DIR/plugins/jev-shadow.js"
install -m 0644 /scripts/jev-auto.js "$OC_DIR/plugins/jev-auto.js"
# Tool-call guard (jev-guard.md); the config block below adds cc-safety-net and bash denies.
install -m 0644 /scripts/jev-guard.js "$OC_DIR/plugins/jev-guard.js"
# Permit vault attachment names while protecting SSH files in both HOME locations.
python3 /scripts/ssh-policy.py "$HOME/.cc-safety-net/policy.json" \
  "$HOME" "$(getent passwd "$(id -u)" | cut -d: -f6)" /root
# Retired OpenCode Loop: remove the copies earlier starts left on the PVC.
rm -f "$OC_DIR"/plugins/opencode-loop.{ts,js} "$OC_DIR"/commands/loop.md \
  "$OC_DIR"/commands/loop-*.md "$OC_DIR"/agents/opencode-loop-local.md
# When OpenCode uses the hub's jev MCP: screening, completion gates and
# picking by meaning. jev-auto.js adds it to the system prompt of OpenAI and
# Anthropic models only, so it is not a global instruction; the MCP hub block
# below removes it and the retired jev-first rule from opencode.json.
install -m 0644 /scripts/jev-checks.md "$OC_DIR/jev-checks.md"
rm -f "$OC_DIR/jev-first.md"
if [[ ! -f $OC_DIR/opencode.json ]]; then
  cat >"$OC_DIR/opencode.json" <<'EOF'
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "cliproxy": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "CLIProxyAPI",
      "options": {
        "baseURL": "{env:ANTHROPIC_BASE_URL}/v1",
        "apiKey": "{env:ANTHROPIC_AUTH_TOKEN}"
      }
    }
  },
  "model": "cliproxy/or-glm-5.3-flash",
  "enabled_providers": ["cliproxy"]
}
EOF
  chmod 0600 "$OC_DIR/opencode.json"
fi

# MCP hub (2-k3s/23.mcp-hub): every agent MCP server behind one gateway and
# this pod's hub token. Register every hub path for OpenCode, Claude and Codex
# in the active home; the token stays an env reference, never a literal on the PVC.
# hub_clients.py also replaces the stdio keepass/searxng bridges and the
# hosted Notion entries the hub superseded, and drops retired hub servers
# (keepass). The claudeAgent instances run with
# CLAUDE_CONFIG_DIR=<home>/.claude (<home>/.claude/.claude.json); a plain
# `claude` in a shell reads <home>/.claude.json. Both get the servers.
if [[ -n ${MCP_HUB_TOKEN:-} && -n ${MCP_HUB_URL:-} ]]; then
  for h in "$HOME"; do
    [[ -d $h ]] || continue
    python3 /scripts/hub_clients.py opencode "$h/.config/opencode/opencode.json" "$MCP_HUB_URL" \
      'Bearer {env:MCP_HUB_TOKEN}' || echo "t3env: hub opencode config failed in $h" >&2
    for cfg in "$h/.claude/.claude.json" "$h/.claude.json"; do
      python3 /scripts/hub_clients.py claude "$cfg" "$MCP_HUB_URL" 'Bearer ${MCP_HUB_TOKEN}' || \
        echo "t3env: hub claude config failed for $cfg" >&2
    done
    python3 /scripts/hub_clients.py codex "$h/.codex" "$MCP_HUB_URL" MCP_HUB_TOKEN || \
      echo "t3env: hub codex config failed in $h" >&2
  done
fi

# Primary home, with or without the hub: drop the Jev rules from the global
# instructions (jev-auto.js scopes jev-checks per model), keeping every other one.
# Verify/install the reviewed guard before startup; failure aborts startup.
/usr/bin/env -i PATH=/usr/local/bin:/usr/bin:/bin /usr/bin/python3 -I -S /scripts/cc-safety-net-install.py \
  "$HOME/.local/share/opencode/cc-safety-net/2.4.11-print-third" --config "$OC_DIR/opencode.json"
# Also seed bash denies once; an existing
# permission.bash is left as the user set it.
python3 - "$OC_DIR/opencode.json" "$OC_DIR/jev-checks.md" "$OC_DIR/jev-first.md" <<'PY' || echo "t3env: opencode.json guard/instructions update failed" >&2
import json, os, sys, tempfile
BASH_DENY = ["mkfs*", "dd *of=/dev/*", "kubectl delete *", "kubectl drain *", "kubectl cordon *",
             "helm uninstall *", "reboot*", "shutdown*", "poweroff*", "qm stop *", "qm shutdown *"]
path, *retired = sys.argv[1:]
with open(path) as f:
    config = json.load(f)
before = json.dumps(config, sort_keys=True)
if "instructions" in config:
    config["instructions"] = [i for i in config["instructions"] if i not in retired]
# Homelab SSH how-to (1-proxmox/ssh/homelab-ssh.md) for every OpenCode session.
if "/scripts/homelab-ssh.md" not in config.setdefault("instructions", []):
    config["instructions"].append("/scripts/homelab-ssh.md")
# No "*" key, so a top-level permission default still applies to other commands.
# Read rules are left to cc-safety-net and jev-guard: OpenCode matches read
# patterns against project-relative paths, so "**/.ssh/**" missed in a test.
permission = config.setdefault("permission", {})
if isinstance(permission, dict):
    permission.setdefault("bash", {p: "deny" for p in BASH_DENY})
if json.dumps(config, sort_keys=True) != before:
    with tempfile.NamedTemporaryFile(mode="w", dir=os.path.dirname(path), delete=False) as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    os.chmod(f.name, 0o600)
    os.replace(f.name, path)
PY

# T3 provider instances, written once so later edits from the UI survive
# restarts.
T3_HOME=$HOME/.t3
SETTINGS=$T3_HOME/userdata/settings.json
if [[ ! -f $SETTINGS ]]; then
  mkdir -p "$T3_HOME/userdata"
  cat >"$SETTINGS" <<EOF
{
  "providers": { "opencode": { "enabled": true } },
  "providerInstances": {
    "opencode": {
      "driver": "opencode",
      "displayName": "OpenCode (via cliproxy)",
      "enabled": true
    },
    "claudeAgent": {
      "driver": "claudeAgent",
      "displayName": "Claude (via cliproxy)",
      "enabled": false
    },
    "codex": {
      "driver": "codex",
      "displayName": "Codex (via cliproxy)",
      "enabled": true,
      "config": {
        "launchArgs": "-c model_providers.cliproxy.name=\"cliproxy\" -c model_providers.cliproxy.base_url=\"${ANTHROPIC_BASE_URL}/v1\" -c model_providers.cliproxy.env_key=\"ANTHROPIC_AUTH_TOKEN\" -c model_providers.cliproxy.wire_api=\"responses\" -c model_provider=\"cliproxy\"",
        "customModels": ["codex/codex-auto-review"]
      }
    }
  }
}
EOF
  chmod 0600 "$SETTINGS"
fi

# T3 only injects thread-scoped MCP tools into servers it manages. Migrate
# the endpoint seeded by older images, preserving other settings and URLs.
python3 - "$SETTINGS" <<'PY'
import json, os, sys, tempfile
path = sys.argv[1]
with open(path) as f:
    settings = json.load(f)
configs = [settings.get("providers", {}).get("opencode", {})]
configs += [v.setdefault("config", {}) for v in settings.get("providerInstances", {}).values()
            if v.get("driver") == "opencode"]
changed = False
for config in configs:
    if config.get("serverUrl") == "http://127.0.0.1:4096":
        del config["serverUrl"]
        changed = True
for config in configs[1:]:
    if config.get("binaryPath") != "/scripts/opencode-fast-version.sh":
        config["binaryPath"] = "/scripts/opencode-fast-version.sh"
        changed = True
# cliproxy runs with force-model-prefix: true, so Claude and Codex models route
# only as claude/... and codex/.... Claude Code offers only its built-in bare
# names, so its instances stay off; OpenCode serves Claude from the catalog.
# T3's built-in bare Codex names fail the same way. The seeded Codex
# list was bare too, and gpt-5.3-codex is no longer served at all.
for instance in settings.get("providerInstances", {}).values():
    if instance.get("driver") == "claudeAgent" and instance.get("enabled", True):
        instance["enabled"] = False
        changed = True
    config = instance.get("config", {})
    if instance.get("driver") == "codex" and config.get("customModels") == ["gpt-5.3-codex", "codex-auto-review"]:
        config["customModels"] = ["codex/codex-auto-review"]
        changed = True
if changed:
    with tempfile.NamedTemporaryFile(mode="w", dir=os.path.dirname(path), delete=False) as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    os.replace(f.name, path)
    print("t3env: migrated T3 provider settings")
PY

echo "t3env: $(t3 --version) claude=$(claude --version 2>/dev/null | head -1) opencode=$(opencode --version 2>/dev/null | head -1) codex=$(codex --version 2>/dev/null | head -1)"
python3 /scripts/private-config.py install /private-agent-config/bundle.json
# Codex has no extra-instructions list: it reads ~/.codex/AGENTS.override.md
# instead of AGENTS.md when present, so give it the bundle plus the homelab SSH how-to.
# Written whole or not at all: a partial override would hide the bundle.
if ! { cat "$HOME/.codex/AGENTS.md" /scripts/homelab-ssh.md >"$HOME/.codex/AGENTS.override.md.tmp" &&
        mv -f "$HOME/.codex/AGENTS.override.md.tmp" "$HOME/.codex/AGENTS.override.md"; } 2>/dev/null; then
  rm -f "$HOME/.codex/AGENTS.override.md" "$HOME/.codex/AGENTS.override.md.tmp" || true
  echo "t3env: codex homelab SSH instructions not written; Codex uses the bundle alone" >&2
fi
# `serve` forces project bootstrap off; `start --no-browser` honors the flag.
exec t3 start --no-browser --host 0.0.0.0 --port 3773 --base-dir "$T3_HOME" \
  --auto-bootstrap-project-from-cwd "$PROJECT_DIR"
