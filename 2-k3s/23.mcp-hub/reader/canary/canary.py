import errno
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from uuid import UUID

INPUTS = Path('/run/vault-canary-client')
HUB_URL = 'http://mcp-hub.mcp-hub-homelab.svc.cluster.local:8000'
READER_URL = 'http://vault-reader.vault-reader-homelab.svc.cluster.local:8000/vaultwarden'
HELPER = ['/usr/local/bin/vault-restricted']
LIMIT = 12 * 1024 * 1024
HTTP_TIMEOUT = 30
HELPER_TIMEOUT = 65
FIXTURE_TITLES = ['reader-canary', 'reader-canary-duplicate', 'reader-canary-duplicate']


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_):
        raise ValueError


class Hub:
    def __init__(self, token):
        self.token = token
        self.identifier = 0
        self.session = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, body, token):
        def timed_out(*_):
            raise TimeoutError
        previous = signal.signal(signal.SIGALRM, timed_out)
        signal.setitimer(signal.ITIMER_REAL, HTTP_TIMEOUT)
        try:
            headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                       'Accept-Encoding': 'identity', 'Mcp-Protocol-Version': '2025-06-18'}
            if token is not None:
                headers['Authorization'] = 'Bearer ' + token
            if self.session and path == '/vaultwarden':
                headers['Mcp-Session-Id'] = self.session
            request = urllib.request.Request(HUB_URL + path, data=json.dumps(body).encode(), headers=headers)
            try:
                response = self.opener.open(request, timeout=HTTP_TIMEOUT)
            except urllib.error.HTTPError as response:
                with response:
                    return response.code, None
            with response:
                self.session = response.headers.get('Mcp-Session-Id', self.session)
                data = response.read(LIMIT + 1)
                if len(data) > LIMIT or response.headers.get('Content-Encoding', 'identity') != 'identity':
                    raise ValueError
                if response.headers.get_content_type() == 'text/event-stream':
                    events = [json.loads('\n'.join(line[5:].lstrip() for line in event.splitlines()
                                                  if line.startswith('data:')))
                              for event in data.decode().replace('\r\n', '\n').split('\n\n')
                              if any(line.startswith('data:') for line in event.splitlines())]
                    value = next(event for event in events if event.get('id') == body.get('id'))
                else:
                    value = json.loads(data)
                return response.status, value
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def rpc(self, method, params=None):
        self.identifier += 1
        body = {'jsonrpc': '2.0', 'id': self.identifier, 'method': method}
        if params is not None:
            body['params'] = params
        status, value = self.request('/vaultwarden', body, self.token)
        if status != 200 or not isinstance(value, dict) or value.get('id') != self.identifier:
            raise ValueError
        return value

    def call(self, name, arguments):
        return self.rpc('tools/call', {'name': name, 'arguments': arguments})


def failed(value):
    return 'error' in value or value.get('result', {}).get('isError') is True


def metadata(value):
    if failed(value):
        raise ValueError
    content = value['result']['content']
    if len(content) != 1 or content[0]['type'] != 'text':
        raise ValueError
    return json.loads(content[0]['text'])


def helper(reference, consumer, attachment=False, capture=False):
    args = HELPER + ['run', '--collection-id', reference['collection_id']]
    args += ['--item-id', reference['item_id']] if 'item_id' in reference else ['--title', reference['title']]
    args += ['--password-env', 'VAULT_PASSWORD', '--timeout', '60']
    if attachment:
        args += ['--field', 'API_TOKEN=VAULT_API_TOKEN', '--attachment', 'canary.txt',
                 '--attachment-env', 'VAULT_INPUT_FILE']
    return subprocess.run(args + ['--', 'python3', '-c', consumer], stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, text=True, timeout=HELPER_TIMEOUT)


def configure():
    os.umask(0o077)
    services = (INPUTS / 'services.json').read_bytes()
    if len(services) > 16384:
        raise ValueError
    target = Path(os.environ['HOME']) / 'services.json'
    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as stream:
        stream.write(services)
    token = (INPUTS / 'token').read_text()
    if len(token) > 4096:
        raise ValueError
    os.environ.update(MCP_HUB_INSTANCE='homelab', T3_RUNTIME_VARIANT='homelab', MCP_HUB_URL=HUB_URL,
                      MCP_HUB_TOKEN=token, MCP_HUB_SERVICES_FILE=str(target))
    fixture = json.loads((INPUTS / 'canary.json').read_bytes())
    if set(fixture) != {'collection_id', 'item_id', 'duplicate_item_ids', 'denied_collection_id', 'denied_item_id'}:
        raise ValueError
    duplicates = fixture['duplicate_item_ids']
    if not isinstance(duplicates, list) or len(duplicates) != 2:
        raise ValueError
    ids = [fixture[k] for k in ('collection_id', 'item_id', 'denied_collection_id', 'denied_item_id')] + duplicates
    if any(not isinstance(value, str) or str(UUID(value)) != value for value in ids) or len(set(ids)) != 6:
        raise ValueError
    return Hub(token), fixture


def checks(hub, fixture):
    reference = {'collection_id': fixture['collection_id'], 'item_id': fixture['item_id']}
    original_metadata = {}
    consumer = ('import os,pathlib; assert os.environ["VAULT_PASSWORD"] == "canary-only-not-real"; '
                'assert os.environ["VAULT_API_TOKEN"] == "synthetic-canary-token"; '
                'assert pathlib.Path(os.environ["VAULT_INPUT_FILE"]).read_bytes() == '
                'b"reader-canary-synthetic-marker\\n"')

    def tools():
        reply = hub.rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                     'clientInfo': {'name': 'reader-canary', 'version': '1'}})
        assert not failed(reply)
        names = [tool['name'] for tool in hub.rpc('tools/list')['result']['tools']]
        assert sorted(names) == ['vault_get', 'vault_list']

    def listing():
        for arguments in ({'collection_id': fixture['collection_id']}, {}):
            values = metadata(hub.call('vault_list', arguments))
            assert sorted(value['title'] for value in values) == FIXTURE_TITLES

    def safe_metadata():
        reply = hub.call('vault_get', {'reference': reference})
        value = metadata(reply)
        assert value['title'] == 'reader-canary' and value['has_password'] is True
        assert 'API_TOKEN' in value['custom_fields']
        assert any(a['filename'] == 'canary.txt' for a in value['attachments'])
        encoded = json.dumps(reply)
        assert all(secret not in encoded for secret in ('canary-only-not-real', 'synthetic-canary-token',
                                                       'reader-canary-synthetic-marker'))
        assert not {'password', 'fields', 'content', 'content_b64'}.intersection(value)
        original_metadata.update(value)

    def consumption(wrong=False):
        before = set(Path('/tmp').glob('vault-restricted-*'))
        result = helper(reference, consumer.replace('reader-canary-synthetic-marker', 'wrong-synthetic-marker')
                        if wrong else consumer, attachment=True, capture=wrong)
        if wrong:
            assert result.returncode != 0
            assert result.stdout == 'vault-restricted: command finished; exit code ' + str(result.returncode) + '\n'
        else:
            assert result.returncode == 0
        assert set(Path('/tmp').glob('vault-restricted-*')) == before

    def duplicates():
        title = {'collection_id': fixture['collection_id'], 'title': 'reader-canary'}
        assert helper(title, 'import os; assert os.environ["VAULT_PASSWORD"] == "canary-only-not-real"').returncode == 0
        title['title'] = 'reader-canary-duplicate'
        assert hub.request('/vault-secret', {'reference': title}, hub.token)[0] == 400
        assert helper(title, 'import os; assert "VAULT_PASSWORD" in os.environ').returncode != 0
        for identifier in fixture['duplicate_item_ids']:
            assert helper(dict(reference, item_id=identifier), 'import os; assert "VAULT_PASSWORD" in os.environ').returncode == 0

    def scope():
        denied = dict(reference, item_id=fixture['denied_item_id'])
        for value in (denied, dict(denied, collection_id=fixture['denied_collection_id'])):
            reply = hub.call('vault_get', {'reference': value})
            assert reply['result']['isError'] is True
            assert reply['result']['content'] == [
                {'type': 'text', 'text': 'invalid or unresolved collection reference'}]
            assert hub.request('/vault-secret', {'reference': value}, hub.token)[0] == 400
            assert helper(value, 'pass').returncode != 0

    def authentication():
        for token in (None, 'homelab.' + secrets.token_urlsafe(32), 'work.' + secrets.token_urlsafe(32)):
            for path, body in [('/vaultwarden', {'jsonrpc': '2.0', 'id': 999, 'method': 'tools/list'}),
                               ('/vault-secret', {'reference': reference})]:
                assert hub.request(path, body, token)[0] in (401, 403)

    def writes():
        names = {tool['name'] for tool in hub.rpc('tools/list')['result']['tools']}
        assert names == {'vault_get', 'vault_list'}
        path = '/homelab-agent-canary/reader-canary'
        probes = [('vault_add', {'path': path + '-write-probe', 'password': 'synthetic-write-probe'}),
                  ('vault_update', {'path': path, 'notes': 'synthetic-write-probe'}),
                  ('vault_trash', {'path': path}),
                  ('vault_attach', {'path': path, 'filename': 'reader-canary-write-probe.txt',
                                    'content_b64': 'c3ludGhldGlj'})]
        for name, arguments in probes:
            assert failed(hub.call(name, arguments))
        values = metadata(hub.call('vault_list', {'collection_id': fixture['collection_id']}))
        assert sorted(value['title'] for value in values) == FIXTURE_TITLES
        assert original_metadata and metadata(hub.call('vault_get', {'reference': reference})) == original_metadata

    def direct():
        url = urlsplit(READER_URL)
        try:
            with socket.create_connection((url.hostname, url.port), timeout=3):
                pass
        except OSError as error:
            if isinstance(error, (TimeoutError, ConnectionRefusedError)) or error.errno in (
                    errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ETIMEDOUT):
                return
            raise
        raise ValueError

    def burst():
        for _ in range(5):
            assert isinstance(metadata(hub.call('vault_list', {'collection_id': fixture['collection_id']})), list)

    return [tools, listing, safe_metadata, consumption, lambda: consumption(True), duplicates,
            scope, authentication, writes, direct, burst]


def main():
    if not __debug__ or sys.flags.optimize:
        for number in range(1, 12):
            print('FAIL C' + str(number) + ': optimized interpreter refused', flush=True)
        return 1
    try:
        hub, fixture = configure()
        operations = checks(hub, fixture)
    except Exception:
        for number in range(1, 12):
            print('FAIL C' + str(number) + ': setup refused', flush=True)
        return 1
    failures = 0
    for number, operation in enumerate(operations, 1):
        try:
            operation()
        except Exception:
            failures += 1
            print('FAIL C' + str(number) + ': check refused or assertion failed', flush=True)
        else:
            print('PASS C' + str(number), flush=True)
    return int(failures != 0)


if __name__ == '__main__':
    sys.exit(main())
