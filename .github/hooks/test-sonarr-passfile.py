#!/usr/bin/env python3
"""Sonarr's pre-auth connection string must be safe while Npgsql still finds pgpass.

Requires Python/PyYAML, a .NET 8+ SDK, and NuGet access. Uses synthetic credentials,
Linux memfd files, and a loopback PostgreSQL authentication stub, never a live DB.
Pass --render-stdin to also check an in-memory KSOPS render without printing it.
"""
import argparse
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading

import yaml

ROOT = Path(__file__).resolve().parents[2]
TIER = ROOT / "2-k3s/08.servarr"
PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
<PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework></PropertyGroup>
<ItemGroup><PackageReference Include="Npgsql" Version="7.0.9" /></ItemGroup>
</Project>
"""
PROGRAM = r"""
using System;
using Npgsql;
var password = Environment.GetEnvironmentVariable("TEST_PASSWORD");
var mode = args[0];
// Same builder fields as Sonarr 4.0.20.3014 ConnectionStringFactory.
var builder = new NpgsqlConnectionStringBuilder {
    Database = "sonarr-main", Host = "127.0.0.1", Username = "sonarr",
    Password = mode == "explicit" ? password : "",
    Port = int.Parse(args[1]), Enlist = false
};
if (mode == "passfile") builder.Passfile = Environment.GetEnvironmentVariable("TEST_PASSFILE");
var connectionString = builder.ConnectionString;
if (mode == "explicit" && !connectionString.Contains(password))
    throw new Exception("Old inline-password log leak was not reproduced");
if (mode != "explicit" && (connectionString.Contains(password) ||
    new NpgsqlConnectionStringBuilder(connectionString).Password != null))
    throw new Exception("Pre-auth migration log would contain a credential");
using var connection = new NpgsqlConnection(connectionString + ";Pooling=false;SSL Mode=Disable;Timeout=5");
try { connection.Open(); return 1; }
// The stub stops after checking the actual authentication password on the wire.
catch (PostgresException ex) when (ex.SqlState == "28000") { return 0; }
"""


def check_manifest(documents):
    deployment = next(d for d in documents if d and d.get("kind") == "Deployment"
                      and d["metadata"]["name"] == "sonarr")
    spec = deployment["spec"]["template"]["spec"]
    container = next(c for c in spec["containers"] if c["name"] == "sonarr")
    env = {v["name"]: v for v in container["env"]}
    assert env["SONARR__POSTGRES__PASSWORD"] == {
        "name": "SONARR__POSTGRES__PASSWORD", "value": ""}
    assert "PGPASSWORD" not in env and "envFrom" not in container
    for setting, key in [("HOST", "host"), ("PORT", "port"),
                         ("MAINDB", "database"), ("USER", "user")]:
        assert env[f"SONARR__POSTGRES__{setting}"]["valueFrom"]["secretKeyRef"] == {
            "name": "servarr-postgres", "key": f"sonarr-{key}"}
    assert env["SONARR__POSTGRES__LOGDB"]["value"] == "sonarr-log"
    assert env["PGPASSFILE"]["value"] == "/run/sonarr-postgres/pgpass"
    mount = next(m for m in container["volumeMounts"] if m["name"] == "postgres-passfile")
    assert mount["mountPath"] == "/run/sonarr-postgres" and mount["readOnly"]
    secret = next(v for v in spec["volumes"] if v["name"] == "postgres-passfile")["secret"]
    assert secret == {"secretName": "servarr-postgres", "defaultMode": 0o440,
                      "items": [{"key": "sonarr-pgpass", "path": "pgpass"}]}
    assert spec["securityContext"]["fsGroup"] == 568
    print("PASS manifest: explicit empty Password, scoped read-only passfile, fsGroup permissions")


def check_rendered_secret(documents):
    secret = next(d for d in documents if d and d.get("kind") == "Secret"
                  and d["metadata"]["name"] == "servarr-postgres")
    values = secret["stringData"]

    def escape(value):
        assert "\n" not in value and "\r" not in value, "Unsupported multiline pgpass field"
        return value.replace("\\", "\\\\").replace(":", "\\:")

    expected = "\n".join(":".join(escape(v) for v in [values["sonarr-host"],
        values["sonarr-port"], db, values["sonarr-user"], values["sonarr-password"]])
        for db in [values["sonarr-database"], "sonarr-log"]) + "\n"
    assert values["sonarr-pgpass"] == expected, "Passfile and legacy maintenance password differ"
    print("PASS rendered Secret: both database entries match the current maintenance credential")


def receive(stream, count):
    data = b""
    while len(data) < count:
        part = stream.recv(count - len(data))
        if not part:
            raise EOFError("Npgsql closed before sending authentication")
        data += part
    return data


def auth_test(dotnet, app, mode, password, expected):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(10)
        port = listener.getsockname()[1]
        results = []

        def server():
            try:
                stream, _ = listener.accept()
                with stream:
                    stream.settimeout(10)
                    length = struct.unpack("!I", receive(stream, 4))[0]
                    receive(stream, length - 4)
                    stream.sendall(b"R" + struct.pack("!II", 8, 3))
                    kind = receive(stream, 1)
                    length = struct.unpack("!I", receive(stream, 4))[0]
                    actual = receive(stream, length - 4)
                    results.append(kind == b"p" and actual == expected.encode() + b"\0")
                    error = b"SFATAL\0C28000\0Mauthentication probe complete\0\0"
                    stream.sendall(b"E" + struct.pack("!I", len(error) + 4) + error)
            except Exception:
                results.append(False)

        thread = threading.Thread(target=server)
        thread.start()
        fd = os.memfd_create("sonarr-pgpass-test")
        try:
            os.fchmod(fd, 0o600)
            file_password = "synthetic-ignored-passfile" if mode == "explicit" else password
            escaped = file_password.replace("\\", "\\\\").replace(":", "\\:")
            os.write(fd, f"127.0.0.1:{port}:sonarr-main:sonarr:{escaped}\n".encode())
            env = os.environ.copy()
            env.pop("PGPASSWORD", None)
            env.update(PGPASSFILE=f"/proc/self/fd/{fd}", TEST_PASSFILE=f"/proc/self/fd/{fd}",
                       TEST_PASSWORD=password, DOTNET_CLI_TELEMETRY_OPTOUT="1")
            if mode == "environment":
                env["PGPASSWORD"] = expected
            if mode == "passfile":
                env["PGPASSFILE"] = "/nonexistent/passfile-to-test-explicit-precedence"
            result = subprocess.run([dotnet, str(app), mode, str(port)], env=env,
                                    pass_fds=(fd,), capture_output=True, timeout=20)
        finally:
            os.close(fd)
            thread.join(12)
        assert results == [True] and result.returncode == 0, f"Authentication failed: {mode}"
    print(f"PASS Npgsql 7.0.9 authentication and pre-auth log safety: {mode}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render-stdin", action="store_true")
    args = parser.parse_args()
    with (TIER / "sonarr/sonarr.yaml").open() as stream:
        check_manifest(list(yaml.safe_load_all(stream)))
    if args.render_stdin:
        documents = list(yaml.safe_load_all(sys.stdin))
        check_manifest(documents)
        check_rendered_secret(documents)
    encrypted = yaml.safe_load((TIER / "_shared/secrets/servarr-postgres.enc.yaml").read_text())
    assert encrypted["stringData"]["sonarr-pgpass"].startswith("ENC[AES256_GCM,")
    assert "_shared/secrets/servarr-postgres.enc.yaml" in yaml.safe_load(
        (TIER / "_shared/secrets/ksops-generator.yaml").read_text())["files"]
    dotnet = os.environ.get("DOTNET", "dotnet")
    with tempfile.TemporaryDirectory(prefix="sonarr-passfile-code-") as temporary:
        directory = Path(temporary)
        (directory / "Probe.csproj").write_text(PROJECT)
        (directory / "Program.cs").write_text(PROGRAM)
        result = subprocess.run([dotnet, "build", str(directory / "Probe.csproj"), "--nologo", "-o",
                                 str(directory / "out")], capture_output=True, timeout=120)
        assert result.returncode == 0, "Npgsql probe build failed; check .NET SDK and NuGet access"
        app = directory / "out/Probe.dll"
        for password in ["synthetic:colon", r"synthetic\slash", r"synthetic\:both", "synthetic-final\\"]:
            auth_test(dotnet, app, "empty", password, password)
        auth_test(dotnet, app, "explicit", "synthetic-explicit", "synthetic-explicit")
        auth_test(dotnet, app, "environment", "synthetic-ignored", "synthetic-environment")
        auth_test(dotnet, app, "passfile", "synthetic-passfile", "synthetic-passfile")
    print("PASS Sonarr passfile regression suite")


if __name__ == "__main__":
    main()
