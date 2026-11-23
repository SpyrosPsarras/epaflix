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
        self.assertIn('export TOKEN="***', out)
        self.assertIn('"apiKey": "***', out)
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

    def test_mask_systemd_environment(self):
        out = scan.mask_text('Environment="DB_PASSWORD=a b c"\nEnvironment=API_TOKEN=xyz\n')
        for v in ("a b c", "xyz"):
            self.assertNotIn(v, out)

    def test_mask_comma_value(self):
        out = scan.mask_text("password=ab,cd\n")
        self.assertNotIn("cd", out)
        self.assertNotIn("ab", out)

    def test_mask_yaml_block(self):
        out = scan.mask_text("db:\n  password: |\n    s3cr3t\n    more\n  host: h\n")
        self.assertNotIn("s3cr3t", out)
        self.assertNotIn("more", out)
        self.assertIn("host: h", out)

    def test_mask_yaml_block_blank_line_and_comment(self):
        out = scan.mask_text("password: | # note\n  aaa1\n\n  bbb2\nnext: ok\n")
        self.assertNotIn("aaa1", out)
        self.assertNotIn("bbb2", out)
        self.assertIn("next: ok", out)

    def test_mask_continued_line(self):
        out = scan.mask_text('Environment="API_TOKEN=ccc3 \\\n  ddd4"\nExecStart=/bin/x\n')
        self.assertNotIn("ccc3", out)
        self.assertNotIn("ddd4", out)
        self.assertIn("ExecStart=/bin/x", out)

    def test_mask_yaml_indent_indicator(self):
        out = scan.mask_text("password: |2\n  eee5\nnext: ok\n")
        self.assertNotIn("eee5", out)
        self.assertIn("next: ok", out)

    def test_mask_continuation_across_comment(self):
        out = scan.mask_text('Environment=API_TOKEN=fff6 \\\n# c\n  ggg7\nExecStart=/bin/x\n')
        self.assertNotIn("ggg7", out)
        self.assertIn("ExecStart=/bin/x", out)

    def test_mask_comment_inside_continuation(self):
        out = scan.mask_text('Environment=API_TOKEN=a1 \\\n# password: hhh8\n  b2\n')
        self.assertNotIn("hhh8", out)
        self.assertNotIn("b2", out)

    def test_mask_split_line_yaml(self):
        out = scan.mask_text("db:\n  password:\n    iii9\n  host: h\n")
        self.assertNotIn("iii9", out)
        self.assertIn("host: h", out)

    def test_mask_long_line_fast(self):
        import time
        start = time.time()
        scan.mask_text("a" * 65536 + "\n" + "b" * 65536 + "=x\n")
        self.assertLess(time.time() - start, 0.5)

    def test_sensitive_key(self):
        self.assertTrue(scan.sensitive_key("DB_PASSWORD"))
        self.assertFalse(scan.sensitive_key("hostname"))

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

    def test_symlink_not_followed(self):
        self.write("/etc/ssh/ssh_host_ed25519_key", b"PRIVATE")
        link = pathlib.Path(self.root, "usr/local/bin/x")
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(pathlib.Path(self.root, "etc/ssh/ssh_host_ed25519_key"), link)
        entry = scan.file_entry("/usr/local/bin/x", self.root)
        self.assertEqual(entry["kind"], "symlink")
        self.assertIsNone(entry["sha256"])
        self.assertIsNone(entry["content"])

    def test_symlinked_dir_not_followed(self):
        self.write("/var/lib/kubelet/secret.txt", b"PRIVATE")
        os.makedirs(pathlib.Path(self.root, "usr/local"), exist_ok=True)
        os.symlink(pathlib.Path(self.root, "var/lib/kubelet"), pathlib.Path(self.root, "usr/local/kube"))
        entry = scan.file_entry("/usr/local/kube/secret.txt", self.root)
        self.assertEqual(entry["kind"], "symlink")
        self.assertIsNone(entry["content"])

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


class GuestConfig(unittest.TestCase):
    def test_only_allowed_fields(self):
        cfg = {"cores": 4, "memory": "2048", "sockets": 1, "name": "vm1",
               "scsi0": "local-lvm:vm-1-disk-0,size=32G,cache=writeback",
               "args": "-fw_cfg name=opt/x,string=SECRET", "lxc.environment": "TOKEN=SECRET",
               "cipassword": "SECRET", "description": "SECRET notes", "net0": "virtio=AA:BB"}
        out = scan.guest_config(cfg)
        self.assertNotIn("SECRET", str(out))
        self.assertEqual(out["scsi0"], "local-lvm:vm-1-disk-0,size=32G")
        self.assertEqual(out["cores"], 4)


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
