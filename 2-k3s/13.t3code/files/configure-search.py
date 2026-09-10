#!/usr/bin/env python3
"""Register SearXNG and disable native search, preserving other user settings."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        out.write(text)
    os.replace(out.name, path)


def configure(home):
    command = "/usr/local/bin/searxng-mcp"
    for relative in (".claude.json", ".claude/settings.json", ".config/opencode/opencode.json"):
        path = home / relative
        cfg = json.loads(path.read_text()) if path.exists() else {}
        if relative == ".claude.json":
            cfg.setdefault("mcpServers", {})["searxng"] = {
                "type": "stdio", "command": command, "args": [],
            }
        elif relative == ".claude/settings.json":
            deny = cfg.setdefault("permissions", {}).setdefault("deny", [])
            if "WebSearch" not in deny:
                deny.append("WebSearch")
        else:
            cfg.setdefault("$schema", "https://opencode.ai/config.json")
            cfg.setdefault("mcp", {})["searxng"] = {
                "type": "local", "command": [command], "enabled": True,
            }
            permission = cfg.setdefault("permission", {})
            if isinstance(permission, str):
                permission = cfg["permission"] = {"*": permission}
            permission["websearch"] = "deny"
        write(path, json.dumps(cfg, indent=2) + "\n")

    subprocess.run(["codex", "mcp", "add", "searxng", "--", command], check=True)
    path = Path(os.environ.get("CODEX_HOME", home / ".codex")) / "config.toml"
    text = path.read_text()
    # Only the root section owns web_search; preserve tables and project trust.
    first_table = re.search(r"^\s*\[", text, re.MULTILINE)
    index = first_table.start() if first_table else len(text)
    root, tables = text[:index], text[index:]
    root = re.sub(r"^web_search\s*=.*\n?", "", root, flags=re.MULTILINE)
    text = 'web_search = "disabled"\n' + root + tables
    tomllib.loads(text)
    write(path, text)


if __name__ == "__main__":
    configure(Path.home())
