#!/usr/bin/env python3
"""Write Pi startup configuration without exposing credentials."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.request import Request, urlopen
from hub_clients import ASK


PACKAGES = ['pi-cliproxyapi-provider', '@gotgenes/pi-permission-system',
            '@spences10/pi-redact', '@juicesharp/rpiv-todo', 'jev-guard',
            'superpowers', '@dietrichgebert/ponytail']
ALIASES = {
    'openrouter/or-glm-5.3-flash': 'openrouter/z-ai/glm-5.3-flash',
    'openrouter/or-deepseek-v4-flash': 'openrouter/deepseek/deepseek-v4-flash',
    'openrouter/or-minimax-m3:free': 'openrouter/minimax/minimax-m3',
    'or-minimax-m3:free': 'openrouter/minimax/minimax-m3'}


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


def fetch_models(base_url, key):
    request = Request(base_url + '/models', headers={'Authorization': 'Bearer ' + key})
    with urlopen(request, timeout=15) as response:
        data = json.load(response)
    if not isinstance(data, dict):
        raise ValueError('Invalid model response')
    models = data.get('data')
    if not isinstance(models, list) or any(not isinstance(model, dict) or
                                          not isinstance(model.get('id'), str) for model in models):
        raise ValueError('Invalid model list')
    return models


def permissions():
    commands = ['mkfs*', 'dd *of=/dev/*', 'kubectl *delete *', 'kubectl *drain *',
                'kubectl *cordon *', 'helm *uninstall *', 'reboot*', 'shutdown*',
                'poweroff*', 'qm *stop *', 'qm *shutdown *', 'env', 'env *',
                'printenv*', 'set', 'set -o', 'set +o', 'export', 'export -p',
                'declare*', 'typeset*', '*/proc/*/environ*', '*/run/jev*']
    commands += ['ps e*'] + [f'ps {letter}*e*' for letter in 'abcdefghijklmnopqrstuvwxyz']
    policy = {'*': 'allow', 'bash': {'*': 'allow', **dict.fromkeys(commands, 'deny')},
              'path': {'*': 'allow', '/run/jev*': 'deny'}, 'mcp': {'*': 'allow'}}
    for server, tools in ASK.items():
        for tool in tools:
            name = re.sub(r'[^A-Za-z0-9_]', '_', f'mcp__{server}__{tool}')
            policy['mcp'][name] = 'ask'
    return {'permission': policy}


def write(home, safety_entry, ssh_path=Path('/scripts/homelab-ssh.md')):
    if not Path(safety_entry).is_absolute():
        raise ValueError('Safety extension entry must be an absolute path')
    agent = home / '.pi/agent'
    settings_path = agent / 'settings.json'
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    settings['packages'] = [str(Path('/tools/node_modules') / name) for name in PACKAGES] + [safety_entry]
    settings['skills'] = ['!**/.agents/skills/ponytail/**']
    settings.setdefault('pi-cliproxyapi-provider', {})['gpt56ContextWindow'] = 'canonical'
    write_json(settings_path, settings)
    write_json(agent / 'extensions/pi-permission-system/config.json', permissions())
    (agent / 'pi-permissions.jsonc').unlink(missing_ok=True)
    base_url = os.environ.get('ANTHROPIC_BASE_URL', '').rstrip('/') + '/v1'
    try:
        models = fetch_models(base_url, os.environ.get('ANTHROPIC_AUTH_TOKEN', ''))
        aliases = {}
        for model in sorted(models, key=lambda model: model['id']):
            ident = model['id']
            for source, target in [('claude/', 'anthropic/'), ('codex/', 'openai/')]:
                if ident.startswith(source):
                    aliases[ident] = target + ident[len(source):]
        if not aliases:
            raise ValueError('No supported models discovered')
        aliases.update(ALIASES)
    except Exception:
        print('pi-setup: model discovery failed; previous provider config kept', file=sys.stderr)
    else:
        write_json(agent / 'pi-cliproxyapi-provider/config.json', {
            'providerName': 'cliproxy', 'baseUrl': base_url, 'modelAliases': aliases})
    try:
        instructions = (home / '.claude/AGENTS.md').read_text() + '\n' + ssh_path.read_text()
    except OSError:
        print('pi-setup: instructions unavailable; previous AGENTS.md kept', file=sys.stderr)
    else:
        atomic_write(agent / 'AGENTS.md', instructions)


def jev_config(key_file, output):
    key = key_file.read_text().strip()
    if not key:
        raise ValueError('Empty key file')
    write_json(output, {'openRouterApiKey': key})


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
    secret = commands.add_parser('jev-config')
    secret.add_argument('key_file', type=Path)
    secret.add_argument('output', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'write':
            write(args.home, args.safety_entry)
        elif args.action == 'jev-config':
            jev_config(args.key_file, args.output)
        else:
            check_packages(args.home, sys.stdin.read())
    except (OSError, ValueError, TypeError) as error:
        parser.exit(1, f'pi-setup {args.action}: {type(error).__name__}: {error}\n')


if __name__ == '__main__':
    main()
