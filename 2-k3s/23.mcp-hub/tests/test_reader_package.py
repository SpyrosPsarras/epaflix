import json
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def test_manual_reader_is_independent_and_unprovisioned(self):
        documents = list(yaml.safe_load_all(subprocess.check_output([
            'kustomize', 'build', str(ROOT / 'reader/homelab')])))
        self.assertFalse(any(doc['kind'] == 'Secret' for doc in documents))
        deployment = next(doc for doc in documents if doc['kind'] == 'Deployment')
        self.assertEqual(deployment['spec']['replicas'], 1)
        self.assertEqual(deployment['spec']['strategy']['type'], 'Recreate')
        template = deployment['spec']['template']
        self.assertEqual(template['metadata']['annotations']['vault-reader.epaflix.com/inputs-revision'], 'unprovisioned')
        spec = template['spec']
        self.assertFalse(spec['automountServiceAccountToken'])
        self.assertEqual(len(spec['containers']), 1)
        container = spec['containers'][0]
        self.assertTrue(container['securityContext']['readOnlyRootFilesystem'])
        self.assertFalse(container['securityContext']['allowPrivilegeEscalation'])
        self.assertEqual(container['securityContext']['capabilities']['drop'], ['ALL'])
        self.assertEqual(spec['securityContext']['runAsUser'], 1000)
        self.assertEqual(container['resources']['limits']['memory'], '1Gi')
        volumes = {value['name']: value for value in spec['volumes']}
        self.assertEqual(volumes['state']['emptyDir'], {'medium': 'Memory', 'sizeLimit': '128Mi'})
        self.assertEqual(volumes['inputs']['secret']['secretName'], 'vault-reader-inputs')
        self.assertEqual(volumes['inputs']['secret']['defaultMode'], 0o440)
        self.assertNotIn('persistentVolumeClaim', json.dumps(spec))
        tag = subprocess.check_output(['bash', str(ROOT / 'reader/image-tag.sh')], text=True).strip()
        self.assertEqual(container['image'], 'ghcr.io/spyrospsarras/vault-reader:inputs-' + tag)
        policy = next(doc for doc in documents if doc['kind'] == 'NetworkPolicy')
        source = policy['spec']['ingress'][0]['from'][0]
        self.assertEqual(source['namespaceSelector']['matchLabels']['kubernetes.io/metadata.name'], 'mcp-hub-homelab')
        self.assertEqual(source['podSelector']['matchLabels'], {'app': 'mcp-hub'})
        apps = ROOT.parent / '11.argocd/apps'
        app = yaml.safe_load((apps / 'app-vault-reader-homelab.yaml').read_text())
        self.assertNotIn('automated', app['spec']['syncPolicy'])
        self.assertEqual(app['spec']['source']['path'], '2-k3s/23.mcp-hub/reader/homelab')
        parent = yaml.safe_load((apps / 'app-of-apps.yaml').read_text())
        self.assertTrue(parent['spec']['syncPolicy']['automated']['selfHeal'])
        for environment in ['homelab', 'work']:
            hub = list(yaml.safe_load_all(subprocess.check_output([
                'kustomize', 'build', str(ROOT / 'instances' / environment)])))
            self.assertFalse(any(doc['kind'] == 'Deployment' and doc['metadata']['name'] == 'vault-reader' for doc in hub))
            self.assertNotIn('vault-reader-inputs', json.dumps(hub))


if __name__ == '__main__':
    unittest.main()
