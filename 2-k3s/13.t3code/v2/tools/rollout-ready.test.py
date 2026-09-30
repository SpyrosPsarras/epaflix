#!/usr/bin/env python3
import importlib.util
import pathlib
import tempfile
import unittest

path = pathlib.Path(__file__).parents[1] / 'files' / 'rollout-ready.py'
spec = importlib.util.spec_from_file_location('rollout_ready', path)
module = importlib.util.module_from_spec(spec)
if path.exists():
    spec.loader.exec_module(module)


class RolloutReadiness(unittest.TestCase):
    def test_fresh_observation_starts_idle_period(self):
        self.assertEqual(module.advance(None, [], 100), {'idleSince': 100, 'eligible': False})

    def test_fifteen_minutes_required(self):
        self.assertFalse(module.advance(100, [], 999)['eligible'])
        self.assertTrue(module.advance(100, [], 1000)['eligible'])

    def test_every_blocker_resets_idle_period(self):
        for blocker in ['clients', 'runs', 'queue', 'requests', 'background', 'effects', 'terminal', 'schedule', 'unknown']:
            with self.subTest(blocker=blocker):
                self.assertEqual(module.advance(100, [blocker], 1000), {'idleSince': None, 'eligible': False})

    def test_clock_rollback_resets(self):
        self.assertEqual(module.advance(100, [], 99), {'idleSince': 99, 'eligible': False})

    def test_unknown_run_status_blocks(self):
        with self.assertRaises(ValueError):
            module.run_blockers([{'status': 'new-upstream-state', 'payload_json': '{}'}])

    def test_held_queue_and_terminal_runs_are_idle(self):
        self.assertEqual(module.run_blockers([
            {'status': 'queued', 'payload_json': '{"queueHeld":true}'},
            {'status': 'completed', 'payload_json': '{}'},
        ]), [])

    def test_active_and_unheld_queue_block(self):
        for status in ['preparing', 'starting', 'running', 'waiting', 'queued']:
            with self.subTest(status=status):
                self.assertTrue(module.run_blockers([{'status': status, 'payload_json': '{}'}]))

    def test_bad_payload_and_non_boolean_hold_block(self):
        for payload in ['invalid', '[]', '{"queueHeld":"true"}']:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                module.run_blockers([{'status': 'queued', 'payload_json': payload}])

    def test_session_count_requires_boolean(self):
        self.assertEqual(module.client_blockers([{'connected': False}]), [])
        self.assertEqual(module.client_blockers([{'connected': True}]), ['clients'])
        for body in [{}, [{'connected': 'false'}], [{}]]:
            with self.subTest(body=body), self.assertRaises(ValueError):
                module.client_blockers(body)

    def test_monitor_credential_requires_exact_read_scopes(self):
        for scopes in ['', 'access:read', 'access:read orchestration:read access:write']:
            with self.subTest(scopes=scopes), self.assertRaises(ValueError):
                module.monitor_headers({'access_token': 'synthetic', 'scope': scopes})
        self.assertEqual(module.monitor_headers({'access_token': 'synthetic', 'scope': 'access:read orchestration:read'})['x-t3-orchestration-protocol'], '2')

    def test_unknown_requests_and_items_fail_closed(self):
        for kind in ['requests', 'items', 'effects']:
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                module.status_blockers(kind, ['new-upstream-state'])

    def test_terminal_missing_server_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp, self.assertRaises(ValueError):
            module.terminal_blockers(1, pathlib.Path(temp))

    def test_terminal_idle_nested_and_background(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            def process(pid, parent, tty, command):
                directory = root / str(pid)
                directory.mkdir(exist_ok=True)
                (directory / 'stat').write_text(f'{pid} ({command}) S {parent} 0 0 {tty} 0')
            process(1, 0, 0, 't3')
            process(2, 1, 1, 'bash')
            self.assertEqual(module.terminal_blockers(1, root), [])
            process(3, 2, 1, 'sleep')
            self.assertEqual(module.terminal_blockers(1, root), ['terminal'])

    def test_exec_replaced_shell_blocks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            for pid, parent, tty, command in [(1, 0, 0, 't3'), (2, 1, 1, 'sleep')]:
                (root / str(pid)).mkdir()
                (root / str(pid) / 'stat').write_text(f'{pid} ({command}) S {parent} 0 0 {tty} 0')
            self.assertEqual(module.terminal_blockers(1, root), ['terminal'])

    def test_malformed_provider_background_blocks(self):
        for payload in ['[]', '{"pendingBackgroundTasks":"running"}', '{"pendingBackgroundTasks":{}}']:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                module.provider_background(payload)

    def test_stale_sample_does_not_count_as_continuous_idle(self):
        self.assertEqual(module.advance(100, [], 1000, last_sample=900), {'idleSince': 1000, 'eligible': False})


if __name__ == '__main__':
    unittest.main()
