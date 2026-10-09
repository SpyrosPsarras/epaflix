#!/usr/bin/python3 -I
"""Offline packaging checks. Optional --build tests a fresh pinned build."""
import contextlib
import hashlib
import io
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
with tempfile.TemporaryDirectory(dir=Path.cwd(), prefix=".safety-cache-test-") as tmp:
    root = Path(tmp)
    package = root / "package"
    entry = package / "dist/pi/index.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("export default function () {}\n")
    (root / "cc-safety-net.patch").write_text("fixture")
    manifest = {"patch": hashlib.sha256(b"fixture").hexdigest(),
                "artifact": {"dist/pi/index.js": hashlib.sha256(entry.read_bytes()).hexdigest()}}
    (root / "cc-safety-net-hashes.json").write_text(json.dumps(manifest))
    with patch.object(installer, "HERE", root):
        assert installer.install(package) == entry, "cached installation must return the Pi entry"
        output = io.StringIO()
        with patch.object(sys, "argv", ["installer", str(package)]), contextlib.redirect_stdout(output):
            installer.main()
        assert output.getvalue().splitlines()[-1] == str(entry), "CLI must print an absolute Pi path"
        assert not (root / "package-opencode2").exists(), "CLI must not create an OpenCode wrapper"
        with patch.object(Path, "home", return_value=root), patch.object(installer, "install", return_value=entry) as build:
            with patch.object(sys, "argv", ["installer"]), contextlib.redirect_stdout(io.StringIO()):
                installer.main()
            assert build.call_args.args[0] == root / ".local/share/pi/cc-safety-net" / installer.VERSION
        with patch.object(sys, "argv", ["installer", str(package), "--config", str(root / "opencode.json")]), contextlib.redirect_stderr(io.StringIO()):
            try:
                installer.main()
            except SystemExit as error:
                assert error.code == 2
            else:
                raise AssertionError("Accepted removed --config argument")
        (entry.parent / "unexpected.js").write_text("tampered")
        try:
            installer.install(package)
        except ValueError:
            pass
        else:
            raise AssertionError("Accepted unreviewed dist file")
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
print("PASS: cached Pi entry, absolute CLI path, default Pi destination, removed --config, no OpenCode wrapper, hash/symlink and unexpected-dist rejection", flush=True)
if len(sys.argv) > 1:
    assert sys.argv[1] == "--build"
    # Test under this repository, outside /tmp, and remove staging on exit.
    with tempfile.TemporaryDirectory(dir=Path.cwd(), prefix=".safety-install-test-") as tmp:
        destination = Path(tmp) / "package"
        config = Path(tmp) / "config.json"
        config.write_text('{"plugin": ["untouched@1"]}\n')
        original = config.read_bytes()
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
        result = subprocess.run([tools["python"], "-I", "-S", str(FILES / "cc-safety-net-install.py"), str(destination)],
                                env=poisoned, check=True, text=True, stdout=subprocess.PIPE)
        print(result.stdout, end="")
        assert result.stdout.splitlines()[-1] == str(destination / "dist/pi/index.js")
        assert not marker.exists()
        assert not ancestor_marker.exists()
        assert config.read_bytes() == original
        before = config.read_bytes()
        subprocess.run([tools["python"], "-I", "-S", str(FILES / "cc-safety-net-install.py"), str(destination)], env=poisoned, check=True)
        assert config.read_bytes() == before
        assert not marker.exists()
        assert not ancestor_marker.exists()
        subprocess.run([tools["node"], "--input-type=module", "-e", """
import assert from 'node:assert/strict';
const {default:extension}=await import(process.argv[1]);
let hook;
extension({on:(event,handler)=>{if(event==='tool_call') hook=handler}, registerCommand:()=>{}});
assert.equal(typeof hook, 'function');
const inspect=command=>hook({toolName:'bash', input:{command}}, {cwd:process.argv[2], sessionManager:{getSessionId:()=> 'packaging-test'}});
assert.equal(inspect(`python3 -c 'import subprocess; args=["true"]; subprocess.run(args); print("protected restore credentials encrypted copy saved")'`), undefined);
for (const command of [
  'cat credentials',
  `python3 -c 'import subprocess; args=["true"]; subprocess.run(args); print("protected restore credentials encrypted copy saved")' > output`,
  `python3 -c 'import subprocess; args=["true"]; subprocess.run(args); print("protected restore credentials encrypted copy saved")' | cat`,
  `echo "$(python3 -c 'import subprocess; args=[\"true\"]; subprocess.run(args); print(\"protected restore credentials encrypted copy saved\")')"`,
  `python3 -c 'open("credentials"); print("protected restore credentials encrypted copy saved")'`,
  `python3 - <<'PY'\nimport subprocess\nargs=["true"]\nsubprocess.run(args)\nprint("protected restore credentials encrypted copy saved")\nPY`,
]) assert.equal(inspect(command)?.block, true, command);
console.log('PASS: built Pi hook allows display prose; sensitive reads, redirection, pipe, substitution and stdin carriers stay blocked; submitted commands were not executed');
""", (destination / "dist/pi/index.js").as_uri(), tmp], env=installer.build_environment(Path(tmp), tools["shell"]), check=True)
        print("PASS: malicious PATH wrappers never ran during fresh build or repeat install; poisoned loader/config environment was not inherited", flush=True)
        print("PASS: malicious ancestor node_modules/.bin wrappers never ran in actual fresh-build runner or repeat installation", flush=True)
        print("PASS: fresh build, complete reviewed artifact hashes/chunks, Node Pi import, unchanged config and repeat install", flush=True)
