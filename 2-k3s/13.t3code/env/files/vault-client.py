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
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import quote, urlencode, urlsplit
from uuid import UUID

ADDRESS = 'http://127.0.0.1:8087'
LIMIT = 12 * 1024 * 1024
SCOPE_FILE = Path('/run/t3-vault/scope.json')
HEALTH_INTERVAL = 2


class Refused(Exception):
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
    if len(data) > 1024:
        raise Refused
    selected = json.loads(data)
    if selected['environment'] != variant:
        raise Refused
    return checked_uuid(selected['organizationId']), checked_uuid(selected['collectionId'])


def fetch(path, binary=False):
    def expired(*_):
        raise Refused
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 10)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(ADDRESS + path, timeout=10) as response:
            if response.status != 200 or response.headers.get_content_type() != (
                    'application/octet-stream' if binary else 'application/json'):
                raise Refused
            data = response.read(LIMIT + 1)
            if len(data) > LIMIT:
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
    parser.add_argument('--timeout', type=float, default=120)
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
            not math.isfinite(args.timeout) or not 0.1 <= args.timeout <= 120):
        raise Refused
    item = selected_item(args)
    additions = {}
    for field, target in mappings:
        values = [item.get('login', {}).get('password')] if field is None else [
            entry.get('value') for entry in item.get('fields') or [] if entry.get('name') == field]
        if len(values) != 1 or not isinstance(values[0], str) or '\0' in values[0] or len(values[0].encode()) > 16384:
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
            output, _ = child.communicate(timeout=45)
            if child.returncode:
                raise Refused
            return output.decode().strip()
        finally:
            stop(child)


def matches_agent(status):
    return status.get('userEmail') == os.environ['BW_EMAIL'] and (
        status.get('serverUrl', '').rstrip('/') == os.environ['BW_SERVER'].rstrip('/'))


def open_session(client_env):
    status = json.loads(bw(['status'], client_env))
    if status['status'] == 'unauthenticated':
        bw(['config', 'server', os.environ['BW_SERVER']], client_env)
        session = bw(['login', os.environ['BW_EMAIL'], '--passwordenv', 'BW_PASSWORD', '--raw'], client_env)
    else:
        if not matches_agent(status):
            raise Refused
        session = bw(['unlock', '--passwordenv', 'BW_PASSWORD', '--raw'], client_env)
    if not session or '\n' in session:
        raise Refused
    client_env['BW_SESSION'] = session
    bw(['sync'], client_env)
    return client_env


def client_ready():
    try:
        status = fetch('/status')['template']
        return status.get('status') == 'unlocked' and matches_agent(status)
    except Cancelled:
        raise
    except Exception:
        return False


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


def serve():
    SCOPE_FILE.unlink(missing_ok=True)
    selected = agent_scope()
    data = Path.home() / 'client'
    data.mkdir(mode=0o700, exist_ok=True)
    info = data.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise Refused
    client_env = {key: os.environ[key] for key in ('HOME', 'PATH', 'BW_PASSWORD')}
    client_env.update(BITWARDENCLI_APPDATA_DIR=str(data), BW_NOINTERACTION='true')
    for attempt in range(3):
        try:
            client_env = open_session(client_env)
            break
        except Refused:
            if attempt == 2:
                raise
            client_env.pop('BW_SESSION', None)
            time.sleep(2)
    client_env.pop('BW_PASSWORD')
    with subprocess.Popen(['bw', 'serve', '--hostname', '127.0.0.1', '--port', '8087'], env=client_env,
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          start_new_session=True) as server:
        try:
            for _ in range(100):
                if server.poll() is not None:
                    raise Refused
                if client_ready():
                    break
                time.sleep(0.1)
            else:
                raise Refused
            publish_scope(selected)
            print('vault: local client ready', flush=True)
            failures = 0
            while server.poll() is None:
                failures = 0 if client_ready() else failures + 1
                if failures >= 3:
                    raise Refused
                time.sleep(HEALTH_INTERVAL)
            return exit_code(server.returncode) or 1
        finally:
            SCOPE_FILE.unlink(missing_ok=True)
            stop(server)


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
