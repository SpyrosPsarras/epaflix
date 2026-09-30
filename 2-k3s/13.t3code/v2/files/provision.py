#!/usr/bin/env python3
"""Apply an explicit private-file allowlist, preserving v2 edits and auth refresh."""
import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import tempfile
import tomllib

MANAGED = {
    '.kube/config', '.kube/davidhornkubeconfig', '.config/gh/config.yml',
    '.config/gh/hosts.yml', '.config/opencode/opencode.json', '.codex/config.toml',
    '.claude/settings.json', '.claude.json', '.claude/.claude.json',
    '.local/bin/aks-auth', '.local/bin/aks-auth-central',
}
AUTH = {
    '.azure/msal_token_cache.json', '.azure/msal_http_cache.bin',
    '.azure/azureProfile.json', '.azure/config', '.azure/clouds.config',
    '.claude/.credentials.json', '.local/share/opencode/auth.json',
}
SEED = {'.config/opencode/opencode.json', '.codex/config.toml', '.claude.json', '.claude/.claude.json', '.claude/settings.json'}


def merge_keys(current, old, incoming, prefix=''):
    result = dict(current)
    conflicts = []
    for key, value in incoming.items():
        label = prefix + key
        if isinstance(value, dict) and isinstance(current.get(key), dict) and isinstance(old.get(key), dict):
            result[key], nested = merge_keys(current[key], old[key], value, label + '.')
            conflicts += nested
        elif key not in current or current[key] == old.get(key) or current[key] == value:
            result[key] = value
        else:
            conflicts.append(label)
    return result, conflicts


def toml_lines(text):
    # Existing Codex configuration uses single-line assignments and table headers.
    # Reject multiline values rather than rewrite formatting we cannot preserve.
    tomllib.loads(text)
    section = ''
    values = {}
    for index, line in enumerate(text.splitlines(keepends=True)):
        stripped = line.strip()
        if stripped.startswith('['):
            section = stripped
        elif stripped and not stripped.startswith('#'):
            match = re.match(r'\s*([^=]+?)\s*=\s*(.+?)\s*$', line)
            if not match or '"""' in line or "'''" in line:
                raise ValueError('Unsupported managed TOML formatting')
            values[(section, match[1].strip())] = (index, line)
    return values


def merge_toml(current, old, incoming):
    old_values, new_values = toml_lines(old), toml_lines(incoming)
    current_values = toml_lines(current)
    lines = current.splitlines(keepends=True)
    conflicts = []
    for key, (_, line) in new_values.items():
        current_values = toml_lines(''.join(lines))
        if key in current_values:
            index, existing = current_values[key]
            if existing == old_values.get(key, (None, None))[1] or existing == line:
                lines[index] = line
            else:
                conflicts.append('.'.join(key))
        else:
            section, _ = key
            if section:
                sections = [index for index, value in enumerate(lines) if value.strip() == section]
                if sections:
                    start = sections[-1] + 1
                    end = next((index for index in range(start, len(lines)) if lines[index].strip().startswith('[')), len(lines))
                    lines.insert(end, line)
                else:
                    lines.extend(['\n', section + '\n', line])
            else:
                lines.insert(0, line)
    merged = ''.join(lines)
    tomllib.loads(merged)
    return merged, conflicts


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install(home, files):
    home = home.resolve()
    state = home / '.local/state/t3-v2/provision.json'
    for parent in [state, *state.parents]:
        if parent == home:
            break
        if parent.is_symlink():
            raise ValueError('Symlink in provisioning metadata path')
    previous = json.loads(state.read_text()) if state.exists() else {}
    source_state = home / '.local/state/t3-v2/source.json'
    sources = json.loads(source_state.read_text()) if source_state.exists() else {}
    conflicts = []
    validated = {}
    for name, value in files.items():
        if name not in MANAGED | AUTH:
            raise ValueError('Path is outside provisioning allowlist')
        path = home / name
        for parent in [path, *path.parents]:
            if parent == home:
                break
            if parent.is_symlink():
                raise ValueError('Symlink in provisioning path')
        validated[name] = value.encode() if isinstance(value, str) else value
    for name, data in validated.items():
        path = home / name
        if path.exists():
            if name in AUTH:
                continue
            if name in SEED and name.endswith('.json'):
                current = json.loads(path.read_text())
                incoming = json.loads(data)
                old = sources.get(name, incoming)
                merged, keys = merge_keys(current, old, incoming)
                conflicts += [name + ':' + key for key in keys]
                atomic(path, json.dumps(merged, indent=2).encode())
                sources[name] = incoming
                continue
            if name == '.codex/config.toml':
                incoming = data.decode()
                merged, keys = merge_toml(path.read_text(), sources.get(name, incoming), incoming)
                conflicts += [name + ':' + key for key in keys]
                atomic(path, merged.encode())
                sources[name] = incoming
                continue
            current = digest(path.read_bytes())
            if current != previous.get(name) and current != digest(data):
                conflicts.append(name)
                continue
        atomic(path, data, 0o700 if name.startswith('.local/bin/') else 0o600)
        previous[name] = digest(data)
        if name in SEED and name.endswith('.json'):
            sources[name] = json.loads(data)
        elif name == '.codex/config.toml':
            sources[name] = data.decode()
    atomic(state, json.dumps(previous, sort_keys=True).encode())
    atomic(source_state, json.dumps(sources, sort_keys=True).encode())
    return conflicts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=pathlib.Path, required=True)
    parser.add_argument('--manifest', type=pathlib.Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    files = {name: base64.b64decode(value, validate=True) for name, value in manifest['files'].items()}
    conflicts = install(args.home, files)
    print(json.dumps({'conflicts': conflicts, 'installedOrPreserved': len(files)}))


if __name__ == '__main__':
    main()
