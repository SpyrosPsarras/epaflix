#!/usr/bin/env python3
"""Synthetic reviewed work inputs. No live accounts or endpoints are read."""
import base64
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
spec = importlib.util.spec_from_file_location('isolated_fixture', Path(__file__).with_name('isolated-runtime.test.py'))
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
runtime = common.runtime
auth = importlib.import_module('work-aks-auth')
ROOT = common.ROOT


def refresh(directory):
    path = directory / 'runtime.json'
    config = json.loads(path.read_text())
    config['sha256'] = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                        for name in runtime.INPUTS | {'work-auth.json'}}
    path.write_text(json.dumps(config))


def fixture(directory):
    common.fixture(directory)
    for name in ('runtime.json', 'services.json', 'policies.json'):
        path = directory / name
        obj = json.loads(path.read_text())
        obj['environment'] = 'work'
        if name == 'runtime.json':
            obj['hubUrl'] = 'http://mcp-hub.mcp-hub-work.svc.cluster.local:8000'
            obj['projectUrl'] = 'https://github.com/work/fixture.git'
            obj['approvals']['workAuth'] = 'synthetic-work-account-and-scope-review'
            obj['grantSha256']['hub-token'] = hashlib.sha256(('work.' + 'h' * 43).encode()).hexdigest()
            obj['grantSha256']['model-token'] = hashlib.sha256(b'tok-fixture-model').hexdigest()
        path.write_text(json.dumps(obj))
    kube = {'apiVersion': 'v1', 'kind': 'Config', 'current-context': 'work-first',
            'clusters': [], 'users': [], 'contexts': []}
    pins = []
    for name in ('work-first', 'work-second'):
        ca = ('synthetic-ca-' + name).encode()
        server = f'https://{name}.example.test:6443'
        kube['clusters'].append({'name': name, 'cluster': {
            'server': server, 'certificate-authority-data': base64.b64encode(ca).decode()}})
        kube['users'].append({'name': name, 'user': {'exec': copy.deepcopy(auth.EXEC)}})
        kube['contexts'].append({'name': name, 'context': {'cluster': name, 'user': name}})
        pins.append({'name': name, 'cluster': name, 'user': name, 'server': server,
                     'certificateAuthoritySha256': hashlib.sha256(ca).hexdigest()})
    (directory / 'kubeconfig').write_text(json.dumps(kube))
    (directory / 'work-auth.json').write_text(json.dumps({'environment': 'work',
        'tenantId': '11111111-1111-1111-1111-111111111111',
        'audience': '22222222-2222-2222-2222-222222222222',
        'subscriptionId': '33333333-3333-3333-3333-333333333333',
        'account': {'name': 'fixture@work.example.test', 'type': 'user'}, 'contexts': pins}))
    refresh(directory)


class WorkRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inputs, self.home = self.root / 'inputs', self.root / 'home'
        self.home.mkdir()
        fixture(self.inputs)
        self.env = patch.dict(os.environ, {'PATH': os.environ['PATH'], 'MCP_HUB_TOKEN': 'work.' + 'h' * 43,
            'ANTHROPIC_AUTH_TOKEN': 'tok-fixture-model'}, clear=True)
        self.env.start()
        self.requirements = self.root / 'etc/codex/requirements.toml'
        self.requirements_patch = patch.object(runtime, 'CODEX_REQUIREMENTS', self.requirements)
        self.requirements_patch.start()

    def tearDown(self):
        self.requirements_patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def validate(self):
        return runtime.validate(self.inputs, self.home, 'work')

    def prepare(self):
        runtime.prepare(self.inputs, self.home, 'work')
        path = self.home / '.pi/agent/pi-cliproxyapi-provider/config.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'providerName': 'cliproxy', 'baseUrl': 'http://127.0.0.1:18317/v1',
                                    'modelAliases': {'claude/fixture': 'anthropic/fixture'}}))

    def change(self, name, transform):
        path = self.inputs / name
        obj = json.loads(path.read_text())
        transform(obj)
        path.write_text(json.dumps(obj))
        refresh(self.inputs)

    def test_work_only_hub_policies_settings_history_and_azure_persist(self):
        self.prepare()
        state = self.home / '.t3/userdata'
        state.mkdir(parents=True)
        (state / 'settings.json').write_text(json.dumps({'theme': 'fixture', 'providerInstances': {
            'pi': {'driver': 'pi', 'displayName': 'Work fixture', 'config': {'customModels': ['cliproxy/fixture']}}}}))
        (state / 'history-fixture').write_text('work-only')
        cache = self.home / '.azure'
        cache.mkdir()
        (cache / 'session-fixture').write_text('synthetic-work-session')
        for _ in range(2):
            runtime.apply(self.inputs, self.home, 'work')
            runtime.prepare(self.inputs, self.home, 'work')
        settings = json.loads((state / 'settings.json').read_text())
        self.assertEqual(settings['theme'], 'fixture')
        self.assertEqual(settings['providerInstances']['pi']['displayName'], 'Work fixture')
        self.assertEqual((state / 'history-fixture').read_text(), 'work-only')
        self.assertEqual((cache / 'session-fixture').read_text(), 'synthetic-work-session')
        for name in ('.pi/agent/mcp.json', '.claude.json', '.claude/.claude.json'):
            servers = json.loads((self.home / name).read_text())['mcpServers']
            self.assertEqual(set(servers), {'searxng'})
            self.assertEqual(servers['searxng']['url'], 'http://mcp-hub.mcp-hub-work.svc.cluster.local:8000/searxng')
            self.assertNotIn(os.environ['MCP_HUB_TOKEN'], (self.home / name).read_text())
        requirements = tomllib.loads(self.requirements.read_text())
        self.assertEqual(requirements['mcp_servers'], {'searxng': {'identity': {
            'url': 'http://mcp-hub.mcp-hub-work.svc.cluster.local:8000/searxng'}}})
        safety = json.loads((self.home / '.cc-safety-net/policy.json').read_text())
        self.assertIn(str(cache), safety['secret_protection']['deny_paths'])
        self.assertEqual(subprocess.run(['python3', str(self.home / '.config/t3-policy/guard.py'), 'git push']).returncode, 2)
        self.assertFalse((self.home / runtime.LEGACY_PERMISSION_PATH).exists())

    def test_legacy_permission_input_is_reviewed_but_not_installed(self):
        self.change('policies.json', lambda obj: obj['files'].update({
            runtime.LEGACY_PERMISSION_PATH: common.encoded({'permission': 'ask'})}))
        self.validate()
        self.prepare()
        runtime.apply(self.inputs, self.home, 'work')
        policy = self.home / runtime.LEGACY_PERMISSION_PATH
        self.assertFalse(policy.exists())
        policy.parent.mkdir(parents=True)
        policy.write_text('stale rollback policy')
        runtime.apply(self.inputs, self.home, 'work')
        self.assertEqual(policy.read_text(), 'stale rollback policy')
        path = self.inputs / 'policies.json'
        path.write_text(path.read_text() + '\n')
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            self.validate()

    def test_legacy_permission_acceptance_checks_are_not_silently_skipped(self):
        self.change('policies.json', lambda obj: obj['files'].update({
            runtime.LEGACY_PERMISSION_PATH: common.encoded({'permission': 'ask'})}))
        self.change('policies.json', lambda obj: obj['checks'].append({
            'argv': ['python3', '-c', 'from pathlib import Path; import sys; assert Path(sys.argv[1]).exists()',
                     '${HOME}/' + runtime.LEGACY_PERMISSION_PATH], 'exit': 0}))
        self.prepare()
        with self.assertRaisesRegex(ValueError, 'policy acceptance check failed'):
            runtime.apply(self.inputs, self.home, 'work')

    def test_legacy_permission_input_still_requires_valid_encoding(self):
        self.change('policies.json', lambda obj: obj['files'].update({
            runtime.LEGACY_PERMISSION_PATH: {'data': '!invalid-base64!'}}))
        with self.assertRaises(ValueError):
            self.validate()

    def test_required_safety_and_hook_inputs_cannot_be_omitted(self):
        for path in runtime.POLICY_PATHS:
            fixture(self.inputs)
            self.change('policies.json', lambda obj: obj['files'].pop(path))
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate()

    def test_required_safety_and_hooks_cannot_be_disabled(self):
        for path, replacement in [('.claude/settings.json', {'disableAllHooks': True}),
                                 ('.cc-safety-net/policy.json', {'version': 1, 'secret_protection': {'overrides': {'x': 'off'}}})]:
            fixture(self.inputs)
            self.change('policies.json', lambda obj: obj['files'].update({path: common.encoded(replacement)}))
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.validate()

    def test_work_project_trust_and_compliant_checkout_settings_survive(self):
        self.prepare()
        project = self.home / 'projects/work'
        config = project / '.claude/settings.json'
        config.parent.mkdir(parents=True)
        content = '{ "mcpServers": {}, "disableAllHooks": false, "theme": "fixture" }\n'
        config.write_text(content)
        before = config.read_bytes(), config.stat().st_mtime_ns, config.stat().st_ino
        codex = self.home / '.codex/config.toml'
        runtime.write_toml(codex, {'model_reasoning_effort': 'high',
            'projects': {str(project): {'trust_level': 'trusted'}, '/foreign/project': {'trust_level': 'trusted'}}})
        runtime.apply(self.inputs, self.home, 'work')
        self.assertEqual((config.read_bytes(), config.stat().st_mtime_ns, config.stat().st_ino), before)
        actual = tomllib.loads(codex.read_text())
        self.assertEqual(actual['projects'], {str(project): {'trust_level': 'trusted'}})
        self.assertEqual(actual['model_reasoning_effort'], 'high')
        self.assertEqual(actual['model_provider'], 'cliproxy')

    def test_work_multiple_contexts_are_pinned_not_classified_by_names(self):
        self.validate()
        mutations = [lambda obj: obj['users'][0].update(user={'token': 'synthetic-homelab-token'}),
            lambda obj: obj['users'][0]['user']['exec'].update(command='/bin/true'),
            lambda obj: obj['users'][0]['user']['exec'].update(env=[{'name': 'AZURE_CONFIG_DIR', 'value': '/foreign'}]),
            lambda obj: obj['users'][0]['user']['exec'].update(args=['login']),
            lambda obj: obj['contexts'][0]['context'].update(user='work-second'),
            lambda obj: obj['contexts'][0].update(name='homelab'),
            lambda obj: obj['contexts'].append(copy.deepcopy(obj['contexts'][0])),
            lambda obj: obj['clusters'][0]['cluster'].update(server='https://homelab.example.test'),
            lambda obj: obj['clusters'][0]['cluster'].update(**{'certificate-authority': '/foreign/ca'}),
            lambda obj: obj.update(**{'current-context': 'unapproved'})]
        for mutation in mutations:
            fixture(self.inputs)
            self.change('kubeconfig', mutation)
            with self.assertRaises(ValueError): self.validate()

    def test_auth_scope_inventory_grants_and_reviews_fail_closed(self):
        for name, transform in [('work-auth.json', lambda obj: obj.update(environment='homelab')),
            ('work-auth.json', lambda obj: obj.pop('tenantId')),
            ('work-auth.json', lambda obj: obj.update(audience='')),
            ('work-auth.json', lambda obj: obj.update(contexts=[])),
            ('runtime.json', lambda obj: obj['approvals'].pop('workAuth')),
            ('services.json', lambda obj: obj.update(environment='homelab')),
            ('services.json', lambda obj: obj.update(services=['vaultwarden'])),
            ('runtime.json', lambda obj: obj.update(hubUrl='http://mcp-hub.mcp-hub-homelab.svc.cluster.local:8000'))]:
            fixture(self.inputs)
            self.change(name, transform)
            with self.subTest(name=name), self.assertRaises((ValueError, KeyError)): self.validate()
        fixture(self.inputs)
        for name, value in [('MCP_HUB_TOKEN', 'homelab.' + 'h' * 43), ('GITHUB_TOKEN', 'unreviewed'),
                            ('SSH_AUTH_SOCK', '/foreign/agent'), ('AZURE_CONFIG_DIR', '/foreign/cache'), ('BW_SESSION', 'foreign')]:
            with patch.dict(os.environ, {name: value}), self.assertRaises(ValueError): self.validate()
        (self.inputs / 'work-auth.json').write_text('{}')
        with self.assertRaises(ValueError): self.validate()

    def test_imported_home_or_azure_cache_refused_and_homelab_gate_unchanged(self):
        cache = self.home / '.azure'
        cache.mkdir()
        (cache / 'cache-fixture').write_text('foreign')
        with self.assertRaises(ValueError): self.validate()
        shutil.rmtree(cache)
        (self.home / '.config/t3-runtime').mkdir(parents=True)
        (self.home / '.config/t3-runtime/environment').write_text('homelab\n')
        with self.assertRaises(ValueError): self.validate()
        for kube in (json.loads((self.inputs / 'kubeconfig').read_text()),):
            with self.assertRaises(ValueError): runtime.validate_homelab_kubeconfig(kube)

    def test_laptop_environments_have_distinct_keys_configs_and_ports(self):
        for environment, port in [('homelab', 2222), ('work', 2223)]:
            directory = self.root / environment
            common.laptop.prepare(directory, f'ssh.{environment}.example.test', 'node', port,
                                  self.inputs / 'authorized_keys', environment)
            settings = subprocess.check_output(['ssh', '-G', '-F', str(directory / 'ssh_config'),
                                                f't3-{environment}'], text=True)
            for expected in ('forwardagent no', 'identityagent none', 'identitiesonly yes', f'port {port}'):
                self.assertIn(expected, settings)
        self.assertNotEqual((self.root / 'homelab/id_ed25519.pub').read_bytes(), (self.root / 'work/id_ed25519.pub').read_bytes())
        broader = self.root / 'broader'
        broader.mkdir()
        (broader / 'config').write_text('Host personal\n')
        with self.assertRaises(ValueError):
            common.laptop.prepare(broader, 'ssh.work.example.test', 'node', 2223,
                                  self.inputs / 'authorized_keys', 'work')
        self.assertEqual((broader / 'config').read_text(), 'Host personal\n')
        self.assertFalse((broader / 'environment').exists())
        before = (self.root / 'homelab/ssh_config').read_bytes()
        with self.assertRaises(ValueError):
            common.laptop.prepare(self.root / 'homelab', 'ssh.work.example.test', 'node', 2223,
                                  self.inputs / 'authorized_keys', 'work')
        self.assertEqual((self.root / 'homelab/ssh_config').read_bytes(), before)
        ssh = self.root / 'ssh'
        ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        ssh.chmod(0o700)
        with patch.dict(os.environ, {'PATH': str(self.root) + ':' + os.environ['PATH']}):
            args = subprocess.check_output(['bash', str(ROOT / 'env/tools/connect-laptop.sh'),
                str(self.root / 'work/ssh_config'), '23773', 'work'], text=True)
        self.assertIn('127.0.0.1:23773:127.0.0.1:3773', args)
        self.assertIn('t3-work', args)
        self.assertIn('IdentityAgent=none', args)
        with patch.dict(os.environ, {'PATH': str(self.root) + ':' + os.environ['PATH']}):
            for environment, port in [('homelab', 13773), ('work', 23773)]:
                args = subprocess.check_output(['bash', str(ROOT / 'env/tools/connect-laptop.sh'),
                    str(self.root / environment / 'ssh_config'), '', environment], text=True)
                self.assertIn(f'127.0.0.1:{port}:127.0.0.1:3773', args)
                self.assertIn(f't3-{environment}', args)

    def test_ci_covers_work_paths_variants_and_only_public_image_inputs(self):
        import yaml
        workflow = yaml.load((ROOT.parents[1] / '.github/workflows/build-t3-runtime.yml').read_text(), Loader=yaml.BaseLoader)
        for event in ('push', 'pull_request'):
            paths = workflow['on'][event]['paths']
            for name in ('13.t3code', '13.t3code-homelab', '13.t3code-work'):
                self.assertIn(f'2-k3s/{name}/**', paths)
        build = workflow['jobs']['build']
        self.assertEqual(build['strategy']['matrix']['variant'], ['mixed', 'homelab', 'work'])
        steps = {step.get('name'): step for step in build['steps'] if 'name' in step}
        verification = steps['Build and boot without production credentials']['run']
        for name in ('13.t3code/one', '13.t3code-homelab', '13.t3code-work'):
            self.assertIn(f'2-k3s/{name}/statefulset.yaml', verification)
        for name in ('Log in to GHCR', 'Publish the tested image'):
            self.assertEqual(steps[name]['if'], "github.event_name != 'pull_request'")
        publish = steps['Publish the tested image']['run']
        self.assertIn('ghcr.io/spyrospsarras/t3-runtime', publish)
        self.assertIn('source=t3-runtime:$variant-dev; prefix=$variant-', publish)
        ci = yaml.load((ROOT.parents[1] / '.github/workflows/ci.yml').read_text(), Loader=yaml.BaseLoader)
        ci_steps = {step.get('name'): step for step in ci['jobs']['validate']['steps'] if 'name' in step}
        self.assertIn('2-k3s/13.t3code-work', ci_steps['Kustomize build (sops-free dirs)']['run'])
        for suite in ('work-runtime.test.py', 'work-aks-auth.test.py'):
            self.assertIn(suite, ci_steps['T3 config and placement tests']['run'])
        smoke = ci_steps['T3 install and runtime smoke']['run']
        for variant in ('homelab', 'work'):
            self.assertIn(f'T3_RUNTIME_VARIANT={variant}', smoke)
        allowed = {line[1:] for line in (ROOT / 'env/Dockerfile.dockerignore').read_text().splitlines() if line.startswith('!')}
        self.assertEqual(allowed, {'versions.env', 'env/', 'env/Dockerfile', 'env/tools/',
            'env/tools/os-packages.txt', 'env/tools/install-cluster-tools.sh',
            'env/tools/package.json', 'env/tools/package-lock.json',
            'env/files/', 'env/files/vault-restricted.sh', 'one/', 'one/files/',
            'one/files/vault-restricted.py', 'one/files/hub_services.py'})

    def test_gitops_work_overlay_and_jobs_are_independent_and_revision_tracked(self):
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp) / '2-k3s'
            tree.mkdir()
            for name in ('13.t3code', '13.t3code-homelab', '13.t3code-work'):
                shutil.copytree(ROOT.parent / name, tree / name, ignore=shutil.ignore_patterns('__pycache__'))
            overlay = tree / '13.t3code-work'
            def render():
                return list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(overlay)])))
            docs = render()
            self.assertTrue(all(doc['metadata'].get('namespace', 't3code-work') == 't3code-work' for doc in docs))
            self.assertFalse(any(doc['kind'] == 'Secret' for doc in docs))
            sts = next(doc for doc in docs if doc['kind'] == 'StatefulSet')
            pod = sts['spec']['template']['spec']
            self.assertEqual(sts['spec']['updateStrategy']['type'], 'OnDelete')
            self.assertEqual(sts['spec']['persistentVolumeClaimRetentionPolicy'], {'whenDeleted': 'Retain', 'whenScaled': 'Retain'})
            self.assertFalse(pod['automountServiceAccountToken'])
            self.assertNotIn('hostPath', json.dumps(pod))
            self.assertEqual([v['persistentVolumeClaim']['claimName'] for v in pod['volumes'] if 'persistentVolumeClaim' in v], ['t3code-home'])
            self.assertEqual({v['secret']['secretName'] for v in pod['volumes'] if 'secret' in v}, {'t3-runtime-inputs'})
            inputs = next(v for v in pod['volumes'] if v['name'] == 'inputs')
            self.assertIn({'key': 'work-auth.json', 'path': 'work-auth.json'}, inputs['secret']['items'])
            image = pod['containers'][0]['image']
            self.assertIn(':work-inputs-', image)
            self.assertTrue(all(c['image'] == image for c in pod['containers'] + pod['initContainers']))
            self.assertEqual(next(e['value'] for e in pod['containers'][0]['env'] if e['name'] == 'MCP_HUB_INSTANCE'), 'work')
            for doc in docs:
                if doc['kind'] in {'Job', 'CronJob'}:
                    job = doc['spec']['jobTemplate']['spec'] if doc['kind'] == 'CronJob' else doc['spec']
                    self.assertEqual(job['template']['spec']['containers'][0]['image'], image)
                    self.assertEqual(job['template']['spec']['affinity'], pod['affinity'])
            role = next(doc for doc in docs if doc['kind'] == 'Role')
            self.assertTrue(all(set(rule['resourceNames']).issubset({'t3code', 't3code-0'}) for rule in role['rules']))
            binding = next(doc for doc in docs if doc['kind'] == 'RoleBinding')
            self.assertEqual(binding['subjects'][0]['namespace'], 't3code-work')
            scripts = next(doc for doc in docs if doc['kind'] == 'ConfigMap' and 'entrypoint.sh' in doc['data'])
            self.assertIn('work-aks-auth.py', scripts['data'])
            self.assertNotIn('vault-run.py', scripts['data'])
            self.assertNotIn('aks-auth-central.sh', scripts['data'])
            homelab = list(yaml.safe_load_all(subprocess.check_output(['kustomize', 'build', str(tree / '13.t3code-homelab')])) )
            self.assertFalse(any('work-aks-auth.py' in doc.get('data', {}) for doc in homelab))
            manifest = overlay / 'statefulset.yaml'
            annotation = 't3code.epaflix.com/inputs-grants-revision'
            before = sts['spec']['template']
            self.assertEqual(before['metadata']['annotations'][annotation], 'unprovisioned')
            manifest.write_text(manifest.read_text().replace('"unprovisioned"', '"r1"'))
            after = next(doc for doc in render() if doc['kind'] == 'StatefulSet')['spec']['template']
            self.assertNotEqual(before, after)
            self.assertEqual(before['spec'], after['spec'])
            app = yaml.safe_load((ROOT.parent / '11.argocd/apps/app-t3code-work.yaml').read_text())
            self.assertNotIn('automated', app['spec']['syncPolicy'])
            self.assertEqual(app['spec']['source']['path'], '2-k3s/13.t3code-work')
            parent = yaml.safe_load((ROOT.parent / '11.argocd/apps/app-of-apps.yaml').read_text())
            self.assertTrue(parent['spec']['syncPolicy']['automated']['selfHeal'])


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == 'fixture':
        fixture(Path(sys.argv[2]))
    else:
        unittest.main()
