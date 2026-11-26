#!/usr/bin/env python3
"""Offline checks for env/files/vpn.sh with stub openvpn, wg-quick and ip."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'files/vpn.sh'
STUBS = {
    'openvpn': '''#!/bin/bash
echo "$*" >"$VPN_ROOT/openvpn.args"
echo $$ >"$VPN_ROOT/openvpn.pid"
config=$2
while [[ $1 != --dev ]]; do shift; done
dev=$2
grep -q FAIL "$config" && exit 1
grep -q HANG "$config" || mkdir "$VPN_ROOT/sys/class/net/$dev"
trap 'rmdir "$VPN_ROOT/sys/class/net/$dev" 2>/dev/null; exit 0' TERM
while :; do sleep 0.1; done
''',
    'wg-quick': '''#!/bin/bash
name=${2##*/}
dev=${name%.conf}
if [[ $1 == down ]]; then
  echo "$dev" >>"$VPN_ROOT/wg.down"
  exec rmdir "$VPN_ROOT/sys/class/net/$dev"
fi
cp "$2" "$VPN_ROOT/wg.conf"
echo "$dev" >>"$VPN_ROOT/wg.up"
grep -q SLOW "$2" && sleep 3
mkdir "$VPN_ROOT/sys/class/net/$dev"
if grep -q FAIL "$2"; then sleep 1.5; rmdir "$VPN_ROOT/sys/class/net/$dev"; exit 1; fi
''',
    'ip': '''#!/bin/bash
[[ "$1 $2 $3" == 'link delete dev' ]] || exit 1
rmdir "$VPN_ROOT/sys/class/net/$4"
''',
}


class VpnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for path in ('run/t3-vpn-config', 'run/t3-vpn', 'sys/class/net', 'tmp', 'bin'):
            (self.root / path).mkdir(parents=True)
        for name, body in STUBS.items():
            stub = self.root / 'bin' / name
            stub.write_text(body)
            stub.chmod(0o755)
        self.env = {**os.environ, 'VPN_ROOT': str(self.root), 'PATH': f'{self.root}/bin:{os.environ["PATH"]}'}
        self.serve = None

    def tearDown(self):
        if self.serve:
            os.killpg(self.serve.pid, signal.SIGTERM)
            self.serve.wait(10)
        self.tmp.cleanup()

    def wait_for(self, check, message):
        for _ in range(100):
            if check():
                return
            time.sleep(0.1)
        self.fail(message)

    def start(self, configs):
        for name, body in configs.items():
            (self.root / 'run/t3-vpn-config' / name).write_text(body)
        self.serve = subprocess.Popen(['bash', str(SCRIPT), 'serve'], env=self.env, start_new_session=True,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait_for((self.root / 'run/t3-vpn/status').exists, 'serve did not publish a status')

    def vpn(self, *args):
        return subprocess.run(['bash', str(SCRIPT), *args], env=self.env, capture_output=True, text=True, timeout=90)

    def test_up_status_down_for_openvpn_and_wireguard(self):
        self.start({'alpha.ovpn': 'client\n', 'beta.conf': '[Interface]\nDNS = 10.0.0.1\nAddress = x\n',
                    'Bad_Name.conf': ''})
        self.assertEqual(self.vpn('status').stdout, 'alpha: down\nbeta: down\n')
        self.assertEqual(self.vpn('up', 'alpha').stdout, 'alpha: up\n')
        args = (self.root / 'openvpn.args').read_text()
        self.assertIn('--dev vpn-alpha --dev-type tun --pull-filter ignore redirect-gateway', args)
        self.assertEqual(self.vpn('up', 'beta').stdout, 'beta: up\n')
        self.assertEqual((self.root / 'wg.conf').read_text(), '[Interface]\nAddress = x\n')
        self.assertEqual(self.vpn('status').stdout, 'alpha: up\nbeta: up\n')
        self.assertEqual(self.vpn('down', 'alpha').stdout, 'alpha: down\n')
        self.assertEqual(self.vpn('down', 'beta').stdout, 'beta: down\n')
        self.assertEqual((self.root / 'wg.down').read_text(), 'vpn-beta\n')
        self.assertFalse((self.root / 'tmp/vpn-beta.conf').exists())
        self.assertEqual(self.vpn('status').stdout, 'alpha: down\nbeta: down\n')
        for site in ('nope', 'Bad_Name', '../beta'):
            self.assertEqual(self.vpn('up', site).returncode, 2)

    def test_a_tunnel_that_fails_is_reported_and_dropped(self):
        self.start({'alpha.ovpn': 'FAIL\n', 'beta.conf': 'FAIL\n'})
        for site in ('alpha', 'beta'):
            result = self.vpn('up', site)
            self.assertEqual((result.returncode, result.stdout), (1, ''))
            self.assertIn(f'{site}: failed', result.stderr)
        self.assertEqual(self.vpn('status').stdout, 'alpha: down\nbeta: down\n')

    def test_down_stops_a_tunnel_that_is_still_connecting(self):
        self.start({'alpha.ovpn': 'HANG\n'})
        (self.root / 'run/t3-vpn/alpha.on').touch()
        self.wait_for(lambda: self.vpn('status').stdout == 'alpha: connecting\n', 'never connecting')
        pid = int((self.root / 'openvpn.pid').read_text())
        self.assertEqual(self.vpn('down', 'alpha').stdout, 'alpha: down\n')
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_down_during_a_slow_wireguard_start_waits_for_teardown(self):
        self.start({'beta.conf': 'SLOW\n'})
        (self.root / 'run/t3-vpn/beta.on').touch()
        self.wait_for((self.root / 'wg.conf').exists, 'wg-quick never started')
        self.assertEqual(self.vpn('down', 'beta').stdout, 'beta: down\n')
        self.assertEqual(list((self.root / 'sys/class/net').iterdir()), [])
        self.assertEqual((self.root / 'wg.down').read_text(), 'vpn-beta\n')

    def test_a_request_cancelled_while_another_site_starts_never_comes_up(self):
        self.start({'alpha.conf': 'SLOW\n', 'beta.conf': 'SLOW\n'})
        (self.root / 'run/t3-vpn/alpha.on').touch()
        self.wait_for((self.root / 'wg.up').exists, 'alpha never started')
        (self.root / 'run/t3-vpn/beta.on').touch()
        self.wait_for((self.root / 'sys/class/net/vpn-alpha').exists, 'alpha never came up')
        time.sleep(0.3)
        self.assertEqual(self.vpn('down', 'beta').stdout, 'beta: down\n')
        self.wait_for(lambda: self.vpn('status').stdout == 'alpha: up\nbeta: down\n', 'alpha never published up')
        time.sleep(4)
        self.assertFalse((self.root / 'sys/class/net/vpn-beta').exists())
        self.assertEqual((self.root / 'wg.up').read_text(), 'vpn-alpha\n')

    def test_removing_an_active_profile_takes_its_tunnel_down(self):
        self.start({'alpha.ovpn': 'client\n', 'beta.conf': '[Interface]\n'})
        self.vpn('up', 'alpha')
        self.vpn('up', 'beta')
        for name in ('alpha.ovpn', 'beta.conf'):
            (self.root / 'run/t3-vpn-config' / name).unlink()
        self.wait_for(lambda: self.vpn('status').returncode == 1, 'removed sites still listed')
        self.assertEqual(list((self.root / 'sys/class/net').iterdir()), [])
        self.assertEqual((self.root / 'wg.down').read_text(), 'vpn-beta\n')

    def test_without_the_secret_there_are_no_sites(self):
        result = self.vpn('status')
        self.assertEqual(result.returncode, 1)
        self.assertIn('t3code-vpn Secret', result.stderr)


if __name__ == '__main__':
    unittest.main()
