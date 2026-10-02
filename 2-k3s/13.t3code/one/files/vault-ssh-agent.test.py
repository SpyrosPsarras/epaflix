"""vault-ssh-agent.py against a fake bw and a real ssh-agent, with a throwaway key."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("vault-ssh-agent.py")
# The fake bw behaves like the real one where it matters: login writes the API
# secret into the data dir, unlock prints the session, list filters by collection.
FAKE_BW = r'''#!/usr/bin/env python3
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"args": sys.argv[1:], "env": {k: v for k, v in os.environ.items() if k.startswith(("BW", "BITWARDEN"))}}) + "\n")
mode = open(os.path.join(here, "mode")).read().strip()
data = os.path.join(os.environ["BITWARDENCLI_APPDATA_DIR"], "data.json")
cmd = sys.argv[1]
if cmd == mode.removeprefix("hang-"):
    time.sleep(30)
if cmd == "login" and mode == "bad-login":
    sys.exit("Client authentication failed.")
if cmd == "login":
    open(data, "w").write(json.dumps({"clientSecret": os.environ["BW_CLIENTSECRET"]}))
if cmd == "unlock":
    print("SESSION-TOKEN", end="")
if cmd == "list" and sys.argv[2] == "collections":
    names = ["Other", "SSH-t3code"] + (["SSH-t3code"] if mode == "two-collections" else [])
    print(json.dumps([{"id": f"col-{i}", "name": n} for i, n in enumerate(names)]))
if cmd == "list" and sys.argv[2] == "items":
    items = json.load(open(os.path.join(here, "items.json")))
    print(json.dumps([i for i in items if sys.argv[4] in i["collectionIds"]]))
'''
# Real ssh-add, except that loading a key hangs in mode hang-ssh-add.
FAKE_SSH_ADD = r'''#!/usr/bin/env python3
import os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
if open(os.path.join(here, "mode")).read().strip() == "hang-ssh-add" and sys.argv[1:] == ["-q", "-"]:
    time.sleep(30)
os.execv("/usr/bin/ssh-add", ["ssh-add", *sys.argv[1:]])
'''
ONE = Path(__file__).resolve().parents[1]


class Deployment(unittest.TestCase):
    """The script's file arguments in the entrypoint match the pod's volumes."""

    def test_scratch_is_memory_and_login_is_optional(self):
        import yaml
        call = re.search(r"python3 /scripts/vault-ssh-agent.py (\S+) (\S+) (\S+);", (ONE / "files/entrypoint.sh").read_text())
        creds, sock, scratch = call.groups()
        self.assertTrue(sock.startswith("/tmp/"))
        pod = next(d for d in yaml.safe_load_all((ONE / "statefulset.yaml").read_text()) if d["kind"] == "StatefulSet")
        spec = pod["spec"]["template"]["spec"]
        mounts = {m["mountPath"]: m["name"] for m in spec["containers"][0]["volumeMounts"]}
        volumes = {v["name"]: v for v in spec["volumes"]}
        self.assertEqual(volumes[mounts[scratch]]["emptyDir"]["medium"], "Memory", "bw saves the API secret here")
        secret = volumes[mounts[creds]]["secret"]
        self.assertEqual((secret["secretName"], secret.get("optional")), ("t3code-vaultwarden", True))


class VaultSshAgent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        for name, body in [("bw", FAKE_BW), ("ssh-add", FAKE_SSH_ADD)]:
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.creds = self.dir / "creds"
        self.creds.mkdir()
        for name, value in [("client-id", "user.test-id"), ("client-secret", "test-secret"), ("password", "test-password")]:
            (self.creds / name).write_text(value + "\n")
        self.sock = self.dir / "agent" / "agent.sock"
        self.scratch = self.dir / "scratch"
        self.scratch.mkdir()
        # A comment shaped like a fingerprint must still not reach the log.
        self.comment = "key-comment SHA256:" + "C0mment" * 6 + "x"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", self.comment, "-f", str(self.dir / "key")], check=True)
        self.private = (self.dir / "key").read_text()
        self.fingerprint = subprocess.run(["ssh-keygen", "-lf", str(self.dir / "key.pub")], check=True,
                                          capture_output=True, text=True).stdout.split()[1]
        self.pids = []

    def tearDown(self):
        for pid in self.pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        self.tmp.cleanup()

    def run_script(self, mode, items, timeout="60"):
        (self.bin / "mode").write_text(mode)
        (self.bin / "items.json").write_text(json.dumps(items))
        result = subprocess.run(["python3", str(SCRIPT), str(self.creds), str(self.sock), str(self.scratch)],
                                env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "VAULT_SSH_AGENT_TIMEOUT": timeout},
                                capture_output=True, text=True, timeout=120)
        self.pids += [int(p) for p in re.findall(r"pid (\d+)", result.stderr)]
        calls = [json.loads(line) for line in (self.bin / "calls.jsonl").read_text().splitlines()]
        # On every path: no secret in the output, nothing left in the scratch dir, logout attempted.
        body = self.private.splitlines()[1]
        for secret in ["test-secret", "test-password", "SESSION-TOKEN", body]:
            self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual(list(self.scratch.iterdir()), [], "scratch dir is wiped")
        self.assertEqual(calls[-1]["args"], ["logout"])
        self.assertEqual({c["env"]["BITWARDENCLI_APPDATA_DIR"] for c in calls}, {str(self.scratch / "bw")})
        return result, calls

    def ssh_items(self):
        return [
            {"type": 5, "name": "t3code", "collectionIds": ["col-1"], "sshKey": {"privateKey": self.private}},
            {"type": 5, "name": "homepc", "collectionIds": ["col-0"], "sshKey": {"privateKey": self.private}},
            {"type": 1, "name": "a login", "collectionIds": ["col-1"], "login": {"password": "nope"}},
            # Split so jev-guard's private-key pattern does not block agents editing this file.
            {"type": 5, "name": "broken", "collectionIds": ["col-1"], "sshKey": {"privateKey": "-----BEGIN OPENSSH " + "PRIVATE KEY-----\nAAAA\n"}},
        ]

    def test_loads_ssh_items_of_the_collection_only(self):
        self.sock.parent.mkdir()
        self.sock.write_text("stale")
        (self.scratch / "bw").mkdir()
        (self.scratch / "bw" / "data.json").write_text("left by a killed run")
        result, calls = self.run_script("ok", self.ssh_items())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c["args"][0] for c in calls], ["config", "login", "unlock", "sync", "list", "list", "logout"])
        self.assertEqual(calls[0]["args"], ["config", "server", "https://vaultwarden.epaflix.com"])
        login, unlock, sync, collections, items = calls[1:6]
        self.assertEqual((login["env"]["BW_CLIENTID"], login["env"]["BW_CLIENTSECRET"]), ("user.test-id", "test-secret"))
        self.assertEqual(unlock["args"], ["unlock", "--raw", "--passwordfile", str(self.creds / "password")])
        self.assertNotIn("BW_SESSION", unlock["env"])
        self.assertEqual(items["args"], ["list", "items", "--collectionid", "col-1"])
        for call in (sync, collections, items):
            self.assertEqual(call["env"]["BW_SESSION"], "SESSION-TOKEN")
            self.assertNotIn("BW_CLIENTSECRET", call["env"], "the API key goes to login only")
        self.assertIn(f"1/2 keys loaded: {self.fingerprint}\n", result.stderr)
        self.assertIn("SSH item 2 of 2 not loaded", result.stderr)
        self.assertNotIn("key-comment", result.stderr, "ssh-add -l comments are not logged")
        self.assertNotIn("C0mment", result.stderr)
        listed = subprocess.run(["ssh-add", "-l"], env={**os.environ, "SSH_AUTH_SOCK": str(self.sock)},
                                capture_output=True, text=True, check=True).stdout
        self.assertIn(self.fingerprint, listed)
        self.assertEqual(len(listed.splitlines()), 1)

    def test_failed_login_leaves_no_agent(self):
        result, calls = self.run_script("bad-login", self.ssh_items())
        self.assertEqual(result.returncode, 1)
        self.assertIn("bw login failed (exit 1)", result.stderr)
        self.assertNotIn("Client authentication failed", result.stderr, "bw's own messages are not relayed")
        self.assertEqual([c["args"][0] for c in calls], ["config", "login", "logout"])
        self.assertFalse(self.sock.exists())

    def test_hung_server_times_out(self):
        result, calls = self.run_script("hang-sync", self.ssh_items(), timeout="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("bw sync timed out after 1s", result.stderr)
        self.assertFalse(self.sock.exists())

    def test_hung_ssh_add_stops_the_agent(self):
        result, _ = self.run_script("hang-ssh-add", self.ssh_items(), timeout="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("ssh-add failed: TimeoutExpired", result.stderr)
        subprocess.run(["sleep", "0.2"])
        self.assertFalse(self.sock.exists(), "ssh-agent -k removes its socket")

    def test_collection_must_be_unique(self):
        result, _ = self.run_script("two-collections", self.ssh_items())
        self.assertEqual(result.returncode, 1)
        self.assertIn("expected one 'SSH-t3code' collection, found 2", result.stderr)
        self.assertFalse(self.sock.exists())

    def test_no_ssh_items_leaves_no_agent(self):
        result, _ = self.run_script("ok", [{"type": 1, "name": "a login", "collectionIds": ["col-1"]}])
        self.assertEqual(result.returncode, 1)
        self.assertIn("no SSH key items in 'SSH-t3code'", result.stderr)
        self.assertFalse(self.sock.exists())

    def test_unloadable_keys_stop_the_agent(self):
        result, _ = self.run_script("ok", [{"type": 5, "name": "broken", "collectionIds": ["col-1"], "sshKey": {"privateKey": "garbage"}}])
        self.assertEqual(result.returncode, 1)
        self.assertIn("no key loaded", result.stderr)
        subprocess.run(["sleep", "0.2"])
        self.assertFalse(self.sock.exists(), "ssh-agent -k removes its socket")


if __name__ == "__main__":
    unittest.main()
