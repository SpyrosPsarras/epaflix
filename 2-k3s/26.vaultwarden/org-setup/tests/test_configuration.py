import importlib.util
import json
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('job_renderer', ROOT / 'render-job.py')
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


class ConfigurationTests(unittest.TestCase):
    def test_base_policy_keeps_registration_closed_without_email_verification(self):
        deployment = next(yaml.safe_load_all((ROOT.parent / 'server.yaml').read_text()))
        settings = {entry['name']: entry.get('value') for entry in deployment['spec']['template']['spec']['containers'][0]['env']}
        self.assertEqual(settings['SIGNUPS_ALLOWED'], 'false')
        self.assertEqual(settings['SIGNUPS_VERIFY'], 'false')
        self.assertEqual(settings['INVITATIONS_ALLOWED'], 'true')
        self.assertEqual(settings.get('EMERGENCY_ACCESS_ALLOWED'), 'false')

    def test_job_uses_the_shared_kubectl_pin(self):
        job = renderer.render_job()
        identities = json.loads((ROOT / 'identities.json').read_text())
        self.assertEqual(job['metadata']['namespace'], identities['vaultNamespace'])
        pins = yaml.safe_load((ROOT.parent / 'kustomization.yaml').read_text())['images']
        pin = next(image for image in pins if image['name'] == 'alpine/kubectl')
        self.assertEqual(job['spec']['template']['spec']['initContainers'][0]['image'], pin['name'] + ':' + pin['newTag'] + '@' + pin['digest'])


if __name__ == '__main__':
    unittest.main()
