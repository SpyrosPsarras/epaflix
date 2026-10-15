#!/usr/bin/env python3
import argparse
import contextlib
import json
import math
import os
from pathlib import Path
import re
import resource
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
import urllib.request
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID

ADDRESS = 'http://127.0.0.1:8087'
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
MAX_SCOPE_BYTES = 1024
MAX_VALUE_BYTES = 16 * 1024
MAX_AGENT_MESSAGE_BYTES = 256 * 1024
SSH_KEY_ITEM_TYPE = 5
SCOPE_FILE = Path('/run/t3-vault/scope.json')
PROC = Path('/proc')
HEALTH_INTERVAL = 2
HTTP_REQUEST_TIMEOUT_SECONDS = 10
VAULT_OPERATION_TIMEOUT_SECONDS = 45
CHILD_TIMEOUT_MIN_SECONDS = 0.1
CHILD_TIMEOUT_MAX_SECONDS = 120
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2
CLIENT_START_ATTEMPTS = 100
CLIENT_START_POLL_SECONDS = 0.1
SSH_REQUEST_TIMEOUT_SECONDS = 10
AGENT_START_ATTEMPTS = 100
AGENT_START_POLL_SECONDS = 0.02
PROXY_POLL_SECONDS = 0.1
SSH_CLIENT_UID = 1000
AGENT_REQUEST_IDENTITIES = 11
AGENT_SIGN_REQUEST = 13
AGENT_FAILURE = b'\x00\x00\x00\x01\x05'


class Refused(Exception):
    pass


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


def fetch(path, binary=False, method='GET', payload=None, timeout=HTTP_REQUEST_TIMEOUT_SECONDS):
    def expired(*_):
        raise Refused
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(ADDRESS + path, method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={} if payload is None else {'Content-Type': 'application/json'})
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
        return result['data']
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def selected_item(args):
    organization, collection = scope()
    if args.item:
        item = fetch('/object/item/' + checked_uuid(args.item))
        if item.get('id') != args.item:
            raise Refused
    else:
        items = fetch('/list/object/items?' + urlencode({'search': args.name, 'organizationid': organization,
                                                       'collectionid': collection}))['data']
        matches = [item for item in items if item.get('name') == args.name and
                   item.get('organizationId') == organization and collection in item.get('collectionIds', [])]
        if len(matches) != 1:
            raise Refused
        item = matches[0]
    if item.get('organizationId') != organization or collection not in item.get('collectionIds', []):
        raise Refused
    checked_uuid(item['id'])
    return item


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
    parser.add_argument('--field', action='append', default=[])
    parser.add_argument('--attachment')
    parser.add_argument('--attachment-env')
    parser.add_argument('--timeout', type=float, default=CHILD_TIMEOUT_MAX_SECONDS)
    boundary = argv.index('--')
    args = parser.parse_args(argv[:boundary])
    command = argv[boundary + 1:]
    mappings = []
    if args.password_env:
        mappings.append((None, environment_name(args.password_env)))
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
    item = selected_item(args)
    additions = {}
    for field, target in mappings:
        values = [item.get('login', {}).get('password')] if field is None else [
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


def unlock(server, password):
    try:
        if not owns_listener(server):
            raise Refused
    except Cancelled:
        raise
    except Exception as error:
        raise UnsafeListener from error
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


@contextlib.contextmanager
def ssh_agent(selected, server):
    if selected['environment'] != 'homelab':
        raise Refused
    public_socket = SCOPE_FILE.with_name('ssh-agent.sock')
    public_socket.unlink(missing_ok=True)
    if not owns_listener(server):
        raise UnsafeListener
    items = fetch('/list/object/items?' + urlencode({'organizationid': selected['organizationId'],
                                                   'collectionid': selected['collectionId']}))['data']
    keys = [item for item in items if item.get('type') == SSH_KEY_ITEM_TYPE and
            item.get('organizationId') == selected['organizationId'] and
            selected['collectionId'] in item.get('collectionIds', [])]
    with tempfile.TemporaryDirectory(prefix='vault-agent-', dir='/tmp') as directory:
        os.chmod(directory, 0o700)
        private_socket = str(Path(directory) / 'agent.sock')
        env = {'HOME': directory, 'PATH': os.environ['PATH'], 'SSH_AUTH_SOCK': private_socket,
               'SSH_ASKPASS_REQUIRE': 'never'}
        with subprocess.Popen(['ssh-agent', '-D', '-a', private_socket], env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              start_new_session=True) as agent:
            try:
                for _ in range(AGENT_START_ATTEMPTS):
                    if agent.poll() is not None:
                        raise Refused
                    if Path(private_socket).is_socket():
                        break
                    time.sleep(AGENT_START_POLL_SECONDS)
                else:
                    raise Refused
                for item in keys:
                    key = (item.get('sshKey') or {}).get('privateKey')
                    if not isinstance(key, str) or not key or len(key.encode()) > MAX_VALUE_BYTES:
                        raise Refused
                    result = subprocess.run(['ssh-add', '-q', '-'], input=(key.strip() + '\n').encode(),
                                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            timeout=SSH_REQUEST_TIMEOUT_SECONDS)
                    if result.returncode:
                        raise Refused
                with AgentProxy(str(public_socket), AgentRequest) as proxy:
                    proxy.agent_socket = private_socket
                    os.chmod(public_socket, 0o666)
                    thread = threading.Thread(target=proxy.serve_forever, kwargs={'poll_interval': PROXY_POLL_SECONDS}, daemon=True)
                    thread.start()
                    try:
                        if not keys:
                            print('vault: no scoped SSH keys; agent is empty', flush=True)
                        yield agent
                    finally:
                        public_socket.unlink(missing_ok=True)
                        proxy.shutdown()
                        thread.join()
            finally:
                public_socket.unlink(missing_ok=True)
                stop(agent)


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
            fetch('/sync', method='POST', timeout=VAULT_OPERATION_TIMEOUT_SECONDS)
            if not client_ready(server):
                raise Refused
            ssh = os.environ.get('T3_VAULT_SSH_AGENT') == '1'
            with ssh_agent(selected, server) if ssh else contextlib.nullcontext() as agent:
                publish_scope(selected)
                print('vault: local client ready', flush=True)
                failures = 0
                while server.poll() is None:
                    failures = 0 if client_ready(server) else failures + 1
                    if failures >= 3 or (agent is not None and agent.poll() is not None):
                        raise Refused
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
        if argv == ['serve']:
            return serve()
        raise Refused
    except subprocess.TimeoutExpired:
        print('vault: timed out; exit code 124')
        return 124
    except Cancelled:
        print('vault: cancelled; exit code 130')
        return 130
    except Exception:
        print('vault: request or startup refused', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
