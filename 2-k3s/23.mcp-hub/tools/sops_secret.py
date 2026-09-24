#!/usr/bin/env python3
"""Write a whole sops-encrypted Secret with a fresh plaintext
mcp-hub.epaflix.com/revision annotation (the kustomizations copy it into the
pod template so a changed Secret rolls the pod). Encrypting needs only sops
and the age recipient in .sops.yaml, no private key.

  rotate-keepass-secret:  sops_secret.py --keepass   new hub<->keepass shared secret, both copies
"""

import json
import os
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

ROOT = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True,
                      cwd=os.path.dirname(os.path.abspath(__file__))).stdout.strip()


def encrypt(path, name, namespace, data):
    """Return a temp file next to path holding the encrypted Secret; caller renames it into place."""
    path = os.path.join(ROOT, path)
    rev = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".rotate-", suffix=".enc.yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(f"apiVersion: v1\nkind: Secret\nmetadata:\n  name: {name}\n  namespace: {namespace}\n"
                    f"  annotations:\n    mcp-hub.epaflix.com/revision: \"{rev}\"\ntype: Opaque\nstringData:\n")
            for k, v in data.items():
                f.write(f"  {k}: {json.dumps(v)}\n")
        # Relative name so .sops.yaml path rules match.
        subprocess.run(["sops", "-e", "-i", os.path.basename(tmp)], cwd=os.path.dirname(path), check=True)
        os.chmod(tmp, 0o644)
        return tmp, path
    except BaseException:
        os.unlink(tmp)
        raise


def publish(staged):
    """Rename every encrypted temp file into place only after all of them encrypted."""
    for tmp, path in staged:
        os.replace(tmp, path)
        print(f"wrote {os.path.relpath(path, ROOT)}")


def keepass():
    secret = secrets.token_urlsafe(32)
    staged = []
    try:
        staged.append(encrypt("2-k3s/15.syncthing/keepass-hub-secret.enc.yaml", "keepass-hub-secret", "syncthing",
                              {"secret": secret}))
        staged.append(encrypt("2-k3s/23.mcp-hub/mcp-hub-keepass.enc.yaml", "mcp-hub-keepass", "mcp-hub",
                              {"secret": secret}))
    except BaseException:
        for tmp, _ in staged:
            os.unlink(tmp)
        raise
    publish(staged)
    print("Commit both, merge. ArgoCD rolls the hub and the keepass pod; /keepass fails until both rolled.")


if __name__ == "__main__":
    if sys.argv[1:] == ["--keepass"]:
        keepass()
    else:
        sys.exit(__doc__)
