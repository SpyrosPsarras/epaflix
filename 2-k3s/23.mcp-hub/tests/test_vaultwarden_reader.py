"""Restricted reader core through actual subprocesses. No real CLI or vault."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

FILES = Path(__file__).resolve().parents[1] / "files"
sys.path.insert(0, str(FILES))
import vaultwarden_reader as reader

USER = "00000000-0000-4000-8000-000000000001"
ORG = "00000000-0000-4000-8000-000000000002"
COLLECTION = "00000000-0000-4000-8000-000000000003"
OTHER_COLLECTION = "00000000-0000-4000-8000-000000000004"
ITEM = "00000000-0000-4000-8000-000000000005"
OTHER_ITEM = "00000000-0000-4000-8000-000000000006"
OTHER_ORG = "00000000-0000-4000-8000-000000000007"
REF = {"collection_id": COLLECTION, "item_id": ITEM}


def item(identifier=ITEM, name="demo"):
    return {"object": "item", "id": identifier, "organizationId": ORG, "collectionIds": [COLLECTION],
            "folderId": OTHER_COLLECTION, "name": name, "type": 1, "deletedDate": None,
            "notes": "synthetic-note-secret", "login": {"username": "synthetic-user-secret",
            "password": "synthetic-password-secret", "totp": "synthetic-totp-secret",
            "uris": [{"uri": "https://synthetic-url-secret.example.test"}]},
            "fields": [{"name": "api token", "type": 1, "value": "synthetic-field-secret"}],
            "passwordHistory": [{"password": "synthetic-history-secret"}],
            "sshKey": {"privateKey": "synthetic-key-secret"},
            "card": {"number": "synthetic-card-secret"},
            "attachments": [{"id": "a" * 32, "fileName": "demo.txt", "size": "25",
                             "sizeName": "25 bytes", "url": "https://synthetic-download-secret.example.test"}]}


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="restricted-reader-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        executable = self.root / "bw"
        shutil.copyfile(Path(__file__).parent / "fixtures" / "restricted_bw.py", executable)
        executable.chmod(0o700)
        credentials = self.root / "credentials"
        credentials.mkdir(mode=0o700)
        for name, value in (("client-id", "user." + USER), ("client-secret", "synthetic-api-secret"),
                            ("password", "synthetic-master-password")):
            (credentials / name).write_bytes(value.encode("utf-8"))
            (credentials / name).chmod(0o600)
        scratch = self.root / "scratch"
        scratch.mkdir(mode=0o700)
        self.config = reader.ReaderConfig("https://vault.homelab.example.test", USER, ORG, (COLLECTION,),
                                          credentials, scratch, executable)
        self.reader = reader.RestrictedVaultReader(self.config)
        self.scenario = {"user_id": USER, "identity_login_limit": 1000, "organizations": [{"object": "organization", "id": ORG,
                         "name": "homelab", "type": 2, "status": 2, "enabled": True}],
                         "collections": [{"object": "collection", "id": COLLECTION, "organizationId": ORG,
                         "name": "homelab", "externalId": None}], "items": [item()]}
        self.save()
        self.addCleanup(self.assert_clean)

    def save(self):
        (self.root / "scenario.json").write_text(json.dumps(self.scenario))

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_clean(self):
        self.reader.close()
        self.assertEqual(list(self.config.scratch_dir.iterdir()), [])
        self.assertTrue(all(not Path(c["home"]).exists() for c in self.calls()))

    def assert_safe_failure(self, operation, *args, **kwargs):
        with self.assertRaises((ValueError, RuntimeError)) as error:
            operation(*args, **kwargs)
        self.assertNotIn("synthetic-", str(error.exception))
        return str(error.exception)

    def test_metadata_get_and_listing_are_allowlisted(self):
        got = self.reader.get(REF)
        self.assertEqual(got, {"references": [REF], "title": "demo", "type": 1, "has_password": True,
                              "custom_fields": ["api token"],
                              "attachments": [{"id": "a" * 32, "filename": "demo.txt"}]})
        self.assertEqual(self.reader.list_items(), [got])
        self.assertEqual(self.reader.list_items(COLLECTION), [got])
        self.assertNotIn("synthetic-", json.dumps(got))

    def test_secret_has_broker_payload_not_notes_or_other_type_secrets(self):
        self.assertEqual(self.reader.secret(REF), {"password": "synthetic-password-secret",
                         "username": "synthetic-user-secret", "fields": {"api token": "synthetic-field-secret"}})

    def test_non_login_metadata_and_fields_only_secret(self):
        self.scenario["items"][0].update(type=2, login=None)
        self.save()
        self.assertFalse(self.reader.get(REF)["has_password"])
        self.assertEqual(self.reader.secret(REF), {"password": None, "username": None,
                                                "fields": {"api token": "synthetic-field-secret"}})

    def test_empty_listing_with_present_grant(self):
        self.scenario["items"] = []
        self.save()
        self.assertEqual(self.reader.list_items(), [])
        self.assert_safe_failure(self.reader.get, REF)

    def test_exact_title_and_ambiguous_title(self):
        title_ref = {"collection_id": COLLECTION, "title": "demo"}
        self.assertEqual(self.reader.get(title_ref)["references"], [REF])
        self.scenario["items"].append(item(OTHER_ITEM))
        self.save()
        self.assertIn("ambiguous", self.assert_safe_failure(self.reader.get, title_ref))
        self.assertEqual(self.reader.get(REF)["references"], [REF])
        self.assert_safe_failure(self.reader.get, {"collection_id": COLLECTION, "title": "dem"})

    def test_all_secret_routes_reject_known_out_of_scope_id(self):
        for changes in ({"organizationId": None}, {"organizationId": OTHER_ORG},
                        {"collectionIds": []}, {"collectionIds": [OTHER_COLLECTION]},
                        {"collectionIds": [COLLECTION, OTHER_COLLECTION]}, {"deletedDate": "2020-01-01"},
                        {"archivedDate": "2020-01-01"}):
            with self.subTest(changes=changes):
                bad = item(OTHER_ITEM, "outside")
                bad.update(changes)
                self.scenario["items"] = [item(), bad]
                self.save()
                outside = {"collection_id": COLLECTION, "item_id": OTHER_ITEM}
                self.assertEqual(len(self.reader.list_items()), 1)
                self.assert_safe_failure(self.reader.get, outside)
                self.assert_safe_failure(self.reader.secret, outside)
                self.assert_safe_failure(self.reader.attachment, outside, "demo.txt")
        self.assertFalse(any(c["args"][0] == "get" for c in self.calls()))

    def test_multiple_approved_visible_collections_require_assigned_selection(self):
        self.config = replace(self.config, collection_ids=(COLLECTION, OTHER_COLLECTION))
        self.reader = reader.RestrictedVaultReader(self.config)
        self.scenario["collections"].append({"id": OTHER_COLLECTION, "organizationId": ORG,
                                            "name": "another", "externalId": None, "object": "collection"})
        self.scenario["items"][0]["collectionIds"].append(OTHER_COLLECTION)
        self.save()
        second_ref = {"collection_id": OTHER_COLLECTION, "item_id": ITEM}
        self.assertEqual(self.reader.get(second_ref)["references"], [REF, second_ref])
        self.assertEqual(len(self.reader.list_items()), 1)
        self.scenario["items"][0]["collectionIds"] = [COLLECTION]
        self.save()
        self.assert_safe_failure(self.reader.secret, second_ref)

    def test_folder_never_authorizes(self):
        self.scenario["items"][0].update(collectionIds=[OTHER_COLLECTION], folderId=COLLECTION)
        self.save()
        self.assertEqual(self.reader.list_items(), [])
        self.assert_safe_failure(self.reader.secret, REF)

    def test_references_fail_before_cli_without_legacy_fallback(self):
        for reference in (None, "/homelab/demo", ITEM, {}, {"item_id": ITEM},
                          {"collection_id": OTHER_COLLECTION, "item_id": ITEM},
                          {**REF, "title": "demo"}, {**REF, "password": "synthetic"},
                          {"collection_id": COLLECTION, "item_id": "--help"},
                          {"collection_id": COLLECTION, "title": ""}):
            with self.subTest(reference=reference):
                self.assert_safe_failure(self.reader.get, reference)
                self.assert_safe_failure(self.reader.secret, reference)
                self.assert_safe_failure(self.reader.attachment, reference, "demo.txt")
        self.assert_safe_failure(self.reader.list_items, OTHER_COLLECTION)
        self.assertEqual(self.calls(), [])

    def test_missing_revoked_or_extra_visible_grants_reject_every_read(self):
        for collections in ([], [self.scenario["collections"][0], {"id": OTHER_COLLECTION, "organizationId": ORG}],
                            [{"id": COLLECTION, "organizationId": OTHER_ORG}]):
            self.scenario["collections"] = collections
            self.save()
            for operation, args in ((self.reader.list_items, ()), (self.reader.get, (REF,)),
                                    (self.reader.secret, (REF,)), (self.reader.attachment, (REF, "demo.txt"))):
                self.reader.invalidate()
                self.assertIn("grants mismatch", self.assert_safe_failure(operation, *args))

    def test_success_then_revocation_does_not_serve_cached_secret(self):
        self.reader.secret(REF)
        self.scenario["collections"] = []
        self.save()
        self.assert_safe_failure(self.reader.secret, REF)
        self.assertEqual(len({c["home"] for c in self.calls()}), 1)

    def test_identity_server_and_lock_checks_before_and_after_sync(self):
        for stage in ("status_before", "status_after"):
            for changes in ({"userId": OTHER_ITEM}, {"serverUrl": "https://other.example.test"},
                            {"status": "locked"}, {"status": "unauthenticated"}):
                with self.subTest(stage=stage, changes=changes):
                    self.reader.invalidate()
                    self.scenario.pop("status_before", None)
                    self.scenario.pop("status_after", None)
                    self.scenario[stage] = changes
                    self.save()
                    self.assertIn("mismatch", self.assert_safe_failure(self.reader.secret, REF))
        self.assertFalse(any(c["args"][0] == "list" for c in self.calls()))

    def test_organization_role_confirmation_and_extra_membership_checks(self):
        original = dict(self.scenario["organizations"][0])
        for changes in ({"type": 0}, {"type": 1}, {"type": 3}, {"type": 4},
                        {"status": 0}, {"status": 1}, {"status": -1}, {"enabled": False}, {"id": OTHER_ORG}):
            self.reader.invalidate()
            self.scenario["organizations"] = [{**original, **changes}]
            self.save()
            self.assertIn("membership mismatch", self.assert_safe_failure(self.reader.secret, REF))
        for memberships in ([], [original, {**original, "id": OTHER_ORG}]):
            self.reader.invalidate()
            self.scenario["organizations"] = memberships
            self.save()
            self.assert_safe_failure(self.reader.list_items)

    def test_refresh_failed_stale_or_malformed_never_returns_previous_item(self):
        self.reader.secret(REF)
        for change in ({"fail": "sync"}, {"stale_sync": True},
                       {"status_after": {"lastSync": None}}, {"status_after": {"lastSync": "2020-01-01T00:00:00Z"}},
                       {"status_after": {"lastSync": "not-a-date"}},
                       {"status_after": {"lastSync": "2999-01-01T00:00:00Z"}}, {"malformed": "items"}):
            self.reader.invalidate()
            self.scenario = {k: v for k, v in self.scenario.items()
                             if k not in ("fail", "stale_sync", "status_after", "malformed")}
            self.scenario.update(change)
            self.save()
            self.assert_safe_failure(self.reader.secret, REF)

    def test_cli_failures_never_include_cli_output(self):
        for command in ("config", "login", "unlock", "status", "sync", "list", "get"):
            self.reader.invalidate()
            self.scenario["fail"] = command
            self.save()
            self.assert_safe_failure(self.reader.attachment, REF, "demo.txt")

    def test_empty_unlock_session_fails(self):
        self.scenario["empty_session"] = True
        self.save()
        self.assert_safe_failure(self.reader.secret, REF)

    def test_unavailable_cli_and_missing_credentials_fail_closed(self):
        self.config.executable.unlink()
        self.assert_safe_failure(self.reader.secret, REF)
        shutil.copyfile(Path(__file__).parent / "fixtures" / "restricted_bw.py", self.config.executable)
        self.config.executable.chmod(0o700)
        (self.config.credentials_dir / "client-secret").unlink()
        self.assert_safe_failure(self.reader.secret, REF)

    def test_malformed_item_metadata_fields_and_duplicate_ids_fail(self):
        for changes in ({"id": "bad-id"}, {"name": None}, {"type": True}, {"fields": [None]},
                        {"fields": [{"name": "token", "value": {"secret": "synthetic"}}]}, {"login": "synthetic"}):
            self.reader.invalidate()
            self.scenario["items"] = [{**item(), **changes}]
            self.save()
            self.assert_safe_failure(self.reader.list_items)
        self.reader.invalidate()
        self.scenario["items"] = [item(), item()]
        self.save()
        self.assert_safe_failure(self.reader.secret, REF)

    def test_duplicate_fields_are_metadata_names_but_secret_fails(self):
        self.scenario["items"][0]["fields"].append({"name": "api token", "value": "synthetic-other-secret", "type": 0})
        self.save()
        self.assertEqual(self.reader.get(REF)["custom_fields"], ["api token"])
        self.assertIn("duplicate", self.assert_safe_failure(self.reader.secret, REF))

    def test_attachment_by_id_and_name_is_item_bound_and_binary(self):
        expected = {"filename": "demo.txt", "content": b"synthetic-attachment-bytes\0\xff"}
        self.assertEqual(self.reader.attachment(REF, "demo.txt"), expected)
        self.assertEqual(self.reader.attachment(REF, "a" * 32), expected)
        downloads = [c["args"] for c in self.calls() if c["args"][0] == "get"]
        self.assertEqual(downloads, [["get", "attachment", "a" * 32, "--itemid", ITEM, "--raw"]] * 2)

    def test_attachment_from_another_item_and_fuzzy_name_fail(self):
        self.scenario["items"].append(item(OTHER_ITEM, "other"))
        self.scenario["items"][1]["attachments"][0].update(id="b" * 32, fileName="other.txt")
        self.save()
        for name in ("b" * 32, "other.txt", "demo", "DEMO.TXT"):
            self.assert_safe_failure(self.reader.attachment, REF, name)
        self.assertFalse(any(c["args"][0] == "get" for c in self.calls()))

    def test_attachment_duplicate_names_and_cli_matching_collision_fail(self):
        attachments = self.scenario["items"][0]["attachments"]
        attachments.append({"id": "b" * 32, "fileName": "demo.txt", "size": "10"})
        self.save()
        self.assert_safe_failure(self.reader.attachment, REF, "demo.txt")
        attachments[1]["fileName"] = "a" * 32
        self.save()
        self.assertIn("collides", self.assert_safe_failure(self.reader.attachment, REF, "demo.txt"))
        self.assertFalse(any(c["args"][0] == "get" for c in self.calls()))

    def test_unsafe_attachment_paths_reject_before_cli(self):
        for value in ("../demo.txt", "/tmp/demo.txt", "a\\b", "..", ".", "demo\n.txt", "", "demo\0.txt"):
            self.assert_safe_failure(self.reader.attachment, REF, value)
        self.assertEqual(self.calls(), [])
        self.scenario["items"][0]["attachments"][0]["fileName"] = "../outside"
        self.save()
        self.assert_safe_failure(self.reader.attachment, REF, "a" * 32)

    def test_attachment_ids_cannot_inject_cli_options(self):
        for identifier in ("--help", "--output", "../outside", "a/b", "a\n"):
            self.reader.invalidate()
            self.scenario["items"][0]["attachments"][0]["id"] = identifier
            self.save()
            self.assert_safe_failure(self.reader.get, REF)
            self.assert_safe_failure(self.reader.attachment, REF, "demo.txt")
        self.assertFalse(any(c["args"][0] == "get" for c in self.calls()))

    def test_temporary_state_and_environment_are_independent_of_ambient_bw(self):
        ambient = {"BW_SESSION": "personal-session", "BW_CLIENTID": "personal-client", "BW_CLIENTSECRET": "personal-secret",
                   "BW_PASSWORD": "personal-password", "BW_SERVE_URL": "http://personal.example.test",
                   "BW_CLEANEXIT": "true", "BW_RESPONSE": "true", "BW_RAW": "true", "BW_QUIET": "true",
                   "BITWARDENCLI_APPDATA_DIR": "/personal-cache", "HOME": "/personal-home",
                   "NODE_OPTIONS": "--bad-option", "HTTP_PROXY": "http://bad.example.test", "EXTRA_PRIVATE": "private"}
        with patch.dict(os.environ, ambient):
            self.reader.get(REF)
            self.reader.secret(REF)
        calls = self.calls()
        self.assertEqual(len({c["home"] for c in calls}), 1)
        for call in calls:
            self.assertEqual(call["mode"], 0o700)
            self.assertEqual(call["home"], call["appdata"])
            self.assertFalse(set(ambient).difference({"HOME", "BITWARDENCLI_APPDATA_DIR", "BW_SESSION",
                             "BW_CLIENTID", "BW_CLIENTSECRET", "BW_PASSWORD"}).intersection(call["env_keys"]))
            self.assertTrue(set(call["env_keys"]).issubset({"PATH", "HOME", "TMPDIR", "BITWARDENCLI_APPDATA_DIR",
                            "BW_NOINTERACTION", "BW_SESSION", "BW_CLIENTID", "BW_CLIENTSECRET", "BW_PASSWORD", "LC_CTYPE"}))

    def test_concurrent_reads_serialize_one_cli_home(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.reader.get(REF), range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(len({c["home"] for c in self.calls()}), 1)
        commands = [c["args"][0] for c in self.calls()]
        self.assertEqual(commands.count("login"), 1)
        self.assertEqual(commands.count("reader-sync"), 2)
        self.assertEqual(commands[-5:], ["reader-sync", "status", "list", "list", "list"])

    def test_output_and_stderr_limits_cleanup(self):
        for key, command in (("overflow", "list"), ("stderr_overflow", "status"), ("overflow", "get")):
            self.reader.invalidate()
            self.scenario.pop("overflow", None)
            self.scenario.pop("stderr_overflow", None)
            self.scenario[key] = command
            self.save()
            self.assertIn("limit exceeded", self.assert_safe_failure(self.reader.attachment, REF, "demo.txt"))
            self.assertEqual(list(self.config.scratch_dir.iterdir()), [])

    def test_command_and_total_operation_timeouts_cleanup(self):
        self.scenario["hang"] = "sync"
        self.save()
        started = time.monotonic()
        with patch.object(reader, "COMMAND_TIMEOUT", 0.5):
            self.assertIn("timed out", self.assert_safe_failure(self.reader.secret, REF))
        self.assertLess(time.monotonic() - started, 3)
        self.reader.invalidate()
        with patch.object(reader, "OPERATION_TIMEOUT", 0):
            self.assertIn("timed out", self.assert_safe_failure(self.reader.list_items))

    def test_scratch_root_must_be_private(self):
        self.config.scratch_dir.chmod(0o755)
        self.assert_safe_failure(self.reader.secret, REF)
        self.assertEqual(self.calls(), [])

    def test_invalid_configuration_has_no_defaults(self):
        self.assert_safe_failure(reader.RestrictedVaultReader, None)
        for changes in ({"server_url": ""}, {"server_url": "http://vault.example.test"},
                        {"server_url": "https://user:password@example.test"}, {"server_url": "https://example.test?x=1"},
                        {"user_id": ""}, {"organization_id": ""}, {"collection_ids": ()},
                        {"collection_ids": (COLLECTION, COLLECTION)}, {"collection_ids": [COLLECTION]},
                        {"collection_ids": ([COLLECTION],)}, {"server_url": "https://bad host.example.test"},
                        {"credentials_dir": Path("relative")}, {"executable": Path("bw")}):
            with self.subTest(changes=changes):
                self.assert_safe_failure(replace, self.config, **changes)
        self.assertEqual(self.calls(), [])

    def test_exact_utf8_password_whitespace_and_invalid_values(self):
        for value in ("  synthetic-passphrase  ", "\tsynthetic\r\n", "synthetic-κλειδί\n", " \t\n"):
            with self.subTest(value=value):
                self.scenario["expected_password"] = value
                self.save()
                (self.config.credentials_dir / "password").write_bytes(value.encode("utf-8"))
                self.reader.secret(REF)
        for data in (b"", b"invalid\0password", b"\xff", b"x" * 16385):
            (self.config.credentials_dir / "password").write_bytes(data)
            before = len(self.calls())
            self.assert_safe_failure(self.reader.secret, REF)
            self.assertEqual(len(self.calls()), before)

    def test_credentials_are_private_regular_files_with_supported_symlinks(self):
        path = self.config.credentials_dir / "password"
        path.chmod(0o640)
        self.assert_safe_failure(self.reader.secret, REF)
        self.assertEqual(self.calls(), [])
        path.unlink()
        os.mkfifo(path, 0o600)
        started = time.monotonic()
        self.assert_safe_failure(self.reader.secret, REF)
        self.assertLess(time.monotonic() - started, 0.5)
        path.unlink()
        actual = self.config.credentials_dir / "projected-password"
        actual.write_bytes(b"synthetic-master-password")
        actual.chmod(0o600)
        path.symlink_to(actual)
        self.reader.secret(REF)
        self.assertEqual(sum(c["args"][0] == "login" for c in self.calls()), 1)

    def test_client_id_account_binding_precedes_login(self):
        for value in ("synthetic-client", "user." + OTHER_ITEM, "user." + USER + "\n", " user." + USER):
            (self.config.credentials_dir / "client-id").write_text(value)
            self.assert_safe_failure(self.reader.secret, REF)
        self.assertEqual(self.calls(), [])

    def test_locked_identity_checked_before_unlock_and_data(self):
        for change in ({"userId": OTHER_ITEM}, {"serverUrl": "https://other.example.test"},
                       {"status": "unlocked"}, {"status": "unauthenticated"}):
            self.reader.invalidate()
            self.scenario["status_locked"] = change
            self.save()
            self.assert_safe_failure(self.reader.secret, REF)
        self.assertFalse(any(c["args"][0] in ("unlock", "reader-sync", "list", "get") for c in self.calls()))

    def test_read_burst_reuses_cli_home_and_explicit_login(self):
        self.scenario["login_limit"] = 10
        self.save()
        for _ in range(11):
            self.reader.get(REF)
        self.assertEqual(sum(c["args"][0] == "login" for c in self.calls()), 1)
        self.assertEqual(sum(c["args"][0] == "reader-sync" for c in self.calls()), 11)
        self.assertEqual(len({c["home"] for c in self.calls()}), 1)
        identities = [json.loads(line) for line in (self.root / "identity-calls.jsonl").read_text().splitlines()]
        self.assertEqual(len(identities), 1)
        self.assertEqual(sum(c["command"] == "sync" for c in identities), 0)
        self.assert_safe_failure(self.reader.get, {"collection_id": COLLECTION, "title": "absent"})
        self.reader.get(REF)
        self.assertEqual(sum(c["args"][0] == "login" for c in self.calls()), 1)

    def test_read_burst_must_survive_pinned_default_identity_login_budget(self):
        self.scenario["identity_login_limit"] = 10
        self.save()
        for _ in range(11):
            self.reader.get(REF)
        self.assertEqual(sum(c["args"][0] == "login" for c in self.calls()), 1)
        self.assertEqual(sum(c["args"][0] == "reader-sync" for c in self.calls()), 11)
        self.assertEqual(len((self.root / "identity-calls.jsonl").read_text().splitlines()), 1)
        self.assertEqual(sum(c["args"] == ["--reader-capabilities"] for c in self.calls()), 1)

    def test_expiry_renews_token_without_repeating_explicit_login(self):
        self.reader.secret(REF)
        self.scenario["token_expired"] = True
        self.save()
        self.reader.secret(REF)
        identities = [json.loads(line) for line in (self.root / "identity-calls.jsonl").read_text().splitlines()]
        self.assertEqual([c["command"] for c in identities], ["login", "sync"])
        self.assertEqual(sum(c["args"][0] == "login" for c in self.calls()), 1)
        self.assertEqual(sum(c["args"][0] == "reader-sync" for c in self.calls()), 2)

    def test_failed_expiry_renewal_latches_without_cached_snapshot(self):
        self.scenario["identity_login_limit"] = 1
        self.save()
        self.reader.secret(REF)
        self.scenario["token_expired"] = True
        self.save()
        self.assert_safe_failure(self.reader.secret, REF)
        before = len(self.calls())
        self.assert_safe_failure(self.reader.secret, REF)
        self.assertEqual(len(self.calls()), before)
        self.assertEqual(list(self.config.scratch_dir.iterdir()), [])

    def test_stock_or_wrong_bridge_capability_rejected_before_login(self):
        for value in ({}, {**reader.BRIDGE_CAPABILITY, "client_version": "0.0.0"},
                      {**reader.BRIDGE_CAPABILITY, "bundle_sha256": "0" * 64},
                      {**reader.BRIDGE_CAPABILITY, "bridge": "stock"},
                      {**reader.BRIDGE_CAPABILITY, "bridge": "restricted-sync-v1"}):
            with self.subTest(value=value):
                self.reader.invalidate()
                self.scenario["bridge_capability"] = value
                self.save()
                before = len(self.calls())
                self.assert_safe_failure(self.reader.secret, REF)
                self.assertEqual([c["args"] for c in self.calls()[before:]], [["--reader-capabilities"]])
                self.assertEqual(list(self.config.scratch_dir.iterdir()), [])

    def test_login_or_unlock_refusal_latches_without_returning_data_or_retry(self):
        for stage in ("login", "unlock"):
            with self.subTest(stage=stage):
                self.reader.invalidate()
                self.scenario["fail"] = stage
                self.save()
                before = len(self.calls())
                self.assertEqual(self.assert_safe_failure(self.reader.get, REF), "restricted vault command failed")
                sequence = [c["args"][0] for c in self.calls()[before:]]
                self.assertEqual(sequence, ["--reader-capabilities", "config", "login"] +
                                 (["status", "unlock"] if stage == "unlock" else []))
                self.scenario.pop("fail")
                self.save()
                count = len(self.calls())
                for operation, args in ((self.reader.list_items, ()), (self.reader.get, (REF,)),
                                        (self.reader.secret, (REF,)), (self.reader.attachment, (REF, "demo.txt"))):
                    self.assertEqual(self.assert_safe_failure(operation, *args), "restricted vault session invalidated")
                self.assertEqual(len(self.calls()), count)
                self.assertEqual(list(self.config.scratch_dir.iterdir()), [])

    def test_separate_instances_never_share_home_or_session_state(self):
        other = reader.RestrictedVaultReader(self.config)
        try:
            self.reader.get(REF)
            other.get(REF)
            self.assertEqual(len({c["home"] for c in self.calls()}), 2)
            self.assertEqual(sum(c["args"][0] == "login" for c in self.calls()), 2)
            other.close()
            self.reader.get(REF)
            self.assertEqual(len(list(self.config.scratch_dir.iterdir())), 1)
        finally:
            other.close()

    def test_failure_invalidates_without_login_retry_loop(self):
        self.reader.secret(REF)
        self.scenario["fail"] = "sync"
        self.save()
        self.assert_safe_failure(self.reader.secret, REF)
        before = len(self.calls())
        self.scenario.pop("fail")
        self.save()
        for _ in range(11):
            self.assert_safe_failure(self.reader.secret, REF)
        self.assertEqual(len(self.calls()), before)
        self.assertEqual(list(self.config.scratch_dir.iterdir()), [])
        self.reader.invalidate()
        self.reader.secret(REF)
        self.assertEqual(sum(c["args"][0] == "login" for c in self.calls()), 2)

    def test_close_interrupts_child_and_prevents_reopening(self):
        self.scenario["hang"] = "sync"
        self.save()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.reader.get, REF)
            end = time.monotonic() + 3
            while not any(c["args"][0] == "reader-sync" for c in self.calls()) and time.monotonic() < end:
                time.sleep(0.01)
            self.assertTrue(any(c["args"][0] == "reader-sync" for c in self.calls()))
            started = time.monotonic()
            self.reader.close()
            self.assertLess(time.monotonic() - started, 1)
            with self.assertRaises(RuntimeError):
                pending.result()
        before = len(self.calls())
        self.reader.invalidate()
        self.assert_safe_failure(self.reader.get, REF)
        self.assertEqual(len(self.calls()), before)
        for call in self.calls():
            with self.assertRaises(ProcessLookupError):
                os.kill(call["pid"], 0)

    def test_close_interrupts_child_after_output_pipes_close(self):
        self.scenario["silent_hang"] = "sync"
        self.save()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.reader.get, REF)
            end = time.monotonic() + 3
            while not any(c["args"][0] == "reader-sync" for c in self.calls()) and time.monotonic() < end:
                time.sleep(0.01)
            self.assertTrue(any(c["args"][0] == "reader-sync" for c in self.calls()))
            time.sleep(0.1)
            started = time.monotonic()
            self.reader.close()
            self.assertLess(time.monotonic() - started, 1)
            with self.assertRaises(RuntimeError):
                pending.result()
        self.assertEqual(list(self.config.scratch_dir.iterdir()), [])

    def test_cli_descendants_cannot_outlive_a_completed_command(self):
        self.scenario["orphan"] = "get"
        self.save()
        self.reader.attachment(REF, "demo.txt")
        pid = int((self.root / "descendant.pid").read_text())

        def cleanup():
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass

        self.addCleanup(cleanup)
        status = Path(f"/proc/{pid}/stat")
        end = time.monotonic() + 1
        while status.exists() and status.read_text().split()[2] != "Z" and time.monotonic() < end:
            time.sleep(0.01)
        self.assertTrue(not status.exists() or status.read_text().split()[2] == "Z",
                        "CLI descendants must be terminated even after a successful parent exit")

    def test_no_writes_or_general_cli_interface(self):
        for name in ("create", "update", "trash", "upload", "execute", "login", "serve"):
            self.assertFalse(hasattr(self.reader, name))
        self.reader.get(REF)
        self.reader.secret(REF)
        self.reader.attachment(REF, "demo.txt")
        self.assertTrue(all(c["args"][0] in {"--reader-capabilities", "config", "login", "unlock", "status", "reader-sync", "list", "get"}
                            for c in self.calls()))


if __name__ == "__main__":
    unittest.main()
