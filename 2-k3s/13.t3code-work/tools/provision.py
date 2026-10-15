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


class ProvisionError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(value).hexdigest()


def kubectl(*args, payload=None):
    try:
        result = subprocess.run(['kubectl', '--context', CONTEXT, *args], input=payload,
                                capture_output=True, timeout=60)
    except FileNotFoundError:
        raise ProvisionError('kubectl is not installed: install it and check context epaflix') from None
    except (OSError, subprocess.TimeoutExpired):
        raise ProvisionError('Cluster unreachable: check kubectl context epaflix') from None
    if result.returncode:
        diagnostic = result.stderr.decode(errors='replace').lower()
        if any(text in diagnostic for text in ('test failed', 'test operation', 'conflict', 'object has been modified')):
            message = 'Cluster state changed: rerun the command; no existing grant was overwritten'
        elif 'forbidden' in diagnostic:
            message = 'Cluster access denied: check permissions for kubectl context epaflix'
        elif 'unauthorized' in diagnostic or 'must be logged in' in diagnostic:
            message = 'Cluster authentication failed: refresh access for kubectl context epaflix'
        elif any(text in diagnostic for text in ('unable to connect', 'connection refused', 'no such host',
                                                 'timeout', 'timed out', 'context "epaflix" does not exist')):
            message = 'Cluster unreachable: check kubectl context epaflix'
        else:
            message = 'Cluster request failed: check kubectl context epaflix and permissions'
        raise ProvisionError(message)
    try:
        return json.loads(result.stdout) if result.stdout.strip() else None
    except ValueError:
        raise ProvisionError('Cluster returned an unreadable response: check kubectl context epaflix') from None


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
        raise ProvisionError('Work pod is running: stop it first')


def github_request(endpoint, token, check='token', optional=False):
    try:
        result = request('https://api.github.com/' + endpoint, token)
    except urllib.error.HTTPError as error:
        if optional and error.code in (403, 404, 422):
            return None
        raise ProvisionError(f'GitHub rejected the {check} (HTTP {error.code})') from None
    except (OSError, urllib.error.URLError):
        raise ProvisionError('Cannot reach GitHub: check your network connection') from None
    except ValueError:
        raise ProvisionError('GitHub returned an unreadable response: retry later') from None
    if result is None:
        raise ProvisionError('GitHub returned an unreadable response: retry later')
    return result


def github_items(endpoint, token, check, optional=False):
    items, page = [], 1
    while True:
        separator = '&' if '?' in endpoint else '?'
        result = github_request(f'{endpoint}{separator}per_page=100&page={page}', token, check, optional)
        if result is None:
            return items or None
        if not isinstance(result, list):
            raise ProvisionError(f'GitHub returned an unexpected {check} response: retry later')
        items.extend(result)
        if len(result) < 100:
            return items
        page += 1


def valid_repo(repo):
    return (isinstance(repo, dict) and isinstance(repo.get('owner'), dict) and
            isinstance(repo.get('private'), bool) and repo['owner'].get('type') in ('User', 'Organization') and
            isinstance(repo['owner'].get('login'), str))


def github_repos(endpoint, token, check, optional=False):
    repos = github_items(endpoint, token, check, optional)
    if repos is not None and not all(valid_repo(repo) for repo in repos):
        raise ProvisionError('GitHub returned incomplete repository details: retry later')
    return repos


def reject_private_personal_repos(repos, login):
    if any(repo['private'] and repo['owner']['type'] == 'User' and
           repo['owner']['login'].casefold() == login.casefold() for repo in repos):
        raise ProvisionError('Private personal repository access is readable: create the token with the organization as resource owner')


def require_private_organization(token, login):
    memberships = github_items('user/orgs', token, 'organization lookup', optional=True)
    if memberships is None:
        visible = github_repos('user/repos?affiliation=owner,collaborator,organization_member', token, 'visible repository lookup')
        reject_private_personal_repos(visible, login)
        names = [repo['owner']['login'] for repo in visible if repo['owner']['type'] == 'Organization']
    else:
        names = [membership.get('login') if isinstance(membership, dict) else None for membership in memberships]
    if any(not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9-]+', name) for name in names):
        raise ProvisionError('GitHub returned incomplete organization details: retry later')
    organizations = sorted({name.casefold() for name in names})
    if not organizations:
        raise ProvisionError('Token organization could not be determined: check organization access and token approval')
    qualified = []
    for organization in organizations:
        private = github_repos(f'orgs/{organization}/repos?type=private', token,
                               'private organization repository check', optional=len(organizations) > 1)
        if private and any(repo['private'] and repo['owner']['type'] == 'Organization' and
                           repo['owner']['login'].casefold() == organization for repo in private):
            qualified.append(organization)
    if len(qualified) > 1:
        raise ProvisionError('Token organization is ambiguous: use a token for one organization')
    if not qualified:
        raise ProvisionError('Private organization repository access is empty: approve the token request or select a private organization repository')


def github():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ProvisionError('Owner enrollment requires a private interactive terminal')
    require_stopped()
    obj = kubectl('-n', RUNTIME_GRANTS[0], 'get', 'secret', RUNTIME_GRANTS[1], '--ignore-not-found', '-o', 'json')
    if obj is None:
        raise ProvisionError('Work grants Secret is missing: run provision.py grants first')
    data = obj.get('data', {})
    if 'github-token' in data:
        raise ProvisionError('Work GitHub grant already exists: refusing to overwrite it')
    if not {'model-token', 'hub-token'}.issubset(data) or not obj.get('metadata', {}).get('resourceVersion'):
        raise ProvisionError('Work grants Secret is incomplete: provision the model and hub grants first')
    token = getpass.getpass('Fine-grained GitHub PAT, hidden: ').encode()
    if not GITHUB_PAT.fullmatch(token):
        raise ProvisionError('Token is not a fine-grained PAT (must start with github_pat_)')
    user = github_request('user', token)
    if not isinstance(user, dict) or not isinstance(user.get('login'), str) or not user['login']:
        raise ProvisionError('GitHub did not return a user for this token')
    require_private_organization(token, user['login'])
    personal = github_repos('user/repos?visibility=private&affiliation=owner', token,
                            'private personal repository check', optional=True)
    if personal is None:
        print("Private personal repository check unavailable: not verified. Check the token's resource owner on the PAT page.")
    else:
        reject_private_personal_repos(personal, user['login'])
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
    except ProvisionError as error:
        print(str(error), file=sys.stderr)
        return 1
    except Exception:
        print('Work provisioning refused or failed. Private diagnostics suppressed.', file=sys.stderr)
        return 1
    print('Work ' + args.action + ' staged. No Applications synced.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
