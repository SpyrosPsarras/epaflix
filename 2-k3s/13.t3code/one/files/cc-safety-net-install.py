#!/usr/bin/python3 -I
"""Verify and install the reviewed cc-safety-net artifact, then print its absolute Pi entry path."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tarfile
import urllib.request

COMMIT = "2171126797a158f8c5e93296ffd1dec770223984"
VERSION = "2.6.4"
IMAGE_ARTIFACT = Path("/opt/cc-safety-net") / VERSION
BUN_URL = "https://registry.npmjs.org/@oven/bun-linux-x64/-/bun-linux-x64-1.4.2.tgz"
BUN_INTEGRITY = "9/E/UXOTpSo3YsV5g+FhtTd/qTpiWoKuxS12cqtuYA1ssu9fRAoPQnipFgGyck3tWO63iUdxBiygq+kELFawng=="
HERE = Path(__file__).resolve().parent
BUILD_PATH = "/usr/local/bin:/usr/bin:/bin"
EXECUTABLES = {
    "python": "/usr/bin/python3",
    "git": "/usr/bin/git",
    "node": "/usr/local/bin/node",
    "shell": "/bin/sh",
}


def trusted_path(name, executable=False):
    path = Path(name)
    if not path.is_absolute():
        raise ValueError(f"Absolute trusted path required: {name}")
    resolved = path.resolve(strict=True)
    for candidate in (path, resolved):
        for entry in (candidate, *candidate.parents):
            info = entry.lstat()
            if info.st_uid != 0 or (not entry.is_symlink() and info.st_mode & 0o022):
                raise ValueError(f"Untrusted ownership or permissions: {entry}")
    if executable and (not resolved.is_file() or not resolved.stat().st_mode & 0o111):
        raise ValueError(f"Not an executable: {name}")
    return str(resolved)


def trusted_tools():
    tools = {name: trusted_path(path, executable=True) for name, path in EXECUTABLES.items()}
    if trusted_path(sys.executable, executable=True) != tools["python"]:
        raise ValueError("Installer must use the trusted system Python")
    for directory in BUILD_PATH.split(":"):
        trusted_path(directory)
    return tools


def build_environment(home, shell):
    # No inherited credentials, loader hooks, Git helpers or user npm config.
    return {"PATH": BUILD_PATH, "HOME": str(home), "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0", "NPM_CONFIG_USERCONFIG": str(home / "npm-user.conf"),
            "NPM_CONFIG_GLOBALCONFIG": str(home / "npm-global.conf"), "NPM_CONFIG_SCRIPT_SHELL": shell}


def bun_runner(work):
    if sys.platform != "linux" or os.uname().machine != "x86_64":
        raise ValueError("Pinned runner requires Linux x86_64 glibc")
    # No package manager or lifecycle command runs to obtain the runner.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(BUN_URL, timeout=60) as response:
        archive = response.read(100_000_001)
    if len(archive) > 100_000_000 or base64.b64encode(hashlib.sha512(archive).digest()).decode() != BUN_INTEGRITY:
        raise ValueError("Bun archive integrity mismatch")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        member = tar.getmember("package/bin/bun")
        if not member.isfile() or member.size > 100_000_000:
            raise ValueError("Invalid Bun executable member")
        binary = tar.extractfile(member).read()
    runner = work / "bun"
    runner.write_bytes(binary)
    runner.chmod(0o700)
    return str(runner)


def build(root, runner, node, env):
    # Invoke functions and compiler directly. Never use npm exec, bun run or .bin
    # resolution, which can add writable ancestor directories to executable PATH.
    run(runner, "install", "--frozen-lockfile", "--ignore-scripts", cwd=root, env=env)
    # The pinned checkout tracks dist. Match upstream clean without a shell.
    shutil.rmtree(root / "dist")
    script = root / "scripts/build.ts"
    source = script.read_text()
    replacements = {
        "import { renameSync, statSync } from 'node:fs';": "import { renameSync, statSync, chmodSync } from 'node:fs';",
        "Bun.spawnSync(['bun', 'run', 'build:types'])": "Bun.spawnSync(" + json.dumps(
            [node, str(root / "node_modules/typescript/bin/tsc"), "--project", "tsconfig.build.json",
             "--emitDeclarationOnly", "--declaration"]) + ", {env: process.env})",
        "await Bun.$`chmod 755 dist/bin/cc-safety-net.js`;": "chmodSync('dist/bin/cc-safety-net.js', 0o755);",
    }
    for old, new in replacements.items():
        if source.count(old) != 1:
            raise ValueError("Upstream direct-build adaptation changed")
        source = source.replace(old, new)
    script.write_text(source)
    run(runner, str(script), cwd=root, env=env)
    print("PASS: direct pinned Bun/build script and absolute Node/compiler; no package lifecycle", flush=True)


def run(*args, cwd=None, env):
    subprocess.run(args, cwd=cwd, env=env, check=True)


def verify(root, hashes):
    for name, expected in hashes.items():
        path = root / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Hash mismatch: {name}")


def verify_artifact(root, hashes, trusted=False):
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Missing or symlink artifact: {root}")
    paths = [root, *root.rglob("*")]
    for path in paths:
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            raise ValueError(f"Unexpected artifact entry: {path}")
        if trusted:
            trusted_path(path)
    if {p.relative_to(root).as_posix() for p in paths if p.is_file()} != set(hashes):
        raise ValueError("Unexpected artifact files")
    verify(root, hashes)


def install(destination, image_build=False):
    tools = trusted_tools()
    if destination.is_symlink():
        raise ValueError("Installation must not be a symlink")
    destination = destination.resolve()
    if destination == Path("/tmp") or Path("/tmp") in destination.parents:
        raise ValueError("Installation must be outside /tmp")
    manifest = json.loads((HERE / "cc-safety-net-hashes.json").read_text())
    verify(HERE, {"cc-safety-net.patch": manifest["patch"]})
    if image_build and os.geteuid() != 0:
        raise ValueError("Image build requires root")
    if destination.exists():
        verify_artifact(destination, manifest["artifact"], trusted=image_build)
        return destination / "dist/pi/index.js"
    if not image_build:
        verify_artifact(IMAGE_ARTIFACT, manifest["artifact"], trusted=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".safety-install-") as work:
            package = Path(work) / "package"
            shutil.copytree(IMAGE_ARTIFACT, package)
            verify_artifact(package, manifest["artifact"])
            os.rename(package, destination)
        return destination / "dist/pi/index.js"
    destination.parent.mkdir(parents=True, exist_ok=True)
    trusted_path(destination.parent)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".safety-build-") as work:
        home = Path(work) / "home"
        home.mkdir()
        env = build_environment(home, tools["shell"])
        root = Path(work) / "source"
        run(tools["git"], "clone", "--quiet", "https://github.com/kenryu42/cc-safety-net.git", str(root), env=env)
        run(tools["git"], "checkout", "--quiet", "--detach", COMMIT, cwd=root, env=env)
        if json.loads((root / "package.json").read_text())["version"] != VERSION:
            raise ValueError("Upstream version mismatch")
        run(tools["git"], "apply", "--check", str(HERE / "cc-safety-net.patch"), cwd=root, env=env)
        run(tools["git"], "apply", str(HERE / "cc-safety-net.patch"), cwd=root, env=env)
        verify(root, manifest["source"])
        runner = bun_runner(Path(work))
        build(root, runner, tools["node"], env)
        verify(root, manifest["artifact"])
        package = Path(work) / "package"
        package.mkdir()
        shutil.copytree(root / "dist", package / "dist")
        for name in ("package.json", "LICENSE"):
            shutil.copy2(root / name, package / name)
        for path in (package, *package.rglob("*")):
            path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
        verify_artifact(package, manifest["artifact"], trusted=True)
        run(tools["node"], "--input-type=module", "-e",
            "const p=await import(process.argv[1]); if(typeof p.default !== 'function') throw Error('Missing Pi extension export')",
            (package / "dist/pi/index.js").as_uri(), env=env)
        os.rename(package, destination)
    return destination / "dist/pi/index.js"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, nargs="?")
    parser.add_argument("--image-build", action="store_true", help="Construct the reviewed image artifact as root")
    args = parser.parse_args()
    destination = args.destination or (IMAGE_ARTIFACT if args.image_build else
                                       Path.home() / ".local/share/pi/cc-safety-net" / VERSION)
    entry = install(destination, image_build=args.image_build)
    print(entry)


if __name__ == "__main__":
    main()
