#!/usr/bin/env python3
"""Local-only regression: two setup runs and live argv inspection, with fake keys."""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.parse

DOCKER = shutil.which("docker")
MC_IMAGE = "fytorium-mc:RELEASE.2025-04-16T18-13-26Z"
SERVER_IMAGE = os.environ.get("FYTORIUM_TEST_MINIO_IMAGE", "fytorium-test-minio:local")
SCRIPT = Path(__file__).with_name("minio-setup.sh")
name = "fytorium-setup-test-" + secrets.token_hex(6)
server, client = name + "-server", name + "-client"
admin_key, admin_secret = "fake-admin", secrets.token_urlsafe(32)
app_key, app_secret = "fake-app", secrets.token_urlsafe(32)
url = "http://" + server + ":9000"
alias = "http://" + admin_key + ":" + urllib.parse.quote(admin_secret, safe="") + "@" + server + ":9000"
env = dict(os.environ, MC_HOST_fytorium=alias)


def docker(*args, check=True, **kwargs):
    return subprocess.run([DOCKER, *args], check=check, capture_output=True, **kwargs)


def mc(*args, **kwargs):
    return docker("run", "--rm", "-i", "--network", name, "-e", "MC_HOST_fytorium", MC_IMAGE, *args, **kwargs)


try:
    docker("network", "create", "--internal", name)
    docker("run", "-d", "--name", server, "--network", name,
           "-e", "MINIO_ROOT_USER", "-e", "MINIO_ROOT_PASSWORD", SERVER_IMAGE,
           "server", "/data", env=dict(os.environ, MINIO_ROOT_USER=admin_key, MINIO_ROOT_PASSWORD=admin_secret))
    for attempt in range(30):
        try:
            docker("run", "--rm", "--network", name, "alpine:3.21",
                   "wget", "-q", "-O", "/dev/null", url + "/minio/health/live")
            break
        except subprocess.CalledProcessError:
            time.sleep(0.2)
    else:
        raise AssertionError("throwaway MinIO did not become ready")

    with tempfile.TemporaryDirectory(prefix=name) as tmp:
        # Use the deployment image unchanged; only isolate networking and delay stdin.
        # Delay stdin delivery so mc user add stays observable in ps and docker top.
        wrapper = Path(tmp) / "docker"
        wrapper.write_text('''#!/usr/bin/env python3
import json, os, subprocess, sys, time
from pathlib import Path
args = sys.argv[1:]
if args[:2] != ["run", "--rm"]:
    os.execv(os.environ["FYTORIUM_TEST_DOCKER"], [os.environ["FYTORIUM_TEST_DOCKER"], *args])
index = args.index("fytorium-mc:RELEASE.2025-04-16T18-13-26Z")
command = args[index + 1:]
with open(os.environ["FYTORIUM_TEST_CALLS"], "a") as calls:
    calls.write(json.dumps(command) + "\\n")
if os.environ.get("FYTORIUM_TEST_FAIL") == "1" and command[:2] == ["version", "suspend"]:
    print(os.environ["VAULT_PASSWORD"], file=sys.stderr)
    sys.exit(42)
args[1:1] = ["--network", os.environ["FYTORIUM_TEST_NETWORK"], "--name", os.environ["FYTORIUM_TEST_CLIENT"]]
if command == ["admin", "user", "add", "fytorium"]:
    keys = sys.stdin.buffer.read()
    child = subprocess.Popen([os.environ["FYTORIUM_TEST_DOCKER"], *args], stdin=subprocess.PIPE)
    # Wait for the parent test to inspect the live client before delivering fake keys.
    ready = Path(os.environ["FYTORIUM_TEST_READY"])
    deadline = time.monotonic() + 15
    while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.02)
    child.communicate(keys)
    sys.exit(child.returncode)
os.execv(os.environ["FYTORIUM_TEST_DOCKER"], [os.environ["FYTORIUM_TEST_DOCKER"], *args])
''')
        wrapper.chmod(0o755)
        calls = Path(tmp) / "calls.jsonl"
        ready = Path(tmp) / "observed"
        setup_env = dict(os.environ, PATH=tmp + ":" + os.environ["PATH"],
                         MINIO_ADMIN_USERNAME=admin_key, MINIO_ADMIN_PASSWORD=admin_secret,
                         VAULT_USERNAME=app_key, VAULT_PASSWORD=app_secret, FYTORIUM_MINIO_URL=url,
                         FYTORIUM_TEST_NETWORK=name,
                         FYTORIUM_TEST_CLIENT=client, FYTORIUM_TEST_DOCKER=DOCKER,
                         FYTORIUM_TEST_CALLS=str(calls), FYTORIUM_TEST_READY=str(ready))
        for run in (1, 2):
            ready.unlink(missing_ok=True)
            seen = False
            process = subprocess.Popen([str(SCRIPT), "--apply"], env=setup_env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            deadline = time.monotonic() + 600
            try:
                while process.poll() is None:
                    assert time.monotonic() < deadline, "setup timed out"
                    host = subprocess.check_output(["ps", "-eo", "pid,args"])
                    top = docker("top", client, "-eo", "pid,args", check=False).stdout
                    assert app_secret.encode() not in host + top, "app secret leaked in argv"
                    assert admin_secret.encode() not in host + top, "admin secret leaked in argv"
                    if b"admin user add fytorium" in top:
                        seen = True
                        ready.touch()
                    time.sleep(0.01)
                stdout, stderr = process.communicate()
                assert process.returncode == 0, (stdout + stderr).decode()
                assert app_secret.encode() not in stdout + stderr
                assert seen, "did not observe live mc admin user add"
                print(f"Run {run}: exit 0; live ps/docker top contained no secrets")
            finally:
                if process.poll() is None:
                    docker("rm", "-f", client, check=False)
                    process.kill()
                process.communicate()
        commands = [json.loads(line) for line in calls.read_text().splitlines()]
        assert sum(command[:3] == ["admin", "policy", "attach"] for command in commands) == 1
        assert all(app_secret not in arg for command in commands for arg in command)
        print("Policy attached once across both runs; secret absent from all Docker argv")
        failed = subprocess.run([str(SCRIPT), "--apply"],
                                env=dict(setup_env, FYTORIUM_TEST_FAIL="1"), capture_output=True)
        assert failed.returncode == 1, "setup ignored a provider failure"
        assert app_secret.encode() not in failed.stdout + failed.stderr
        assert "Provider output withheld" in failed.stderr.decode()
        later = [json.loads(line) for line in calls.read_text().splitlines()][len(commands):]
        assert len(later) == 2, "setup continued after a provider failure"
        print("Injected provider failure stops setup and withholds credential-bearing output")
    policy = json.loads(mc("--json", "admin", "policy", "info", "fytorium", "fytorium-photos-rw", env=env).stdout)
    assert "s3:ListBucket" not in json.dumps(policy)
    assert "s3:DeleteObject" in json.dumps(policy)
    print("Scoped policy excludes ListBucket and retains DeleteObject")
    mc("mb", "fytorium/other-bucket", env=env)
    mc("pipe", "fytorium/other-bucket/object", input=b"private", env=env)
    app_env = dict(os.environ, MC_HOST_fytorium="http://" + app_key + ":" + app_secret + "@" + server + ":9000")
    mc("pipe", "fytorium/fytorium-photos/object", input=b"photo", env=app_env)
    assert mc("cat", "fytorium/fytorium-photos/object", env=app_env).stdout == b"photo"
    mc("rm", "fytorium/fytorium-photos/object", env=app_env)
    assert mc("cat", "fytorium/fytorium-photos/object", env=env, check=False).returncode != 0
    for command in [("ls", "fytorium/fytorium-photos"),
                    ("ls", "fytorium/other-bucket"),
                    ("cat", "fytorium/other-bucket/object"),
                    ("pipe", "fytorium/other-bucket/new-object"),
                    ("rm", "fytorium/other-bucket/object")]:
        assert mc(*command, input=b"denied", env=app_env, check=False).returncode != 0, command
    assert mc("cat", "fytorium/other-bucket/object", env=env).stdout == b"private"
    print("App can put/get/delete its objects; listing and other-bucket access denied")
finally:
    docker("rm", "-f", client, server, check=False)
    docker("network", "rm", name, check=False)
    print("Throwaway containers and network removed")
