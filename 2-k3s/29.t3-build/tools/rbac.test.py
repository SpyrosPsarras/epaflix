#!/usr/bin/env python3
"""Allow/deny matrix for the t3-build ServiceAccounts, evaluated like manager-rbac.test.py."""
from itertools import product
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT.parent / '11.argocd/apps'
BUILDERS = ('t3-build-homelab', 't3-build-work')
ACCOUNT = 'builder'
VERBS = ('get', 'list', 'watch', 'create', 'update', 'patch', 'delete', 'deletecollection')
# Exactly what kubectl needs for t3-build (create, logs -f job/, get job, delete) and t3-run (run --rm -i).
EXPECTED = {(group, resource, verb) for group, resource, verbs in (
    ('', 'pods', ('create', 'get', 'list', 'watch', 'delete')),
    ('', 'pods/log', ('get',)),
    ('', 'pods/attach', ('create',)),
    ('', 'configmaps', ('create', 'get', 'delete')),
    ('', 'secrets', ('create', 'delete')),
    ('batch', 'jobs', ('create', 'get', 'delete'))) for verb in verbs}
RESOURCES = (('', 'pods'), ('', 'pods/log'), ('', 'pods/attach'), ('', 'pods/exec'), ('', 'pods/portforward'),
             ('', 'configmaps'), ('', 'secrets'), ('', 'serviceaccounts'), ('', 'serviceaccounts/token'),
             ('', 'services'), ('', 'events'), ('batch', 'jobs'), ('batch', 'cronjobs'), ('apps', 'deployments'),
             ('apps', 'deployments/scale'), ('rbac.authorization.k8s.io', 'roles'),
             ('rbac.authorization.k8s.io', 'rolebindings'), ('networking.k8s.io', 'networkpolicies'))
PSA = {f'pod-security.kubernetes.io/{mode}': 'baseline' for mode in ('enforce', 'warn', 'audit')}


def allowed(docs, home, verb, group, resource, namespace):
    identity = f'system:serviceaccount:{home}:{ACCOUNT}'
    groups = ('system:serviceaccounts', f'system:serviceaccounts:{home}', 'system:authenticated')
    for binding in docs:
        if binding['kind'] not in ('RoleBinding', 'ClusterRoleBinding'):
            continue
        if binding['kind'] == 'RoleBinding' and binding['metadata'].get('namespace') != namespace:
            continue
        if not any(subject == {'kind': 'ServiceAccount', 'name': ACCOUNT, 'namespace': home}
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

    def test_exact_matrix_in_own_namespace(self):
        for home, (group, resource), verb in product(BUILDERS, RESOURCES, VERBS):
            with self.subTest(home=home, resource=resource, verb=verb):
                self.assertEqual(allowed(self.docs, home, verb, group, resource, home),
                                 (group, resource, verb) in EXPECTED)

    def test_nothing_in_the_other_builder_namespace_or_anywhere_else(self):
        for home, namespace, (group, resource), verb in product(
                BUILDERS, (*BUILDERS, 't3code-homelab', 't3code-work', 't3code', 'default', 'kube-system', 'argocd'),
                RESOURCES, VERBS):
            if namespace == home:
                continue
            with self.subTest(home=home, namespace=namespace, resource=resource, verb=verb):
                self.assertFalse(allowed(self.docs, home, verb, group, resource, namespace))
        for home, (group, resource), verb in product(
                BUILDERS, (('', 'nodes'), ('', 'namespaces'), ('', 'persistentvolumes'), ('', 'secrets'),
                           ('rbac.authorization.k8s.io', 'clusterroles')), VERBS):
            with self.subTest(home=home, resource=resource, verb=verb):
                self.assertFalse(allowed(self.docs, home, verb, group, resource, None))

    def test_shape(self):
        for namespace in BUILDERS:
            with self.subTest(namespace=namespace):
                docs = [doc for doc in self.docs if namespace in (doc['metadata'].get('namespace'),
                                                                  doc['metadata']['name'])]
                kinds = sorted((doc['kind'], doc['metadata']['name']) for doc in docs)
                self.assertEqual(kinds, [('ConfigMap', 'kaniko-job'), ('LimitRange', 'builder'),
                                         ('Namespace', namespace), ('NetworkPolicy', 'default-deny'),
                                         ('NetworkPolicy', 'public-egress'), ('ResourceQuota', 'builder'),
                                         ('Role', 'builder'), ('RoleBinding', 'builder'),
                                         ('ServiceAccount', ACCOUNT)])
                (account,) = (doc for doc in docs if doc['kind'] == 'ServiceAccount')
                self.assertIs(account['automountServiceAccountToken'], False)
        self.assertEqual(len(self.docs), 2 * 9)

    def test_namespaces_enforce_baseline_pod_security(self):
        namespaces = {doc['metadata']['name']: doc['metadata'].get('labels', {})
                      for doc in self.docs if doc['kind'] == 'Namespace'}
        self.assertEqual(set(namespaces), set(BUILDERS))
        for name, labels in namespaces.items():
            with self.subTest(namespace=name):
                self.assertLessEqual(PSA.items(), labels.items())

    def test_kaniko_template_is_pinned_and_baseline_compatible(self):
        for doc in (doc for doc in self.docs if doc['kind'] == 'ConfigMap'):
            job = yaml.safe_load(doc['data']['kaniko-job.yaml'])
            spec = job['spec']['template']['spec']
            self.assertRegex(spec['containers'][0]['image'],
                             r'^gcr\.io/kaniko-project/executor:v[0-9.]+@sha256:[0-9a-f]{64}$')
            self.assertEqual(job['spec']['activeDeadlineSeconds'], 1800)
            # Baseline forbids host namespaces, hostPath, privileged and added capabilities.
            for key in ('hostNetwork', 'hostPID', 'hostIPC', 'securityContext'):
                self.assertNotIn(key, spec)
            self.assertNotIn('securityContext', spec['containers'][0])
            self.assertEqual({next(k for k in v if k != 'name') for v in spec['volumes']}, {'configMap', 'secret'})

    def test_network_policy_denies_lan_and_allows_public_egress(self):
        work = next(doc for doc in yaml.safe_load_all((ROOT.parent / '13.t3code-work/network-policy.yaml').read_text())
                    if doc['metadata']['name'] == 't3code-runtime')
        for namespace in BUILDERS:
            with self.subTest(namespace=namespace):
                policies = {doc['metadata']['name']: doc['spec'] for doc in self.docs
                            if doc['kind'] == 'NetworkPolicy' and doc['metadata']['namespace'] == namespace}
                self.assertEqual(policies['default-deny'], {'podSelector': {}, 'policyTypes': ['Ingress', 'Egress'],
                                                            'ingress': [], 'egress': []})
                public = policies['public-egress']
                self.assertEqual(public['policyTypes'], ['Egress'])
                self.assertEqual(public['egress'][-1], work['spec']['egress'][-1])
                self.assertEqual(len(public['egress']), 2)
                self.assertEqual({p['port'] for p in public['egress'][0]['ports']}, {53})

    def test_argo_app_syncs_and_prunes(self):
        self.assertIn('app-t3-build.yaml', yaml.safe_load((APPS / 'kustomization.yaml').read_text())['resources'])
        app = yaml.safe_load((APPS / 'app-t3-build.yaml').read_text())
        self.assertEqual(app['spec']['source']['path'], '2-k3s/29.t3-build')
        self.assertNotIn('namespace', app['spec']['destination'])
        self.assertEqual(app['spec']['syncPolicy']['automated'], {'prune': True})


if __name__ == '__main__':
    unittest.main()
