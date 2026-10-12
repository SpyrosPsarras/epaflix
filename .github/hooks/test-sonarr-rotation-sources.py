#!/usr/bin/env python3
"""Offline guards against replaying stale bootstrap Sonarr credentials."""
import os
from pathlib import Path
import subprocess
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[2]
TIER = ROOT / "2-k3s/08.servarr"
LOOKUP = "existing_secret=$(kubectl get secret servarr-postgres -n servarr --ignore-not-found -o name)"
STUB = '''#!/usr/bin/python3
import os
from pathlib import Path
import sys
args = sys.argv[1:]
name = Path(sys.argv[0]).name
if args[:1] == ['--context']:
    args = args[2:]
if name == 'kubectl' and args[:3] == ['get', 'secret', 'servarr-postgres']:
    with open(os.environ['TRACE'], 'a') as f:
        f.write('lookup\\n')
    case = os.environ['CASE']
    if case in ('rbac', 'network'):
        sys.exit(1)
    if case == 'exists':
        print('secret/servarr-postgres')
    sys.exit(0)
if name == 'openssl':
    print('syntheticbootstrapvalue')
    sys.exit(0)
with open(os.environ['TRACE'], 'a') as f:
    f.write('dbwrite\\n' if name == 'psql' or 'exec' in args else 'secretwrite\\n')
if name == 'kubectl' and args[:1] == ['get']:
    print('c3ludGhldGlj')
sys.exit(0)
'''


def check_bootstrap(script):
    for case in ('exists', 'absent', 'rbac', 'network'):
        with tempfile.TemporaryDirectory(prefix='sonarr-bootstrap-test-') as tmp:
            root = Path(tmp)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            (root / 'secrets').mkdir()
            for name in ('kubectl', 'psql', 'openssl'):
                stub = bin_dir / name
                stub.write_text(STUB)
                stub.chmod(0o700)
            for name in ('tr', 'cut', 'base64'):
                (bin_dir / name).symlink_to('/usr/bin/' + name)
            trace = root / 'trace'
            env = {'PATH': str(bin_dir), 'HOME': tmp, 'CASE': case,
                   'TRACE': str(trace), 'KUBECTL_CONTEXT': 'synthetic'}
            result = subprocess.run(['/bin/bash', '-s'], input=script, text=True,
                                    capture_output=True, cwd=root, env=env, timeout=10)
            calls = trace.read_text().splitlines() if trace.exists() else []
            assert calls[:1] == ['lookup'], 'Bootstrap must first check the canonical Secret'
            if case == 'absent':
                assert result.returncode == 0 and 'dbwrite' in calls and 'secretwrite' in calls, 'Missing Secret must permit bootstrap'
            else:
                assert result.returncode != 0 and calls == ['lookup'], 'Existing Secret or failed lookup must prevent every write'


def check_mutations(script):
    assert LOOKUP in script
    mutants = [script.replace(LOOKUP, LOOKUP + ' || true', 1),
               script.replace('set -e\n', '', 1).replace(LOOKUP, LOOKUP + '\nset -e', 1),
               script.replace(LOOKUP, 'set +e\n' + LOOKUP, 1)]
    for mutant in mutants:
        try:
            check_bootstrap(mutant)
        except AssertionError:
            continue
        raise AssertionError('Unsafe bootstrap mutation was accepted')


def main():
    setup = (TIER / '_shared/scripts/01-setup-postgres.sh').read_text()
    check_bootstrap(setup)
    check_mutations(setup)
    assert 'SONARR_PASSWORD=$(generate_password)' in setup
    assert 'kubectl apply -f secrets/postgres-secret-generated.yaml' not in setup
    docs = list(yaml.safe_load_all((TIER / 'postgres-setup-job.yaml').read_text()))
    job = next(d for d in docs if d['kind'] == 'Job')
    script = job['spec']['template']['spec']['containers'][0]['command'][-1]
    check_bootstrap(script)
    check_mutations(script)
    assert 'SONARR_PW=$(openssl rand ' in script
    result = subprocess.run(['bash', '-n'], input=script, text=True, capture_output=True)
    assert result.returncode == 0, 'Bootstrap Job shell syntax failed'
    role = next(d for d in docs if d['kind'] == 'Role')
    assert any(rule.get('resourceNames') == ['servarr-postgres'] and
               rule['resources'] == ['secrets'] and 'get' in rule['verbs']
               for rule in role['rules'])
    deploy = (TIER / '_shared/scripts/deploy.sh').read_text()
    assert 'set -o pipefail' in deploy
    assert 'sops decrypt "$SHARED_DIR/secrets/servarr-postgres.enc.yaml" | kubectl apply -f -' in deploy
    assert 'postgres-secret-generated.yaml' not in deploy
    assert not (TIER / '_shared/secrets/postgres-secret.yaml').exists()
    print('PASS Sonarr bootstrap behavior, both scripts, four states and six unsafe mutations')


if __name__ == '__main__':
    main()
