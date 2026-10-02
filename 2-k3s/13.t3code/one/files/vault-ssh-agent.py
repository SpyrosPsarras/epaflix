"""Load the SSH keys in this pod's Vaultwarden collection into an ssh-agent.

Usage: vault-ssh-agent.py <credentials dir> <agent socket> <scratch dir>

The credentials dir is the t3code-vaultwarden Secret: client-id and
client-secret (the account's API key) and password (its master password).
Every SSH key item in the SSH-t3code collection is loaded. The private keys
go from bw to ssh-add through a pipe. bw saves its login (including the API
secret) in its data dir, so that dir lives in the scratch dir, which must be
memory-backed; it is wiped before and after the run.

Exit 0 when the agent holds at least one key, 1 otherwise (no agent left).
"""
import json
import os
import re
import shutil
import subprocess
import sys

SSH_KEY = 5  # Bitwarden cipher type
COLLECTION = "SSH-t3code"
# Seconds per bw/ssh call; startup must not hang on a dead server.
TIMEOUT = int(os.environ.get("VAULT_SSH_AGENT_TIMEOUT", "60"))


def log(message):
    print(f"t3env: vault ssh-agent: {message}", file=sys.stderr)


def wipe(directory):
    for entry in os.listdir(directory):
        path = os.path.join(directory, entry)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)


def main(creds, sock, scratch):
    server = os.environ.get("VAULTWARDEN_URL", "https://vaultwarden.epaflix.com")
    wipe(scratch)  # a killed earlier run may have left bw's data dir behind
    appdata = os.path.join(scratch, "bw")
    os.mkdir(appdata, 0o700)
    env = {"PATH": os.environ["PATH"], "HOME": appdata, "BITWARDENCLI_APPDATA_DIR": appdata, "BW_NOINTERACTION": "true"}

    def bw(*args, **extra):
        try:
            return subprocess.run(["bw", *args], env={**env, **extra}, check=True, capture_output=True,
                                  text=True, timeout=TIMEOUT).stdout
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"bw {args[0]} timed out after {TIMEOUT}s") from None
        except subprocess.CalledProcessError as e:
            # Command name and exit code only: bw's own messages are not ours to vet.
            raise RuntimeError(f"bw {args[0]} failed (exit {e.returncode})") from None

    def read(name):
        with open(os.path.join(creds, name)) as f:
            return f.read().strip()

    try:
        bw("config", "server", server)
        bw("login", "--apikey", BW_CLIENTID=read("client-id"), BW_CLIENTSECRET=read("client-secret"))
        session = bw("unlock", "--raw", "--passwordfile", os.path.join(creds, "password")).strip()
        bw("sync", BW_SESSION=session)
        ids = [c["id"] for c in json.loads(bw("list", "collections", BW_SESSION=session)) if c.get("name") == COLLECTION]
        if len(ids) != 1:
            raise RuntimeError(f"expected one {COLLECTION!r} collection, found {len(ids)}")
        items = json.loads(bw("list", "items", "--collectionid", ids[0], BW_SESSION=session))
    except (OSError, RuntimeError, ValueError, KeyError) as e:
        log(str(e))
        return 1
    finally:
        try:
            bw("logout")
        except (OSError, RuntimeError):
            pass
        wipe(scratch)

    keys = [i for i in items if i.get("type") == SSH_KEY and (i.get("sshKey") or {}).get("privateKey")]
    if not keys:
        log(f"no SSH key items in {COLLECTION!r}")
        return 1

    os.makedirs(os.path.dirname(sock), mode=0o700, exist_ok=True)
    if os.path.exists(sock):  # left by a previous container in this pod
        os.unlink(sock)
    out = subprocess.run(["ssh-agent", "-s", "-a", sock], check=True, capture_output=True, text=True, timeout=TIMEOUT).stdout
    pid = int(re.search(r"SSH_AGENT_PID=(\d+)", out).group(1))
    agent = {**os.environ, "SSH_AUTH_SOCK": sock, "SSH_ASKPASS_REQUIRE": "never"}
    try:
        for n, item in enumerate(keys, 1):
            key = item["sshKey"]["privateKey"].strip() + "\n"
            if subprocess.run(["ssh-add", "-q", "-"], input=key, env=agent, capture_output=True, timeout=TIMEOUT, text=True).returncode:
                log(f"SSH item {n} of {len(keys)} not loaded (passphrase-protected or not OpenSSH/PKCS#8)")
        # Lines are "<bits> SHA256:<fingerprint> <free-text comment> (<type>)";
        # keep the fingerprint field only, the comment can hold anything.
        listed = subprocess.run(["ssh-add", "-l"], env=agent, capture_output=True, text=True, timeout=TIMEOUT).stdout
        fields = [line.split() for line in listed.splitlines()]
        fingerprints = [f[1] for f in fields if len(f) > 1 and re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", f[1])]
        if not fingerprints:
            raise RuntimeError("no key loaded")
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        subprocess.run(["ssh-agent", "-k"], env={**agent, "SSH_AGENT_PID": str(pid)}, capture_output=True, timeout=10)
        log(str(e) if isinstance(e, RuntimeError) else f"ssh-add failed: {type(e).__name__}")
        return 1
    log(f"pid {pid}, {len(fingerprints)}/{len(keys)} keys loaded: {', '.join(fingerprints)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
