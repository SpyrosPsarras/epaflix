#!/usr/bin/env python3
import importlib.util
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('sync_rbac', ROOT / 'tools/sync-rbac.py')
sync_rbac = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sync_rbac)


class RbacTest(unittest.TestCase):
    def render(self):
        return [doc for doc in yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(ROOT / 'rbac')])) if doc]

    def test_cluster_grant_contains_only_explicit_cluster_scoped_inventory_reads(self):
        docs = self.render()
        role = next(doc for doc in docs if doc['kind'] == 'ClusterRole')
        allowed = {'namespaces', 'nodes', 'persistentvolumes', 'storageclasses', 'volumeattachments', 'csidrivers', 'csinodes', 'ingressclasses', 'customresourcedefinitions'}
        for rule in role['rules']:
            self.assertLessEqual(set(rule['verbs']), {'get', 'list', 'watch'})
            self.assertLessEqual(set(rule['resources']), allowed)
        bindings = [doc for doc in docs if doc['kind'] == 'ClusterRoleBinding']
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]['roleRef']['name'], role['metadata']['name'])
        self.assertNotEqual(bindings[0]['roleRef']['name'], 'view')

    def test_namespaced_view_is_explicit_and_never_targets_excluded_or_temporary_namespaces(self):
        docs = self.render()
        bindings = [doc for doc in docs if doc['kind'] == 'RoleBinding']
        namespaces = {doc['metadata']['namespace'] for doc in bindings}
        self.assertIn('servarr', namespaces)
        self.assertIn('t3code-homelab', namespaces)
        self.assertFalse(namespaces & sync_rbac.EXCLUDED)
        self.assertFalse(any(name.endswith('-work') or name.startswith('vault-org-') for name in namespaces))
        self.assertEqual(len(bindings), len(namespaces))
        for binding in bindings:
            self.assertEqual(binding['roleRef']['name'], 'view')
            self.assertEqual(binding['subjects'], [{'kind': 'ServiceAccount', 'name': 'homelab-agent', 'namespace': 't3code-homelab'}])
        subprocess.run(['python3', str(ROOT / 'tools/sync-rbac.py'), '--check'], check=True)

    def test_generator_rejects_excluded_duplicate_unsorted_and_temporary_namespaces(self):
        self.assertIn('searxng', sync_rbac.EXCLUDED)
        for names in ([name] for name in sync_rbac.EXCLUDED):
            with self.assertRaises(ValueError):
                sync_rbac.render(names)
        for names in ([], ['servarr', 'servarr'], ['servarr', 'argocd'], ['fixture-work'], ['vault-org-fixture']):
            with self.assertRaises(ValueError):
                sync_rbac.render(names)

    def test_rbac_application_is_manual_and_separate_from_runtime_namespace_transformation(self):
        self.render()
        apps = ROOT.parent / '11.argocd/apps'
        self.assertIn('app-t3code-homelab-access.yaml', yaml.safe_load((apps / 'kustomization.yaml').read_text())['resources'])
        app = yaml.safe_load((apps / 'app-t3code-homelab-access.yaml').read_text())
        self.assertEqual(app['spec']['source']['path'], '2-k3s/13.t3code-homelab/rbac')
        self.assertEqual(app['spec']['source']['targetRevision'], 'main')
        self.assertNotIn('automated', app['spec']['syncPolicy'])


if __name__ == '__main__':
    unittest.main()
