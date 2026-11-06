#!/usr/bin/env python3
"""Keeper access for the work pod.

Sidecar (Commander venv): `keeper serve` holds the owner's persistent-login session and answers on
127.0.0.1:8088 for every typed record that login can see.
`keeper login` opens Commander's shell for the one-time interactive login.
t3 container (stdlib only): `keeper list|run|create|update|attach|trash`.
"""
import argparse
import contextlib
import fcntl
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import logging
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
import threading
import time
import traceback
from types import SimpleNamespace
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlsplit

ADDRESS = ('127.0.0.1', 8088)
CONFIG = Path('/keeper/config.json')
LOGIN_LOCK = Path('/keeper/login.lock')
COMMANDER = '/opt/keeper/bin/keeper'
MAX_BODY_BYTES = 12 * 1024 * 1024
MAX_VALUE_BYTES = 16 * 1024
SYNC_SECONDS = 10
KEEPALIVE_SECONDS = 300
LOGIN_RETRY_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 120
STOP_DEADLINE_SECONDS = 45  # under the pod's 60 s grace: Commander can block up to 300 s on a read or throttle sleep
CHILD_TIMEOUT_MIN_SECONDS = 0.1
CHILD_TIMEOUT_MAX_SECONDS = 120
DOCUMENT_KEYS = {'type', 'title', 'notes', 'fields', 'custom'}
# Field types that link other records (Commander's extract_typed_record_refs): documents could link any other record
# into this one, so they may not set them; `attach` manages files and updates keep the existing links.
LINK_TYPES = {'fileRef', 'addressRef', 'cardRef', 'recordRef', 'script'}
# TypedField strips the type, so " fileRef" would still become a link: accept only bare identifiers.
FIELD_TYPE = re.compile(r'[A-Za-z][A-Za-z0-9]{0,63}')
LINK_TYPES_FOLDED = {link.casefold() for link in LINK_TYPES}
kc = None  # Commander modules, loaded only by the sidecar; tests substitute fakes.


class Refused(Exception):
    pass


class Uncertain(Exception):
    def __init__(self, uid):
        self.uid = uid


class Cancelled(Exception):
    pass


class NotLoggedIn(Refused):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused


# ---- sidecar ----

def commander():
    from keepercommander import api, attachment, record_management, utils, vault
    from keepercommander.__main__ import get_params_from_config
    from keepercommander.commands.record import RecordRemoveCommand
    return SimpleNamespace(api=api, attachment=attachment, record_management=record_management, utils=utils,
                           vault=vault, get_params_from_config=get_params_from_config, remove_record=RecordRemoveCommand())


class NoPrompt:
    """Persistent login only: any password, 2FA, SSO or device-approval step fails the attempt."""
    def on_device_approval(self, step):
        step.cancel()

    def on_two_factor(self, step):
        step.cancel()

    def on_password(self, step):
        step.cancel()

    def on_sso_redirect(self, step):
        step.cancel()

    def on_sso_data_key(self, step):
        step.cancel()


def all_records(params):
    records = []
    for uid in sorted(params.record_cache):
        record = kc.vault.KeeperRecord.load(params, uid)
        if isinstance(record, kc.vault.TypedRecord):
            records.append(record)
    return records


def selected_record(params, query):
    records = all_records(params)
    if set(query) == {'uid'}:
        matches = [record for record in records if record.record_uid == query['uid']]
    elif set(query) == {'title'}:
        matches = [record for record in records if record.title == query['title']]
    else:
        raise Refused
    if len(matches) != 1:
        raise Refused
    return matches[0]


def file_references(record):
    return [field for field in record.fields + record.custom if field.type == 'fileRef']


def record_files(params, record):
    result = []
    for uid in [uid for reference in file_references(record) for uid in reference.value]:
        file = kc.vault.KeeperRecord.load(params, uid)
        if isinstance(file, kc.vault.FileRecord):
            result.append((uid, file.name))
    return result


def field_name(field):
    return field.label or field.type


def metadata(params, record):
    fields = [field for field in record.fields + record.custom if field.type != 'fileRef']
    return {
        'uid': record.record_uid,
        'title': record.title,
        'type': record.record_type,
        'password': any(field.type == 'password' and any(field.value) for field in record.fields),
        'fields': sorted({field_name(field) for field in fields}),
        'attachments': sorted(name for _, name in record_files(params, record)),
    }


def checked_document(body, creating):
    document = json.loads(body)
    if not isinstance(document, dict) or not document or document.keys() - DOCUMENT_KEYS:
        raise Refused
    if creating and not {'type', 'title'} <= document.keys():
        raise Refused
    for key in ('type', 'title'):
        if key in document and (not isinstance(document[key], str) or not document[key].strip()):
            raise Refused
    if 'notes' in document and not isinstance(document['notes'], str):
        raise Refused
    for key in ('fields', 'custom'):
        entries = document.get(key, [])
        if not isinstance(entries, list) or any(
                not isinstance(entry, dict) or not isinstance(entry.get('type'), str)
                or not FIELD_TYPE.fullmatch(entry['type']) or entry['type'].casefold() in LINK_TYPES_FOLDED
                or not isinstance(entry.get('value', []), list) for entry in entries):
            raise Refused
    return document


def attachment_name(name):
    if not name or len(name) > 255 or any(ord(char) < 32 or ord(char) > 126 or char in '"\\/' for char in name):
        raise Refused
    return name


class Session:
    def __init__(self, params):
        self.params = params
        self.synced = 0.0
        self.dead = False

    def sync(self, force=False):
        if force or time.monotonic() - self.synced >= SYNC_SECONDS:
            try:
                kc.api.sync_down(self.params)
            except Exception:
                self.dead = True
                raise
            self.synced = time.monotonic()

    @contextlib.contextmanager
    def writing(self, uid):
        # Anything after the first remote call may have reached Keeper.
        try:
            yield
        except Exception as error:
            raise Uncertain(uid) from error

    def confirm(self, check):
        self.sync(force=True)
        if not check():
            raise Refused

    def uids(self):
        return {record.record_uid for record in all_records(self.params)}

    def handle(self, method, path, query, body):
        params = self.params
        self.sync()
        if (method, path) == ('GET', '/list'):
            return [metadata(params, record) for record in all_records(params)]
        if (method, path) == ('GET', '/record'):
            record = selected_record(params, query)
            return {'uid': record.record_uid, 'title': record.title, 'type': record.record_type,
                    'fields': [vars(field) for field in record.fields if field.type != 'fileRef'],
                    'custom': [vars(field) for field in record.custom]}
        if (method, path) == ('GET', '/attachment'):
            record = selected_record(params, {'uid': query.get('uid', '')})
            matches = [uid for uid, name in record_files(params, record) if name == query.get('name')]
            if len(matches) != 1:
                raise Refused
            # By the file record's own uid: Commander's record-uid path reads only the first fileRef and skips v6.
            downloads = list(kc.attachment.prepare_attachment_download(params, matches[0]))
            if len(downloads) != 1:
                raise Refused
            with tempfile.TemporaryFile() as stream:
                downloads[0].download_to_stream(params, stream)
                stream.seek(0)
                data = stream.read(MAX_BODY_BYTES + 1)
            if len(data) > MAX_BODY_BYTES:
                raise Refused
            return data
        if method != 'POST':
            raise Refused
        if path == '/create':
            document = checked_document(body, creating=True)
            record = kc.vault.TypedRecord()
            record.load_record_data(document)
            record.record_uid = kc.utils.generate_uid()
            with self.writing(record.record_uid):
                kc.record_management.add_record_to_folder(params, record, None)  # My Vault root
                self.confirm(lambda: record.record_uid in self.uids())
            return {'status': 'created', 'uid': record.record_uid}
        record = selected_record(params, {'uid': query.get('uid', '')})
        uid = record.record_uid
        if path == '/update':
            document = checked_document(body, creating=False)
            if 'title' in document:
                record.title = document['title'].strip()
            if 'type' in document:
                record.type_name = document['type'].strip()
            if 'notes' in document:
                record.notes = document['notes']
            for key in ('fields', 'custom'):
                if key in document:
                    links = [field for field in getattr(record, key) if field.type in LINK_TYPES]
                    setattr(record, key, [kc.vault.TypedField(entry) for entry in document[key]] + links)
            with self.writing(uid):
                kc.record_management.update_record(params, record)
                self.confirm(lambda: uid in self.uids())
            return {'status': 'updated', 'uid': uid}
        if path == '/attach':
            name = attachment_name(query.get('name', ''))
            previous = [file_uid for file_uid, file_name in record_files(params, record) if file_name == name]
            if len(previous) > 1:
                raise Refused
            task = kc.attachment.BytesUploadTask(body)
            task.name = task.title = name
            task.mime_type = 'application/octet-stream'
            with self.writing(uid):
                kc.attachment.upload_attachments(params, record, [task])
                for reference in file_references(record):
                    reference.value = [file_uid for file_uid in reference.value if file_uid not in previous]
                kc.record_management.update_record(params, record)

                def attached():
                    current = record_files(params, selected_record(params, {'uid': uid}))
                    return [n for _, n in current].count(name) == 1 and not set(previous) & {f for f, _ in current}

                # Replacement: the new file is linked and the old link dropped in one record update.
                self.confirm(attached)
            return {'status': 'attached', 'uid': uid}
        if path == '/trash':
            with self.writing(uid):
                # Commander's own `rm`: unlinks the record from every folder holding it, so it leaves the vault.
                kc.remove_record.execute(params, records=[uid], force=True)
                self.confirm(lambda: uid not in self.uids())
            return {'status': 'trashed', 'uid': uid}
        raise Refused


class Server(HTTPServer):
    def handle_error(self, request, client_address):
        # No traceback: its text could carry record data.
        print('keeper: request handling failed', file=sys.stderr, flush=True)


class Handler(BaseHTTPRequestHandler):
    timeout = 10  # an idle local connection must not block the single-threaded sidecar

    def log_message(self, *_):
        pass

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        url = urlsplit(self.path)
        query = {key: values[0] for key, values in parse_qs(url.query).items() if len(values) == 1}
        status, result = 200, None
        try:
            length = int(self.headers.get('Content-Length') or 0)
            if not 0 <= length <= MAX_BODY_BYTES:
                raise Refused
            if self.server.session is None or self.server.session.dead:
                raise NotLoggedIn
            result = self.server.session.handle(self.command, url.path, query, self.rfile.read(length))
        except Uncertain as error:
            status, result = 502, {'error': 'uncertain', 'uid': error.uid}
        except NotLoggedIn:
            status, result = 503, {'error': 'NotLoggedIn'}
        except Exception as error:
            status, result = 400, {'error': type(error).__name__}
        data = result if isinstance(result, bytes) else json.dumps(result).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/octet-stream' if isinstance(result, bytes) else 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@contextlib.contextmanager
def login_lock(blocking):
    with os.fdopen(os.open(LOGIN_LOCK, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600), 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True


def login():
    """Persistent-login only. Re-reads config.json each time: Commander rotates its clone code on every login."""
    with login_lock(blocking=False) as locked:
        if not locked or not CONFIG.is_file():
            return None
        try:
            params = kc.get_params_from_config(str(CONFIG))
            params.batch_mode = True  # no spinner or prompts
            params.rest_context.fail_on_throttle = True  # fail fast instead of minute-long back-off sleeps
            if not params.user or not params.clone_code:
                return None
            kc.api.login(params, login_ui=NoPrompt())
            if not params.session_token:
                return None
            kc.api.sync_down(params)
            return params
        except (Exception, KeyboardInterrupt) as error:
            # Commander raises KeyboardInterrupt when an SSO step is cancelled; keep waiting instead of crash-looping.
            print('keeper: persistent login failed (' + type(error).__name__ + ')', file=sys.stderr, flush=True)
            return None


def serve():
    global kc
    kc = commander()
    logging.getLogger().setLevel(logging.WARNING)
    # A signal sets a flag that the loops below check between steps, so the current request or login can finish;
    # raising inside them would be swallowed by request handling. A blocked Commander call is cut off at the deadline.
    stopping = threading.Event()

    def stop(*_):
        if not stopping.is_set():
            stopping.set()
            deadline = threading.Timer(STOP_DEADLINE_SECONDS, lambda: (
                print('keeper: stop deadline reached', file=sys.stderr, flush=True), os._exit(143)))
            deadline.daemon = True
            deadline.start()

    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, stop)
    # Bind once, before login: nothing else in the pod can take the port, and requests are refused at once while
    # the sidecar waits for a login instead of hanging.
    with Server(ADDRESS, Handler) as server:
        server.session = None
        server.timeout = 1
        announced, next_login = False, 0.0
        while not stopping.is_set():
            if server.session is None or server.session.dead:
                if server.session is not None:
                    print('keeper: session lost; retrying persistent login', file=sys.stderr, flush=True)
                    server.session = None
                if time.monotonic() >= next_login:
                    params = login()
                    if params is None:
                        if not announced:
                            print('keeper: not logged in; run `keeper login` in the keeper container', flush=True)
                            announced = True
                        next_login = time.monotonic() + LOGIN_RETRY_SECONDS
                    else:
                        server.session = Session(params)
                        server.session.synced = time.monotonic()
                        announced = False
                        print('keeper: ready', flush=True)
            if stopping.is_set():
                break
            server.handle_request()
            session = server.session
            if session and not session.dead and not stopping.is_set() and (
                    time.monotonic() - session.synced >= KEEPALIVE_SECONDS):
                with contextlib.suppress(Exception):
                    session.sync(force=True)
    print('keeper: stopped', flush=True)
    return 0


def bootstrap():
    print('keeper: Commander shell. Run: login <email>, then this-device register, this-device persistent-login on,\n'
          '  this-device 2fa_expiration forever, this-device timeout 30d. Leave with quit, not logout.', flush=True)
    with login_lock(blocking=True):
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C belongs to the Commander shell, not this wrapper.
        return subprocess.call([COMMANDER, '--config', str(CONFIG), '--server', os.environ['KEEPER_SERVER'], 'shell'],
                               preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))


# ---- t3 client ----

def fetch(path, method='GET', body=None, binary=False):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request('http://%s:%d%s' % (*ADDRESS, path), method=method, data=body,
                                     headers={'Content-Type': 'application/octet-stream'} if body is not None else {})
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            data = response.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as error:
        if error.code == 503:
            raise NotLoggedIn from None
        if error.code == 502:
            with contextlib.suppress(ValueError, AttributeError):
                raise Uncertain(json.loads(error.read(4096)).get('uid')) from None
            raise Uncertain(None) from None
        raise Refused from None
    if len(data) > MAX_BODY_BYTES:
        raise Refused
    return data if binary else json.loads(data)


def environment_name(value):
    if not re.fullmatch(r'KEEPER_[A-Z][A-Z0-9_]{0,63}', value):
        raise Refused
    return value


def single_value(fields):
    if len(fields) != 1 or len(fields[0].get('value') or []) != 1:
        raise Refused
    value = fields[0]['value'][0]
    if not isinstance(value, str) or '\0' in value or len(value.encode()) > MAX_VALUE_BYTES:
        raise Refused
    return value


def list_records(argv):
    parser = Parser(description='List metadata for the Keeper records this pod can reach.')
    parser.add_argument('filter', nargs='?', default='')
    args = parser.parse_args(argv)
    records = [record for record in fetch('/list') if args.filter.casefold() in record['title'].casefold()]
    print(json.dumps(sorted(records, key=lambda record: (record['title'].casefold(), record['title']))))
    return 0


def run(argv):
    parser = Parser(description='Run a quiet command with values from one Keeper record.')
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument('--uid')
    selection.add_argument('--title')
    parser.add_argument('--password-env')
    parser.add_argument('--login-env')
    parser.add_argument('--field', action='append', default=[])
    parser.add_argument('--attachment')
    parser.add_argument('--attachment-env')
    parser.add_argument('--timeout', type=float, default=CHILD_TIMEOUT_MAX_SECONDS)
    if '--' not in argv:
        raise Refused
    boundary = argv.index('--')
    args = parser.parse_args(argv[:boundary])
    command = argv[boundary + 1:]
    mappings = [(source, environment_name(target)) for source, target in
                ((('type', 'password'), args.password_env), (('type', 'login'), args.login_env)) if target]
    for field in args.field:
        name, separator, target = field.rpartition('=')
        if not separator or not name:
            raise Refused
        mappings.append((('name', name), environment_name(target)))
    targets = [target for _, target in mappings]
    if args.attachment_env is not None:
        targets.append(environment_name(args.attachment_env))
    if not command or not targets or len(targets) != len(set(targets)) or (
            (args.attachment is None) != (args.attachment_env is None)) or (
            not math.isfinite(args.timeout) or not CHILD_TIMEOUT_MIN_SECONDS <= args.timeout <= CHILD_TIMEOUT_MAX_SECONDS):
        raise Refused
    selected = {'uid': args.uid} if args.uid else {'title': args.title}
    record = fetch('/record?' + urlencode(selected))
    additions = {}
    for (kind, wanted), target in mappings:
        if kind == 'type':
            matches = [field for field in record['fields'] if field.get('type') == wanted]
        else:
            matches = [field for field in record['fields'] + record['custom']
                       if (field.get('label') or field.get('type')) == wanted]
        additions[target] = single_value(matches)
    with tempfile.TemporaryDirectory(prefix='keeper-client-', dir='/tmp') as directory:
        os.chmod(directory, 0o700)
        if args.attachment is not None:
            data = fetch('/attachment?' + urlencode({'uid': record['uid'], 'name': args.attachment}), binary=True)
            path = Path(directory) / 'attachment'
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as output:
                output.write(data)
            additions[args.attachment_env] = str(path)
        environment = {key: value for key, value in os.environ.items() if not key.startswith(('KEEPER_', 'KSM_'))}
        environment.update(additions)
        with subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, start_new_session=True) as child:
            try:
                code = child.wait(timeout=args.timeout)
            finally:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
    code = code if code >= 0 else 128 - code
    print('keeper: command finished; exit code ' + str(code))
    return code


def read_file(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise Refused
        data = source.read(MAX_BODY_BYTES + 1)
    if len(data) > MAX_BODY_BYTES:
        raise Refused
    return data


def write(command, argv):
    parser = Parser(description='Write Keeper records from private files or KEEPER_ variables.')
    if command != 'create':
        parser.add_argument('--uid', required=True)
    if command in ('create', 'update'):
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument('--input-env')
        source.add_argument('--input-file')
    if command == 'attach':
        parser.add_argument('--file', required=True)
    args = parser.parse_args(argv)
    query = {} if command == 'create' else {'uid': args.uid}
    body = b''
    if command in ('create', 'update'):
        body = os.environ[environment_name(args.input_env)].encode() if args.input_env else read_file(args.input_file)
        json.loads(body)
    if command == 'attach':
        query['name'] = attachment_name(Path(args.file).name)
        body = read_file(args.file)
    print(json.dumps(fetch('/' + command + ('?' + urlencode(query) if query else ''), method='POST', body=body)))
    return 0


def main(argv):
    def cancel(*_):
        raise Cancelled

    signal.signal(signal.SIGTERM, cancel)
    signal.signal(signal.SIGINT, cancel)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.umask(0o077)
    try:
        if argv == ['serve']:
            return serve()
        if argv == ['login']:
            return bootstrap()
        if argv and argv[0] == 'list':
            return list_records(argv[1:])
        if argv and argv[0] == 'run':
            return run(argv[1:])
        if argv and argv[0] in ('create', 'update', 'attach', 'trash'):
            return write(argv[0], argv[1:])
        raise Refused
    except NotLoggedIn:
        print('keeper: the keeper sidecar is not logged in; ask the owner to run `keeper login` in the keeper container',
              file=sys.stderr)
        return 1
    except Uncertain as error:
        print('keeper: write sent, not confirmed; record ' + str(error.uid) + ' may have changed; inspect before retrying',
              file=sys.stderr)
        return 2
    except subprocess.TimeoutExpired:
        print('keeper: timed out; exit code 124')
        return 124
    except Cancelled:
        print('keeper: cancelled; exit code 130')
        return 130
    except Exception as error:
        # Type and own-file call chain only: exception text could carry record data.
        chain = ' > '.join(f'{frame.name}:{frame.lineno}' for frame in traceback.extract_tb(error.__traceback__)
                           if frame.filename == __file__)
        print(f'keeper: request refused ({type(error).__name__} at {chain})', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
