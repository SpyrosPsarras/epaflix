#!/usr/bin/env python3
"""Run this kustomization's kube-vip image and env against a throwaway k3s in Docker.

The control-plane VIP must come back after the API stalls long enough to lose the
lease, and both DaemonSets must exit on SIGTERM. kube-vip v1.3.1 with
health_check_port never returns from Manager.Start after shutdown, so a pod that
lost the lease stayed Running without campaigning and the VIP went dark.
"""
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
K3S = 'rancher/k3s:v1.36.5-k3s1@sha256:057cfd5a9d51b89f1f0ee139c28d5d5fdd5aa7ddeac2742a0ba90c6dbc0329bb'
BUSYBOX = 'busybox:1.37@sha256:a7dbc319b1438115a207e11930dc4bb3e643dcb3ec0bdd694bb36cd8fad289ec'
PREFIX = 'kube-vip-test'
NETWORK, SUBNET, API_IP, VIP = PREFIX, '172.31.99.0/24', '172.31.99.10', '172.31.99.250'
OVERRIDES = {
    'address': VIP,
    'vip_nodename': 'test-node',
    'k8s_config_file': '/kc',
    'vip_leaseduration': '6',
    'vip_renewdeadline': '4',
    'vip_retryperiod': '1',
}


def docker(*args, check=True):
    return subprocess.run(['docker', *args], capture_output=True, text=True, check=check)


def wait_for(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(1)
    return False


def daemonsets():
    built = subprocess.run(['kustomize', 'build', str(ROOT)], capture_output=True, text=True, check=True).stdout
    return {doc['metadata']['name']: doc['spec']['template']['spec']['containers'][0]
            for doc in yaml.safe_load_all(built) if doc and doc['kind'] == 'DaemonSet'}


class KubeVipFailoverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.containers = daemonsets()
        cls.tmp = tempfile.TemporaryDirectory()
        docker('rm', '-f', f'{PREFIX}-k3s', check=False)
        docker('network', 'rm', NETWORK, check=False)
        docker('network', 'create', '--subnet', SUBNET, NETWORK)
        docker('run', '-d', '--name', f'{PREFIX}-k3s', '--privileged', '--network', NETWORK, '--ip', API_IP,
               K3S, 'server', '--disable', 'traefik,servicelb,metrics-server')
        if not wait_for(lambda: docker('exec', f'{PREFIX}-k3s', 'kubectl', 'get', '--raw', '/readyz',
                                       check=False).returncode == 0, 180):
            raise RuntimeError('k3s never became ready')
        kubeconfig = docker('exec', f'{PREFIX}-k3s', 'cat', '/etc/rancher/k3s/k3s.yaml').stdout
        cls.kubeconfig = Path(cls.tmp.name) / 'kc'
        cls.kubeconfig.write_text(kubeconfig.replace('https://127.0.0.1:6443', f'https://{API_IP}:6443'))
        cls.kubeconfig.chmod(0o644)

    @classmethod
    def tearDownClass(cls):
        docker('rm', '-f', f'{PREFIX}-k3s', check=False)
        docker('network', 'rm', NETWORK, check=False)
        cls.tmp.cleanup()

    def start(self, daemonset, restart):
        container = self.containers[daemonset]
        env = {}
        for var in container.get('env', []):
            self.assertIn('value', var, f'{daemonset} env {var["name"]} uses valueFrom, which this test cannot reproduce')
            env[var['name']] = var['value']
        env.update(OVERRIDES)
        name = f'{PREFIX}-{daemonset}'
        docker('rm', '-f', name, check=False)
        self.addCleanup(docker, 'rm', '-f', name, check=False)
        docker('exec', f'{PREFIX}-k3s', 'kubectl', '-n', 'kube-system', 'delete', 'lease',
               'plndr-cp-lock', 'plndr-svcs-lock', '--ignore-not-found')
        caps = container.get('securityContext', {}).get('capabilities', {}).get('add', [])
        docker('run', '-d', '--name', name, '--network', NETWORK, '--restart', 'always' if restart else 'no',
               '-v', f'{self.kubeconfig}:/kc:ro',
               *[arg for cap in caps for arg in ('--cap-add', cap)],
               *[arg for key, value in env.items() for arg in ('-e', f'{key}={value}')],
               container['image'], *container.get('args', []))
        return name

    def logs(self, name):
        result = docker('logs', name, check=False)
        return result.stdout + result.stderr

    def has_vip(self, name):
        result = docker('run', '--rm', '--network', f'container:{name}', BUSYBOX, 'ip', '-4', 'addr', 'show', 'eth0',
                        check=False)
        return f'{VIP}/' in result.stdout

    def test_control_plane_vip_returns_after_lost_lease(self):
        name = self.start('kube-vip', restart=True)
        self.assertTrue(wait_for(lambda: self.has_vip(name), 60), self.logs(name)[-3000:])
        docker('pause', f'{PREFIX}-k3s')
        try:
            time.sleep(12)
        finally:
            docker('unpause', f'{PREFIX}-k3s')
        self.assertIn('lost leadership', self.logs(name), 'the API freeze did not cost kube-vip its lease')
        restarts = lambda: docker('inspect', '-f', '{{.RestartCount}}', name).stdout.strip()
        self.assertTrue(wait_for(lambda: self.has_vip(name), 45),
                        f'no VIP 45 s after the API recovered (restarts={restarts()})\n{self.logs(name)[-3000:]}')

    def test_daemonsets_exit_on_sigterm(self):
        for daemonset in self.containers:
            with self.subTest(daemonset=daemonset):
                name = self.start(daemonset, restart=False)
                self.assertTrue(wait_for(lambda: 'starting Kube-vip Manager' in self.logs(name), 60),
                                self.logs(name)[-3000:])
                time.sleep(3)
                docker('stop', '-t', '15', name)
                code = docker('inspect', '-f', '{{.State.ExitCode}}', name).stdout.strip()
                self.assertEqual(code, '0', f'{daemonset} ignored SIGTERM until SIGKILL\n{self.logs(name)[-3000:]}')


if __name__ == '__main__':
    unittest.main()
