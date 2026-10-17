import ipaddress
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[3]
RUNTIME = {'app.kubernetes.io/name': 't3code'}
ROLLOUT = {'app.kubernetes.io/name': 't3code-idle-rollout'}


def matches(selector, labels):
    if not all(labels.get(key) == value for key, value in selector.get('matchLabels', {}).items()):
        return False
    for expression in selector.get('matchExpressions', []):
        key, operator = expression['key'], expression['operator']
        if operator == 'NotIn':
            if labels.get(key) in expression['values']:
                return False
        else:
            raise ValueError(f'Unsupported selector operator: {operator}')
    return True


def peer_matches(peer, address, namespace, labels, local_namespace):
    if not peer:
        return True
    if 'ipBlock' in peer:
        block = peer['ipBlock']
        ip = ipaddress.ip_address(address)
        return ip in ipaddress.ip_network(block['cidr']) and not any(
            ip in ipaddress.ip_network(cidr) for cidr in block.get('except', []))
    if 'namespaceSelector' in peer:
        if not namespace or not matches(peer['namespaceSelector'], {'kubernetes.io/metadata.name': namespace}):
            return False
    elif namespace != local_namespace:
        return False
    return matches(peer.get('podSelector', {}), labels)


class NetworkPolicyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policies = {}
        for environment in ('homelab', 'work'):
            docs = list(yaml.safe_load_all(subprocess.check_output(
                ['kustomize', 'build', str(ROOT / f'13.t3code-{environment}')], text=True)))
            cls.policies[environment] = [doc for doc in docs if doc and doc['kind'] == 'NetworkPolicy']

    def allowed(self, environment, direction, address, port, namespace='', labels=None,
                protocol='TCP', pod=RUNTIME):
        local_namespace = f't3code-{environment}'
        policies = [policy['spec'] for policy in self.policies[environment]
                    if matches(policy['spec']['podSelector'], pod)
                    and direction in policy['spec']['policyTypes']]
        if not policies:
            return True
        for policy in policies:
            for rule in policy.get(direction.lower(), []):
                ports = rule.get('ports', [])
                if ports and not any(p.get('protocol', 'TCP') == protocol and p['port'] == port for p in ports):
                    continue
                peers = rule.get('to' if direction == 'Egress' else 'from', [])
                if not peers or any(peer_matches(peer, address, namespace, labels or {}, local_namespace)
                                    for peer in peers):
                    return True
        return False

    def test_overlays_isolate_every_pod_in_both_directions(self):
        for environment, policies in self.policies.items():
            with self.subTest(environment=environment):
                self.assertTrue(policies)
                self.assertTrue(all(p['metadata']['namespace'] == f't3code-{environment}' for p in policies))
                baseline = next(p['spec'] for p in policies if p['spec']['podSelector'] == {})
                self.assertEqual(set(baseline['policyTypes']), {'Ingress', 'Egress'})
                self.assertEqual(baseline.get('ingress', []), [])
                self.assertEqual(baseline.get('egress', []), [])
                self.assertFalse(self.allowed(environment, 'Egress', '1.1.1.1', 443, pod={}))
                self.assertFalse(self.allowed(environment, 'Ingress', '192.168.10.10', 2222, pod={}))

    def test_work_runtime_allows_only_required_cluster_destinations(self):
        environment = 'work'
        own_hub = 'mcp-hub-work'
        other_hub = 'mcp-hub-homelab'
        cases = [
            ('10.42.0.10', 53, 'kube-system', {'k8s-app': 'kube-dns'}, 'UDP', True),
            ('10.42.0.10', 53, 'kube-system', {'k8s-app': 'kube-dns'}, 'TCP', True),
            ('10.42.0.10', 9153, 'kube-system', {'k8s-app': 'kube-dns'}, 'TCP', False),
            ('10.42.0.10', 53, 'kube-system', {}, 'UDP', False),
            ('10.42.3.10', 8317, 'remote-pi', {'app.kubernetes.io/name': 'cliproxy'}, 'TCP', True),
            ('10.42.3.10', 8317, 'remote-pi', {'app.kubernetes.io/name': 'cliproxy-free'}, 'TCP', False),
            ('10.42.3.10', 18317, 'remote-pi', {'app.kubernetes.io/name': 'cliproxy'}, 'TCP', False),
            ('10.42.3.11', 8000, own_hub, {'app': 'mcp-hub'}, 'TCP', True),
            ('10.42.3.12', 8000, other_hub, {'app': 'mcp-hub'}, 'TCP', False),
            ('10.42.3.12', 8000, 'mcp-hub', {'app': 'mcp-hub'}, 'TCP', False),
            ('10.42.3.13', 80, 'observability', {'app.kubernetes.io/name': 'loki'}, 'TCP', False),
            ('10.43.190.120', 80, '', {}, 'TCP', False),
            ('10.42.3.14', 8080, 'vaultwarden', {'app': 'vaultwarden'}, 'TCP', False),
            ('10.42.3.15', 8443, 'traefik-system', {'app.kubernetes.io/name': 'traefik'}, 'TCP', True),
            ('10.42.3.15', 443, 'traefik-system', {'app.kubernetes.io/name': 'traefik'}, 'TCP', False),
            ('10.42.3.15', 8443, 'other', {'app.kubernetes.io/name': 'traefik'}, 'TCP', False),
        ]
        for address, port, namespace, labels, protocol, expected in cases:
            with self.subTest(environment=environment, address=address, port=port, namespace=namespace):
                self.assertEqual(self.allowed(environment, 'Egress', address, port, namespace, labels, protocol), expected)

    def test_work_public_internet_does_not_include_private_or_special_networks(self):
        environment = 'work'
        for address in ('1.1.1.1', '140.82.112.3', '20.50.1.1'):
            with self.subTest(environment=environment, public=address):
                self.assertTrue(self.allowed(environment, 'Egress', address, 443))
        for address in ('0.1.2.3', '10.42.2.3', '10.43.1.2', '10.0.0.80', '100.64.1.1',
                        '127.0.0.2', '169.254.169.254', '172.16.1.1', '172.31.255.254',
                        '192.168.10.102', '192.168.1.1', '224.0.0.1', '255.255.255.255'):
            with self.subTest(environment=environment, private=address):
                self.assertFalse(self.allowed(environment, 'Egress', address, 443))
        self.assertFalse(self.allowed(environment, 'Egress', '2606:4700:4700::1111', 443))

    def test_api_and_rollout_jobs_keep_only_dns_and_api_access(self):
        for environment in self.policies:
            for pod in (RUNTIME, ROLLOUT):
                for address in ('10.0.0.51', '10.0.0.52', '10.0.0.53'):
                    with self.subTest(environment=environment, pod=pod, api=address):
                        self.assertTrue(self.allowed(environment, 'Egress', address, 6443, pod=pod))
                        self.assertEqual(self.allowed(environment, 'Egress', address, 443, pod=pod),
                                         environment == 'homelab' and pod == RUNTIME)
                self.assertEqual(self.allowed(environment, 'Egress', '10.0.0.61', 6443, pod=pod),
                                 environment == 'homelab' and pod == RUNTIME)
            self.assertTrue(self.allowed(environment, 'Egress', '10.42.0.10', 53, 'kube-system',
                                         {'k8s-app': 'kube-dns'}, 'UDP', pod=ROLLOUT))
            self.assertFalse(self.allowed(environment, 'Egress', '1.1.1.1', 443, pod=ROLLOUT))
            self.assertFalse(self.allowed(environment, 'Egress', '10.42.3.11', 8000,
                                          f'mcp-hub-{environment}', {'app': 'mcp-hub'}, pod=ROLLOUT))

    def test_homelab_ssh_covers_inventory_and_lan_while_work_stays_restricted(self):
        config = (ROOT / '13.t3code/one/files/homelab-ssh.conf').read_text()
        hosts = [line.split()[1] for line in config.splitlines() if line.strip().startswith('HostName ')]
        self.assertTrue(hosts)
        for host in hosts:
            address = str(ipaddress.IPv4Address(host))
            with self.subTest(address=address):
                self.assertTrue(self.allowed('homelab', 'Egress', address, 22))
                self.assertTrue(self.allowed('homelab', 'Egress', address, 80))
                self.assertFalse(self.allowed('work', 'Egress', address, 22))
        for address in ('192.168.10.1', '192.168.10.102', '192.168.10.114', '10.0.0.80'):
            self.assertTrue(self.allowed('homelab', 'Egress', address, 22))

    def test_homelab_allows_cluster_and_lan_except_direct_work_namespace_traffic(self):
        for namespace in ('argocd', 'observability', 'remote-pi', 'mcp-hub', 'mcp-hub-homelab',
                          'vaultwarden', 'servarr', 't3code', 't3code-homelab', 'kube-system'):
            for address in ('10.42.3.13', '10.43.190.120'):
                for port in (22, 53, 80, 443, 8080, 8443):
                    with self.subTest(namespace=namespace, address=address, port=port):
                        self.assertTrue(self.allowed('homelab', 'Egress', address, port, namespace))
        for namespace in ('t3code-work', 'mcp-hub-work'):
            for address in ('10.42.3.12', '10.43.190.120'):
                for port in (22, 53, 80, 443, 3773, 8000, 8087):
                    for protocol in ('TCP', 'UDP'):
                        with self.subTest(namespace=namespace, address=address, port=port, protocol=protocol):
                            self.assertFalse(self.allowed('homelab', 'Egress', address, port, namespace,
                                                          protocol=protocol))
        for address in ('10.0.0.80', '192.168.10.102', '192.168.1.1', '172.16.1.1',
                        '100.64.1.1', '1.1.1.1', '140.82.112.3'):
            for port, protocol in ((22, 'TCP'), (443, 'TCP'), (7844, 'UDP')):
                with self.subTest(address=address, port=port, protocol=protocol):
                    self.assertTrue(self.allowed('homelab', 'Egress', address, port, protocol=protocol))
        self.assertFalse(self.allowed('homelab', 'Egress', '2606:4700:4700::1111', 443))

    def test_ingress_keeps_ssh_reachable_for_lan_masqueraded_and_pod_sources(self):
        for environment in self.policies:
            for address in ('192.168.10.10', '192.168.10.11', '1.1.1.1',
                            '10.42.0.0', '10.42.3.20', '10.43.1.2'):
                with self.subTest(environment=environment, source=address):
                    self.assertTrue(self.allowed(environment, 'Ingress', address, 2222))
                    for port in (22, 3773, 8087):
                        self.assertFalse(self.allowed(environment, 'Ingress', address, port))


if __name__ == '__main__':
    unittest.main()
