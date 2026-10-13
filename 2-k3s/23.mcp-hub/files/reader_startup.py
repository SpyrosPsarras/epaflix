import contextlib
import fcntl
import logging
import os
from pathlib import Path
import resource
import shutil
import stat
import sys

import anyio
from starlette.responses import PlainTextResponse

from vaultwarden_reader import RestrictedVaultReader, private_bytes
from vaultwarden_reader_http import http_app, load_config

FILES = {'config.json': 65536, 'client-id': 16384, 'client-secret': 16384,
         'password': 16384, 'metadata-token': 4096, 'broker-token': 4096}


def owned_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('private directory required')


def remove_private(path):
    if path.exists() or path.is_symlink():
        owned_directory(path)
        shutil.rmtree(path)


def projected_values(source):
    values = {}
    generation = os.open(Path(source) / '..data', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name, limit in FILES.items():
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=generation)
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.getuid()} or info.st_mode & 0o037:
                    raise ValueError('invalid projected input')
                value = stream.read(limit + 1)
            if not value or len(value) > limit:
                raise ValueError('invalid projected input size')
            values[name] = value
    finally:
        os.close(generation)
    return values


class ProjectionGuard:
    def __init__(self, app, source, expected, reader):
        self.app, self.source, self.expected, self.reader = app, source, expected, reader
        self.changed = False

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http' and scope['path'] != '/healthz':
            if not self.changed:
                try:
                    self.changed = projected_values(self.source) != self.expected
                except (OSError, ValueError):
                    self.changed = True
                if self.changed:
                    await anyio.to_thread.run_sync(self.reader.close)
            if self.changed:
                await PlainTextResponse('restricted reader inputs changed; restart required', status_code=503,
                                        headers={'cache-control': 'no-store'})(scope, receive, send)
                return
        await self.app(scope, receive, send)


def stage(source, state):
    source, state = Path(source), Path(state)
    if not source.is_absolute() or not state.is_absolute() or state.is_symlink():
        raise ValueError('absolute real mounts required')
    descriptor = os.open(state / '.startup-lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    lock = os.fdopen(descriptor, 'rb+')
    private = state / 'private'
    acquired = False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('private lock required')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired = True
        remove_private(private)
        values = projected_values(source)
        private.mkdir(mode=0o700)
        (private / 'credentials').mkdir(mode=0o700)
        (private / 'scratch').mkdir(mode=0o700)
        for name, value in values.items():
            target = private / ('credentials/' + name if name in {'client-id', 'client-secret', 'password'} else name)
            with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as stream:
                stream.write(value)
        config_path = private / 'config.json'
        config, _, _ = load_config(config_path)
        if config.credentials_dir != private / 'credentials' or config.scratch_dir != private / 'scratch' or (
                config.executable != Path('/tools/reader_bw_bridge.cjs')):
            raise ValueError('dedicated package paths required')
        from vaultwarden_reader_http import strict_json
        value = strict_json(values['config.json'])
        if value['metadata_token_file'] != str(private / 'metadata-token') or (
                value['broker_token_file'] != str(private / 'broker-token')):
            raise ValueError('dedicated token paths required')
        return lock, config_path
    except BaseException:
        if acquired:
            remove_private(private)
        lock.close()
        raise


def main():
    import uvicorn

    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    for name in ('mcp.server.streamable_http_manager', 'mcp.server.streamable_http'):
        logging.getLogger(name).setLevel(logging.WARNING)
    lock = None
    try:
        lock, path = stage(Path('/run/vault-reader-inputs'), Path('/run/vault-reader'))
        config, metadata, broker = load_config(path)
        reader = RestrictedVaultReader(config)
        app = http_app(reader, metadata, broker)
        expected = {name: private_bytes(path.parent / ('credentials/' + name if name in {
            'client-id', 'client-secret', 'password'} else name), limit) for name, limit in FILES.items()}
        original_lifespan = app.router.lifespan_context

        @contextlib.asynccontextmanager
        async def lifespan(application):
            try:
                async with original_lifespan(application):
                    yield
            finally:
                remove_private(path.parent)

        app.router.lifespan_context = lifespan
        guarded = ProjectionGuard(app, Path('/run/vault-reader-inputs'), expected, reader)
        uvicorn.run(guarded, host='0.0.0.0', port=8000, log_level='warning', access_log=False)
        return 0
    except Exception:
        print('restricted reader startup refused', file=sys.stderr)
        return 1
    finally:
        if lock is not None:
            remove_private(Path('/run/vault-reader/private'))
            lock.close()


if __name__ == '__main__':
    sys.exit(main())
