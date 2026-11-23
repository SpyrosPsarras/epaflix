import importlib.util
import os
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("scan", HERE / "rackpeek-scan.py")
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)


class MaskText(unittest.TestCase):
    def test_mask_key_value(self):
        text = 'DB_PASSWORD=x\nexport TOKEN="y"\n{"apiKey": "z"}\npwhash: q\n'
        out = scan.mask_text(text)
        self.assertIn("DB_PASSWORD=***", out)
        self.assertIn('export TOKEN="***"', out)
        self.assertIn('"apiKey": "***"', out)
        self.assertIn("pwhash: ***", out)
        for value in ("=x", '"y"', '"z"', ": q"):
            self.assertNotIn(value, out)

    def test_mask_url_password(self):
        url = "postgres" + "://user:" + "pw@h/db"
        self.assertEqual(scan.mask_text(url), "postgres" + "://user:***@h/db")

    def test_mask_bearer(self):
        line = "Authorization: " + "Bearer " + "abc123"
        self.assertEqual(scan.mask_text(line), "Authorization: ***")

    def test_mask_cli_flag(self):
        out = scan.mask_text('qm set 1 --cipassword "abc def" --name vm1\n')
        self.assertIn("--cipassword ***", out)
        self.assertIn("--name vm1", out)
        self.assertNotIn("abc", out)

    def test_mask_url_token_segment(self):
        tok = "Ab3" * 12
        out = scan.mask_text("url https://hooks.example/api/webhooks/123/" + tok + "\n")
        self.assertNotIn(tok, out)
        self.assertIn("/123/***", out)

    def test_mask_keeps_plain(self):
        self.assertEqual(scan.mask_text("hostname=takaros\n"), "hostname=takaros\n")


class Classify(unittest.TestCase):
    def test_name_only(self):
        for path in ("/etc/ssh/sshd_config", "/root/.bashrc", "/root/.ssh/x",
                     "/etc/x/a.pem", "/etc/x/b.key", "/etc/x/c.pw",
                     "/home/u/id_ed25519", "/etc/shadow-", "/etc/gshadow", "/etc/gshadow-", "/etc/pve/priv/authkey"):
            self.assertEqual(scan.classify(path), "name-only", path)

    def test_skip_var_lib(self):
        self.assertEqual(scan.classify("/var/lib/rancher/x"), "skip")

    def test_skip_git_dirs(self):
        self.assertEqual(scan.classify("/etc/.pihole/.git/hooks/x.sample"), "skip")

    def test_read(self):
        self.assertEqual(scan.classify("/etc/hostname"), "read")


class FileEntry(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def write(self, rel, data):
        p = pathlib.Path(self.root, rel.lstrip("/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return rel

    def test_name_only_entry(self):
        rel = self.write("/etc/ssh/ssh_host_ed25519_key", b"secret")
        entry = scan.file_entry(rel, self.root)
        self.assertEqual(entry["kind"], "name-only")
        self.assertIsNone(entry["sha256"])
        self.assertIsNone(entry["content"])
        self.assertEqual(entry["size"], 6)

    def test_binary_hash_only(self):
        rel = self.write("/usr/local/bin/tool", b"\x7fELF\x00\x01")
        entry = scan.file_entry(rel, self.root)
        self.assertEqual(entry["kind"], "binary")
        self.assertEqual(len(entry["sha256"]), 64)
        self.assertIsNone(entry["content"])

    def test_large_text_no_content(self):
        rel = self.write("/etc/big.conf", b"a" * 65537)
        entry = scan.file_entry(rel, self.root)
        self.assertEqual(entry["kind"], "text")
        self.assertEqual(len(entry["sha256"]), 64)
        self.assertIsNone(entry["content"])

    def test_text_masked_and_raw_hash(self):
        raw = b"user=a\npassword=hunter2\n"
        rel = self.write("/etc/app.conf", raw)
        entry = scan.file_entry(rel, self.root)
        self.assertEqual(entry["content"], "user=a\npassword=***\n")
        self.assertTrue(entry["masked"])
        import hashlib
        self.assertEqual(entry["sha256"], hashlib.sha256(raw).hexdigest())

    def test_collect_uses_root(self):
        for rel in ("/usr/local/bin/x", "/root/notes.txt", "/root/.ssh/id_rsa",
                    "/etc/systemd/system/a.service", "/var/lib/y", "/etc/cron.d/job"):
            self.write(rel, b"x\n")
        paths = scan.collect_paths(self.root, pve=False)
        for rel in ("/usr/local/bin/x", "/root/notes.txt", "/root/.ssh/id_rsa",
                    "/etc/systemd/system/a.service", "/etc/cron.d/job"):
            self.assertIn(rel, paths)
        self.assertNotIn("/var/lib/y", paths)


class Conffiles(unittest.TestCase):
    def test_modified_conffiles(self):
        import hashlib
        root = tempfile.mkdtemp()
        for rel, data in (("etc/a.conf", b"changed"), ("etc/b.conf", b"same")):
            p = pathlib.Path(root, rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        status = pathlib.Path(root, "var/lib/dpkg/status")
        status.parent.mkdir(parents=True)
        status.write_text(
            "Package: x\nConffiles:\n /etc/a.conf " + hashlib.md5(b"orig").hexdigest()
            + "\n /etc/b.conf " + hashlib.md5(b"same").hexdigest()
            + "\n /etc/gone.conf " + hashlib.md5(b"z").hexdigest() + "\nDescription: d\n")
        self.assertEqual(scan.modified_conffiles(root), ["/etc/a.conf"])


class Main(unittest.TestCase):
    def test_ignores_original_command(self):
        os.environ["SSH_ORIGINAL_COMMAND"] = "rm -rf /"
        root = tempfile.mkdtemp()
        os.environ["RACKPEEK_SCAN_ROOT"] = root
        doc = scan.scan()
        self.assertEqual(doc["schema"], 1)
        self.assertIsInstance(doc["errors"], list)
        del os.environ["SSH_ORIGINAL_COMMAND"]
        del os.environ["RACKPEEK_SCAN_ROOT"]


if __name__ == "__main__":
    unittest.main()
