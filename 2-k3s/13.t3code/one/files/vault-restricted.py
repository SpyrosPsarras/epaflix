import argparse
import base64
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
import urllib.request
from urllib.parse import urlsplit
from uuid import UUID

from hub_services import validate_services

RESPONSE_LIMIT = 12 * 1024 * 1024
ATTACHMENT_LIMIT = 8 * 1024 * 1024
REQUEST_TIMEOUT = 130
COMMAND_TIMEOUT = 120


class Refused(Exception):
    pass


class Cancelled(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refused


def strict_json(data):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise Refused
            result[key] = value
        return result
    return json.loads(data, object_pairs_hook=pairs)


def text(value, limit):
    return isinstance(value, str) and 0 < len(value) <= limit and not any(
        ord(c) < 32 or ord(c) == 127 for c in value)


def private_read(path, limit):
    if not Path(path).is_absolute():
        raise Refused
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Refused
        result = source.read(limit + 1)
    if len(result) > limit:
        raise Refused
    return result


def configuration(route):
    if os.environ.get('MCP_HUB_INSTANCE') != 'homelab' or os.environ.get(
            'T3_RUNTIME_VARIANT', 'homelab') != 'homelab':
        raise Refused
    marker = Path(os.environ.get('HOME', '/nonexistent')) / '.config/t3-runtime/environment'
    try:
        marker.lstat()
    except FileNotFoundError:
        pass
    else:
        if private_read(marker, 16) != b'homelab\n':
            raise Refused
    services = validate_services(strict_json(private_read(os.environ['MCP_HUB_SERVICES_FILE'], 16384)), 'homelab')
    if services.get('vault_reader') != 'restricted-v1' or route not in services['services']:
        raise Refused
    token = os.environ['MCP_HUB_TOKEN']
    if not re.fullmatch(r'homelab\.[A-Za-z0-9_-]{16,4088}', token):
        raise Refused
    address = os.environ['MCP_HUB_URL']
    if not text(address, 2048) or any(ord(c) <= 32 or ord(c) >= 127 or c == '\\' for c in address):
        raise Refused
    url = urlsplit(address)
    if not url.hostname or url.port == 0 or url.username is not None or url.password is not None or (
            url.path not in ('', '/') or url.query or url.fragment):
        raise Refused
    if url.scheme != 'https' and not (url.scheme == 'http' and url.hostname in (
            '127.0.0.1', '::1', 'mcp-hub.mcp-hub-homelab.svc.cluster.local')):
        raise Refused
    return address.rstrip('/'), token


def fetch(route, value):
    address, token = configuration(route)
    request = urllib.request.Request(address + '/' + route, data=json.dumps(value).encode(), headers={
        'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
        'Accept': 'application/json', 'Accept-Encoding': 'identity'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    def timed_out(*_):
        raise Refused
    previous = signal.signal(signal.SIGALRM, timed_out)
    signal.setitimer(signal.ITIMER_REAL, REQUEST_TIMEOUT)
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            if response.status != 200 or response.headers.get_content_type() != 'application/json' or (
                    response.headers.get('Content-Encoding', 'identity').lower() != 'identity'):
                raise Refused
            if any(len(response.headers.get_all(name, [])) > 1 for name in (
                    'Content-Type', 'Content-Encoding', 'Transfer-Encoding')):
                raise Refused
            lengths = response.headers.get_all('Content-Length', [])
            if len(lengths) > 1 or lengths and (not re.fullmatch(r'[0-9]{1,10}', lengths[0]) or
                                               int(lengths[0]) > RESPONSE_LIMIT):
                raise Refused
            data = response.read(RESPONSE_LIMIT + 1)
            if len(data) > RESPONSE_LIMIT or lengths and len(data) != int(lengths[0]):
                raise Refused
            return strict_json(data.decode('utf-8'))
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def environment_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'VAULT_[A-Z][A-Z0-9_]{0,63}', name):
        raise Refused
    return name


def mappings(args):
    result = []
    for source, target in (('password', args.password_env), ('username', args.username_env)):
        if target is not None:
            result.append((source, None, environment_name(target)))
    for mapping in args.field:
        source, separator, target = mapping.rpartition('=')
        if not separator or not text(source, 512):
            raise Refused
        result.append(('fields', source, environment_name(target)))
    targets = [value[2] for value in result]
    if len(targets) != len(set(targets)) or len(result) > 128:
        raise Refused
    return result


def secret_environment(value, mapping):
    if not isinstance(value, dict) or set(value) != {'password', 'username', 'fields'} or (
            not isinstance(value['fields'], dict) or len(value['fields']) > 128):
        raise Refused
    values = [value['password'], value['username'], *value['fields'].values()]
    if any(v is not None and (not isinstance(v, str) or '\0' in v or len(v.encode('utf-8')) > 16384)
           for v in values) or any(not text(k, 512) for k in value['fields']):
        raise Refused
    result = {}
    for source, field, target in mapping:
        selected = value[source] if field is None else value['fields'].get(field)
        if selected is None:
            raise Refused
        result[target] = selected
    if sum(len(v.encode('utf-8')) for v in result.values()) > 65536:
        raise Refused
    return result


def safe_filename(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', value) or value.endswith('.'):
        raise Refused
    return value


def attachment(value):
    if not isinstance(value, dict) or set(value) != {'filename', 'content_b64'}:
        raise Refused
    filename = safe_filename(value['filename'])
    encoded = value['content_b64']
    if not isinstance(encoded, str) or len(encoded) > 4 * math.ceil(ATTACHMENT_LIMIT / 3):
        raise Refused
    content = base64.b64decode(encoded, validate=True)
    if len(content) > ATTACHMENT_LIMIT or base64.b64encode(content).decode('ascii') != encoded:
        raise Refused
    return filename, content


@contextlib.contextmanager
def destination(path):
    if not isinstance(path, str) or not text(path, 4096) or not path.startswith('/'):
        raise Refused
    parts = path.split('/')[1:]
    if any(p in ('', '.', '..') for p in parts):
        raise Refused
    safe_filename(parts[-1])
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Refused
        try:
            os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise Refused
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def save_file(directory, name, content):
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(content)
    except BaseException:
        os.unlink(name, dir_fd=directory)
        raise


def child_environment(additions):
    result = {k: v for k, v in os.environ.items() if not k.startswith(('VAULT_', 'BW_', 'MCP_HUB_'))}
    result.update(additions)
    return result


def quiet_command(command, additions, timeout):
    def limits():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (math.ceil(timeout) + 1, math.ceil(timeout) + 1))
    with subprocess.Popen(command, env=child_environment(additions), stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          start_new_session=True, preexec_fn=limits) as child:
        try:
            code = child.wait(timeout=timeout)
            return code if code >= 0 else 128 - code
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()


def arguments(argv):
    parser = Parser(description='Quiet homelab restricted-v1 command and attachment helper.')
    parser.add_argument('mode', choices=('run', 'download'))
    parser.add_argument('--collection-id', required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--item-id')
    selection.add_argument('--title')
    parser.add_argument('--password-env')
    parser.add_argument('--username-env')
    parser.add_argument('--field', action='append', default=[])
    parser.add_argument('--attachment')
    parser.add_argument('--attachment-env')
    parser.add_argument('--destination')
    parser.add_argument('--timeout', type=float, default=COMMAND_TIMEOUT)
    options, separated, command = argv, False, []
    if '--' in argv:
        index = argv.index('--')
        options, separated, command = argv[:index], True, argv[index + 1:]
    args = parser.parse_args(options)
    reference = {'collection_id': args.collection_id}
    for value in (args.collection_id, args.item_id):
        if value is not None and str(UUID(value)) != value:
            raise Refused
    if args.title is not None:
        if not text(args.title, 512):
            raise Refused
        reference['title'] = args.title
    else:
        reference['item_id'] = args.item_id
    mapping = mappings(args)
    if args.attachment is not None:
        safe_filename(args.attachment)
    if not math.isfinite(args.timeout) or not 0.1 <= args.timeout <= COMMAND_TIMEOUT:
        raise Refused
    if args.mode == 'download':
        if mapping or args.attachment is None or args.destination is None or args.attachment_env or separated:
            raise Refused
    else:
        if not command or args.destination or not mapping and args.attachment is None:
            raise Refused
        if (args.attachment is None) != (args.attachment_env is None):
            raise Refused
        if args.attachment_env is not None:
            environment_name(args.attachment_env)
            if args.attachment_env in [v[2] for v in mapping]:
                raise Refused
    return args, reference, mapping, command


def main(argv):
    def cancel(*_):
        raise Cancelled
    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    try:
        args, reference, mapping, command = arguments(argv)
        if args.mode == 'download':
            configuration('vault-attachment')
            with destination(args.destination) as (directory, name):
                _, content = attachment(fetch('vault-attachment', {'reference': reference, 'attachment': args.attachment}))
                save_file(directory, name, content)
            print(json.dumps({'status': 'saved', 'destination': args.destination, 'bytes': len(content)}))
            return 0
        for route in (['vault-secret'] if mapping else []) + (['vault-attachment'] if args.attachment else []):
            configuration(route)
        additions = secret_environment(fetch('vault-secret', {'reference': reference}), mapping) if mapping else {}
        with contextlib.ExitStack() as stack:
            if args.attachment is not None:
                filename, content = attachment(fetch('vault-attachment', {'reference': reference, 'attachment': args.attachment}))
                temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix='vault-restricted-', dir='/tmp'))
                path = temporary + '/' + filename
                with destination(path) as (directory, name):
                    save_file(directory, name, content)
                additions[args.attachment_env] = path
            code = quiet_command(command, additions, args.timeout)
        print('vault-restricted: command finished; exit code ' + str(code))
        return code
    except subprocess.TimeoutExpired:
        print('vault-restricted: command timed out; exit code 124')
        return 124
    except Cancelled:
        print('vault-restricted: cancelled; exit code 130')
        return 130
    except Exception:
        print('vault-restricted: request or execution refused', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
