import base64
import hashlib
import json
from pathlib import Path
import re
import secrets
import sys
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'files'))
from hub_services import validate_services
from vaultwarden_reader import ReaderConfig


def read_values(count):
    raw = sys.stdin.buffer.read(131073)
    values = raw.split(b'\0')
    if len(raw) > 131072 or len(values) != count + 1 or values[-1] != b'':
        raise ValueError
    return [value.decode('utf-8') for value in values[:-1]]


def validate_ids(values):
    if len(values) != 7 or any(str(UUID(value)) != value for value in values) or len(set(values)) != 7:
        raise ValueError
    return values


def validate_login(values):
    client, _, _ = values
    if not client.startswith('user.') or str(UUID(client[5:])) != client[5:]:
        raise ValueError
    if any(not value or '\0' in value or len(value.encode()) > 16384 for value in values):
        raise ValueError
    return client[5:]


def bearer(value):
    if not 32 <= len(value) <= 4096 or value.startswith(('homelab.', 'work.')) or any(
            ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise ValueError
    return value


def encode(value):
    return json.dumps(value, separators=(',', ':')).encode()


def secret(name, namespace, values):
    return {'apiVersion': 'v1', 'kind': 'Secret', 'type': 'Opaque',
            'metadata': {'name': name, 'namespace': namespace},
            'data': {key: base64.b64encode(value).decode() for key, value in values.items()}}


def build(values):
    organization, collection, denied_collection, item, first, second, denied_item = validate_ids(values[:7])
    client, client_secret, password = values[7:]
    user = validate_login(values[7:])
    if user in values[:7]:
        raise ValueError
    private = '/run/vault-reader/private'
    reader = {'server_url': 'https://vaultwarden.epaflix.com', 'user_id': user,
              'organization_id': organization, 'collection_ids': [collection],
              'credentials_dir': private + '/credentials', 'scratch_dir': private + '/scratch',
              'executable': '/tools/reader_bw_bridge.cjs'}
    ReaderConfig(reader['server_url'], user, organization, (collection,),
                 Path(reader['credentials_dir']), Path(reader['scratch_dir']), Path(reader['executable']))
    config = {'environment': 'homelab', 'reader': reader, 'metadata_token_file': private + '/metadata-token',
              'broker_token_file': private + '/broker-token'}
    metadata, broker = bearer(secrets.token_urlsafe(48)), bearer(secrets.token_urlsafe(48))
    token = 'homelab.' + secrets.token_urlsafe(32)
    if metadata == broker or not re.fullmatch(r'homelab\.[A-Za-z0-9_-]{16,4088}', token):
        raise ValueError
    services = validate_services({'environment': 'homelab',
                                  'services': ['vaultwarden', 'vault-secret', 'vault-attachment'],
                                  'vault_reader': 'restricted-v1'}, 'homelab')
    service_bytes = encode(services)
    origin = 'http://vault-reader.vault-reader-homelab.svc.cluster.local:8000'
    credentials = {'clients': {'canary': hashlib.sha256(token.encode()).hexdigest()}, 'upstreams': {
        name: {'url': origin + route, 'token': metadata if name == 'vaultwarden' else broker,
               'protocol': 'restricted-v1'}
        for name, route in [('vaultwarden', '/vaultwarden'), ('vault-secret', '/secret'),
                            ('vault-attachment', '/attachment')]}}
    canary = {'collection_id': collection, 'item_id': item, 'duplicate_item_ids': [first, second],
              'denied_collection_id': denied_collection, 'denied_item_id': denied_item}
    return {'apiVersion': 'v1', 'kind': 'List', 'items': [
        secret('vault-reader-inputs', 'vault-reader-homelab', {
            'config.json': encode(config), 'client-id': client.encode(), 'client-secret': client_secret.encode(),
            'password': password.encode(), 'metadata-token': metadata.encode(), 'broker-token': broker.encode()}),
        secret('mcp-hub-instance', 'mcp-hub-homelab', {
            'services.json': service_bytes, 'credentials.json': encode(credentials)}),
        secret('vault-canary-client', 'mcp-hub-homelab', {
            'token': token.encode(), 'services.json': service_bytes, 'canary.json': encode(canary)})]}


def main():
    try:
        mode = sys.argv[1:]
        if mode == ['--validate-ids']:
            validate_ids(read_values(7))
        elif mode == ['--validate-login']:
            validate_login(read_values(3))
        elif mode == ['--prelogin-body']:
            email = read_values(1)[0]
            if not email or len(email.encode()) > 16384:
                raise ValueError
            sys.stdout.buffer.write(encode({'email': email}))
        elif mode == ['--kdf']:
            value = json.loads(sys.stdin.buffer.read(65537))
            kind, iterations = value['kdf'], value['kdfIterations']
            if type(kind) is not int or kind not in (0, 1) or type(iterations) is not int or iterations <= 0:
                raise ValueError
            numbers = [kind, iterations]
            if kind == 1:
                memory, parallelism = value['kdfMemory'], value['kdfParallelism']
                if any(type(number) is not int or number <= 0 for number in (memory, parallelism)):
                    raise ValueError
                numbers.extend((memory, parallelism))
            print(*numbers)
        elif not mode:
            sys.stdout.buffer.write(encode(build(read_values(10))))
        else:
            raise ValueError
        return 0
    except Exception:
        print('provisioning inputs refused', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
