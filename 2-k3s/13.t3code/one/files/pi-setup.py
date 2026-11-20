#!/usr/bin/env python3
"""Write Pi startup configuration without exposing credentials."""
import argparse
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.request


PACKAGES = ['pi-cliproxyapi-provider',
            '@spences10/pi-redact', '@juicesharp/rpiv-todo',
            '@juicesharp/rpiv-ask-user-question',
            'superpowers', '@dietrichgebert/ponytail', 'not-enough-retry',
            'pi-t3code-sticky-prefix']
ALIASES = {
    'or-glm-5.3-flash': 'openrouter/z-ai/glm-5.3-flash',
    'or-deepseek-v4-flash': 'openrouter/deepseek/deepseek-v4-flash',
    'or-minimax-m3:free': 'openrouter/minimax/minimax-m3'}
ACCOUNT_CLAUDE_ID = re.compile(r'([a-z0-9][a-z0-9-]*)/(claude-([a-z]+)-(\d+(?:-\d+)*))')
HAIKU_CONTEXT_WINDOW = 100000


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.pi-setup-')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def write_json(path, data):
    atomic_write(path, json.dumps(data, indent=2) + '\n')


def fetch_account_models(base_url):
    aliases, overrides = {}, {}
    if not base_url:
        return aliases, overrides
    try:
        token = os.environ.get('ANTHROPIC_AUTH_TOKEN')
        if not token:
            raise ValueError
        request = urllib.request.Request(base_url + '/v1/models')
        request.add_unredirected_header('Authorization', f'Bearer {token}')
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
            raise ValueError
    except (HTTPException, OSError, ValueError, RecursionError):
        print('pi-setup: proxy model list unavailable; account model entries skipped', file=sys.stderr)
        return aliases, overrides

    for model in payload['data']:
        model_id = model.get('id') if isinstance(model, dict) else None
        match = ACCOUNT_CLAUDE_ID.fullmatch(model_id) if isinstance(model_id, str) else None
        if not match:
            continue
        prefix, claude_id, family, version = match.groups()
        aliases[model_id] = 'anthropic/' + claude_id
        override = {'name': f"{prefix.capitalize()} {family.capitalize()} {version.replace('-', '.')}"}
        if family == 'haiku':
            override['contextWindow'] = HAIKU_CONTEXT_WINDOW
        overrides[model_id] = override
    return aliases, overrides


def write_provider(home, base_url, account_aliases):
    if not base_url:
        print('pi-setup: ANTHROPIC_BASE_URL is empty; previous provider config kept', file=sys.stderr)
        return
    write_json(home / '.pi/agent/pi-cliproxyapi-provider/config.json', {
        'providerName': 'cliproxy', 'baseUrl': base_url + '/v1', 'modelAliases': {**ALIASES, **account_aliases}})


def write_models(home, base_url, account_overrides):
    if not base_url:
        print('pi-setup: ANTHROPIC_BASE_URL is empty; previous models.json kept', file=sys.stderr)
        return
    path = home / '.pi/agent/models.json'
    models = json.loads(path.read_text()) if path.exists() else {}
    providers = models.setdefault('providers', {})
    overrides = providers.setdefault('cliproxy', {}).setdefault('modelOverrides', {})
    for model_id in tuple(overrides):
        if ACCOUNT_CLAUDE_ID.fullmatch(model_id):
            del overrides[model_id]
    overrides['claude-haiku-5-5'] = {'contextWindow': HAIKU_CONTEXT_WINDOW}
    overrides.update(account_overrides)
    providers['cliproxy-textgen'] = {
        'baseUrl': base_url, 'api': 'anthropic-messages', 'apiKey': '$CLIPROXYAPI_API_KEY',
        'models': [{'id': 'claude-haiku-5-5', 'name': 'Claude Haiku 5.5 (text generation)',
                    'reasoning': True, 'input': ['text'], 'contextWindow': 1000000,
                    'maxTokens': 128000, 'compat': {'forceAdaptiveThinking': True}}]}
    write_json(path, models)


def write(home, safety_entry, ssh_path=Path('/scripts/homelab-ssh.md')):
    if not Path(safety_entry).is_absolute():
        raise ValueError('Safety extension entry must be an absolute path')
    agent = home / '.pi/agent'
    settings_path = agent / 'settings.json'
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    settings['packages'] = [str(Path('/tools/node_modules') / name) for name in PACKAGES] + [safety_entry]
    settings['skills'] = ['!**/.agents/skills/ponytail/**']
    if os.environ.get('MCP_HUB_INSTANCE') == 'work':
        # Work skills load from the owner's ai-skills clone; `git pull` there updates them.
        settings['skills'].append(str(home / 'work/ai-skills/plugins'))
    settings.setdefault('pi-cliproxyapi-provider', {})['gpt56ContextWindow'] = 'canonical'
    settings.setdefault('retry', {})['maxRetries'] = 8
    write_json(settings_path, settings)
    base_url = os.environ.get('ANTHROPIC_BASE_URL', '').rstrip('/')
    account_aliases, account_overrides = fetch_account_models(base_url)
    write_models(home, base_url, account_overrides)
    write_provider(home, base_url, account_aliases)
    try:
        instructions = (home / '.claude/AGENTS.md').read_text() + '\n' + ssh_path.read_text()
    except OSError:
        print('pi-setup: instructions unavailable; previous AGENTS.md kept', file=sys.stderr)
    else:
        atomic_write(agent / 'AGENTS.md', instructions)


def check_packages(home, listing):
    packages = json.loads((home / '.pi/agent/settings.json').read_text())['packages']
    listed = {line.strip() for line in listing.splitlines()}
    missing = [package for package in packages if package not in listed or not Path(package).exists()]
    if not packages or missing:
        for package in missing:
            print(package, file=sys.stderr)
        raise ValueError('Registered package missing or unlisted')
    print('pi-setup: all registered packages listed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    setup = commands.add_parser('write')
    setup.add_argument('home', type=Path)
    setup.add_argument('safety_entry')
    check = commands.add_parser('check-packages')
    check.add_argument('home', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'write':
            write(args.home, args.safety_entry)
        else:
            check_packages(args.home, sys.stdin.read())
    except (OSError, ValueError, TypeError) as error:
        parser.exit(1, f'pi-setup {args.action}: {type(error).__name__}: {error}\n')


if __name__ == '__main__':
    main()
