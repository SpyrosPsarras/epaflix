#!/usr/bin/env python3
"""Allow/deny matrix for the t3-build ServiceAccounts, evaluated like manager-rbac.test.py."""
from itertools import product
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT.parent / '11.argocd/apps'
NAMESPACE = 't3-build'
ACCOUNTS = ('builder-homelab', 'builder-work')
VERBS = ('get', 'list', 'watch', 'create', 'update', 'patch', 'delete', 'deletecollection')
FULL = ('create', 'get', 'list', 'watch', 'delete')
EXPECTED = {(group, resource, verb) for (group, resource), verb in product(
    (('', 'pods'), ('', 'pods/log'), ('', 'pods/attach'), ('', 'configmaps'), ('', 'secrets'), ('batch', 'jobs')), FULL)
} | {('', 'pods/exec', 'create')}
RESOURCES = (('', 'pods'), ('', 'pods/log'), ('', 'pods/attach'), ('', 'pods/exec'), ('', 'pods/portforward'),
             ('', 'configmaps'), ('', 'secrets'), ('', 'serviceaccounts'), ('', 'serviceaccounts/token'),
             ('', 'services'), ('', 'events'), ('batch', 'jobs'), ('batch', 'cronjobs'), ('apps', 'deployments'),
             ('apps', 'deployments/scale'), ('rbac.authorization.k8s.io', 'roles'),
             ('rbac.authorization.k8s.io', 'rolebindings'), ('networking.k8s.io', 'networkpolicies'))


def allowed(docs, account, verb, group, resource, namespace):
    identity = f'system:serviceaccount:{NAMESPACE}:{account}'
    groups = ('system:serviceaccounts', f'system:serviceaccounts:{NAMESPACE}', 'system:authenticated')
    for binding in docs:
        if binding['kind'] not in ('RoleBinding', 'ClusterRoleBinding'):
            continue
        if binding['kind'] == 'RoleBinding' and binding['metadata'].get('namespace') != namespace:
            continue
        if not any(subject == {'kind': 'ServiceAccount', 'name': account, 'namespace': NAMESPACE}
                   or subject.get('kind') == 'User' and subject.get('name') == identity
                   or subject.get('kind') == 'Group' and subject.get('name') in groups
                   for subject in binding.get('subjects', [])):
            continue
        ref = binding['roleRef']
        for role in docs:
            if role['kind'] != ref['kind'] or role['metadata']['name'] != ref['name']:
                continue
            if role['kind'] == 'Role' and role['metadata'].get('namespace') != binding['metadata'].get('namespace'):
                continue
            for rule in role.get('rules', []):
                if (verb in rule['verbs'] or '*' in rule['verbs']) and \
                        (group in rule['apiGroups'] or '*' in rule['apiGroups']) and \
                        (resource in rule['resources'] or '*' in rule['resources']) and 'resourceNames' not in rule:
                    return True
    return False


class BuilderRbacTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(['kustomize', 'build', str(ROOT)], capture_output=True, text=True, check=True)
        cls.docs = [doc for doc in yaml.safe_load_all(result.stdout) if doc]

    def test_exact_matrix_in_t3_build(self):
        for account, (group, resource), verb in product(ACCOUNTS, RESOURCES, VERBS):
            with self.subTest(account=account, resource=resource, verb=verb):
                self.assertEqual(allowed(self.docs, account, verb, group, resource, NAMESPACE),
                                 (group, resource, verb) in EXPECTED)

    def test_nothing_outside_t3_build(self):
        for account, namespace, (group, resource), verb in product(
                ACCOUNTS, ('t3code-homelab', 't3code-work', 't3code', 'default', 'kube-system', 'argocd'),
                RESOURCES, VERBS):
            with self.subTest(account=account, namespace=namespace, resource=resource, verb=verb):
                self.assertFalse(allowed(self.docs, account, verb, group, resource, namespace))
        for account, (group, resource), verb in product(
                ACCOUNTS, (('', 'nodes'), ('', 'namespaces'), ('', 'persistentvolumes'), ('', 'secrets'),
                           ('rbac.authorization.k8s.io', 'clusterroles')), VERBS):
            with self.subTest(account=account, resource=resource, verb=verb):
                self.assertFalse(allowed(self.docs, account, verb, group, resource, None))

    def test_shape(self):
        kinds = sorted((doc['kind'], doc['metadata']['name']) for doc in self.docs)
        self.assertEqual(kinds, [('ConfigMap', 'kaniko-job'), ('LimitRange', 'builder'), ('Namespace', 't3-build'),
                                 ('NetworkPolicy', 'default-deny'), ('NetworkPolicy', 'public-egress'),
                                 ('ResourceQuota', 'builder'), ('Role', 'builder'), ('RoleBinding', 'builder'),
                                 ('ServiceAccount', 'builder-homelab'), ('ServiceAccount', 'builder-work')])
        self.assertTrue(all(doc['metadata'].get('namespace', NAMESPACE) == NAMESPACE for doc in self.docs))
        for account in (doc for doc in self.docs if doc['kind'] == 'ServiceAccount'):
            self.assertIs(account['automountServiceAccountToken'], False)
        image = yaml.safe_load(next(doc for doc in self.docs if doc['kind'] == 'ConfigMap')['data']['kaniko-job.yaml'])[
            'spec']['template']['spec']['containers'][0]['image']
        self.assertRegex(image, r'^gcr\.io/kaniko-project/executor:v[0-9.]+@sha256:[0-9a-f]{64}$')

    def test_network_policy_denies_lan_and_allows_public_egress(self):
        policies = {doc['metadata']['name']: doc['spec'] for doc in self.docs if doc['kind'] == 'NetworkPolicy'}
        self.assertEqual(policies['default-deny'], {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'],
                                                    'ingress': [], 'egress': []})
        work = next(doc for doc in yaml.safe_load_all((ROOT.parent / '13.t3code-work/network-policy.yaml').read_text())
                    if doc['metadata']['name'] == 't3code-runtime')
        public = policies['public-egress']
        self.assertEqual(public['policyTypes'], ['Egress'])
        self.assertEqual(public['egress'][-1], work['spec']['egress'][-1])
        self.assertEqual(len(public['egress']), 2)
        self.assertEqual({p['port'] for p in public['egress'][0]['ports']}, {53})

    def test_argo_app_syncs_and_prunes(self):
        self.assertIn('app-t3-build.yaml', yaml.safe_load((APPS / 'kustomization.yaml').read_text())['resources'])
        app = yaml.safe_load((APPS / 'app-t3-build.yaml').read_text())
        self.assertEqual(app['spec']['source']['path'], '2-k3s/29.t3-build')
        self.assertEqual(app['spec']['destination']['namespace'], NAMESPACE)
        self.assertEqual(app['spec']['syncPolicy']['automated'], {'prune': True})


if __name__ == '__main__':
    unittest.main()
