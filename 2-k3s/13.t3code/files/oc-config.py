#!/usr/bin/env python3
"""Configure CLIProxyAPI without replacing the user's OpenCode settings."""
import json
import os
from pathlib import Path
import tempfile

path = Path.home() / ".config/opencode/opencode.json"
cfg = json.loads(path.read_text()) if path.exists() else {}
cfg.setdefault("$schema", "https://opencode.ai/config.json")
provider = cfg.setdefault("provider", {}).setdefault("cliproxy", {})
provider.update(npm="@ai-sdk/openai-compatible", name="CLIProxyAPI")
provider.setdefault("options", {}).update(
    baseURL="{env:ANTHROPIC_BASE_URL}/v1",
    apiKey="{env:ANTHROPIC_AUTH_TOKEN}",
)
# The config hook supplies the catalog each time OpenCode loads a workspace.
provider.pop("models", None)
cfg.setdefault("model", "cliproxy/or-glm-5.3-flash")
enabled_providers = cfg.setdefault("enabled_providers", [])
if "cliproxy" not in enabled_providers:
    enabled_providers.append("cliproxy")
cfg.setdefault("mcp", {}).setdefault("keepass", {
    "type": "local", "command": ["/usr/local/bin/keepass-mcp"], "enabled": True,
})
skill_path = str(Path.home() / ".claude/skills")
skill_paths = cfg.setdefault("skills", {}).setdefault("paths", [])
if skill_path not in skill_paths:
    skill_paths.append(skill_path)

# Install on setup and daily refresh, before removing the old static catalog.
plugins_dir = path.parent / "plugins"
plugins_dir.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(mode="w", dir=plugins_dir, delete=False) as hook:
    hook.write(Path(__file__).with_name("cliproxy-models.js").read_text())
    os.fchmod(hook.fileno(), 0o644)
os.replace(hook.name, plugins_dir / "cliproxy-models.js")
path.parent.mkdir(parents=True, exist_ok=True)
with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
    json.dump(cfg, out, indent=2)
    out.write("\n")
os.replace(out.name, path)
print("OpenCode configured for the CLIProxyAPI catalog")
