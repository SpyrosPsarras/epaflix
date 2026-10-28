#!/usr/bin/python3
"""Reviewed work AKS ExecCredential using only this backend's Azure CLI session."""
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit

INPUTS = Path('/run/t3-inputs')
HOME = Path('/home/t3')
AZ = '/usr/bin/az'
LOCK_TIMEOUT = 900
TOKEN_TIMEOUT = 120
LOGIN_TIMEOUT = 900
EXEC = {'apiVersion': 'client.authentication.k8s.io/v1',
        'command': '/scripts/work-aks-auth.py', 'args': ['token'],
        'interactiveMode': 'Never', 'provideClusterInfo': False}
AUTH_ERRORS = re.compile(r'az login|interaction_required|invalid_grant|AADSTS(?:50058|50076|50079|50173|70043|700082|700084)', re.I)


class AuthError(ValueError):
    pass


SCOPE_FIELDS = {'tenantId', 'audience', 'subscriptionId', 'account'}
PIN_FIELDS = {'name', 'cluster', 'user', 'server', 'certificateAuthoritySha256'}


def validate_scope(scope):
    if not isinstance(scope, dict) or set(scope) != SCOPE_FIELDS:
        raise AuthError('A complete authentication scope is required')
    for name in ('tenantId', 'subscriptionId'):
        if not isinstance(scope[name], str) or not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', scope[name]):
            raise AuthError('Explicit tenant and subscription are required')
    audience = scope['audience']
    if not isinstance(audience, str):
        raise AuthError('Explicit approved audience is required')
    parsed = urlsplit(audience)
    if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', audience) and not (
            parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment):
        raise AuthError('Explicit approved audience is required')
    account = scope['account']
    if not isinstance(account, dict) or set(account) != {'name', 'type'} or account['type'] != 'user' or not (
            isinstance(account['name'], str) and account['name'].strip() == account['name'] and account['name']):
        raise AuthError('An exact reviewed work account is required')


def scope_for(auth, context=None):
    defaults = {name: auth[name] for name in SCOPE_FIELDS}
    if context is None:
        return defaults
    matches = [entry for entry in auth['contexts'] if entry['name'] == context]
    if len(matches) != 1:
        raise AuthError('Select exactly one approved work context')
    return matches[0].get('authentication', defaults)


def load_kube(path):
    binary = shutil.which('kubectl', path='/usr/bin:/bin:/usr/local/bin')
    if binary is None:
        raise AuthError('kubectl is required to read work kubeconfigs')
    result = subprocess.run([binary, '--kubeconfig', str(path),
                             'config', 'view', '--raw', '-o', 'json'], capture_output=True,
                            timeout=30, env={'PATH': '/usr/bin:/bin'})
    if result.returncode:
        raise AuthError('Invalid work kubeconfig')
    return json.loads(result.stdout)


def validate(inputs, kube=None):
    config = json.loads((inputs / 'runtime.json').read_text())
    if config['environment'] != 'work' or config['hubUrl'] != 'http://mcp-hub.mcp-hub-work.svc.cluster.local:8000':
        raise AuthError('Work authentication requires reviewed work inputs')
    if any(not isinstance(config['approvals'].get(name), str) or not config['approvals'][name].strip()
           for name in ('workAuth', 'kubeconfig')):
        raise AuthError('Work authentication and scope reviews are required')
    for name in ('work-auth.json', 'kubeconfig'):
        if hashlib.sha256((inputs / name).read_bytes()).hexdigest() != config['sha256'].get(name):
            raise AuthError('Reviewed authentication input mismatch')
    auth = json.loads((inputs / 'work-auth.json').read_text())
    if set(auth) != {'environment', 'tenantId', 'audience', 'subscriptionId', 'account', 'contexts'} or auth['environment'] != 'work':
        raise AuthError('Invalid work authentication configuration')
    validate_scope(scope_for(auth))
    contexts = auth['contexts']
    if not isinstance(contexts, list) or not contexts or any(
            not isinstance(entry, dict) or set(entry) not in (PIN_FIELDS, PIN_FIELDS | {'authentication'}) or
            any(not isinstance(entry[key], str) or not entry[key] for key in PIN_FIELDS) for entry in contexts):
        raise AuthError('Exact approved work context pins are required')
    for entry in contexts:
        if 'authentication' in entry:
            validate_scope(entry['authentication'])
    if kube is None:
        kube = load_kube(inputs / 'kubeconfig')
    maps = {}
    for group in ('clusters', 'users', 'contexts'):
        entries = kube.get(group, [])
        maps[group] = {entry['name']: entry[group[:-1] if group != 'contexts' else 'context'] for entry in entries}
        if len(maps[group]) != len(entries):
            raise AuthError('Duplicate kubeconfig references are forbidden')
    if len({entry['name'] for entry in contexts}) != len(contexts) or any(
            set(maps[group]) != {entry[key] for entry in contexts}
            for group, key in (('clusters', 'cluster'), ('users', 'user'), ('contexts', 'name'))):
        raise AuthError('Kubeconfig must contain only the exact reviewed work entries')
    if kube.get('current-context') not in maps['contexts']:
        raise AuthError('Work current context must be approved')
    for entry in contexts:
        context = maps['contexts'][entry['name']]
        if not set(context).issubset({'cluster', 'user', 'namespace'}) or any(context.get(key) != entry[key] for key in ('cluster', 'user')):
            raise AuthError('Work context references differ from their review')
        cluster = maps['clusters'][entry['cluster']]
        endpoint = urlsplit(cluster.get('server', ''))
        if set(cluster) != {'server', 'certificate-authority-data'} or cluster['server'] != entry['server'] or not (
                endpoint.scheme == 'https' and endpoint.hostname and not endpoint.username and not endpoint.password
                and not endpoint.query and not endpoint.fragment):
            raise AuthError('Work API requires the exact reviewed HTTPS endpoint and inline CA')
        ca = base64.b64decode(cluster['certificate-authority-data'], validate=True)
        if not ca or hashlib.sha256(ca).hexdigest() != entry['certificateAuthoritySha256']:
            raise AuthError('Work API CA differs from its review')
        user = maps['users'][entry['user']]
        allowed = [{**EXEC, 'args': ['token', entry['name']]}]
        if scope_for(auth, entry['name']) == scope_for(auth):
            allowed.append(EXEC)
        if not any(user in ({'exec': command}, {'exec': {**command, 'env': None}}) for command in allowed):
            raise AuthError('Only the context-pinned work authentication helper without exec environment is supported')
    return auth


def environment(home, scope=None, defaults=None):
    if any(os.environ.get(name) for name in ('AZURE_CONFIG_DIR', 'SSH_AUTH_SOCK')) or any(
            name.startswith(('BW_', 'BITWARDENCLI_')) for name in os.environ):
        raise AuthError('Inherited credential environments are forbidden')
    marker = home / '.config/t3-runtime/environment'
    if not marker.is_file() or marker.read_text() != 'work\n':
        raise AuthError('Work authentication requires an enrolled clean work home')
    cache = home / '.azure'
    if scope is not None and (scope['tenantId'].lower() != defaults['tenantId'].lower() or scope['account'] != defaults['account']):
        identity = hashlib.sha256(scope['account']['name'].encode()).hexdigest()
        cache = cache / 'tenants' / scope['tenantId'].lower() / identity
    if cache.is_symlink() or any(parent.is_symlink() for parent in cache.parents):
        raise AuthError('Work Azure session directory must not be a symlink')
    for directory in reversed([cache, *cache.parents]):
        if directory.is_relative_to(home / '.azure'):
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)
    return {'HOME': str(home), 'PATH': '/usr/bin:/bin', 'AZURE_CONFIG_DIR': str(cache),
            'AZURE_CORE_LOGIN_EXPERIENCE_V2': 'off', 'AZURE_CORE_COLLECT_TELEMETRY': 'no'}


@contextmanager
def locked(env):
    path = Path(env['AZURE_CONFIG_DIR']) / 't3-work-auth.lock'
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as lock:
        deadline = time.monotonic() + LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AuthError('Timed out waiting for work authentication')
                time.sleep(0.05)
        yield


def az(args, env):
    try:
        result = subprocess.run([AZ, *args, '--only-show-errors', '--output', 'json'], env=env,
                                capture_output=True, timeout=TOKEN_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise AuthError('Work authentication command timed out') from None
    if result.returncode:
        if AUTH_ERRORS.search(result.stderr.decode(errors='replace')):
            raise AuthError('Work authentication required; enroll privately with the explicit login command')
        raise AuthError('Work authentication command failed; no login attempted')
    return json.loads(result.stdout)


def account(auth, env):
    actual = az(['account', 'show', '--subscription', auth['subscriptionId']], env)
    if actual.get('tenantId') != auth['tenantId'] or actual.get('id') != auth['subscriptionId'] or actual.get('user') != auth['account']:
        raise AuthError('Azure session does not match the reviewed work account')


def run(action, inputs=INPUTS, home=HOME, context=None):
    if action not in {'token', 'login'}:
        raise AuthError('Only token and explicit private login are supported')
    reviewed = validate(inputs)
    auth = scope_for(reviewed, context)
    env = environment(home, auth, scope_for(reviewed))
    with locked(env):
        if action == 'login':
            with open('/dev/tty', 'r+b', buffering=0) as terminal:
                try:
                    result = subprocess.run([AZ, 'login', '--tenant', auth['tenantId'], '--use-device-code',
                                             '--output', 'none', '--only-show-errors'], env=env, stdin=terminal,
                                            stdout=terminal, stderr=terminal, timeout=LOGIN_TIMEOUT)
                except subprocess.TimeoutExpired:
                    raise AuthError('Private work enrollment timed out') from None
            if result.returncode:
                raise AuthError('Private work enrollment failed')
            account(auth, env)
            return None
        account(auth, env)
        result = az(['account', 'get-access-token', '--subscription', auth['subscriptionId'],
                     '--resource', auth['audience']], env)
        if result.get('tenant') != auth['tenantId'] or result.get('subscription') != auth['subscriptionId']:
            raise AuthError('Azure token scope differs from the approved work scope')
        expiry = datetime.fromtimestamp(int(result['expires_on']), timezone.utc)
        if expiry <= datetime.now(timezone.utc) or not isinstance(result.get('accessToken'), str) or not result['accessToken']:
            raise AuthError('Work authentication required; session expired')
        return {'apiVersion': EXEC['apiVersion'], 'kind': 'ExecCredential',
                'status': {'token': result['accessToken'], 'expirationTimestamp': expiry.isoformat().replace('+00:00', 'Z')}}


if __name__ == '__main__':
    try:
        if len(sys.argv) > 3:
            raise AuthError('Supply an authentication action and optional approved context name')
        result = run(sys.argv[1] if len(sys.argv) >= 2 else 'token',
                     context=sys.argv[2] if len(sys.argv) == 3 else None)
        if result is not None:
            print(json.dumps(result))
    except AuthError as error:
        sys.exit(str(error))
    except Exception:
        sys.exit('Work authentication failed; private details suppressed')
