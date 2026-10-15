#!/usr/bin/env python3
import contextlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from threading import Thread
from urllib.parse import urlencode
from uuid import uuid4


def require(condition):
    if not condition:
        raise RuntimeError


def invoke(client, arguments, expected=0):
    with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
        code = client.main(arguments)
    require(code == expected)
    return json.loads(output.getvalue()) if expected == 0 and arguments[0] != 'run' else None


@contextlib.contextmanager
def failed_upload(client):
    attempts = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            attempts.append(self.path)
            self.send_response(400)
            self.end_headers()

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = client.fetch

    def fetch(path, **kwargs):
        if path.startswith('/attachment?') and kwargs.get('method') == 'POST':
            address = client.ADDRESS
            try:
                client.ADDRESS = f'http://127.0.0.1:{server.server_port}'
                return original(path, **kwargs)
            finally:
                client.ADDRESS = address
        return original(path, **kwargs)

    client.fetch = fetch
    try:
        yield attempts
    finally:
        client.fetch = original
        server.shutdown()
        server.server_close()
        thread.join()


def canary_items(client, item_name):
    return [item for trash in ('false', 'true') for item in client.fetch('/list/object/items?' +
            urlencode({'search': item_name, 'trash': trash}))['data'] if item.get('name') == item_name]


def synced_canary_items(client, item_name):
    client.fetch('/sync?force=true', method='POST', timeout=45)
    return canary_items(client, item_name)


def exercise(client, variant):
    require(variant in ('homelab', 'work') and os.environ['T3_RUNTIME_VARIANT'] == variant)
    organization, collection = client.scope()
    item_id = None
    item_name = 'synthetic-vault-write-' + uuid4().hex
    with tempfile.TemporaryDirectory(prefix='vault-write-canary-') as temporary:
        directory = Path(temporary)
        document = directory / 'item.json'
        attachment = directory / 'canary.bin'
        expected = directory / 'expected.json'
        check = directory / 'check.py'
        value, replacement, field = (uuid4().hex for _ in range(3))
        document.write_text(json.dumps({'type': 1, 'name': item_name,
            'login': {'username': 'synthetic-user', 'password': value},
            'fields': [{'name': 'api', 'value': field, 'type': 1}]}))
        failure = None
        try:
            result = invoke(client, ['create', '--input-file', str(document)])
            item_id = client.checked_uuid(result['itemId'])
            item = client.scoped_item(client.fetch('/object/item/' + item_id), organization, collection, item_id)
            require(item['login']['password'] == value and item['collectionIds'] == [collection])
            document.write_text(json.dumps({'login': {'username': 'synthetic-user', 'password': replacement}}))
            invoke(client, ['update', '--item', item_id, '--input-file', str(document)])
            attachment.write_bytes(b'synthetic-initial-' + uuid4().bytes)
            initial = attachment.read_bytes()
            invoke(client, ['attach', '--item', item_id, '--file', str(attachment)])
            attachment.write_bytes(b'synthetic-replacement-' + uuid4().bytes)
            with failed_upload(client) as attempts:
                invoke(client, ['attach', '--item', item_id, '--file', str(attachment)], expected=1)
                require(attempts == ['/attachment?itemid=' + item_id])
            item = client.fetch('/object/item/' + item_id)
            require(len(item['attachments']) == 1)
            old_attachment_id = item['attachments'][0]['id']
            require(client.fetch('/object/attachment/' + old_attachment_id + '?itemid=' + item_id, binary=True) == initial)
            invoke(client, ['attach', '--item', item_id, '--file', str(attachment)])
            item = client.fetch('/object/item/' + item_id)
            require(len(item['attachments']) == 1 and item['attachments'][0]['id'] != old_attachment_id)
            expected.write_text(json.dumps({'password': replacement, 'field': field, 'attachment': attachment.read_bytes().hex()}))
            check.write_text('import json,os,pathlib,sys\n'
                'expected=json.loads(pathlib.Path(sys.argv[1]).read_text())\n'
                'valid=(os.environ["VAULT_VALUE"]==expected["password"] and '
                'os.environ["VAULT_API"]==expected["field"] and '
                'pathlib.Path(os.environ["VAULT_FILE"]).read_bytes().hex()==expected["attachment"])\n'
                'sys.exit(0 if valid else 1)\n')
            arguments = ['run', '--item', item_id, '--password-env', 'VAULT_VALUE', '--field', 'api=VAULT_API',
                '--attachment', 'canary.bin', '--attachment-env', 'VAULT_FILE', '--', sys.executable, str(check), str(expected)]
            invoke(client, arguments)
            invoke(client, ['trash', '--item', item_id])
            require(client.fetch('/object/item/' + item_id).get('deletedDate') is not None)
            invoke(client, arguments, expected=1)
        except BaseException as error:
            failure = error
            raise
        finally:
            try:
                item_ids = {item_id} if item_id is not None else {
                    client.checked_uuid(item['id']) for item in client.wait_for_cache(lambda: synced_canary_items(client, item_name))}
                for cleanup_id in item_ids:
                    client.wait_for_cache(lambda: client.fetch('/object/item/' + cleanup_id))
                    client.fetch('/object/item/' + cleanup_id + '?permanent=true', method='DELETE', timeout=45)
                client.wait_for_cache(lambda: not synced_canary_items(client, item_name))
            except BaseException as cleanup_error:
                if failure is not None:
                    print('vault canary: lifecycle failed: ' + type(failure).__name__, file=sys.stderr)
                print('vault canary: cleanup failed: ' + type(cleanup_error).__name__, file=sys.stderr)
                if failure is not None:
                    raise failure from None
                raise


def main():
    os.umask(0o077)
    try:
        require(len(sys.argv) == 2)
        loader = SourceFileLoader('vault_canary_client', '/usr/local/bin/vault')
        spec = spec_from_loader(loader.name, loader)
        client = module_from_spec(spec)
        loader.exec_module(client)
        exercise(client, sys.argv[1])
        print('vault canary: passed; synthetic item permanently deleted')
        return 0
    except BaseException:
        print('vault canary: failed; check synthetic-item cleanup privately', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
