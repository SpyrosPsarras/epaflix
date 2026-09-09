#!/usr/bin/env python3
"""Writes the OpenCode config for the t3code guest: cliproxy as an
openai-compatible provider. The model list is queried live from cliproxy's
/v1/models rather than hardcoded, so it tracks whatever cliproxy actually
serves (run by update.sh's daily timer once the guest is provisioned; skipped
on a fresh guest before /etc/t3code/t3code.env exists).

Excludes native subscription-account models by id pattern: claude-* (own
driver: claudeAgent) and anything codex-shaped plus gpt-image-* (own driver:
codex; also don't answer on /v1/chat/completions at all — verified empty
response for gpt-5.3-codex there, only /v1/responses works, which is what
the codex provider instance uses). cliproxy's /v1/models `owned_by`/`created`
fields do NOT reliably distinguish these: gpt-5.3-codex reports the same
shape ("OpenAI", no `created`) as the Copilot-routed chat-completions models
it sits next to, so id patterns are the only dependable signal here.

Also excludes non-chat utility models cliproxy reports alongside the real
ones: anything with "embedding" in its id, and "trajectory-compaction" (an
internal housekeeping model, not something you'd chat with) — neither
belongs in a chat-model picker.

Fails loudly rather than writing a broken/empty config: a cliproxy hiccup
during the nightly refresh should be visible, not silently wipe the model
list. The API key comes from the T3 env file and is never printed."""
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_MODEL_PREFERENCE = "or-glm-5.3-flash"

env = {}
for line in open("/etc/t3code/t3code.env"):
    line = line.strip()
    if line.startswith("ANTHROPIC_AUTH_TOKEN="):
        v = line.split("=", 1)[1]
        env["token"] = v[1:-1] if v.startswith("'") and v.endswith("'") else v
    if line.startswith("ANTHROPIC_BASE_URL="):
        env["base"] = line.split("=", 1)[1].strip("'\"")


def fetch_models(base, token):
    request = urllib.request.Request(
        f"{base}/v1/models", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.load(response)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        sys.exit(f"cliproxy /v1/models request failed: {exc}")
    entries = body.get("data", [])
    if not entries:
        sys.exit("cliproxy /v1/models returned no models")
    return entries


def is_native_account_model(model_id):
    return model_id.startswith("claude-") or "codex" in model_id or model_id.startswith(
        "gpt-image-"
    )


def is_non_chat_model(model_id):
    return "embedding" in model_id or model_id == "trajectory-compaction"


entries = fetch_models(env["base"], env["token"])
model_ids = sorted(
    entry["id"]
    for entry in entries
    if not is_native_account_model(entry["id"]) and not is_non_chat_model(entry["id"])
)
if not model_ids:
    sys.exit("no models left after filtering out native-account and non-chat entries")

default_model = (
    DEFAULT_MODEL_PREFERENCE if DEFAULT_MODEL_PREFERENCE in model_ids else model_ids[0]
)

cfg = {
    "$schema": "https://opencode.ai/config.json",
    "provider": {
        "cliproxy": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Cliproxy",
            "options": {
                "baseURL": f"{env['base']}/v1",
                "apiKey": env["token"],
            },
            "models": {model_id: {"name": model_id} for model_id in model_ids},
        }
    },
    "model": f"cliproxy/{default_model}",
    # The read-only KeePass vault, same server Claude Code and Codex use, so a
    # credential lookup works in whichever harness you happen to be in.
    # It belongs HERE rather than in provision.sh next to the other two
    # registrations: this script rewrites opencode.json from scratch on every
    # daily refresh, so an mcp block added to that file directly would be
    # silently erased the next morning.
    "mcp": {
        "keepass": {
            "type": "local",
            "command": ["/usr/local/bin/keepass-mcp"],
            "enabled": True,
        }
    },
    # Claude's skill library, pointed at rather than duplicated. OpenCode takes
    # a search path here, so unlike Codex (which needs one symlink per skill -
    # see provision.sh) a newly added skill is picked up with no re-run of
    # anything. Same folders, same SKILL.md files, one copy on disk.
    "skills": {"paths": ["/home/spyros/.claude/skills"]},
}

CONFIG_PATH = "/home/spyros/.config/opencode/opencode.json"
os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
# fchmod before writing, not chmod after: this file embeds the cliproxy client
# key, and a chmod that follows json.dump leaves it world-readable at the
# prevailing umask for however long the write takes.
with open(CONFIG_PATH, "w") as handle:
    os.fchmod(handle.fileno(), 0o600)
    json.dump(cfg, handle, indent=2)
print("opencode.json written with", len(model_ids), "models")
