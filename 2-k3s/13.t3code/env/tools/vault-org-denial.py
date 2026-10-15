#!/usr/bin/env python3
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from uuid import UUID


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError


def require(condition):
    if not condition:
        raise RuntimeError


def bw(arguments, environment, document=None):
    return subprocess.run(['bw', *arguments], env=environment,
        input=None if document is None else base64.b64encode(json.dumps(document).encode()),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)


def access_token(data, user_id):
    matches = set()

    def visit(value):
        if isinstance(value, dict):
            for entry in value.values():
                visit(entry)
        elif isinstance(value, list):
            for entry in value:
                visit(entry)
        elif isinstance(value, str) and value.count('.') == 2:
            try:
                encoded = value.split('.')[1]
                claims = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
                if claims.get('sub') == user_id:
                    matches.add(value)
            except (ValueError, UnicodeError):
                pass

    visit(data)
    require(len(matches) == 1)
    return matches.pop()


def request(server, token, path, payload=None, method='GET'):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(server + '/api' + path, method=method,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    try:
        response = opener.open(request, timeout=15)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        data = response.read(1024 * 1024 + 1)
        require(len(data) <= 1024 * 1024)
        return response.code, json.loads(data) if data else None


def record_cleanup(item_id):
    descriptor, _ = tempfile.mkstemp(prefix='vault-denial-cleanup-', suffix='.json', dir=Path.home())
    with os.fdopen(descriptor, 'w') as output:
        json.dump({'itemId': item_id}, output)


def cleanup_or_record(item_id, remove):
    require(str(UUID(item_id)) == item_id)
    try:
        require(remove())
    except BaseException:
        record_cleanup(item_id)
        raise


def exercise(foreign):
    require(os.environ['T3_RUNTIME_VARIANT'] == 'homelab' and foreign['environment'] == 'work')
    organization, collection = foreign['organizationId'], foreign['collectionId']
    require(str(UUID(organization)) == organization and str(UUID(collection)) == collection)
    server = os.environ['BW_SERVER'].rstrip('/')
    url = urlsplit(server)
    require(url.scheme == 'https' and url.hostname and not url.username and not url.password and not url.query and not url.fragment)
    with tempfile.TemporaryDirectory(prefix='vault-org-denial-') as temporary:
        environment = {key: os.environ[key] for key in ('PATH', 'BW_EMAIL', 'BW_PASSWORD')}
        environment.update(HOME=temporary, BITWARDENCLI_APPDATA_DIR=temporary, BW_NOINTERACTION='true')
        require(bw(['config', 'server', server], environment).returncode == 0)
        login = bw(['login', environment['BW_EMAIL'], '--passwordenv', 'BW_PASSWORD', '--raw'], environment)
        require(login.returncode == 0)
        environment['BW_SESSION'] = login.stdout.decode().strip()
        require(environment['BW_SESSION'] and '\n' not in environment['BW_SESSION'])
        environment.pop('BW_PASSWORD')
        status = bw(['status'], environment)
        require(status.returncode == 0)
        status = json.loads(status.stdout)
        require(status['status'] == 'unlocked' and status['userEmail'] == environment['BW_EMAIL'] and status['serverUrl'].rstrip('/') == server)
        user_id = status['userId']
        document = {'type': 2, 'name': 'synthetic-foreign-write-denial', 'secureNote': {'type': 0},
                    'organizationId': organization, 'collectionIds': [collection]}
        created = bw(['create', 'item'], environment, document)
        if created.returncode == 0:
            item_id = json.loads(created.stdout)['id']
            cleanup_or_record(item_id, lambda: bw(['delete', 'item', item_id, '--permanent'], environment).returncode == 0)
            raise RuntimeError
        token = access_token(json.loads((Path(temporary) / 'data.json').read_bytes()), user_id)
        code, profile = request(server, token, '/accounts/profile')
        require(code == 200 and profile['id'] == user_id)
        require(all(org['id'] != organization for org in profile['organizations']))
        encrypted_name = '2.' + '|'.join(base64.b64encode(os.urandom(size)).decode() for size in (16, 32, 32))
        payload = {'cipher': {'type': 2, 'name': encrypted_name, 'secureNote': {'type': 0},
                             'organizationId': organization, 'encryptedFor': user_id}, 'collectionIds': [collection]}
        code, result = request(server, token, '/ciphers/create', payload, 'POST')
        if 200 <= code < 300:
            item_id = result['id']
            cleanup_or_record(item_id, lambda: request(server, token, '/ciphers/' + item_id, method='DELETE')[0] in (200, 204))
            raise RuntimeError
        require(code in (400, 403) and result.get('message', result.get('Message')) in (
            'No rights to modify the collection', "You don't have permission to add item to organization"))


def main():
    os.umask(0o077)
    try:
        exercise(json.loads(os.environ['VAULT_DENIED_SCOPE']))
        print('vault org denial: account and server refused')
        return 0
    except BaseException:
        print('vault org denial: failed; check synthetic cleanup privately', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
