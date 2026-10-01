#!/usr/bin/python3 -I
"""Offline packaging checks. Optional --build tests a fresh pinned build."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

FILES = Path(__file__).resolve().parents[1] / "files"
spec = importlib.util.spec_from_file_location("installer", FILES / "cc-safety-net-install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
tools = installer.trusted_tools()
print("Validated trusted executable paths: " + json.dumps(tools), flush=True)
uri = "file:///home/fixture/.local/share/opencode/cc-safety-net/2.4.11-print-third/dist/index.js"
original = {"plugin": ["other@1", ["cc-safety-net@2.4.11", {"mode": "strict"}], "after@2"],
            "permission": {"bash": {"*": "ask"}}, "provider": {"fixture": {"options": {"apiKey": "test-value"}}}}
expected = copy.deepcopy(original)
expected["plugin"][1][0] = uri
assert installer.replace_pin(copy.deepcopy(original), uri) == expected
assert installer.replace_pin(copy.deepcopy(expected), uri) == expected
assert installer.replace_pin({"plugin": ["other@1"]}, uri) == {"plugin": ["other@1", uri]}
assert installer.replace_pin({}, uri) == {"plugin": [uri]}
for plugins in (["cc-safety-net@9"], ["file:///unknown/cc-safety-net/index.js"],
                ["cc-safety-net@2.4.11", "cc-safety-net@2.4.11"], "invalid"):
    try:
        installer.replace_pin({"plugin": plugins}, uri)
    except ValueError:
        pass
    else:
        raise AssertionError(f"Accepted ambiguous declaration: {plugins}")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "file").write_text("original")
    hashes = {"file": hashlib.sha256(b"original").hexdigest()}
    installer.verify(root, hashes)
    (root / "file").write_text("tampered")
    try:
        installer.verify(root, hashes)
    except ValueError:
        pass
    else:
        raise AssertionError("Accepted tampered artifact")
    (root / "link").symlink_to(root / "file")
    try:
        installer.verify(root, {"link": hashlib.sha256(b"tampered").hexdigest()})
    except ValueError:
        pass
    else:
        raise AssertionError("Accepted artifact symlink")
    try:
        installer.trusted_path(root / "file", executable=True)
    except ValueError:
        pass
    else:
        raise AssertionError("Accepted user-owned executable")
    for invalid in (str(root / "missing"), str(root / "file")):
        with patch.dict(installer.EXECUTABLES, {"git": invalid}):
            with patch.object(installer.subprocess, "run") as spawn:
                try:
                    installer.install(root / "must-not-build")
                except (ValueError, FileNotFoundError):
                    pass
                else:
                    raise AssertionError("Accepted missing/untrusted tool")
                spawn.assert_not_called()
    real_lstat = Path.lstat
    for change in ({"st_uid": 1000}, {"st_mode": 0o40777}):
        def unsafe_parent(path):
            info = real_lstat(path)
            if str(path) == "/usr/local/bin":
                values = list(info)
                for key, value in change.items():
                    values[{"st_mode": 0, "st_uid": 4}[key]] = value
                return os.stat_result(values)
            return info
        with patch.object(Path, "lstat", unsafe_parent):
            try:
                installer.trusted_tools()
            except ValueError:
                pass
            else:
                raise AssertionError("Accepted untrusted executable parent")
    clean = installer.build_environment(root, tools["shell"])
    assert clean["PATH"] == installer.BUILD_PATH
    assert "NODE_OPTIONS" not in clean and "ANTHROPIC_AUTH_TOKEN" not in clean
    with patch.object(installer.urllib.request, "build_opener") as opener:
        opener.return_value.open.return_value.__enter__.return_value.read.return_value = b"tampered runner"
        try:
            installer.bun_runner(root)
        except ValueError:
            pass
        else:
            raise AssertionError("Accepted runner integrity mismatch")
        assert not (root / "bun").exists()
print("PASS: runner archive integrity mismatch rejected before executable creation", flush=True)
print("PASS: trusted missing/user-owned tools fail before spawn; unsafe parent ownership/mode rejected; build env is an explicit allowlist", flush=True)
print("PASS: preserve unrelated config/options, idempotency, fresh config, unknown/duplicate pins, hash and symlink rejection", flush=True)
if len(sys.argv) > 1:
    assert sys.argv[1] == "--build"
    # Test under this repository, outside /tmp, and remove staging on exit.
    with tempfile.TemporaryDirectory(dir=Path.cwd(), prefix=".safety-install-test-") as tmp:
        destination = Path(tmp) / "package"
        config = Path(tmp) / "config.json"
        config.write_text(json.dumps(original))
        ancestor_bin = Path(tmp) / "node_modules/.bin"
        ancestor_bin.mkdir(parents=True)
        ancestor_marker = Path(tmp) / "ancestor-executable-ran"
        for name in ("bun", "node", "sh", "rm", "tsc", "npm"):
            wrapper = ancestor_bin / name
            wrapper.write_text(f"#!/bin/sh\n/usr/bin/touch '{ancestor_marker}'\nexit 99\n")
            wrapper.chmod(0o755)
        # This is an actual ancestor of source/build staging, as in npm set-path.
        assert ancestor_bin.parent.parent == destination.parent
        poison = Path(tmp) / "malicious-bin"
        poison.mkdir()
        marker = Path(tmp) / "malicious-executable-ran"
        for name in ("python3", "git", "npm", "node", "bun", "sh", "rm", "tsc", "env"):
            wrapper = poison / name
            wrapper.write_text(f"#!/bin/sh\n/usr/bin/touch '{marker}'\nexit 99\n")
            wrapper.chmod(0o755)
        poisoned = dict(os.environ, PATH=str(poison) + ":" + os.environ.get("PATH", ""),
                        NODE_OPTIONS="--require=/nonexistent/poison.cjs",
                        NPM_CONFIG_USERCONFIG="/nonexistent/poison.npmrc",
                        GIT_CONFIG_GLOBAL="/nonexistent/poison.gitconfig",
                        ANTHROPIC_AUTH_TOKEN="test-only-never-inherited")
        subprocess.run([tools["python"], "-I", "-S", str(FILES / "cc-safety-net-install.py"), str(destination),
                        "--config", str(config)], env=poisoned, check=True)
        assert not marker.exists()
        assert not ancestor_marker.exists()
        staged = json.loads(config.read_text())
        wanted = copy.deepcopy(original)
        wanted["plugin"][1][0] = (destination / "dist/index.js").as_uri()
        assert staged == wanted
        before = config.read_bytes()
        subprocess.run([tools["python"], "-I", "-S", str(FILES / "cc-safety-net-install.py"), str(destination),
                        "--config", str(config)], env=poisoned, check=True)
        assert config.read_bytes() == before
        assert not marker.exists()
        assert not ancestor_marker.exists()
        subprocess.run([tools["node"], "--input-type=module", "-e", """
import assert from 'node:assert/strict';
const {CCSafetyNetPlugin}=await import(process.argv[1]);
const hooks=await CCSafetyNetPlugin({directory:process.argv[2], homeDir:process.argv[2]});
assert.equal(typeof hooks['tool.execute.before'], 'function');
const inspect=command=>hooks['tool.execute.before']({tool:'bash',sessionID:'packaging-test'}, {args:{command}});
await inspect(`python3 -c 'import subprocess; args=["true"]; subprocess.run(args); print("protected restore credentials encrypted copy saved")'`);
await assert.rejects(()=>inspect('cat credentials'));
console.log('PASS: built OpenCode hook allows display prose and denies sensitive-file read; commands were inspection data only');
""", (destination / "dist/index.js").as_uri(), tmp], env=installer.build_environment(Path(tmp), tools["shell"]), check=True)
        print("PASS: malicious PATH wrappers never ran during fresh build or repeat install; poisoned loader/config environment was not inherited", flush=True)
        print("PASS: malicious ancestor node_modules/.bin wrappers never ran in actual fresh-build runner or repeat installation", flush=True)
        print("PASS: fresh build, complete reviewed artifact hashes/chunks, Node plugin import, isolated config update and repeat install", flush=True)
