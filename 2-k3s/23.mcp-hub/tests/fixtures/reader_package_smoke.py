import json
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, '/app')
from vaultwarden_reader import BRIDGE_CAPABILITY, private_bytes

SOURCE = Path('/run/vault-reader-inputs')
STATE = Path('/run/vault-reader/private')
META = 'synthetic-package-metadata-grant-00000000'
BROKER = 'synthetic-package-broker-grant-0000000000'
PASSWORD = b' \t synthetic password \r\n'


def provision(target):
    generation = target / '..generation'
    generation.mkdir(mode=0o750)
    os.chown(generation, 0, 1000)
    (target / '..data').symlink_to('..generation')
    config = {'environment': 'homelab', 'reader': {
        'server_url': 'https://vault.homelab.example.test',
        'user_id': '00000000-0000-4000-8000-000000000001',
        'organization_id': '00000000-0000-4000-8000-000000000002',
        'collection_ids': ['00000000-0000-4000-8000-000000000003'],
        'credentials_dir': str(STATE / 'credentials'), 'scratch_dir': str(STATE / 'scratch'),
        'executable': '/tools/reader_bw_bridge.cjs'},
        'metadata_token_file': str(STATE / 'metadata-token'), 'broker_token_file': str(STATE / 'broker-token')}
    values = {'config.json': json.dumps(config).encode(),
              'client-id': b'user.00000000-0000-4000-8000-000000000001',
              'client-secret': b'synthetic-package-client-secret', 'password': PASSWORD,
              'metadata-token': META.encode(), 'broker-token': BROKER.encode()}
    for name, value in values.items():
        path = generation / name
        path.write_bytes(value)
        os.chown(path, 0, 1000)
        path.chmod(0o440)
        (target / name).symlink_to('..data/' + name)


def start():
    process = subprocess.Popen([sys.executable, '/app/reader_startup.py'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError('production startup exited: ' + repr(process.communicate()))
        try:
            with urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=0.2) as response:
                if response.read() == b'ok':
                    return process
        except OSError:
            time.sleep(0.05)
    process.kill()
    process.wait()
    raise AssertionError('production health timeout')


def smoke():
    assert os.getuid() == 1000
    outer = Path('/run/vault-reader').stat()
    assert outer.st_uid == 0 and outer.st_gid == 1000 and outer.st_mode & 0o777 == 0o770
    info = (SOURCE / 'password').stat()
    assert info.st_uid == 0 and info.st_gid == 1000 and info.st_mode & 0o777 == 0o440
    try:
        private_bytes(SOURCE / 'password', 16384)
    except ValueError:
        pass
    else:
        raise AssertionError('root-owned projection must not satisfy private core checks')
    assert subprocess.check_output(['node', '--version'], text=True).startswith('v22.')
    assert sys.version_info[:2] == (3, 12)
    capabilities = json.loads(subprocess.check_output(['/tools/reader_bw_bridge.cjs', '--reader-capabilities']))
    assert capabilities == BRIDGE_CAPABILITY
    assert capabilities['bridge'] == 'restricted-sync-v2'
    with tempfile.TemporaryDirectory(dir='/run/vault-reader') as home:
        env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': home, 'BITWARDENCLI_APPDATA_DIR': home,
               'TMPDIR': home, 'BW_NOINTERACTION': 'true'}
        subprocess.run(['/tools/reader_bw_bridge.cjs', 'config', 'server', 'https://vault.homelab.example.test'],
                       env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        status = json.loads(subprocess.check_output(['/tools/reader_bw_bridge.cjs', 'status'], env=env))
        assert status['status'] == 'unauthenticated'
    print('actual Node 22 production bridge capability/config/unauthenticated status PASS', flush=True)
    process = start()
    try:
        assert resource.prlimit(process.pid, resource.RLIMIT_CORE) == (0, 0)
        request = urllib.request.Request('http://127.0.0.1:8000/vaultwarden',
            data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}}).encode(),
            headers={'Authorization': 'Bearer ' + META, 'Content-Type': 'application/json',
                     'Accept': 'application/json, text/event-stream'})
        with urllib.request.urlopen(request, timeout=3) as response:
            tools = json.loads(response.read())['result']['tools']
        assert {tool['name'] for tool in tools} == {'vault_list', 'vault_get'}
        assert list((STATE / 'scratch').iterdir()) == []
        assert private_bytes(STATE / 'credentials/password', 16384) == PASSWORD
        print('root-owned projected bytes staged privately; health/schema perform no login PASS', flush=True)
        (STATE / 'scratch/orphan').mkdir()
        (STATE / 'scratch/orphan/data').write_bytes(b'synthetic-old-state')
        process.kill()
        out, err = process.communicate(timeout=5)
        assert not out and not err, (out, err)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert (STATE / 'scratch/orphan').exists()
    process = start()
    try:
        assert not (STATE / 'scratch/orphan').exists()
        assert private_bytes(STATE / 'credentials/password', 16384) == PASSWORD
        process.send_signal(signal.SIGTERM)
        out, err = process.communicate(timeout=10)
        assert process.returncode in (0, -signal.SIGTERM) and not out and not err, (process.returncode, out, err)
        assert not STATE.exists()
        print('actual startup after SIGKILL cleans dedicated orphan; graceful shutdown clears state PASS', flush=True)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


if __name__ == '__main__':
    if sys.argv[1:2] == ['provision']:
        provision(Path(sys.argv[2]))
    else:
        smoke()
