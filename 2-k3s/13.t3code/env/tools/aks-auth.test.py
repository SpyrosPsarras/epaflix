#!/usr/bin/env python3
"""Check forced-command rejection, token stdout and central login routing."""
import os
from pathlib import Path
import subprocess
import tempfile

scripts = Path(__file__).resolve().parents[1] / 'files'
with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp)
    bin_dir = home / '.local/bin'
    bin_dir.mkdir(parents=True)
    for name, text in {
        'kubelogin': '#!/bin/sh\nprintf \'{"kind":"ExecCredential"}\\n\'\n',
        'az': '#!/bin/sh\necho "device-code $*"\n',
        'ssh': '#!/bin/sh\nprintf "%s\\n" "$@"\n',
    }.items():
        p = bin_dir / name
        p.write_text(text)
        p.chmod(0o700)
    env = dict(os.environ, HOME=tmp, PATH=str(bin_dir) + ':' + os.environ['PATH'])
    def central(command):
        return subprocess.run(['bash', str(scripts / 'aks-auth-central.sh')],
                              env=dict(env, SSH_ORIGINAL_COMMAND=command), capture_output=True, text=True)
    r = central('token')
    assert r.returncode == 0 and r.stdout == '{"kind":"ExecCredential"}\n', r
    r = central('login')
    assert r.returncode == 0 and not r.stdout and '--use-device-code' in r.stderr, r
    assert '--tenant 9d4e1d19-7bb4-460c-b762-c2d9d38ea759' in r.stderr
    for command in ['id', 'token; touch /tmp/aks-auth-injected', 'login extra']:
        assert central(command).returncode == 2
    (bin_dir / 'kubelogin').write_text('#!/bin/sh\nexit 1\n')
    r = central('token')
    assert r.returncode == 1 and 'device-code' not in r.stderr and not r.stdout
    (bin_dir / 'kubelogin').write_text('''#!/bin/sh
if [ ! -f "$HOME/authenticated" ]; then
  echo "ERROR: Please run 'az login' to setup account." >&2
  echo 'partial failed output'
  exit 1
fi
printf '{"kind":"ExecCredential"}\\n'
''')
    (bin_dir / 'az').write_text('#!/bin/sh\necho "device-code $*"\ntouch "$HOME/authenticated"\n')
    r = central('token')
    assert r.returncode == 0 and r.stdout == '{"kind":"ExecCredential"}\n', r
    assert 'device-code' in r.stderr and 'central host' in r.stderr
    assert 'partial failed output' not in r.stdout
    r = central('token')
    assert r.returncode == 0 and 'device-code' not in r.stderr
    (home / 'authenticated').unlink()
    (bin_dir / 'az').write_text('#!/bin/sh\necho "login denied" >&2\nexit 1\n')
    r = central('token')
    assert r.returncode == 1 and not r.stdout and 'login denied' in r.stderr
    for action in ['token', 'login']:
        r = subprocess.run(['bash', str(scripts / 'aks-auth.sh'), action], env=env, capture_output=True, text=True)
        assert r.returncode == 0 and r.stdout.endswith('spyros@192.168.10.240\n' + action + '\n')
        assert 'StrictHostKeyChecking=yes' in r.stdout
    r = subprocess.run(['bash', str(scripts / 'aks-auth.sh'), 'id'], env=env, capture_output=True)
    assert r.returncode == 2
print('PASS: allowlist, clean token stdout, automatic central login and retry, failed login, no login on non-auth errors, SSH routing')
