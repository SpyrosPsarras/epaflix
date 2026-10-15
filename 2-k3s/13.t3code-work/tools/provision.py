#!/usr/bin/env python3
import argparse
import base64
import getpass
import hashlib
import importlib
import json
import os
import re
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

CONTEXT = 'epaflix'
MODEL_URL = 'http://cliproxy.remote-pi.svc.cluster.local:8317'
SERVICES = {'environment': 'work', 'services': ['searxng']}
RUNTIME_GRANTS = ('t3code-work', 't3-runtime-grants')
GITHUB_PAT = re.compile(rb'github_pat_[A-Za-z0-9_]{20,}')
MODEL_KEY_PREFIX = 't3-work-'
SEARXNG_ORIGIN = 'http://searxng-mcp.mcp-hub-work.svc.cluster.local:8000'


def digest(value):
    return hashlib.sha256(value).hexdigest()


def kubectl(*args, payload=None):
    result = subprocess.run(['kubectl', '--context', CONTEXT, *args], input=payload,
                            capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError('Cluster operation failed; private output suppressed')
    return json.loads(result.stdout) if result.stdout.strip() else None


def namespace(name):
    if not kubectl('get', 'namespace', name, '--ignore-not-found', '-o', 'json'):
        kubectl('create', 'namespace', name, '-o', 'json')


def read_secret(namespace_name, name):
    obj = kubectl('-n', namespace_name, 'get', 'secret', name, '--ignore-not-found', '-o', 'json')
    return {key: base64.b64decode(value, validate=True) for key, value in obj['data'].items()} if obj else None


def create_secret(namespace_name, name, data):
    obj = {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
           'metadata': {'namespace': namespace_name, 'name': name},
           'data': {key: base64.b64encode(value).decode() for key, value in data.items()}}
    kubectl('create', '-f', '-', '-o', 'json', payload=json.dumps(obj).encode())
    return data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(url, token, method='GET', body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': 'Bearer ' + token.decode(), 'Content-Type': 'application/json',
        'User-Agent': 'work-backend-provisioner'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=60) as response:
        return json.load(response)


def register_model(token):
    management = read_secret('remote-pi', 'cliproxy-secrets')['management-password']
    endpoint = MODEL_URL + '/v0/management/api-keys'
    current = request(endpoint, management)['api-keys']
    client_key = token.decode()
    if any(key != client_key and key.startswith(MODEL_KEY_PREFIX[:4]) for key in current):
        raise ValueError('Another client key shares the work attribution prefix; refusing registration')
    if client_key not in current:
        request(endpoint, management, 'PATCH', {'old': '', 'new': client_key})
    for attempt in range(10):
        try:
            request(MODEL_URL + '/v1/models', token)
            return
        except urllib.error.HTTPError as error:
            if error.code != 401 or attempt == 9:
                raise
            time.sleep(1)


def grants():
    for name in ('t3code-work', 'mcp-hub-work'):
        namespace(name)
    runtime = read_secret(*RUNTIME_GRANTS)
    if runtime is None:
        runtime = create_secret(*RUNTIME_GRANTS, {
            'model-token': (MODEL_KEY_PREFIX + secrets.token_urlsafe(32)).encode(),
            'hub-token': ('work.' + secrets.token_urlsafe(32)).encode()})
    if not runtime.get('model-token') or not runtime.get('hub-token', b'').startswith(b'work.'):
        raise ValueError('Existing work grants are invalid; refusing rotation')
    backend = read_secret('mcp-hub-work', 'mcp-hub-searxng')
    if backend is None:
        backend = create_secret('mcp-hub-work', 'mcp-hub-searxng', {'token': secrets.token_urlsafe(32).encode()})
    credentials = {'clients': {'t3code-work': digest(runtime['hub-token'])}, 'upstreams': {
        'searxng': {'url': SEARXNG_ORIGIN + '/mcp',
                    'token': backend['token'].decode()}}}
    desired = {'services.json': json.dumps(SERVICES).encode(),
               'credentials.json': json.dumps(credentials).encode()}
    existing = read_secret('mcp-hub-work', 'mcp-hub-instance')
    if existing is None:
        create_secret('mcp-hub-work', 'mcp-hub-instance', desired)
    else:
        live = json.loads(existing['credentials.json'])
        if live.get('clients', {}).get('t3code-work') != credentials['clients']['t3code-work'] or (
                live.get('upstreams', {}).get('searxng') != credentials['upstreams']['searxng']):
            raise ValueError('Owned work hub entries differ; refusing rotation')
    register_model(runtime['model-token'])


def require_stopped():
    if kubectl('-n', 't3code-work', 'get', 'pods', '-l', 'app.kubernetes.io/name=t3code', '-o', 'json')['items']:
        raise ValueError('Stop work consumers through the reviewed GitOps procedure first')


def github():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError('Owner enrollment requires a private interactive terminal')
    require_stopped()
    expected = getpass.getpass('Approved shared GitHub login, hidden: ')
    repository = getpass.getpass('Selected private work repository as org/repo, hidden: ')
    token = getpass.getpass('Dedicated organization fine-grained PAT, hidden: ').encode()
    if not GITHUB_PAT.fullmatch(token) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('A fine-grained PAT and selected organization repository are required')
    if not expected or request('https://api.github.com/user', token).get('login', '').casefold() != expected.casefold():
        raise ValueError('GitHub identity does not match the approved shared login')
    repo = request('https://api.github.com/repos/' + repository, token)
    if repo.get('private') is not True or repo.get('owner', {}).get('type') != 'Organization' or (
            repo.get('full_name', '').casefold() != repository.casefold()):
        raise ValueError('Selected repository must be private and belong to the approved organization')
    if input('Confirm PAT resource owner is this organization, with selected org repos only [yes/no]: ') != 'yes':
        raise ValueError('Organization-only resource owner confirmation is required')
    obj = kubectl('-n', RUNTIME_GRANTS[0], 'get', 'secret', RUNTIME_GRANTS[1], '-o', 'json')
    if 'github-token' in obj['data']:
        raise ValueError('Work GitHub grant already exists; refusing rotation')
    operations = [{'op': 'test', 'path': '/metadata/resourceVersion', 'value': obj['metadata']['resourceVersion']},
                  {'op': 'add', 'path': '/data/github-token', 'value': base64.b64encode(token).decode()}]
    kubectl('-n', RUNTIME_GRANTS[0], 'patch', 'secret', RUNTIME_GRANTS[1], '--type=json', '--patch-file=/dev/stdin', '-o', 'json',
            payload=json.dumps(operations).encode())


def inputs(directory):
    require_stopped()
    directory = Path(directory).resolve()
    root = Path(__file__).resolve().parents[2]
    for path in (root / '13.t3code/env/files', root / '13.t3code/one/files'):
        sys.path.insert(0, str(path))
    validator = importlib.import_module('isolated-config')
    names = validator.INPUTS | {'runtime.json', 'work-auth.json'}
    if {path.name for path in directory.iterdir() if not path.name.startswith('.')} != names:
        raise ValueError('Supply only the seven reviewed work input files')
    data = {name: (directory / name).read_bytes() for name in names}
    config = json.loads(data['runtime.json'])
    auth = json.loads(data['work-auth.json'])
    if config['modelUrl'] != MODEL_URL or config['gitAuth'] != 'github-token' or len(auth['contexts']) != 1:
        raise ValueError('Work requires the shared proxy, work GitHub grant and one reviewed AKS dev context')
    if json.loads(data['services.json']) != SERVICES:
        raise ValueError('Initial work service inventory must contain searxng only')
    runtime_grants = read_secret(*RUNTIME_GRANTS)
    if not GITHUB_PAT.fullmatch(runtime_grants.get('github-token', b'')):
        raise ValueError('Work requires its dedicated fine-grained GitHub grant')
    config['grantSha256'] = {name: digest(value) for name, value in runtime_grants.items()}
    data['runtime.json'] = json.dumps(config).encode()
    with tempfile.TemporaryDirectory() as temporary:
        temporary = Path(temporary)
        reviewed = temporary / 'inputs'
        reviewed.mkdir(mode=0o700)
        home = temporary / 'home'
        home.mkdir(mode=0o700)
        for name, value in data.items():
            path = reviewed / name
            path.write_bytes(value)
            path.chmod(0o600)
        saved = dict(os.environ)
        try:
            os.environ.clear()
            os.environ.update({'PATH': '/usr/local/bin:/usr/bin:/bin',
                               'ANTHROPIC_AUTH_TOKEN': runtime_grants['model-token'].decode(),
                               'MCP_HUB_TOKEN': runtime_grants['hub-token'].decode(),
                               'GITHUB_TOKEN': runtime_grants['github-token'].decode()})
            validator.validate(reviewed, home, 'work')
        finally:
            os.environ.clear()
            os.environ.update(saved)
    create_secret('t3code-work', 't3-runtime-inputs', data)


def main():
    parser = argparse.ArgumentParser(description='Stage isolated work Secrets without printing private values')
    parser.add_argument('action', choices=('grants', 'github', 'inputs'))
    parser.add_argument('--directory')
    args = parser.parse_args()
    try:
        if args.action == 'inputs':
            if not args.directory:
                parser.error('inputs requires --directory')
            inputs(args.directory)
        else:
            {'grants': grants, 'github': github}[args.action]()
    except Exception:
        print('Work provisioning refused or failed. Private diagnostics suppressed.', file=sys.stderr)
        return 1
    print('Work ' + args.action + ' staged. No Applications synced.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
