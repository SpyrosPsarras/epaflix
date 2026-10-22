#!/usr/bin/env python3
from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

private_config = importlib.import_module('private-config')
pi_setup = importlib.import_module('pi-setup')


class ConfigurationError(ValueError):
    def __init__(self, path, home):
        super().__init__(f'Invalid configuration: {os.path.relpath(path, home)}')


@contextmanager
def configuration(path, home):
    try:
        yield
    except Exception:
        raise ConfigurationError(path, home) from None


def safe(path, home):
    if not path.resolve().is_relative_to(home):
        raise ConfigurationError(path, home)
    return path


def clear(state):
    if not isinstance(state, dict):
        raise ValueError('Unsupported agent configuration')
    for key, value in state.items():
        if key in {'mcpServers', 'mcp'}:
            if not isinstance(value, dict):
                raise ValueError('Unsupported MCP map')
            state[key] = {}
        elif key == 'enableAllProjectMcpServers':
            state[key] = False
        elif key in {'enabledMcpjsonServers', 'disabledMcpjsonServers'}:
            state[key] = []
        elif key == 'mcp_servers':
            raise ValueError('Unsupported alternate MCP configuration')
        elif isinstance(value, dict):
            clear(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    clear(item)
    return state


def has_mcp(state):
    if isinstance(state, dict):
        return any(key in {'mcp_servers', 'mcpServers', 'mcp'} and bool(value) or has_mcp(value)
                   for key, value in state.items())
    if isinstance(state, list):
        return any(has_mcp(value) for value in state)
    return False


def configure(home: Path, project: Path, guidance: Path,
              bundle: Path = Path('/private-agent-config/bundle.json')) -> None:
    home = home.resolve()
    guidance_bytes = guidance.read_bytes()
    if not guidance_bytes.strip():
        raise ValueError('Manager guidance is required')
    paths = {home / name for name in ('.pi/agent/mcp.json', '.claude.json', '.claude/.claude.json',
             '.claude/settings.json', '.claude/settings.local.json', '.config/opencode/opencode.json')}
    for directory in (project, home / '.t3/worktrees'):
        safe(directory, home)
        for current, dirs, names in os.walk(directory, followlinks=False):
            parent = Path(current)
            dirs[:] = [name for name in dirs if name not in {'.git', 'node_modules'}]
            for name in dirs:
                child = safe(parent / name, home)
                if child.is_symlink() and (name in {'.claude', '.codex', '.pi', '.mcp.json'}
                                           or name.startswith('opencode.json')):
                    raise ConfigurationError(child, home)
            if '.mcp.json' in names:
                paths.add(parent / '.mcp.json')
            if parent.name == '.pi' and 'mcp.json' in names:
                paths.add(parent / 'mcp.json')
            if parent.name == '.claude':
                paths.update(parent / name for name in ('settings.json', 'settings.local.json') if name in names)
            if parent.name == '.codex' and 'config.toml' in names:
                alternate = safe(parent / 'config.toml', home)
                with configuration(alternate, home):
                    if has_mcp(tomllib.loads(alternate.read_text())):
                        raise ValueError('Unsupported project MCP configuration')
            for name in ('opencode.json', 'opencode.jsonc'):
                if name in names:
                    raise ConfigurationError(parent / name, home)
    alternate = safe(home / '.config/opencode/opencode.jsonc', home)
    if alternate.exists():
        raise ConfigurationError(alternate, home)
    updates = {}
    for path in sorted(paths):
        safe(path, home)
        if path.exists():
            with configuration(path, home):
                updates[path] = clear(json.loads(path.read_text()))
    codex = safe(home / '.codex/config.toml', home)
    with configuration(codex, home):
        config = tomllib.loads(codex.read_text()) if codex.exists() else {}
        servers = config.pop('mcp_servers', {})
        if not isinstance(servers, dict) or has_mcp(config):
            raise ValueError('Unsupported alternate Codex MCP configuration')
    for name in ('.local/share/t3-private-config', '.claude/AGENTS.md', '.codex/AGENTS.md',
                 '.claude/skills', '.agents/skills', '.codex/AGENTS.override.md'):
        safe(home / name, home)
    with configuration(codex, home):
        for name in servers:
            subprocess.run(['codex', 'mcp', 'remove', name], check=True, capture_output=True,
                           env={**os.environ, 'HOME': str(home), 'CODEX_HOME': str(codex.parent)})
        if codex.exists() and has_mcp(tomllib.loads(codex.read_text())):
            raise ValueError('Codex MCP cleanup incomplete')
    for path, state in updates.items():
        with configuration(path, home):
            pi_setup.write_json(path, state)
    private_config.install(bundle.read_bytes(), home, instructions=guidance_bytes)
    pi_setup.atomic_write(home / '.codex/AGENTS.override.md', (home / '.codex/AGENTS.md').read_text())


if __name__ == '__main__':
    try:
        configure(*(Path(arg) for arg in sys.argv[1:]))
    except Exception as error:
        detail = f'; {error}' if isinstance(error, ConfigurationError) else ''
        sys.exit(f'manager-setup: {type(error).__name__}{detail}; startup refused')
