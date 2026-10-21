#!/usr/bin/env python3
from itertools import product
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
APPS = ROOT.parent / '11.argocd/apps'
NAMESPACES = ('t3code-homelab', 't3code-work', 'mcp-hub-homelab', 'mcp-hub-work')
HOMELAB = ('t3code-homelab', 'mcp-hub-homelab')
VERBS = ('get', 'list', 'watch', 'create', 'update', 'patch', 'delete', 'deletecollection')
SUBJECT = {'kind': 'ServiceAccount', 'name': 'manager-agent', 'namespace': 't3code'}
IDENTITY = 'system:serviceaccount:t3code:manager-agent'
GROUPS = ('system:serviceaccounts', 'system:serviceaccounts:t3code', 'system:authenticated')


def allowed(docs: list[dict], verb: str, group: str, resource: str,
            namespace: str | None, name: str | None = None) -> bool:
    for binding in docs:
        if binding['kind'] not in ('RoleBinding', 'ClusterRoleBinding'):
            continue
        if binding['kind'] == 'RoleBinding' and binding['metadata'].get('namespace') != namespace:
            continue
        if not any(
            subject == SUBJECT
            or subject.get('kind') == 'User' and subject.get('name') == IDENTITY
            or subject.get('kind') == 'Group' and subject.get('name') in GROUPS
            for subject in binding.get('subjects', [])
        ):
            continue
        ref = binding['roleRef']
        for role in docs:
            if role['kind'] != ref['kind'] or role['metadata']['name'] != ref['name']:
                continue
            if role['kind'] == 'Role' and role['metadata'].get('namespace') != binding['metadata'].get('namespace'):
                continue
            for rule in role.get('rules', []):
                if verb not in rule['verbs'] and '*' not in rule['verbs']:
                    continue
                if group not in rule['apiGroups'] and '*' not in rule['apiGroups']:
                    continue
                resources = rule['resources']
                if resource not in resources and '*' not in resources and not (
                    '/' in resource and '*/' + resource.split('/', 1)[1] in resources
                ):
                    continue
                if 'resourceNames' not in rule or name in rule['resourceNames']:
                    return True
    return False


class ManagerRbacTest(unittest.TestCase):
    def render(self, path=None):
        result = subprocess.run(['kustomize', 'build', str(path or ROOT / 'manager-access')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [doc for doc in yaml.safe_load_all(result.stdout) if doc]

    def test_exact_recovery_matrix(self):
        docs = self.render()
        expected = {
            ('', 'pods', verb) for verb in ('get', 'list', 'watch', 'delete')
        } | {
            (group, resource, verb)
            for group, resource in (('', 'events'), ('events.k8s.io', 'events'),
                                    ('apps', 'statefulsets'), ('apps', 'deployments'))
            for verb in ('get', 'list', 'watch')
        }
        resources = (('', 'pods'), ('', 'events'), ('events.k8s.io', 'events'),
                     ('apps', 'statefulsets'), ('apps', 'deployments'),
                     ('apps', 'statefulsets/scale'), ('apps', 'deployments/scale'))
        for namespace in NAMESPACES:
            role = next(doc for doc in docs if doc['kind'] == 'Role'
                        and doc['metadata'] == {'name': 'manager-recovery', 'namespace': namespace})
            actual = set()
            for rule in role['rules']:
                self.assertEqual(set(rule), {'apiGroups', 'resources', 'verbs'})
                actual.update(product(rule['apiGroups'], rule['resources'], rule['verbs']))
            extra = {('', 'pods/exec', 'create'), ('', 'pods/log', 'get')} if namespace in HOMELAB else set()
            self.assertEqual(actual, expected | extra)
            for (group, resource), verb in product(resources, VERBS):
                with self.subTest(namespace=namespace, group=group, resource=resource, verb=verb):
                    self.assertEqual(allowed(docs, verb, group, resource, namespace),
                                     (group, resource, verb) in expected)

    def test_only_homelab_can_exec_and_read_logs(self):
        docs = self.render()
        for namespace, resource, verb in product(
            NAMESPACES, ('pods/exec', 'pods/log', 'pods/portforward', 'secrets'), VERBS
        ):
            with self.subTest(namespace=namespace, resource=resource, verb=verb):
                self.assertEqual(allowed(docs, verb, '', resource, namespace),
                                 namespace in HOMELAB and (resource, verb) in (
                                     ('pods/exec', 'create'), ('pods/log', 'get')))

    def test_named_application_get_only(self):
        docs = self.render()
        role = next(doc for doc in docs if doc['kind'] == 'Role'
                    and doc['metadata'] == {'name': 'manager-applications', 'namespace': 'argocd'})
        self.assertEqual(role['rules'], [{
            'apiGroups': ['argoproj.io'], 'resources': ['applications'],
            'resourceNames': list(NAMESPACES), 'verbs': ['get']}])
        for namespace, name, verb, resource in product(
            ('argocd', 't3code', *NAMESPACES), (None, 'other', 't3code-manager-access', *NAMESPACES),
            VERBS, ('applications', 'applications/status', 'applications/sync')
        ):
            with self.subTest(namespace=namespace, name=name, verb=verb, resource=resource):
                self.assertEqual(allowed(docs, verb, 'argoproj.io', resource, namespace, name),
                                 namespace == 'argocd' and name in NAMESPACES
                                 and verb == 'get' and resource == 'applications')

    def test_no_self_other_namespace_or_cluster_resource_grants(self):
        docs = self.render()
        self.assertEqual(sorted(doc['kind'] for doc in docs),
                         ['Role'] * 5 + ['RoleBinding'] * 5 + ['ServiceAccount'])
        pairs = {(namespace, 'manager-recovery') for namespace in NAMESPACES} | {('argocd', 'manager-applications')}
        for kind in ('Role', 'RoleBinding'):
            self.assertEqual({(doc['metadata']['namespace'], doc['metadata']['name'])
                              for doc in docs if doc['kind'] == kind}, pairs)
        for binding in (doc for doc in docs if doc['kind'] == 'RoleBinding'):
            self.assertEqual(binding['subjects'], [SUBJECT])
            self.assertEqual(binding['roleRef'], {
                'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': binding['metadata']['name']})
        with tempfile.TemporaryDirectory() as tmp:
            overlay = Path(tmp) / 'one'
            shutil.copytree(ROOT / 'one', overlay, ignore=shutil.ignore_patterns('*.enc.yaml', '__pycache__'))
            subprocess.run(['python3', str(ROOT / 'env/tools/private-config.test.py'), 'overlay', str(overlay)], check=True)
            runtime = self.render(overlay)
        idle = next(doc for doc in runtime if doc['kind'] == 'RoleBinding'
                    and doc['metadata']['name'] == 't3code-idle-rollout')
        self.assertEqual(idle['subjects'], [
            {'kind': 'ServiceAccount', 'name': 't3code-idle-rollout', 'namespace': 't3code'}])
        combined = docs + runtime
        resources = (('', 'pods'), ('', 'pods/exec'), ('', 'pods/log'), ('', 'pods/portforward'),
                     ('', 'secrets'), ('', 'events'), ('events.k8s.io', 'events'),
                     ('apps', 'statefulsets'), ('apps', 'deployments'), ('argoproj.io', 'applications'))
        for namespace, (group, resource), verb, name in product(
            ('t3code', 'default', 'kube-system', 'mcp-hub', 'arbitrary'), resources, VERBS,
            (None, 't3code', 't3code-0', *NAMESPACES)
        ):
            with self.subTest(namespace=namespace, group=group, resource=resource, verb=verb, name=name):
                self.assertFalse(allowed(combined, verb, group, resource, namespace, name))
        for (group, resource), verb in product(
            (('', 'nodes'), ('', 'namespaces'), ('', 'persistentvolumes'),
             ('rbac.authorization.k8s.io', 'clusterroles'), ('rbac.authorization.k8s.io', 'clusterrolebindings')), VERBS
        ):
            with self.subTest(group=group, resource=resource, verb=verb):
                self.assertFalse(allowed(combined, verb, group, resource, None))

    def test_access_app_is_manual_and_namespaces_survive_rendering(self):
        docs = self.render()
        self.assertIn('app-t3code-manager-access.yaml',
                      yaml.safe_load((APPS / 'kustomization.yaml').read_text())['resources'])
        apps = self.render(APPS)
        app = next(doc for doc in apps if doc['metadata']['name'] == 't3code-manager-access')
        self.assertEqual(app['metadata']['namespace'], 'argocd')
        self.assertEqual(app['spec']['source']['path'], '2-k3s/13.t3code/manager-access')
        self.assertEqual(app['spec']['source']['targetRevision'], 'main')
        self.assertEqual(app['spec']['destination']['namespace'], 't3code')
        self.assertNotIn('automated', app['spec'].get('syncPolicy', {}))
        self.assertEqual({doc['metadata']['namespace'] for doc in docs if doc['kind'] in ('Role', 'RoleBinding')},
                         {*NAMESPACES, 'argocd'})
        account = next(doc for doc in docs if doc['kind'] == 'ServiceAccount')
        self.assertEqual(account['metadata'], {'name': 'manager-agent', 'namespace': 't3code'})
        self.assertIs(account['automountServiceAccountToken'], False)
        self.assertEqual([doc['kind'] for doc in docs if doc['metadata']['namespace'] == 't3code'], ['ServiceAccount'])
        for binding in (doc for doc in docs if doc['kind'] == 'RoleBinding'):
            self.assertEqual(binding['subjects'], [SUBJECT])


if __name__ == '__main__':
    unittest.main()
