#!/usr/bin/env python3
"""Offline credential-loading checks with synthetic Bitwarden and Docker clients."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("minio-setup.sh").resolve()
ROOT = SCRIPT.parents[2]


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".minio-credentials-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.values = ["synthetic-admin-user", "synthetic-admin-password", "synthetic-app-user", "synthetic-app-password"]
        self.folders = [{"id": "admin-folder", "name": "Personal"}]
        self.collections = [{"id": "app-collection", "name": "homelab"}]
        self.items = [
            {"name": name, "folderId": folder, "collectionIds": collections, "type": 1,
             "login": dict(zip(("username", "password"), self.values[index:index + 2]))}
            for name, folder, collections, index in (
                ("MinIO Object Store", "admin-folder", [], 0),
                ("fytorium - minio", None, ["app-collection"], 2))]
        self.env = dict(os.environ, PATH=str(self.home) + os.pathsep + os.environ["PATH"],
                        BW_SESSION="synthetic-owner-session", TEST_DIR=str(self.home))
        self.stub("bw", '''import json, os, sys
from pathlib import Path
home = Path(os.environ["TEST_DIR"])
assert sys.argv[1] == "--nointeraction"
args = sys.argv[2:]
assert args in (["status"], ["list", "folders"], ["list", "collections"],
                ["list", "items", "--folderid", "admin-folder", "--search", "MinIO Object Store"],
                ["list", "items", "--collectionid", "app-collection", "--search", "fytorium - minio"])
if os.environ.get("TEST_BW_ERROR"):
    print("synthetic-admin-password", file=sys.stderr)
    sys.exit(1)
kind = args[0] if args == ["status"] else args[1]
print((home / (kind + ".json")).read_text())
''')
        self.stub("docker", '''import json, os, sys
from pathlib import Path
home = Path(os.environ["TEST_DIR"])
values = json.loads((home / "values.json").read_text())
keys = ("MINIO_ADMIN_USERNAME", "MINIO_ADMIN_PASSWORD", "VAULT_USERNAME", "VAULT_PASSWORD")
assert [os.environ.get(key) for key in keys] == values
assert all(value not in sys.argv for value in values[1::2])
(home / "applied").touch()
if "create" in sys.argv or "add" in sys.argv:
    data = sys.stdin.read()
    if "add" in sys.argv:
        assert data == "\\n".join(values[2:]) + "\\n"
if "info" in sys.argv:
    print('{"policyName": "fytorium-photos-rw"}')
else:
    print(values[1], file=sys.stderr)
''')
        (self.home / "values.json").write_text(json.dumps(self.values))
        self.write_fixtures()

    def stub(self, name, code):
        path = self.home / name
        path.write_text("#!/usr/bin/env python3\n" + code)
        path.chmod(0o700)

    def write_fixtures(self, status="unlocked"):
        for name, data in (("status", {"status": status}), ("folders", self.folders),
                           ("collections", self.collections), ("items", self.items)):
            (self.home / (name + ".json")).write_text(json.dumps(data))

    def run_setup(self, success=False):
        result = subprocess.run(["bash", str(SCRIPT)], env=self.env, capture_output=True, text=True, timeout=10)
        self.assertTrue(all(value not in result.stdout + result.stderr for value in self.values))
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(result.returncode, 0 if success else 1)
        self.assertEqual((self.home / "applied").exists(), success)
        return result

    def test_exact_logins_reach_apply_without_output_leaks(self):
        self.items.append({**self.items[0], "folderId": "unrelated-folder"})
        self.write_fixtures()
        result = self.run_setup(success=True)
        self.assertEqual(result.stdout, "MinIO ready: private fytorium-photos bucket, versioning suspended, app policy attached.\n")
        self.assertEqual(result.stderr, "")

    def test_locked_session_is_rejected(self):
        self.write_fixtures(status="locked")
        result = self.run_setup()
        self.assertEqual(result.stderr, "Bitwarden CLI must be installed and unlocked with BW_SESSION exported.\n")

    def test_missing_session_is_rejected(self):
        self.env.pop("BW_SESSION")
        self.run_setup()

    def test_missing_cli_is_rejected(self):
        (self.home / "bw").unlink()
        for name in ("bash", "python3", "dirname"):
            (self.home / name).symlink_to(shutil.which(name))
        self.env["PATH"] = str(self.home)
        self.run_setup()

    def test_ambiguous_or_missing_match_is_rejected(self):
        for kind in ("missing-folder", "duplicate-folder", "missing-item", "duplicate-item", "wrong-type", "empty-password"):
            with self.subTest(kind=kind):
                folders = json.loads(json.dumps(self.folders))
                items = json.loads(json.dumps(self.items))
                if kind == "missing-folder":
                    folders.pop()
                elif kind == "duplicate-folder":
                    folders.append(folders[0])
                elif kind == "missing-item":
                    items.pop()
                elif kind == "duplicate-item":
                    items.append(items[0])
                elif kind == "wrong-type":
                    items[0]["type"] = 2
                else:
                    items[0]["login"]["password"] = ""
                (self.home / "folders.json").write_text(json.dumps(folders))
                (self.home / "items.json").write_text(json.dumps(items))
                self.run_setup()

    def test_app_item_in_folder_only_is_rejected(self):
        self.folders.append({"id": "app-folder", "name": "homelab"})
        self.items[1].update(folderId="app-folder", collectionIds=None)
        self.write_fixtures()
        self.run_setup()

    def test_duplicate_collection_names_are_rejected(self):
        self.collections.append({"id": "decoy-collection", "name": "homelab"})
        self.write_fixtures()
        self.run_setup()

    def test_app_item_in_another_collection_is_rejected(self):
        self.items[1]["collectionIds"] = ["decoy-collection"]
        self.write_fixtures()
        self.run_setup()

    def test_cli_failure_withholds_diagnostics(self):
        self.env["TEST_BW_ERROR"] = "1"
        result = self.run_setup()
        self.assertEqual(result.stderr, "Bitwarden credential lookup failed; expected one login per folder or collection and item.\n")


if __name__ == "__main__":
    unittest.main()
