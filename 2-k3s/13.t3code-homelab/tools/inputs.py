#!/usr/bin/env python3
import argparse
import base64
import getpass
import hashlib
import importlib
import json
from pathlib import Path
import re
import runpy
import secrets
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT.parent / '13.t3code/env/files', ROOT.parent / '13.t3code/one/files'):
    sys.path.insert(0, str(directory))
validator = importlib.import_module('isolated-config')
CONTEXT = 'epaflix'
NAMESPACE = 't3code-homelab'
MODEL_URL = 'http://cliproxy.remote-pi.svc.cluster.local:8317'
HUB_URL = 'http://mcp-hub.mcp-hub-homelab.svc.cluster.local:8000'
PROJECT_URL = 'https://github.com/SpyrosPsarras/epaflix.git'
APPROVAL_REFERENCE = 'issue-1836'
REQUIRED_GRANTS = {'model-token', 'hub-token'}
OPTIONAL_GRANTS = {'github-token'}
DEFAULT_SKILL_FILE = Path('/tools/node_modules/@dietrichgebert/ponytail/skills/ponytail/SKILL.md')


def json_bytes(value):
    return json.dumps(value, sort_keys=True).encode()


def validate_grants(grants):
    if not REQUIRED_GRANTS <= set(grants) <= REQUIRED_GRANTS | OPTIONAL_GRANTS:
        raise ValueError('Only homelab grants are supported')


def runtime_config(files, grants):
    validate_grants(grants)
    if set(files) != validator.INPUTS:
        raise ValueError('Only homelab inputs are supported')
    return {
        'environment': 'homelab',
        'hubUrl': HUB_URL,
        'modelUrl': MODEL_URL,
        'projectUrl': PROJECT_URL,
        'gitAuth': 'github-token' if 'github-token' in grants else 'public',
        'approvals': {name: APPROVAL_REFERENCE for name in validator.APPROVALS},
        'sha256': {name: hashlib.sha256(value).hexdigest() for name, value in files.items()},
        'grantSha256': {name: hashlib.sha256(value).hexdigest() for name, value in grants.items()}}


def add_github_token(files, grants, token):
    if not re.fullmatch(r'github_pat_[A-Za-z0-9_]{40,}', token):
        raise ValueError('A fine-grained GitHub PAT is required')
    grants = {**grants, 'github-token': token.encode()}
    inputs = {name: value for name, value in files.items() if name != 'runtime.json'}
    return {**inputs, 'runtime.json': json_bytes(runtime_config(inputs, grants))}, grants


def public_inputs(skill_file=DEFAULT_SKILL_FILE):
    version = runpy.run_path(str(ROOT.parent / '13.t3code/one/files/cc-safety-net-install.py'))['VERSION']
    cli = f'.local/share/pi/cc-safety-net/{version}/dist/bin/cc-safety-net.js'
    def encoded(value):
        return {'data': base64.b64encode(value if isinstance(value, bytes) else json_bytes(value)).decode(), 'executable': False}
    skill = skill_file.read_bytes()
    bundle = {'sourceHome': '/home/t3', 'files': {
        'instructions.md': encoded((ROOT / 'instructions.md').read_bytes()),
        'skills/ponytail/SKILL.md': encoded(skill)}}
    policies = {'environment': 'homelab', 'files': {
        '.cc-safety-net/policy.json': encoded({'version': 1}),
        '.claude/settings.json': encoded({'hooks': {'PreToolUse': [{'hooks': [
            {'type': 'command', 'command': f'node "$HOME/{cli}" hook --coding-cli'}]}]}}),
        '.config/t3-policy/check.py': encoded((ROOT / 'tools/policy-check.py').read_bytes())},
        'checks': [{'argv': ['python3', '${HOME}/.config/t3-policy/check.py', cli], 'exit': 0}]}
    return {'bundle.json': json_bytes(bundle), 'policies.json': json_bytes(policies)}


class Cluster:
    def command(self, *args, value=None):
        result = subprocess.run(['kubectl', '--context', CONTEXT, *args], input=value, capture_output=True)
        if result.returncode:
            raise RuntimeError('Kubernetes operation failed; no command output was printed')
        return result.stdout

    def get(self, kind, namespace, name):
        args = ['get', kind, name, '--ignore-not-found', '-o', 'json']
        if namespace:
            args += ['-n', namespace]
        raw = self.command(*args)
        return json.loads(raw) if raw.strip() else None

    def put(self, resource):
        metadata = resource['metadata']
        current = self.get(resource['kind'], metadata.get('namespace'), metadata['name'])
        if current and 'resourceVersion' not in metadata:
            metadata['resourceVersion'] = current['metadata']['resourceVersion']
        self.command('replace' if current else 'create', '-f', '-', value=json_bytes(resource))

    def secret(self, namespace, name):
        resource = self.get('secret', namespace, name)
        return {key: base64.b64decode(value) for key, value in resource['data'].items()} if resource else {}


def secret_resource(namespace, name, data):
    return {'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': name, 'namespace': namespace},
            'type': 'Opaque', 'data': {key: base64.b64encode(value).decode() for key, value in data.items()}}


def require_stopped(cluster):
    workload = cluster.get('statefulset', NAMESPACE, 'homelab')
    pod = cluster.get('pod', NAMESPACE, 'homelab-0')
    if pod or (workload and workload['spec'].get('replicas', 1) != 0):
        raise ValueError('The homelab backend must be stopped and scaled to zero before updating Secrets')


def register_model_key(cluster, token):
    management = cluster.secret('remote-pi', 'cliproxy-secrets')['management-password'].decode()
    key = token.decode()
    endpoint = MODEL_URL + '/v0/management/api-keys'
    headers = {'Authorization': 'Bearer ' + management, 'Content-Type': 'application/json'}
    with urllib.request.urlopen(urllib.request.Request(endpoint, headers=headers), timeout=20) as response:
        keys = json.load(response)['api-keys']
    if any(other != key and other[:4] == key[:4] for other in keys):
        raise ValueError('Another model client already uses this masked key prefix')
    if key in keys:
        return
    request = urllib.request.Request(endpoint, data=json_bytes({'old': key, 'new': key}),
                                     method='PATCH', headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise RuntimeError('Model client registration failed')


def register_hub_digest(cluster, token):
    hub = cluster.get('secret', 'mcp-hub-homelab', 'mcp-hub-instance')
    credentials = json.loads(base64.b64decode(hub['data']['credentials.json']))
    digest = hashlib.sha256(token).hexdigest()
    old = credentials['clients'].get('t3code-homelab')
    if old and old != digest:
        raise ValueError('Refusing to replace an existing hub client grant')
    credentials['clients']['t3code-homelab'] = digest
    hub['data']['credentials.json'] = base64.b64encode(json_bytes(credentials)).decode()
    cluster.put(hub)
    return base64.b64decode(hub['data']['services.json'])


def agent_kubeconfig(cluster):
    if not cluster.get('serviceaccount', NAMESPACE, 'homelab-agent'):
        raise ValueError('Sync the tracked homelab access Application before provisioning')
    if not cluster.get('secret', NAMESPACE, 'homelab-agent-kubeconfig'):
        resource = secret_resource(NAMESPACE, 'homelab-agent-kubeconfig', {})
        resource['type'] = 'kubernetes.io/service-account-token'
        resource['metadata']['annotations'] = {'kubernetes.io/service-account.name': 'homelab-agent'}
        cluster.put(resource)
    for _ in range(30):
        account = cluster.secret(NAMESPACE, 'homelab-agent-kubeconfig')
        if account.get('token') and account.get('ca.crt'):
            break
        time.sleep(1)
    else:
        raise RuntimeError('Homelab Kubernetes grant was not populated')
    return json_bytes({'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'homelab',
        'clusters': [{'name': 'homelab', 'cluster': {'server': 'https://kubernetes.default.svc',
            'certificate-authority-data': base64.b64encode(account['ca.crt']).decode()}}],
        'users': [{'name': 'homelab-agent', 'user': {'token': account['token'].decode()}}],
        'contexts': [{'name': 'homelab', 'context': {'cluster': 'homelab', 'user': 'homelab-agent'}}]})


def provision(cluster, authorized_keys, skill_file=DEFAULT_SKILL_FILE):
    require_stopped(cluster)
    if cluster.get('secret', NAMESPACE, 't3-runtime-inputs'):
        raise ValueError('Inputs already exist; use the GitHub command or an explicitly reviewed rotation')
    keys = authorized_keys.read_bytes()
    result = subprocess.run(['ssh-keygen', '-l', '-f', str(authorized_keys)], capture_output=True)
    if result.returncode or not keys.splitlines() or any(not re.fullmatch(rb'ssh-ed25519 [A-Za-z0-9+/]+={0,2}( [^\r\n]+)?', line) for line in keys.splitlines()):
        raise ValueError('Valid option-free Ed25519 public login keys are required')
    files = {**public_inputs(skill_file), 'authorized_keys': keys, 'kubeconfig': agent_kubeconfig(cluster)}
    grants = cluster.secret(NAMESPACE, 't3-runtime-grants')
    grants.setdefault('model-token', ('t3-homelab-' + secrets.token_urlsafe(32)).encode())
    grants.setdefault('hub-token', ('homelab.' + secrets.token_urlsafe(32)).encode())
    validate_grants(grants)
    cluster.put(secret_resource(NAMESPACE, 't3-runtime-grants', grants))
    register_model_key(cluster, grants['model-token'])
    files['services.json'] = register_hub_digest(cluster, grants['hub-token'])
    files['runtime.json'] = json_bytes(runtime_config(files, grants))
    cluster.put(secret_resource(NAMESPACE, 't3-runtime-inputs', files))
    print('Homelab inputs and grants prepared. Reload the homelab hub before backend acceptance.')


def install_github(cluster):
    if not sys.stdin.isatty():
        raise ValueError('Run the GitHub installer in an interactive terminal')
    require_stopped(cluster)
    confirmation = input('Confirm the PAT resource owner is your personal account, not an organization [yes]: ')
    if confirmation != 'yes':
        raise ValueError('Personal resource-owner confirmation is required')
    token = getpass.getpass('Fine-grained personal PAT, hidden: ')
    files, grants = add_github_token(cluster.secret(NAMESPACE, 't3-runtime-inputs'),
                                   cluster.secret(NAMESPACE, 't3-runtime-grants'), token)
    cluster.put(secret_resource(NAMESPACE, 't3-runtime-grants', grants))
    cluster.put(secret_resource(NAMESPACE, 't3-runtime-inputs', files))
    print('Personal GitHub grant installed. Keep the backend stopped until its revised inputs are reviewed, merged and synced.')


def main():
    parser = argparse.ArgumentParser(description='Prepare only the homelab backend private inputs without displaying secrets.')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('github', help='Owner-only hidden-input fine-grained personal PAT installer')
    prepare = commands.add_parser('provision', help='First provisioning; no mixed-home imports')
    prepare.add_argument('--authorized-keys', type=Path, required=True)
    prepare.add_argument('--skill-file', type=Path, default=DEFAULT_SKILL_FILE)
    args = parser.parse_args()
    try:
        if args.command == 'github':
            install_github(Cluster())
        else:
            provision(Cluster(), args.authorized_keys, args.skill_file)
    except (Exception, KeyboardInterrupt):
        sys.exit('Homelab provisioning failed. No captured diagnostics or secret values were printed. Check resource presence, then retry.')


if __name__ == '__main__':
    main()
