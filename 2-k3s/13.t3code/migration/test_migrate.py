import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import migrate as m


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()
        self.output = self.root / "merged"
        for source, (home, _) in m.SOURCES.items():
            folder = self.inputs / source
            folder.mkdir()
            c = sqlite3.connect(folder / "state.sqlite")
            c.executescript('''
                CREATE TABLE effect_sql_migrations(migration_id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE orchestration_events(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE, event_type TEXT, payload_json TEXT);
                CREATE TABLE projection_turns(row_id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT);
                CREATE TABLE projection_projects(project_id TEXT PRIMARY KEY, workspace_root TEXT);
                CREATE TABLE projection_threads(thread_id TEXT PRIMARY KEY, project_id TEXT, model_selection_json TEXT);
                CREATE TABLE provider_session_runtime(thread_id TEXT PRIMARY KEY, provider_name TEXT,
                    provider_instance_id TEXT, runtime_payload_json TEXT);
                CREATE TABLE projection_state(projector TEXT PRIMARY KEY, last_applied_sequence INTEGER, updated_at TEXT);
                CREATE TABLE orchestration_command_receipts(command_id TEXT PRIMARY KEY, result_sequence INTEGER);
                CREATE TABLE projection_thread_activities(activity_id TEXT PRIMARY KEY, sequence INTEGER);
                CREATE TABLE projection_thread_messages(message_id TEXT PRIMARY KEY, text TEXT);
            ''')
            c.executemany("INSERT INTO effect_sql_migrations VALUES (?,?)", [(i, str(i)) for i in range(53)])
            c.execute("INSERT INTO orchestration_events VALUES (5,?,?,?)", (source, "thread.activity-appended",
                json.dumps({"activity": {"sequence": 5}, "cwd": home + "/repo", "text": "/home/t3/private"})))
            c.execute("INSERT INTO projection_turns VALUES (9,?)", (source,))
            c.execute("INSERT INTO projection_projects VALUES (?,?)", (source, home + "/repo"))
            c.execute("INSERT INTO projection_threads VALUES (?,?,?)", (source, source, '{"instanceId":"codex"}'))
            c.execute("INSERT INTO provider_session_runtime VALUES (?, 'codex', NULL, ?)",
                      (source, json.dumps({"cwd": home + "/repo"})))
            c.executemany("INSERT INTO projection_state VALUES (?,5,'now')", [(p,) for p in m.PROJECTORS])
            c.execute("INSERT INTO orchestration_command_receipts VALUES (?,5)", (source,))
            c.execute("INSERT INTO projection_thread_activities VALUES (?,5)", (source,))
            c.execute("INSERT INTO projection_thread_messages VALUES (?,?)", (source, "Keep /home/t3 verbatim"))
            c.commit()
            self.schema_hash = hashlib.sha256(json.dumps(m.schema(c), separators=(",", ":")).encode()).hexdigest()
            c.close()
            (folder / "settings.json").write_text(json.dumps({"providerInstances": {"codex": {"driver": "codex", "config": {
                "launchArgs": '-c model_providers.cliproxy.base_url="https://cliproxy.epaflix.com/v1" -c x=1'}}}}))
            (folder / "home" / ".codex").mkdir(parents=True)
            (folder / "home" / ".codex" / "session.jsonl").write_bytes(b'{"cwd":"/home/t3","text":"raw"}\r\n\xff')
        self.hash_patch = patch.object(m, "SCHEMA_SHA256", self.schema_hash)
        self.hash_patch.start()
        self.addCleanup(self.hash_patch.stop)

    def edit(self, sql):
        with sqlite3.connect(self.inputs / "t3env-0" / "state.sqlite") as c:
            c.execute(sql)

    def test_merge_offsets_paths_instances_and_bytes(self):
        before = {p: m.digest(p) for p in self.inputs.rglob("*") if p.is_file()}
        report = m.merge(self.inputs, self.output)
        with sqlite3.connect(self.output / "state.sqlite") as c:
            self.assertEqual(c.execute("SELECT sequence FROM orchestration_events ORDER BY sequence").fetchall(), [(5,), (10,), (15,)])
            self.assertEqual(c.execute("SELECT row_id FROM projection_turns ORDER BY row_id").fetchall(), [(9,), (18,), (27,)])
            self.assertEqual(c.execute("SELECT result_sequence FROM orchestration_command_receipts ORDER BY result_sequence").fetchall(), [(5,), (10,), (15,)])
            self.assertEqual(c.execute("SELECT DISTINCT last_applied_sequence FROM projection_state").fetchall(), [(15,)])
            self.assertEqual(c.execute("SELECT workspace_root FROM projection_projects WHERE project_id='t3env-0'").fetchone()[0], "/home/t3env-0/repo")
            payload = json.loads(c.execute("SELECT payload_json FROM orchestration_events WHERE sequence=10").fetchone()[0])
            self.assertEqual(payload["activity"]["sequence"], 10)
            self.assertEqual(payload["text"], "/home/t3/private")
            self.assertEqual(c.execute("SELECT DISTINCT text FROM projection_thread_messages").fetchall(), [("Keep /home/t3 verbatim",)])
            self.assertEqual(c.execute("SELECT provider_instance_id FROM provider_session_runtime WHERE thread_id='t3env-0'").fetchone()[0], "t3env-0__codex")
            self.assertEqual(c.execute("SELECT provider_instance_id FROM provider_session_runtime WHERE thread_id='host'").fetchone()[0], "codex")
            c.execute("INSERT INTO orchestration_events(event_id) VALUES ('new')")
            self.assertEqual(c.execute("SELECT max(sequence) FROM orchestration_events").fetchone()[0], 16)
        settings = json.loads((self.output / "settings.json").read_text())
        self.assertEqual(sorted(settings["providerInstances"]), ["codex", "t3env-0__codex", "t3env-1__codex"])
        env0 = {e["name"]: e["value"] for e in settings["providerInstances"]["t3env-0__codex"]["environment"]}
        self.assertEqual(env0["HOME"], "/home/t3env-0")
        self.assertEqual(env0["CODEX_HOME"], "/home/t3env-0/.codex")
        host = {e["name"]: e["value"] for e in settings["providerInstances"]["codex"]["environment"]}
        self.assertEqual(host["HOME"], "/home/spyros")
        self.assertIn("[t3env-0 history]", settings["providerInstances"]["t3env-0__codex"]["displayName"])
        for inst in settings["providerInstances"].values():
            self.assertEqual(inst["config"]["launchArgs"],
                             '-c model_providers.cliproxy.base_url="http://cliproxy.remote-pi.svc.cluster.local:8317/v1" -c x=1')
        self.assertNotIn("history", settings["providerInstances"]["codex"].get("displayName", ""))
        self.assertEqual(before, {p: m.digest(p) for p in before})
        for source in m.SOURCES:
            self.assertEqual(m.digest(self.inputs / source / "home/.codex/session.jsonl"),
                             m.digest(self.output / "homes" / source / ".codex/session.jsonl"))
        self.assertFalse(report["cutover_ready"])

    def test_collision_fails_without_publishing(self):
        self.edit("UPDATE projection_projects SET project_id='host'")
        with self.assertRaisesRegex(m.Invalid, "key collision"):
            m.merge(self.inputs, self.output)
        self.assertFalse(self.output.exists())

    def test_lag_fails(self):
        self.edit("UPDATE projection_state SET last_applied_sequence=4 WHERE projector='projection.threads'")
        with self.assertRaisesRegex(m.Invalid, "projection lag"):
            m.merge(self.inputs, self.output)

    def cleanup_watermark(self):
        with sqlite3.connect(self.output / "state.sqlite") as c:
            return c.execute("SELECT last_applied_sequence FROM projection_state "
                             "WHERE projector='projection.attachment-cleanup'").fetchone()[0]

    def test_cleanup_lag_without_effect_is_safe(self):
        self.edit("UPDATE projection_state SET last_applied_sequence=0 WHERE projector='projection.attachment-cleanup'")
        m.merge(self.inputs, self.output)

    def test_cleanup_pending_deletion_rewinds_watermark_with_offset(self):
        # t3env-0 is the second source, so its events sit after host's 5. A cleanup
        # watermark of 3 there must become 5+3 in the merged store, not 3.
        self.edit("UPDATE projection_state SET last_applied_sequence=3 WHERE projector='projection.attachment-cleanup'")
        self.edit("UPDATE orchestration_events SET event_type='thread.deleted'")
        m.merge(self.inputs, self.output)
        self.assertEqual(self.cleanup_watermark(), 8)
        with sqlite3.connect(self.output / "state.sqlite") as c:
            self.assertEqual(c.execute("SELECT count(*) FROM orchestration_events WHERE sequence>8 "
                                       "AND event_type='thread.deleted'").fetchone()[0], 1)
            self.assertEqual(c.execute("SELECT count(DISTINCT last_applied_sequence) FROM projection_state "
                                       "WHERE projector!='projection.attachment-cleanup'").fetchone()[0], 1)

    def test_cleanup_fully_consumed_gets_high_water(self):
        m.merge(self.inputs, self.output)
        self.assertEqual(self.cleanup_watermark(), 15)

    def test_cleanup_pending_revert_fails(self):
        self.edit("UPDATE projection_state SET last_applied_sequence=0 WHERE projector='projection.attachment-cleanup'")
        self.edit("UPDATE orchestration_events SET event_type='thread.reverted'")
        with self.assertRaisesRegex(m.Invalid, "pending attachment prune"):
            m.merge(self.inputs, self.output)
        self.assertFalse(self.output.exists())

    def test_schema_drift_fails(self):
        self.edit("ALTER TABLE projection_threads ADD COLUMN surprise TEXT")
        with self.assertRaisesRegex(m.Invalid, "schema differs"):
            m.merge(self.inputs, self.output)

    def test_existing_destination_untouched(self):
        self.output.mkdir()
        (self.output / "sentinel").write_text("keep")
        with self.assertRaisesRegex(m.Invalid, "already exists"):
            m.merge(self.inputs, self.output)
        self.assertEqual((self.output / "sentinel").read_text(), "keep")

    def test_symlink_fails(self):
        (self.inputs / "host/home/escape").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(m.Invalid, "symlinks"):
            m.merge(self.inputs, self.output)
        self.assertFalse(self.output.exists())

    def test_path_boundary(self):
        self.assertEqual(m.path_map("/home/t30/repo", "t3env-0"), "/home/t30/repo")
        self.assertEqual(m.path_map("/home/t3", "t3env-1"), "/home/t3env-1")


if __name__ == "__main__":
    unittest.main()
