#!/usr/bin/env python3
import argparse
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import resource
import runpy
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from types import SimpleNamespace
import urllib.error
import urllib.request
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID, uuid4

ADDRESS = 'http://127.0.0.1:8087'
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
MAX_SCOPE_BYTES = 1024
MAX_VALUE_BYTES = 16 * 1024
MAX_AGENT_MESSAGE_BYTES = 256 * 1024
SSH_KEY_ITEM_TYPE = 5
SCOPE_FILE = Path('/run/t3-vault/scope.json')
WORK_SSH_INPUTS = Path('/run/t3-work-ssh')
PROC = Path('/proc')
HEALTH_INTERVAL = 2
HTTP_REQUEST_TIMEOUT_SECONDS = 10
VAULT_OPERATION_TIMEOUT_SECONDS = 45
CHILD_TIMEOUT_MIN_SECONDS = 0.1
CHILD_TIMEOUT_MAX_SECONDS = 120
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2
CACHE_WAIT_SECONDS = 5
CACHE_POLL_SECONDS = 0.1
CLIENT_START_ATTEMPTS = 100
CLIENT_START_POLL_SECONDS = 0.1
SSH_REQUEST_TIMEOUT_SECONDS = 10
SSH_KEY_REFRESH_SECONDS = 10
AGENT_START_ATTEMPTS = 100
AGENT_START_POLL_SECONDS = 0.02
PROXY_POLL_SECONDS = 0.1
SSH_CLIENT_UID = 1000
AGENT_REQUEST_IDENTITIES = 11
AGENT_SIGN_REQUEST = 13
AGENT_FAILURE = b'\x00\x00\x00\x01\x05'


class Refused(Exception):
    pass


class WriteUnconfirmed(Exception):
    def __init__(self, item_id, attachment_id=None):
        self.item_id = item_id
        self.attachment_id = attachment_id


class UnsafeListener(Refused):
    pass


class Cancelled(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise Refused


def checked_uuid(value):
    if str(UUID(value)) != value:
        raise Refused
    return value


def scope():
    variant = os.environ['T3_RUNTIME_VARIANT']
    if variant not in ('homelab', 'work') or os.environ.get('T3_VAULT_CLIENT') != '1':
        raise Refused
    marker = Path.home() / '.config/t3-runtime/environment'
    info = marker.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or (
            marker.read_text() != variant + '\n'):
        raise Refused
    if not stat.S_ISREG(SCOPE_FILE.lstat().st_mode):
        raise Refused
    data = SCOPE_FILE.read_bytes()
    if len(data) > MAX_SCOPE_BYTES:
        raise Refused
    selected = json.loads(data)
    if selected['environment'] != variant:
        raise Refused
    return checked_uuid(selected['organizationId']), checked_uuid(selected['collectionId'])


def fetch(path, binary=False, method='GET', payload=None, timeout=HTTP_REQUEST_TIMEOUT_SECONDS,
          content_type='application/json'):
    def expired(*_):
        raise Refused

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(ADDRESS + path, method=method,
            data=payload if isinstance(payload, bytes) else None if payload is None else json.dumps(payload).encode(),
            headers={} if payload is None else {'Content-Type': content_type})
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200 or response.headers.get_content_type() != (
                    'application/octet-stream' if binary else 'application/json'):
                raise Refused
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise Refused
        if binary:
            return data
        result = json.loads(data)
        if result.get('success') is not True:
            raise Refused
        return result.get('data') if method in ('POST', 'PUT', 'DELETE') else result['data']
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@contextlib.contextmanager
def confirming_write(item_id, attachment_id=None):
    try:
        yield
    except Cancelled:
        raise
    except Exception as error:
        raise WriteUnconfirmed(item_id, attachment_id) from error


def wait_for_cache(check):
    deadline = time.monotonic() + CACHE_WAIT_SECONDS
    while True:
        try:
            result = check()
            if result:
                return result
        except urllib.error.HTTPError as error:
            if error.code != 400:
                raise
            try:
                body = json.loads(error.read(MAX_RESPONSE_BYTES + 1))
            except ValueError:
                raise error from None
            if not isinstance(body, dict) or body.get('message') != 'Not found.':
                raise
        if time.monotonic() >= deadline:
            raise Refused
        time.sleep(CACHE_POLL_SECONDS)


def scoped_item(item, organization, collection, item_id=None, *, allow_trashed=False):
    if not isinstance(item, dict) or item.get('organizationId') != organization or (
            collection not in (item.get('collectionIds') or [])) or (
            not allow_trashed and item.get('deletedDate') is not None):
        raise Refused
    checked_uuid(item['id'])
    if item_id is not None and item['id'] != item_id:
        raise Refused
    return item


def matches_fields(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and matches_fields(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            matches_fields(value, wanted) for value, wanted in zip(actual, expected))
    return actual == expected


def synced_scope():
    organization, collection = scope()
    fetch('/sync', method='POST', timeout=VAULT_OPERATION_TIMEOUT_SECONDS)
    return organization, collection


def selected_item(item_id, name=None):
    organization, collection = synced_scope()
    if not item_id:
        items = fetch('/list/object/items?' + urlencode({'search': name, 'organizationid': organization,
                                                       'collectionid': collection}))['data']
        matches = [item for item in items if item.get('name') == name and
                   item.get('organizationId') == organization and collection in (item.get('collectionIds') or []) and
                   item.get('deletedDate') is None]
        if len(matches) != 1:
            raise Refused
        item_id = matches[0]['id']
    item = scoped_item(fetch('/object/item/' + checked_uuid(item_id)), organization, collection, item_id)
    if name is not None and item.get('name') != name:
        raise Refused
    return item


def list_items(argv):
    parser = Parser(description='List metadata for items in this pod\'s vault scope.')
    parser.add_argument('filter', nargs='?', default='')
    args = parser.parse_args(argv)
    organization, collection = synced_scope()
    items = fetch('/list/object/items?' + urlencode({'organizationid': organization,
                                                   'collectionid': collection}))['data']
    metadata = []
    for item in items:
        try:
            scoped_item(item, organization, collection)
        except Refused:
            continue
        if args.filter.casefold() not in item['name'].casefold():
            continue
        metadata.append({
            'name': item['name'],
            'type': item['type'],
            'password': isinstance((item.get('login') or {}).get('password'), str),
            'fields': sorted(entry['name'] for entry in item.get('fields') or []
                             if isinstance(entry.get('name'), str)),
            'attachments': sorted(entry['fileName'] for entry in item.get('attachments') or []
                                  if isinstance(entry.get('fileName'), str)),
        })
    print(json.dumps(sorted(metadata, key=lambda item: (item['name'].casefold(), item['name']))))
    return 0


def environment_name(value):
    if not re.fullmatch(r'VAULT_[A-Z][A-Z0-9_]{0,63}', value):
        raise Refused
    return value


def child_environment(additions):
    result = {key: value for key, value in os.environ.items() if not key.startswith(('BW_', 'BITWARDENCLI_', 'VAULT_'))}
    result.update(additions)
    return result


def exit_code(code):
    return code if code >= 0 else 128 - code


def stop(child):
    with contextlib.suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGKILL)
    child.wait()


def run(argv):
    parser = Parser(description='Run a quiet command with organization-scoped vault values.')
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--item')
    selection.add_argument('--name')
    parser.add_argument('--password-env')
    parser.add_argument('--username-env')
    parser.add_argument('--field', action='append', default=[])
    parser.add_argument('--attachment')
    parser.add_argument('--attachment-env')
    parser.add_argument('--timeout', type=float, default=CHILD_TIMEOUT_MAX_SECONDS)
    boundary = argv.index('--')
    args = parser.parse_args(argv[:boundary])
    command = argv[boundary + 1:]
    mappings = []
    for login_key, target in (('password', args.password_env), ('username', args.username_env)):
        if target:
            mappings.append(((login_key,), environment_name(target)))
    for field in args.field:
        name, separator, target = field.rpartition('=')
        if not separator or not name:
            raise Refused
        mappings.append((name, environment_name(target)))
    targets = [target for _, target in mappings]
    if args.attachment_env is not None:
        targets.append(environment_name(args.attachment_env))
    if not command or not targets or len(targets) != len(set(targets)) or (
            (args.attachment is None) != (args.attachment_env is None)) or (
            not math.isfinite(args.timeout) or not CHILD_TIMEOUT_MIN_SECONDS <= args.timeout <= CHILD_TIMEOUT_MAX_SECONDS):
        raise Refused
    item = selected_item(args.item, args.name)
    additions = {}
    for field, target in mappings:
        values = [(item.get('login') or {}).get(field[0])] if isinstance(field, tuple) else [
            entry.get('value') for entry in item.get('fields') or [] if entry.get('name') == field]
        if len(values) != 1 or not isinstance(values[0], str) or '\0' in values[0] or len(values[0].encode()) > MAX_VALUE_BYTES:
            raise Refused
        additions[target] = values[0]
    with tempfile.TemporaryDirectory(prefix='vault-client-', dir='/tmp') as directory:
        os.chmod(directory, 0o700)
        if args.attachment is not None:
            matches = [entry for entry in item.get('attachments') or [] if entry.get('fileName') == args.attachment]
            if len(matches) != 1:
                raise Refused
            data = fetch('/object/attachment/' + quote(matches[0]['id'], safe='') + '?' +
                         urlencode({'itemid': item['id']}), binary=True)
            path = Path(directory) / 'attachment'
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
                output.write(data)
            additions[args.attachment_env] = str(path)
        with subprocess.Popen(command, env=child_environment(additions), stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True) as child:
            try:
                code = child.wait(timeout=args.timeout)
            finally:
                stop(child)
    code = exit_code(code)
    print('vault: command finished; exit code ' + str(code))
    return code


def read_file(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise Refused
        data = source.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise Refused
    return data


def input_document(args, creating):
    data = os.environ[environment_name(args.input_env)].encode() if args.input_env else read_file(args.input_file)
    if len(data) > MAX_RESPONSE_BYTES:
        raise Refused
    document = json.loads(data)
    allowed = {'name', 'type', 'notes', 'login', 'fields', 'secureNote', 'card', 'identity', 'sshKey', 'favorite', 'reprompt'}
    if not isinstance(document, dict) or not document or document.keys() - allowed:
        raise Refused
    if creating and not {'name', 'type'}.issubset(document):
        raise Refused
    if 'name' in document and (not isinstance(document['name'], str) or not document['name'].strip()):
        raise Refused
    if 'type' in document and (type(document['type']) is not int or document['type'] not in (1, 2, 3, 4, 5)):
        raise Refused
    return document


@contextlib.contextmanager
def write_lock():
    path = Path.home() / '.vault-write.lock'
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600), 'w') as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Refused
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def attach(item, path, organization, collection):
    name = Path(path).name
    if not name or len(name) > 255 or any(ord(char) < 32 or ord(char) > 126 or char in ('"', '\\') for char in name):
        raise Refused
    data = read_file(path)
    previous = [entry for entry in item.get('attachments') or [] if entry.get('fileName') == name]
    if len(previous) > 1:
        raise Refused
    boundary = 'vault-' + uuid4().hex
    body = ('--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="' + name +
            '"\r\nContent-Type: application/octet-stream\r\n\r\n').encode() + data + ('\r\n--' + boundary + '--\r\n').encode()
    updated = fetch('/attachment?' + urlencode({'itemid': item['id']}), method='POST', payload=body,
        content_type='multipart/form-data; boundary=' + boundary, timeout=VAULT_OPERATION_TIMEOUT_SECONDS)
    with confirming_write(item['id']):
        updated = scoped_item(updated, organization, collection, item['id'])
        existing = {entry['id'] for entry in item.get('attachments') or []}
        added = [entry for entry in updated.get('attachments') or [] if entry.get('fileName') == name and entry['id'] not in existing]
        if len(added) != 1 or not isinstance(added[0]['id'], str) or not re.fullmatch(r'[A-Za-z0-9-]{1,128}', added[0]['id']):
            raise Refused
        attachment_id = added[0]['id']
    with confirming_write(item['id'], attachment_id):
        expected_attachment = {key: added[0][key] for key in ('id', 'fileName', 'size')}

        def attachment_ready(removed_id=None):
            current = scoped_item(fetch('/object/item/' + item['id']), organization, collection, item['id'])
            attachments = current.get('attachments') or []
            matches = [entry for entry in attachments if entry.get('id') == attachment_id]
            return len(matches) == 1 and matches_fields(matches[0], expected_attachment) and (
                removed_id is None or not any(entry.get('id') == removed_id for entry in attachments))

        wait_for_cache(attachment_ready)
        suffix = '?' + urlencode({'itemid': item['id']})
        attachment_path = '/object/attachment/' + quote(attachment_id, safe='') + suffix
        try:
            if fetch(attachment_path, binary=True, timeout=VAULT_OPERATION_TIMEOUT_SECONDS) != data:
                raise Refused
        except Exception:
            with contextlib.suppress(Exception):
                fetch(attachment_path, method='DELETE', timeout=VAULT_OPERATION_TIMEOUT_SECONDS)
            raise
        if previous:
            fetch('/object/attachment/' + quote(previous[0]['id'], safe='') + suffix, method='DELETE',
                  timeout=VAULT_OPERATION_TIMEOUT_SECONDS)
            wait_for_cache(lambda: attachment_ready(previous[0]['id']))
    return attachment_id


def write(command, argv):
    parser = Parser(description='Write organization-scoped vault items from private files or VAULT_ environment variables.')
    if command != 'create':
        parser.add_argument('--item', required=True)
    if command in ('create', 'update'):
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument('--input-env')
        source.add_argument('--input-file')
    if command == 'attach':
        parser.add_argument('--file', required=True)
    args = parser.parse_args(argv)
    organization, collection = scope()
    document = input_document(args, command == 'create') if command in ('create', 'update') else None
    with write_lock():
        if command == 'create':
            created = fetch('/object/item', method='POST', payload={**document,
                'organizationId': organization, 'collectionIds': [collection], 'folderId': None},
                timeout=VAULT_OPERATION_TIMEOUT_SECONDS)
            item_id = checked_uuid(created['id'])
            with confirming_write(item_id):
                item = scoped_item(created, organization, collection, item_id)
                wait_for_cache(lambda: scoped_item(fetch('/object/item/' + item_id),
                    organization, collection, item_id))
        else:
            item = selected_item(args.item)
        result = {'status': {'create': 'created', 'update': 'updated', 'trash': 'trashed', 'attach': 'attached'}[command],
                  'itemId': item['id']}
        if command == 'update':
            updated = fetch('/object/item/' + item['id'], method='PUT', payload={**item, **document},
                            timeout=VAULT_OPERATION_TIMEOUT_SECONDS)
            with confirming_write(item['id']):
                scoped_item(updated, organization, collection, item['id'])
                wait_for_cache(lambda: matches_fields(scoped_item(fetch('/object/item/' + item['id']),
                    organization, collection, item['id']), document))
        elif command == 'trash':
            fetch('/object/item/' + item['id'], method='DELETE', timeout=VAULT_OPERATION_TIMEOUT_SECONDS)
            with confirming_write(item['id']):
                wait_for_cache(lambda: scoped_item(fetch('/object/item/' + item['id']), organization, collection,
                    item['id'], allow_trashed=True).get('deletedDate') is not None)
        elif command == 'attach':
            result['attachmentId'] = attach(item, args.file, organization, collection)
    print(json.dumps(result))
    return 0


def bw(arguments, client_env):
    with subprocess.Popen(['bw', *arguments], env=client_env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True) as child:
        try:
            output, _ = child.communicate(timeout=VAULT_OPERATION_TIMEOUT_SECONDS)
            if child.returncode:
                raise Refused
            return output.decode().strip()
        finally:
            stop(child)


def matches_agent(status):
    return status.get('userEmail') == os.environ['BW_EMAIL'] and isinstance(status.get('serverUrl'), str) and (
        status['serverUrl'].rstrip('/') == os.environ['BW_SERVER'].rstrip('/'))


def retry(action):
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return action()
        except (Cancelled, UnsafeListener):
            raise
        except Exception:
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            time.sleep(RETRY_DELAY_SECONDS)


def open_session(client_env):
    status = json.loads(bw(['status'], client_env))
    if status['status'] == 'unauthenticated':
        bw(['config', 'server', os.environ['BW_SERVER']], client_env)
        session = bw(['login', os.environ['BW_EMAIL'], '--passwordenv', 'BW_PASSWORD', '--raw'], client_env)
        if not session or '\n' in session:
            raise Refused
        client_env['BW_SESSION'] = session
    elif not matches_agent(status):
        raise Refused
    return client_env


def owns_listener(server):
    addresses = {host + f':{urlsplit(ADDRESS).port:04X}' for host in (
        '0100007F', '00000000', '00000000000000000000000001000000',
        '0000000000000000FFFF00000100007F', '0000000000000000FFFF000000000000',
        '00000000000000000000000000000000')}
    listeners = []
    for name in ('tcp', 'tcp6'):
        try:
            rows = (PROC / 'net' / name).read_text().splitlines()[1:]
        except FileNotFoundError:
            if name == 'tcp6':
                continue
            raise Refused
        for row in rows:
            fields = row.split()
            if fields[3] == '0A' and fields[1] in addresses:
                if int(fields[7]) != os.getuid():
                    raise Refused
                listeners.append('socket:[' + fields[9] + ']')
    if not listeners:
        return False
    sockets = set()
    for descriptor in (PROC / str(server.pid) / 'fd').iterdir():
        with contextlib.suppress(FileNotFoundError):
            sockets.add(os.readlink(descriptor))
    if not set(listeners).issubset(sockets):
        raise Refused
    return True


def client_status(server):
    if not owns_listener(server):
        return {}
    try:
        status = fetch('/status')['template']
        return status if isinstance(status, dict) else {}
    except Cancelled:
        raise
    except Exception:
        return {}


def client_ready(server):
    status = client_status(server)
    return status.get('status') == 'unlocked' and matches_agent(status)


def wait_for_status(server):
    for _ in range(CLIENT_START_ATTEMPTS):
        status = client_status(server)
        if server.poll() is not None or status:
            return status
        time.sleep(CLIENT_START_POLL_SECONDS)
    raise Refused


def require_listener(server):
    try:
        if not owns_listener(server):
            raise Refused
    except Cancelled:
        raise
    except Exception as error:
        raise UnsafeListener from error


def unlock(server, password):
    require_listener(server)
    return fetch('/unlock', method='POST', payload={'password': password})


def agent_scope():
    variant = os.environ['T3_RUNTIME_VARIANT']
    server = urlsplit(os.environ['BW_SERVER'])
    if variant not in ('homelab', 'work') or not os.environ['BW_PASSWORD'] or '@' not in os.environ['BW_EMAIL'] or (
            server.scheme != 'https' or not server.hostname or server.username or server.password or server.query or server.fragment):
        raise Refused
    return {'environment': variant, 'organizationId': checked_uuid(os.environ['BW_ORGANIZATION_ID']),
            'collectionId': checked_uuid(os.environ['BW_COLLECTION_ID'])}


def publish_scope(selected):
    descriptor, temporary = tempfile.mkstemp(dir=SCOPE_FILE.parent, prefix='.scope-')
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(selected, output)
            os.fchmod(output.fileno(), 0o444)
        os.replace(temporary, SCOPE_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def running_client(client_env):
    with subprocess.Popen(['bw', 'serve', '--hostname', '127.0.0.1', '--port', '8087'], env=client_env,
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          start_new_session=True) as server:
        try:
            yield server
        finally:
            SCOPE_FILE.unlink(missing_ok=True)
            stop(server)


def log_agent_refusal():
    print('vault: SSH agent proxy request refused', file=sys.stderr, flush=True)


def agent_message(stream):
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise Refused
    length, = struct.unpack('!I', header)
    if not 1 <= length <= MAX_AGENT_MESSAGE_BYTES:
        raise Refused
    message = stream.read(length)
    if len(message) != length:
        raise Refused
    return header + message


class AgentProxy(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        log_agent_refusal()


class AgentRequest(socketserver.StreamRequestHandler):
    timeout = SSH_REQUEST_TIMEOUT_SECONDS

    def handle(self):
        try:
            _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != SSH_CLIENT_UID:
                log_agent_refusal()
                return
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
                upstream.settimeout(SSH_REQUEST_TIMEOUT_SECONDS)
                upstream.connect(self.server.agent_socket)
                with upstream.makefile('rb') as stream:
                    while True:
                        message = agent_message(self.rfile)
                        if message is None:
                            return
                        if message[4] not in (AGENT_REQUEST_IDENTITIES, AGENT_SIGN_REQUEST):
                            self.wfile.write(AGENT_FAILURE)
                            log_agent_refusal()
                            continue
                        upstream.sendall(message)
                        reply = agent_message(stream)
                        if reply is None:
                            raise Refused
                        self.wfile.write(reply)
        except (OSError, Refused):
            log_agent_refusal()


def scoped_keys(selected, server, wanted):
    require_listener(server)
    items = fetch('/list/object/items?' + urlencode({'organizationid': selected['organizationId'],
                                                   'collectionid': selected['collectionId']}))['data'] if wanted else []
    keys = []
    for item in items:
        if item.get('type') != SSH_KEY_ITEM_TYPE or item.get('organizationId') != selected['organizationId'] or (
                selected['collectionId'] not in item.get('collectionIds', [])):
            continue
        key = (item.get('sshKey') or {}).get('privateKey')
        if not isinstance(key, str) or not key or len(key.encode()) > MAX_VALUE_BYTES:
            raise Refused
        keys.append(key.strip() + '\n')
    return keys


def loaded_agent(env, path, keys):
    env = {**env, 'SSH_AUTH_SOCK': path}
    Path(path).unlink(missing_ok=True)
    agent = subprocess.Popen(['ssh-agent', '-D', '-a', path], env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        for _ in range(AGENT_START_ATTEMPTS):
            if agent.poll() is not None:
                raise Refused
            if Path(path).is_socket():
                break
            time.sleep(AGENT_START_POLL_SECONDS)
        else:
            raise Refused
        for key in keys:
            result = subprocess.run(['ssh-add', '-q', '-'], input=key.encode(), env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=SSH_REQUEST_TIMEOUT_SECONDS)
            if result.returncode:
                raise Refused
    except BaseException:
        stop(agent)
        raise
    return agent


@contextlib.contextmanager
def ssh_agent(selected, server):
    if selected['environment'] not in ('homelab', 'work'):
        raise Refused
    public_socket = SCOPE_FILE.with_name('ssh-agent.sock')
    public_socket.unlink(missing_ok=True)
    wanted = selected['environment'] != 'work' or bool(
        runpy.run_path(str(WORK_SSH_INPUTS / 'work-ssh.py'))['hosts'](WORK_SSH_INPUTS / 'hosts.json'))
    keys = scoped_keys(selected, server, wanted)
    with tempfile.TemporaryDirectory(prefix='vault-agent-', dir='/tmp') as directory:
        os.chmod(directory, 0o700)
        private_socket = str(Path(directory) / 'agent.sock')
        env = {'HOME': directory, 'PATH': os.environ['PATH'], 'SSH_ASKPASS_REQUIRE': 'never'}
        agent = loaded_agent(env, private_socket, keys)
        try:
            def refresh():
                nonlocal agent, keys
                fetch('/sync', method='POST', timeout=VAULT_OPERATION_TIMEOUT_SECONDS)
                current = scoped_keys(selected, server, wanted)
                if set(current) == set(keys):
                    return
                # A fully loaded replacement takes over the proxied path in one rename, so no request sees a
                # partial key set. stop() uses SIGKILL, so the old agent never unlinks the shared path on exit.
                # Proxied connections opened before the swap still point at the old agent and fail; clients reconnect.
                replacement = loaded_agent(env, str(Path(directory) / 'next.sock'), current)
                try:
                    os.replace(Path(directory) / 'next.sock', private_socket)
                    agent, replacement, keys = replacement, agent, current
                finally:
                    stop(replacement)  # the old agent after a swap, the new one if the rename failed or was cancelled
                print('vault: SSH agent reloaded; ' + str(len(keys)) + ' scoped keys', flush=True)

            with AgentProxy(str(public_socket), AgentRequest) as proxy:
                proxy.agent_socket = private_socket
                os.chmod(public_socket, 0o666)
                thread = threading.Thread(target=proxy.serve_forever, kwargs={'poll_interval': PROXY_POLL_SECONDS}, daemon=True)
                thread.start()
                try:
                    if not keys and wanted:
                        print('vault: no scoped SSH keys; agent is empty', flush=True)
                    yield SimpleNamespace(poll=lambda: agent.poll(), refresh=refresh)
                finally:
                    public_socket.unlink(missing_ok=True)
                    proxy.shutdown()
                    thread.join()
        finally:
            public_socket.unlink(missing_ok=True)
            stop(agent)


def refresh_keys(agent):
    try:
        agent.refresh()
    except (Cancelled, UnsafeListener):
        raise
    except Exception as error:
        print('vault: SSH key refresh failed (' + type(error).__name__ + '); keeping loaded keys',
              file=sys.stderr, flush=True)


def serve():
    SCOPE_FILE.unlink(missing_ok=True)
    selected = agent_scope()
    data = Path.home() / 'client'
    data.mkdir(mode=0o700, exist_ok=True)
    info = data.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Refused
    password = os.environ['BW_PASSWORD']
    client_env = {key: os.environ[key] for key in ('HOME', 'PATH')}
    client_env.update(BITWARDENCLI_APPDATA_DIR=str(data), BW_NOINTERACTION='true')
    cached = (data / 'data.json').is_file()
    while True:
        if not cached:
            client_env['BW_PASSWORD'] = password
            try:
                client_env = retry(lambda: open_session(client_env))
            finally:
                client_env.pop('BW_PASSWORD', None)
        with running_client(client_env) as server:
            status = wait_for_status(server)
            if cached and (server.poll() is not None or status.get('status') == 'unauthenticated'):
                cached = False
                continue
            if server.poll() is not None or status.get('status') not in ('locked', 'unlocked') or not matches_agent(status):
                raise Refused
            if status['status'] == 'locked':
                retry(lambda: unlock(server, password))
            if not owns_listener(server):
                raise UnsafeListener
            fetch('/sync', method='POST', timeout=VAULT_OPERATION_TIMEOUT_SECONDS)
            if not client_ready(server):
                raise Refused
            ssh = os.environ.get('T3_VAULT_SSH_AGENT') == '1'
            with ssh_agent(selected, server) if ssh else contextlib.nullcontext() as agent:
                publish_scope(selected)
                print('vault: local client ready', flush=True)
                failures = 0
                due = time.monotonic() + SSH_KEY_REFRESH_SECONDS
                while server.poll() is None:
                    failures = 0 if client_ready(server) else failures + 1
                    if failures >= 3 or (agent is not None and agent.poll() is not None):
                        raise Refused
                    if agent is not None and time.monotonic() >= due:
                        refresh_keys(agent)
                        due = time.monotonic() + SSH_KEY_REFRESH_SECONDS
                    time.sleep(HEALTH_INTERVAL)
                return exit_code(server.returncode) or 1


def main(argv):
    def cancel(*_):
        raise Cancelled

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    try:
        if argv and argv[0] == 'run':
            return run(argv[1:])
        if argv and argv[0] == 'list':
            return list_items(argv[1:])
        if argv == ['serve']:
            return serve()
        if argv and argv[0] in ('create', 'update', 'trash', 'attach'):
            return write(argv[0], argv[1:])
        raise Refused
    except WriteUnconfirmed as error:
        message = 'vault: write sent, local cache not confirmed; item ' + error.item_id + ' may exist'
        if error.attachment_id is not None:
            message += '; attachment ' + error.attachment_id + ' may exist'
        print(message + '; inspect before retrying', file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print('vault: timed out; exit code 124')
        return 124
    except Cancelled:
        print('vault: cancelled; exit code 130')
        return 130
    except Exception as error:
        # Type and own-file call chain only: no exception text, which could carry vault data.
        chain = ' > '.join(f'{f.name}:{f.lineno}' for f in traceback.extract_tb(error.__traceback__)
                           if f.filename == __file__)
        print(f'vault: request or startup refused ({type(error).__name__} at {chain})', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
