#!/usr/bin/env python3
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class RbacTest(unittest.TestCase):
    def render(self):
        return [doc for doc in yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(ROOT / 'rbac')])) if doc]

    def test_homelab_agent_has_builtin_cluster_admin_without_redundant_read_grants(self):
        docs = self.render()
        self.assertEqual(sorted(doc['kind'] for doc in docs), ['ClusterRoleBinding', 'ServiceAccount'])
        account = next(doc for doc in docs if doc['kind'] == 'ServiceAccount')
        self.assertEqual(account['metadata']['name'], 'homelab-agent')
        self.assertEqual(account['metadata']['namespace'], 't3code-homelab')
        self.assertFalse(account['automountServiceAccountToken'])
        binding = next(doc for doc in docs if doc['kind'] == 'ClusterRoleBinding')
        self.assertEqual(binding['roleRef'], {
            'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'ClusterRole', 'name': 'cluster-admin'})
        self.assertEqual(binding['subjects'], [
            {'kind': 'ServiceAccount', 'name': 'homelab-agent', 'namespace': 't3code-homelab'}])

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
