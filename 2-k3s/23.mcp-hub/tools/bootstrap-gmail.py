#!/usr/bin/env python3
"""One-time, on a workstation with a browser: turn a Google OAuth desktop
client into the SOPS-encrypted Secret the hub's /gmail server needs.

Steps performed:
  1. PKCE authorization-code flow for scope gmail.modify (access_type=offline,
     prompt=consent so Google always returns a refresh token).
  2. Exchange the code, fetch the profile to print which mailbox was granted.
  3. Write ../mcp-hub-gmail.enc.yaml (client-id, client-secret, refresh-token,
     plus a fresh revision annotation that restarts the hub), encrypted with
     sops via a temp file. Encryption only needs the age
     recipient from .sops.yaml, not the private key.

Nothing secret is printed. Stdlib only; no pip install.

Usage:
  bootstrap-gmail.py --client-json ~/Downloads/client_secret_*.json
  bootstrap-gmail.py --client-id ID --client-secret SECRET

Prerequisites (Google Cloud console, once): a project with the Gmail API
enabled, an OAuth consent screen of user type External published to
"In production" (Testing-status refresh tokens expire after 7 days), and an
OAuth client of type Desktop app. See ../README.md.
"""

import argparse
import base64
import datetime
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
SCOPE = "https://www.googleapis.com/auth/gmail.modify"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "mcp-hub-gmail.enc.yaml")


def _client(args):
    if args.client_json:
        with open(args.client_json) as f:
            data = json.load(f)
        c = data.get("installed") or data.get("web")
        if not c:
            sys.exit("client JSON has neither 'installed' nor 'web'; download the Desktop app client")
        return c["client_id"], c["client_secret"]
    if args.client_id and args.client_secret:
        return args.client_id, args.client_secret
    sys.exit("pass --client-json FILE or --client-id/--client-secret")


def _authorize(client_id, client_secret):
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    got = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"mcp-hub: authorization received, you can close this tab.")

        def log_message(self, *_):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{srv.server_port}/"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    print("Opening the Google consent page. If no browser appears, open this URL:\n" + url + "\n")
    webbrowser.open(url)
    srv.timeout = 300
    srv.handle_request()  # one callback, or timeout
    srv.server_close()
    if not got:
        sys.exit("no OAuth callback within 5 minutes; aborting")
    if got.get("state") != state:
        sys.exit("state mismatch in the OAuth callback; aborting")
    if "code" not in got:
        sys.exit(f"consent failed: {got.get('error', 'no code returned')}")

    data = urllib.parse.urlencode({
        "code": got["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier,
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data), timeout=30) as resp:
        tokens = json.load(resp)
    if "refresh_token" not in tokens:
        sys.exit("Google returned no refresh_token. Revoke the app at myaccount.google.com/permissions and retry.")
    return tokens


def _profile(access_token):
    req = urllib.request.Request(PROFILE_URL, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["emailAddress"]


def _write_secret(path, client_id, client_secret, refresh_token):
    path = os.path.abspath(path)
    # The revision annotation stays plaintext (sops encrypts only stringData);
    # ../kustomization.yaml copies it into the pod template so the hub restarts.
    revision = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # JSON string literals are valid YAML double-quoted scalars, so no PyYAML.
    doc = "\n".join([
        "apiVersion: v1", "kind: Secret", "metadata:", "  name: mcp-hub-gmail", "  namespace: mcp-hub",
        "  annotations:", f'    mcp-hub.epaflix.com/revision: "{revision}"',
        "type: Opaque", "stringData:",
        f"  client-id: {json.dumps(client_id)}",
        f"  client-secret: {json.dumps(client_secret)}",
        f"  refresh-token: {json.dumps(refresh_token)}", "",
    ])
    # Encrypt a sibling temp file (name still matches .sops.yaml's `\.enc\.yaml$`
    # rule) and move it into place, so a sops failure leaves the committed
    # file untouched and no plaintext behind.
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".bootstrap-", suffix=".enc.yaml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(doc)
        subprocess.run(["sops", "-e", "-i", tmp], check=True, cwd=os.path.dirname(path))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except (OSError, subprocess.CalledProcessError) as e:
        sys.exit(f"sops encryption failed ({e}); {os.path.relpath(path)} left unchanged")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--client-json", help="client_secret_*.json downloaded from the Google Cloud console")
    p.add_argument("--client-id")
    p.add_argument("--client-secret")
    p.add_argument("--out", default=OUT, help=f"encrypted Secret to write (default {os.path.relpath(OUT)})")
    args = p.parse_args()

    client_id, client_secret = _client(args)
    tokens = _authorize(client_id, client_secret)
    mailbox = _profile(tokens["access_token"])
    _write_secret(args.out, client_id, client_secret, tokens["refresh_token"])
    print(f"granted mailbox: {mailbox}")
    print(f"wrote {os.path.relpath(args.out)} (sops-encrypted, new revision annotation). "
          "Commit and merge it; ArgoCD syncs the Secret and the annotation change restarts the hub.")


if __name__ == "__main__":
    main()
