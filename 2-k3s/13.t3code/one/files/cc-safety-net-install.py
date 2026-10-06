#!/usr/bin/python3 -I
"""Build the reviewed runtime hunks, verify every artifact, then update one pin to its OpenCode 2 package directory."""
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

COMMIT = "bbade36ac6314a1bce762aa94948a6dc1a35e8ae"
VERSION = "2.4.11"
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


def replace_pin(config, uri, legacy=()):
    plugins = config.get("plugin", [])
    if not isinstance(plugins, list):
        raise ValueError("plugin must be an array")
    if "cc-safety-net" in json.dumps(config.get("plugins", [])):
        raise ValueError("cc-safety-net in plugins; inspect before replacing")
    # OpenCode 1 pins: the npm package and the reviewed build's dist/index.js file.
    known = (f"cc-safety-net@{VERSION}", *legacy)
    matches = []
    for index, plugin in enumerate(plugins):
        name = plugin[0] if isinstance(plugin, list) and plugin else plugin
        if name == uri or name in known:
            matches.append(index)
        elif isinstance(name, str) and "cc-safety-net" in name:
            raise ValueError("Unknown cc-safety-net declaration; inspect before replacing")
    if not matches:
        plugins.append(uri)
        config["plugin"] = plugins
        return config
    if len(matches) != 1:
        # Older entrypoints re-added the bare npm pin beside the reviewed build. Drop only those copies.
        found = [plugins[i] for i in matches]
        if found.count(uri) == 1 and all(p == uri or (isinstance(p, str) and p in known) for p in found):
            config["plugin"] = [p for p in plugins if not (isinstance(p, str) and p in known)]
            return config
        raise ValueError("Expected exactly one known cc-safety-net declaration")
    index = matches[0]
    plugin = plugins[index]
    plugins[index] = [uri, *plugin[1:]] if isinstance(plugin, list) else uri
    return config


def wrapper(destination):
    # OpenCode 2 loads a plugin package directory, not a file, and the reviewed
    # package.json exports no server entry. This directory re-exports the build.
    path = destination.parent / f"{destination.name}-opencode2"
    files = {
        "package.json": json.dumps({"name": "cc-safety-net-opencode2", "private": True, "type": "module", "main": "index.js"}) + "\n",
        "index.js": f"export {{ default }} from {json.dumps((destination / 'dist/index.js').as_uri())}\n",
    }
    if path.is_symlink():
        raise ValueError("Wrapper must not be a symlink")
    path.mkdir(exist_ok=True)
    for name, text in files.items():
        target = path / name
        if target.is_symlink() or not target.exists() or target.read_text() != text:
            with tempfile.NamedTemporaryFile("w", dir=path, delete=False) as out:
                out.write(text)
            os.chmod(out.name, 0o644)
            os.replace(out.name, target)
    return path


def install(destination):
    tools = trusted_tools()
    destination = destination.resolve()
    if destination == Path("/tmp") or Path("/tmp") in destination.parents:
        raise ValueError("Installation must be outside /tmp")
    manifest = json.loads((HERE / "cc-safety-net-hashes.json").read_text())
    dist_files = {name for name in manifest["artifact"] if name.startswith("dist/")}
    verify(HERE, {"cc-safety-net.patch": manifest["patch"]})
    if destination.exists():
        verify(destination, manifest["artifact"])
        if {p.relative_to(destination).as_posix() for p in (destination / "dist").rglob("*") if p.is_file()} != dist_files:
            raise ValueError("Unexpected dist files")
        return destination / "dist/index.js"
    destination.parent.mkdir(parents=True, exist_ok=True)
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
        if {p.relative_to(root).as_posix() for p in (root / "dist").rglob("*") if p.is_file()} != dist_files:
            raise ValueError("Unexpected dist files")
        package = Path(work) / "package"
        package.mkdir()
        shutil.copytree(root / "dist", package / "dist")
        for name in ("package.json", "LICENSE"):
            shutil.copy2(root / name, package / name)
        run(tools["node"], "--input-type=module", "-e",
            "const p=await import(process.argv[1]); if(typeof p.CCSafetyNetPlugin !== 'function' || p.default?.server !== p.CCSafetyNetPlugin) throw Error('Missing plugin export')",
            (package / "dist/index.js").as_uri(), env=env)
        os.rename(package, destination)
    return destination / "dist/index.js"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--config", type=Path, help="explicitly enable the config update after building")
    args = parser.parse_args()
    entry = install(args.destination)
    plugin = wrapper(entry.parent.parent)
    if args.config:
        path = args.config.resolve()
        original = path.read_bytes()
        config = replace_pin(json.loads(original), plugin.as_uri(), legacy=(entry.as_uri(),))
        updated = (json.dumps(config, indent=2) + "\n").encode()
        if original != updated:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
                temporary = Path(out.name)
                out.write(updated)
            try:
                os.chmod(temporary, path.stat().st_mode & 0o777)
                if path.read_bytes() != original:
                    raise ValueError("Config changed during installation")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    print(plugin.as_uri())


if __name__ == "__main__":
    main()
