#!/usr/bin/env python3
"""Create a restricted MCP SSH identity and stream its Secret to kubectl.

Run on the LXC as the user whose existing keepass-mcp wrapper unlocks the vault.
No credential values are printed. The local identity is retained for re-runs.
"""
import base64
import json
from pathlib import Path
import subprocess


def main():
    home = Path.home()
    directory = home / ".local/share/t3-vault-access"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = directory / "identity"
    if not identity.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t3env-vault-only", "-f", str(identity)], check=True)
    public = identity.with_suffix(".pub").read_text().strip()
    ssh = home / ".ssh"
    ssh.mkdir(exist_ok=True, mode=0o700)
    authorized = ssh / "authorized_keys"
    content = authorized.read_text() if authorized.exists() else ""
    line = 'restrict,command="/usr/local/bin/keepass-mcp" ' + public
    if public.split()[1] not in content:
        with authorized.open("a") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write(line + "\n")
        authorized.chmod(0o600)
    host = "192.168.10.240"
    host_key = Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text().split()
    known = f"{host} {host_key[0]} {host_key[1]}\n"
    (directory / "known_hosts").write_text(known)
    secret = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": "t3env-vault-access", "namespace": "remote-pi"},
        "type": "Opaque",
        "data": {name: base64.b64encode(value).decode() for name, value in {
            "identity": identity.read_bytes(), "known_hosts": known.encode(),
        }.items()},
    }
    encrypted = subprocess.run([
        "sops", "--encrypt", "--input-type", "json", "--output-type", "yaml",
        "--filename-override", "2-k3s/13.t3code/env/vault-access.enc.yaml", "/dev/stdin",
    ], input=json.dumps(secret).encode(), stdout=subprocess.PIPE, check=True)
    destination = Path(__file__).resolve().parents[1] / "vault-access.enc.yaml"
    destination.write_bytes(encrypted.stdout)
    subprocess.run(["kubectl", "--context", "epaflix", "apply", "--server-side", "-f", "-"], input=json.dumps(secret).encode(), check=True, stdout=subprocess.DEVNULL)
    print("Restricted vault identity installed; encrypted manifest saved; runtime Secret applied.")


if __name__ == "__main__":
    main()
