#!/usr/bin/env python3
"""Restore Pi tools offline, splitting --tools= into argv as T3's Pi driver does."""
import importlib.util
import json
import os
from pathlib import Path
import select
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
PI = shutil.which('pi')
DIRECT_TOOLS = ['mcp__t3-code__orchestrator_capabilities', 'mcp__t3-code__delegate_task', 'mcp__t3-code__task_status']
OPTIONAL_TOOL = 'mcp__fixture__read'


@unittest.skipUnless(PI, 'Install the pinned Pi runtime to exercise transcript restoration')
class PiResumeTests(unittest.TestCase):
    def resume(self, root, launch_args):
        extension = root / 'probe.ts'
        extension.write_text('''export default function(pi) {
  for (const name of ''' + json.dumps([*DIRECT_TOOLS, OPTIONAL_TOOL]) + ''') {
    pi.registerTool({name, label: name, description: name,
      exposure: name === "mcp__fixture__read" ? "deferred" : "direct",
      parameters: {type: "object", properties: {}},
      async execute() { throw new Error("The restore fixture must not execute tools"); }});
  }
  pi.on("session_start", (event, ctx) => ctx.ui.notify(JSON.stringify({
    reason: event.reason, active: pi.getActiveTools(),
    registered: pi.getAllTools().map(tool => tool.name)
  }), "info"));
}
''')
        session = root / 'damaged.jsonl'
        tools = ['read', 'bash', 'edit', 'write', *DIRECT_TOOLS]
        entries = [
            {'type': 'session', 'version': 3, 'id': 'sanitized-resume-fixture',
             'timestamp': '2026-01-01T00:00:00.000Z', 'cwd': str(root)},
            {'type': 'message', 'id': 'initial', 'parentId': None,
             'timestamp': '2026-01-01T00:00:01.000Z', 'message': {
                 'role': 'system', 'content': 'Offline restore fixture', 'timestamp': 1,
                 'toolsAdded': [{'name': name, 'description': name,
                                 'parameters': {'type': 'object', 'properties': {}}} for name in tools]}},
            {'type': 'message', 'id': 'removed', 'parentId': 'initial',
             'timestamp': '2026-01-01T00:00:02.000Z', 'message': {
                 'role': 'system', 'content': '', 'timestamp': 2,
                 'toolsRemoved': [{'name': name} for name in DIRECT_TOOLS]}}]
        session.write_text(''.join(json.dumps(entry) + '\n' for entry in entries))
        pi_args = [part for arg in shlex.split(launch_args)
                   for part in (arg.split('=', 1) if arg.startswith('--tools=') else [arg])]
        command = [PI, '--mode', 'rpc', '--offline', '--no-extensions', '--no-skills',
                   '--no-context-files', '--no-prompt-templates', '--no-themes',
                   '--extension', str(extension), *pi_args]
        env = {'PATH': os.environ['PATH'], 'HOME': str(root), 'PI_OFFLINE': '1',
               'PI_CODING_AGENT_DIR': str(root / 'agent')}
        with tempfile.TemporaryFile() as errors, subprocess.Popen(command, cwd=root, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors) as process:
            try:
                request = {'id': 'resume', 'type': 'switch_session', 'sessionPath': str(session)}
                process.stdin.write((json.dumps(request) + '\n').encode())
                process.stdin.flush()
                deadline, buffered, probes = time.monotonic() + 30, b'', []
                while True:
                    remaining = deadline - time.monotonic()
                    self.assertGreater(remaining, 0, 'Pi session restore timed out')
                    if b'\n' not in buffered:
                        self.assertTrue(select.select([process.stdout], [], [], remaining)[0],
                                        'Pi session restore timed out')
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            errors.seek(0)
                            self.fail('Pi exited before restoring the fixture: ' + errors.read().decode())
                        buffered += chunk
                        continue
                    line, buffered = buffered.split(b'\n', 1)
                    event = json.loads(line)
                    if event.get('type') == 'extension_ui_request' and event.get('method') == 'notify':
                        probes.append(json.loads(event['message']))
                    if event.get('id') == 'resume' and event.get('type') == 'response':
                        self.assertTrue(event['success'], event)
                        return next(probe for probe in reversed(probes) if probe['reason'] == 'resume')
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    def test_managed_arguments_restore_direct_tools_removed_from_the_transcript(self):
        spec = importlib.util.spec_from_file_location('pi_settings', ROOT / 'one/files/t3-pi-settings.py')
        settings = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(settings)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = self.resume(root, '')
            self.assertTrue(set(DIRECT_TOOLS).issubset(baseline['registered']))
            self.assertTrue(set(DIRECT_TOOLS).isdisjoint(baseline['active']))
            cases = [('', {'read', 'bash', 'edit', 'write'}),
                     ('--tools=+grep,-write', {'read', 'bash', 'edit', 'grep'}),
                     ('--tools read,bash', {'read', 'bash'})]
            for index, (original, expected) in enumerate(cases):
                with self.subTest(launch_args=original):
                    home = root / f't3-{index}'
                    path = home / 'userdata/settings.json'
                    path.parent.mkdir(parents=True)
                    path.write_text(json.dumps({'providerInstances': {'pi': {'config': {'launchArgs': original}}}}))
                    settings.migrate(home, 'http://proxy.test')
                    config = json.loads(path.read_text())
                    restored = self.resume(root, config['providerInstances']['pi']['config']['launchArgs'])
                    self.assertTrue(set(DIRECT_TOOLS).issubset(restored['registered']))
                    self.assertTrue(set(DIRECT_TOOLS).issubset(restored['active']))
                    self.assertIn(OPTIONAL_TOOL, restored['registered'])
                    self.assertEqual({'read', 'bash', 'edit', 'write', 'grep'} & set(restored['active']), expected)


if __name__ == '__main__':
    unittest.main()
