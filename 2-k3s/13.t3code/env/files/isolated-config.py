#!/usr/bin/env python3
"""Validate reviewed runtime inputs before reading or creating agent state."""
import base64
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
import tomllib

from urllib.parse import urlsplit

import hub_clients
import hub_services

private_config = importlib.import_module('private-config')
t3_pi_settings = importlib.import_module('t3-pi-settings')

INPUTS = {'bundle.json', 'policies.json', 'services.json', 'kubeconfig', 'authorized_keys'}
APPROVALS = {'git', 'models', 'policies', 'kubeconfig', 'hub', 'laptop', 'routing'}
CODEX_REQUIREMENTS = Path('/etc/codex/requirements.toml')
POLICY_PATHS = {'.cc-safety-net/policy.json', '.claude/settings.json'}
LEGACY_PERMISSION_PATH = '.pi/agent/extensions/pi-permission-system/config.json'


class InputError(ValueError):
    pass


def url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise InputError('Invalid approved endpoint')
    return value.rstrip('/')


def safe_policy_path(name):
    path = PurePosixPath(name)
    return not path.is_absolute() and '..' not in path.parts and str(path) == name and (
        name in POLICY_PATHS or name == LEGACY_PERMISSION_PATH or name.startswith('.config/t3-policy/'))


def validate(inputs, home, environment):
    reviewed_inputs = INPUTS | ({'work-auth.json'} if environment == 'work' else set())
    approvals = APPROVALS | ({'workAuth'} if environment == 'work' else set())
    if {path.name for path in inputs.iterdir() if not path.name.startswith('.')} != reviewed_inputs | {'runtime.json'}:
        raise InputError('Runtime inputs must contain only the reviewed files')
    config = json.loads((inputs / 'runtime.json').read_text())
    if environment not in {'homelab', 'work'} or config['environment'] != environment:
        raise InputError('Runtime environment mismatch')
    expected_hub = f'http://mcp-hub.mcp-hub-{environment}.svc.cluster.local:8000'
    if config['hubUrl'] != expected_hub:
        raise InputError('Only this environment hub is supported')
    url(config['modelUrl'])
    project = url(config['projectUrl'])
    if not project.startswith('https://github.com/') or not project.endswith('.git'):
        raise InputError('Project must be an explicitly approved GitHub HTTPS repository')
    if config['gitAuth'] not in {'public', 'github-token'} or set(config['approvals']) != approvals or any(
            not isinstance(reference, str) or not reference.strip() for reference in config['approvals'].values()):
        raise InputError('Explicit grant and policy reviews are required')
    if set(config['sha256']) != reviewed_inputs or any(
            hashlib.sha256((inputs / name).read_bytes()).hexdigest() != digest
            for name, digest in config['sha256'].items()):
        raise InputError('Reviewed input digest mismatch')
    token = os.environ.get('MCP_HUB_TOKEN', '')
    if not re.fullmatch(environment + r'\.[A-Za-z0-9_-]{40,}', token):
        raise InputError('An independent environment hub grant is required')
    if not os.environ.get('ANTHROPIC_AUTH_TOKEN'):
        raise InputError('An approved model grant is required')
    if bool(os.environ.get('GITHUB_TOKEN')) != (config['gitAuth'] == 'github-token'):
        raise InputError('Git grant does not match the approved authentication mode')
    grants = {'model-token': os.environ['ANTHROPIC_AUTH_TOKEN'], 'hub-token': token}
    if config['gitAuth'] == 'github-token':
        grants['github-token'] = os.environ['GITHUB_TOKEN']
    if config['grantSha256'] != {name: hashlib.sha256(value.encode()).hexdigest() for name, value in grants.items()}:
        raise InputError('Credential grants do not match their reviews')
    if any(os.environ.get(name) for name in ('SSH_AUTH_SOCK', 'GH_TOKEN', 'AZURE_CONFIG_DIR', 'BW_SESSION', 'BW_CLIENTID', 'BW_CLIENTSECRET')):
        raise InputError('Legacy credential environment is forbidden')
    services = hub_services.load_services(inputs / 'services.json', environment)
    bundle = json.loads((inputs / 'bundle.json').read_text())
    if not bundle.get('sourceHome') or not bundle.get('files') or 'instructions.md' not in bundle['files']:
        raise InputError('Required instructions are missing')
    for name, entry in bundle['files'].items():
        path = PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts or str(path) != name or (
                name != 'instructions.md' and not name.startswith('skills/')):
            raise InputError('Unsafe instruction bundle')
        base64.b64decode(entry['data'], validate=True)
    if not any(name.endswith('/SKILL.md') for name in bundle['files']):
        raise InputError('Required skills are missing')
    policies = json.loads((inputs / 'policies.json').read_text())
    if policies['environment'] != environment or not POLICY_PATHS.issubset(policies['files']) or not policies['checks']:
        raise InputError('Required organizational policy inputs and checks are missing')
    for name, entry in policies['files'].items():
        if not safe_policy_path(name):
            raise InputError('Unsafe policy target')
        base64.b64decode(entry['data'], validate=True)
    safety = json.loads(base64.b64decode(policies['files']['.cc-safety-net/policy.json']['data']))
    if safety.get('version') != 1 or any(value == 'off' for value in safety.get('secret_protection', {}).get('overrides', {}).values()):
        raise InputError('Secret protections must not be disabled')
    hooks = json.loads(base64.b64decode(policies['files']['.claude/settings.json']['data']))
    if not hooks.get('hooks') or hooks.get('disableAllHooks') or hooks.get('mcpServers') or hooks.get(
            'enableAllProjectMcpServers') or hooks.get('enabledMcpjsonServers'):
        raise InputError('Approved hooks are required and cannot add or auto-enable project MCP servers')
    for check in policies['checks']:
        if not isinstance(check['argv'], list) or not check['argv'] or any(not isinstance(arg, str) for arg in check['argv']) or not isinstance(check['exit'], int):
            raise InputError('Invalid policy acceptance check')
    result = subprocess.run(['kubectl', '--kubeconfig', str(inputs / 'kubeconfig'), 'config', 'view', '--raw', '-o', 'json'], capture_output=True)
    if result.returncode:
        raise InputError('Invalid isolated kubeconfig')
    kube = json.loads(result.stdout)
    if environment == 'work':
        importlib.import_module('work-aks-auth').validate(inputs, kube)
    else:
        validate_homelab_kubeconfig(kube)
    keys = (inputs / 'authorized_keys').read_text().splitlines()
    if not keys or any(not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]+={0,2}( [^\r\n]+)?', key) for key in keys):
        raise InputError('Only approved option-free Ed25519 public login keys are supported')
    result = subprocess.run(['ssh-keygen', '-l', '-f', str(inputs / 'authorized_keys')], capture_output=True)
    if result.returncode:
        raise InputError('Invalid public login key')
    marker = home / '.config/t3-runtime/environment'
    if marker.exists() or marker.is_symlink():
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise InputError('Persistent home marker must be owned and private')
        if marker.read_text() != environment + '\n':
            raise InputError('Persistent home belongs to another environment')
    else:
        files = [path for path in home.rglob('*') if path.is_file() or path.is_symlink()]
        allowed = home / '.t3/userdata/server-runtime.json'
        if (home / '.azure').exists() or any(path != allowed or path.is_symlink() or path.stat().st_size for path in files):
            raise InputError('First boot requires a clean home, not imported state')
    forbidden = ('.aws', '.config/gcloud') + (('.azure',) if environment == 'homelab' else ())
    if any((home / name).exists() or (home / name).is_symlink() for name in forbidden) or (home / '.azure').is_symlink():
        raise InputError('Cloud credential caches are forbidden')
    return config, services, policies


def validate_homelab_kubeconfig(kube):
    if any(len(kube.get(key, [])) != 1 for key in ('clusters', 'users', 'contexts')):
        raise InputError('Only one approved homelab context, cluster and user is allowed')
    cluster, user, context = kube['clusters'][0], kube['users'][0], kube['contexts'][0]
    if kube.get('current-context') != context['name'] or context['context']['cluster'] != cluster['name'] or context['context']['user'] != user['name']:
        raise InputError('Invalid kubeconfig context references')
    if not url(cluster['cluster']['server']).startswith('https://') or cluster['cluster'].get('insecure-skip-tls-verify') or not cluster['cluster'].get('certificate-authority-data'):
        raise InputError('Homelab API requires HTTPS and an inline trusted CA')
    if not set(user['user']).issubset({'token', 'client-certificate-data', 'client-key-data'}) or not user['user']:
        raise InputError('Exec, cloud authentication and external credential files are forbidden')


def clear_services_snapshot(home, environment):
    if environment != 'homelab':
        return
    path = home / '.config/t3-runtime/services.json'
    if path.exists() or path.is_symlink():
        directory = path.parent.lstat()
        info = path.lstat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid() or directory.st_mode & 0o077 or (
                not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise InputError('Managed service snapshot must be owned and private')
        path.unlink()


def prepare(inputs, home, environment):
    clear_services_snapshot(home, environment)
    _, services, _ = validate(inputs, home, environment)
    marker = home / '.config/t3-runtime/environment'
    marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_private(marker, (environment + '\n').encode())
    private_config.install((inputs / 'bundle.json').read_bytes(), home)
    if environment == 'homelab':
        write_private(marker.parent / 'services.json', json.dumps(services).encode())


def write_private(path, data, mode=0o600):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.isolated-policy-')
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def toml_value(value):
    if isinstance(value, dict):
        return '{' + ', '.join(toml_value(key) + ' = ' + toml_value(item) for key, item in value.items()) + '}'
    if isinstance(value, list):
        return '[' + ', '.join(toml_value(item) for item in value) + ']'
    if isinstance(value, float):
        return str(value)
    if isinstance(value, (str, bool, int)):
        return json.dumps(value, ensure_ascii=False).replace('\x7f', '\\u007f')
    return value.isoformat()


def write_toml(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.isolated-codex-')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(''.join(toml_value(key) + ' = ' + toml_value(value) + '\n' for key, value in values.items()))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply(inputs, home, environment):
    clear_services_snapshot(home, environment)
    config, services, policies = validate(inputs, home, environment)
    aliases_path = home / '.pi/agent/pi-cliproxyapi-provider/config.json'
    aliases = json.loads(aliases_path.read_text())
    if aliases.get('providerName') != 'cliproxy' or aliases.get('baseUrl') != url(config['modelUrl']) + '/v1' or not aliases.get('modelAliases'):
        raise InputError('Approved model configuration was not installed')
    for name, entry in policies['files'].items():
        if name == LEGACY_PERMISSION_PATH:
            continue
        target = home / name
        if target.is_symlink() or any(parent.is_symlink() for parent in target.parents if parent.is_relative_to(home)):
            raise InputError('Policy target must not be a symlink')
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_private(target, base64.b64decode(entry['data'], validate=True),
                      0o700 if entry.get('executable') else 0o600)
    safety_path = home / '.cc-safety-net/policy.json'
    safety = json.loads(safety_path.read_text())
    denied = safety.setdefault('secret_protection', {}).setdefault('deny_paths', [])
    for path in (str(home / '.ssh'), str(home / '.config/t3-sshd'), str(inputs), '/run/t3-grants', str(home / '.azure')):
        if path not in denied:
            denied.append(path)
    hub_clients._write_json(str(safety_path), safety)
    for check in policies['checks']:
        args = [arg.replace('${HOME}', str(home)) for arg in check['argv']]
        result = subprocess.run(args, cwd=home, capture_output=True, timeout=30,
                                env={'HOME': str(home), 'PATH': '/tools/node_modules/.bin:/usr/local/bin:/usr/bin:/bin'})
        if result.returncode != check['exit']:
            raise InputError('Organizational policy acceptance check failed')
    for relative in ('.pi/agent/mcp.json', '.claude.json', '.claude/.claude.json'):
        path = home / relative
        before = json.loads(path.read_text()) if path.exists() else {}
        before['mcpServers'] = {}
        if not relative.startswith('.pi'):
            for state in [before, *before.get('projects', {}).values()]:
                state['mcpServers'] = {}
                state['enableAllProjectMcpServers'] = False
                state['enabledMcpjsonServers'] = []
        after = hub_clients.pi(before, config['hubUrl'], 'MCP_HUB_TOKEN', services) if relative.startswith('.pi') else hub_clients.claude(before, config['hubUrl'], 'Bearer ${MCP_HUB_TOKEN}', services)
        hub_clients._write_json(str(path), after)
    project = home / 'projects' / environment
    for path in (home / '.claude/settings.local.json', project / '.claude/settings.json',
                 project / '.claude/settings.local.json', project / '.mcp.json'):
        if path.exists():
            before = json.loads(path.read_text())
            settings = dict(before)
            managed = {'mcpServers': {}}
            if path.name != '.mcp.json':
                managed.update(disableAllHooks=False, enableAllProjectMcpServers=False, enabledMcpjsonServers=[])
            for key, value in managed.items():
                if key in settings:
                    settings[key] = value
            if settings != before:
                hub_clients._write_json(str(path), settings)
    path = home / '.codex/config.toml'
    codex = tomllib.loads(path.read_text()) if path.exists() else {}
    codex['model_provider'] = 'cliproxy'
    codex['model_providers'] = {'cliproxy': {'name': 'cliproxy', 'base_url': url(config['modelUrl']) + '/v1',
                                          'env_key': 'ANTHROPIC_AUTH_TOKEN', 'wire_api': 'responses'}}
    codex['mcp_servers'] = {}
    codex.pop('profile', None)
    codex.pop('profiles', None)
    for name, route in hub_services.client_servers(services).items():
        codex['mcp_servers'][name] = {'url': config['hubUrl'] + route, 'bearer_token_env_var': 'MCP_HUB_TOKEN'}
    projects = codex.get('projects', {})
    codex['projects'] = {name: state for name, state in projects.items()
                         if Path(name).is_absolute() and Path(name).resolve().is_relative_to(project.resolve())}
    write_toml(path, codex)
    write_toml(CODEX_REQUIREMENTS, {
        'model_provider': codex['model_provider'], 'model_providers': codex['model_providers'],
        'mcp_servers': {name: {'identity': {'url': server['url']}} for name, server in codex['mcp_servers'].items()}})
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        t3_pi_settings.migrate(root, url(config['modelUrl']))
        canonical = json.loads((root / 'userdata/settings.json').read_text())
    settings_path = home / '.t3/userdata/settings.json'
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    previous = settings.get('providerInstances', {})
    settings['providerInstances'] = canonical['providerInstances']
    for name, instance in settings['providerInstances'].items():
        retained = previous.get(name, {})
        if retained.get('driver') != instance['driver']:
            continue
        if isinstance(retained.get('displayName'), str):
            instance['displayName'] = retained['displayName']
        if name != 'claudeAgent' and isinstance(retained.get('enabled'), bool):
            instance['enabled'] = retained['enabled']
        prefix = 'cliproxy/' if name == 'pi' else 'codex/'
        models = retained.get('config', {}).get('customModels')
        if name != 'claudeAgent' and isinstance(models, list):
            instance.setdefault('config', {})['customModels'] = [model for model in models
                if isinstance(model, str) and model.startswith(prefix) and len(model) > len(prefix)]
    settings.pop('providers', None)
    for key in ('defaultModelSelection', 'textGenerationModelSelection'):
        current = settings.get(key, {})
        name = current.get('instanceId')
        prefix = {'pi': 'cliproxy/', 'codex': 'codex/'}.get(name)
        if not prefix or not current.get('model', '').startswith(prefix) or len(current['model']) <= len(prefix):
            settings[key] = canonical[key]
    hub_clients._write_json(str(settings_path), settings)
    if environment == 'homelab':
        write_private(home / '.config/t3-runtime/services.json', json.dumps(services).encode())
    print('isolated-config: reviewed policies and restricted hub installed')


if __name__ == '__main__':
    try:
        action, source, target, environment = sys.argv[1:]
        {'validate': validate, 'prepare': prepare, 'apply': apply}[action](Path(source), Path(target), environment)
    except InputError as error:
        sys.exit(f'isolated-config: {error}; startup refused')
    except Exception as error:
        sys.exit(f'isolated-config: {type(error).__name__}; startup refused')
