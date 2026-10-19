#!/usr/bin/env python3
"""One-time, on a workstation with a browser: turn a Google OAuth desktop
client into the SOPS-encrypted Secret the hub's /gmail and /drive servers
need (one refresh token covers both).

Steps performed:
  1. PKCE authorization-code flow for scopes gmail.modify and drive
     (access_type=offline, prompt=consent so Google always returns a refresh token).
  2. Exchange the code, fetch the profile to print which mailbox was granted.
  3. Write ../mcp-hub-gmail.enc.yaml (client-id, client-secret, refresh-token, email,
     plus a fresh revision annotation that restarts the hub), encrypted with
     sops via a temp file. Encryption only needs the age
     recipient from .sops.yaml, not the private key.

Nothing secret is printed. Stdlib only; no pip install.

Usage:
  bootstrap-gmail.py --reuse    # the client already in the cluster Secret
  bootstrap-gmail.py --client-json ~/Downloads/client_secret_*.json
  bootstrap-gmail.py --client-id ID --client-secret SECRET

Prerequisites (Google Cloud console, once): a project with the Gmail and
Google Drive APIs enabled, an OAuth consent screen of user type External published to
"In production" (Testing-status refresh tokens expire after 7 days), and an
OAuth client of type Desktop app. See ../README.md.
"""

import argparse
import base64
import datetime
import hashlib
import getpass
import http.server
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import webbrowser
import warnings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
SCOPE = "https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/drive"
INSTANCE_SCOPES = {
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
}
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "mcp-hub-gmail.enc.yaml")


def _client(args: argparse.Namespace, namespace: str = "mcp-hub") -> tuple[str, str]:
    if args.reuse:
        r = subprocess.run(["kubectl", "--context", args.context, "-n", namespace, "get", "secret", "mcp-hub-gmail",
                            "-o", "json"], capture_output=True, text=True)
        if r.returncode != 0:
            if namespace != "mcp-hub":
                sys.exit("Google client read failed; nothing written")
            sys.exit(f"kubectl get secret mcp-hub-gmail failed: {r.stderr.strip()}")
        data = json.loads(r.stdout)["data"]
        return (base64.b64decode(data["client-id"]).decode(), base64.b64decode(data["client-secret"]).decode())
    if args.client_json:
        with open(args.client_json) as f:
            data = json.load(f)
        if namespace != "mcp-hub":
            c = data.get("installed")
            if not isinstance(c, dict) or not all(isinstance(c.get(key), str) and c[key].strip()
                                                  for key in ("client_id", "client_secret")):
                sys.exit("a Desktop OAuth client JSON is required; nothing written")
            return c["client_id"], c["client_secret"]
        c = data.get("installed") or data.get("web")
        if not c:
            sys.exit("client JSON has neither 'installed' nor 'web'; download the Desktop app client")
        return c["client_id"], c["client_secret"]
    if args.client_id and args.client_secret:
        return args.client_id, args.client_secret
    sys.exit("pass --reuse, --client-json FILE or --client-id/--client-secret")


def _authorize(client_id, client_secret, *, instance=False) -> dict:
    if instance:
        return _authorize_instance(client_id, client_secret)
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


def _authorize_instance(client_id, client_secret):
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state, got = secrets.token_urlsafe(32), {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path == "/" and ("code" in query or "error" in query):
                got.update({key: values[0] for key, values in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"mcp-hub: return to the local consent command.")

        def log_message(self, *_):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    try:
        redirect = f"http://127.0.0.1:{srv.server_port}/"
        url = AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
            "scope": " ".join(sorted(INSTANCE_SCOPES)), "access_type": "offline",
            "prompt": "select_account consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })
        if not webbrowser.open(url):
            sys.exit("a local browser is required; nothing written")
        deadline = time.monotonic() + 300
        while not got:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            srv.timeout = remaining
            srv.handle_request()
    finally:
        srv.server_close()
    if got.get("state") != state or got.get("error") or not got.get("code"):
        sys.exit("Google consent failed or timed out; nothing written")
    data = urllib.parse.urlencode({
        "code": got["code"], "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier,
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, data=data), timeout=30) as response:
        tokens = json.load(response)
    if not all(isinstance(tokens.get(key), str) and tokens[key].strip()
               for key in ("refresh_token", "access_token", "scope")):
        sys.exit("Google returned an incomplete grant; nothing written")
    if set(tokens["scope"].split()) != INSTANCE_SCOPES:
        sys.exit("Google grant scopes differ from the requested set; nothing written")
    return tokens


def _write_instance_secret(context: str, data: dict[str, str]) -> None:
    command = ["kubectl", "--context", context, "-n", "mcp-hub-homelab"]
    result = subprocess.run(command + ["get", "secret", "mcp-hub-gmail", "--ignore-not-found", "-o", "json"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit("Google Secret read failed; nothing written")
    metadata = {"name": "mcp-hub-gmail", "namespace": "mcp-hub-homelab"}
    verb = "create"
    if result.stdout.strip():
        metadata["resourceVersion"] = json.loads(result.stdout)["metadata"]["resourceVersion"]
        if not metadata["resourceVersion"]:
            sys.exit("Google Secret version missing; nothing written")
        verb = "replace"
    document = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": metadata,
                "data": {key: base64.b64encode(value.encode()).decode() for key, value in data.items()}}
    result = subprocess.run(command + [verb, "-f", "-"], input=json.dumps(document), capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit("Google Secret staging failed; no retry performed")


def _profile(access_token):
    req = urllib.request.Request(PROFILE_URL, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["emailAddress"]


def _write_secret(path, client_id, client_secret, refresh_token, email):
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
        f"  refresh-token: {json.dumps(refresh_token)}",
        f"  email: {json.dumps(email)}", "",
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
    p.add_argument("--reuse", action="store_true",
                   help="reuse the OAuth client already in Secret mcp-hub/mcp-hub-gmail (needs kubectl)")
    p.add_argument("--context", help="kubectl context for --reuse or --instance")
    p.add_argument("--instance", choices=("homelab",))
    p.add_argument("--client-json", help="client_secret_*.json downloaded from the Google Cloud console")
    p.add_argument("--client-id")
    p.add_argument("--client-secret")
    p.add_argument("--out", help=f"encrypted Secret to write (default {os.path.relpath(OUT)})")
    args = p.parse_args()
    if args.instance:
        if not args.context:
            p.error("--instance requires an explicit --context")
        if args.out is not None or args.client_id is not None or args.client_secret is not None:
            p.error("--instance forbids --out and inline OAuth client fields")
        if bool(args.reuse) == bool(args.client_json):
            p.error("--instance requires exactly one of --reuse or --client-json")
        try:
            client_id, client_secret = _client(args, namespace="mcp-hub-homelab")
            if not client_id.strip() or not client_secret.strip():
                sys.exit("OAuth client fields are required")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                expected = getpass.getpass("Expected Google account email, hidden: ").strip()
            if not expected:
                sys.exit("an expected Google account is required")
            tokens = _authorize(client_id, client_secret, instance=True)
            mailbox = _profile(tokens["access_token"])
            if mailbox != expected:
                sys.exit("Google account mismatch")
            _write_instance_secret(args.context, {"client-id": client_id, "client-secret": client_secret,
                                                  "refresh-token": tokens["refresh_token"], "email": mailbox})
        except Exception:
            sys.exit("homelab Google consent or staging failed; no automatic retry")
        print("homelab Google grant staged; rollout still required")
        return
    args.context = args.context if args.context is not None else "epaflix"
    args.out = args.out if args.out is not None else OUT

    client_id, client_secret = _client(args)
    tokens = _authorize(client_id, client_secret)
    mailbox = _profile(tokens["access_token"])
    granted = set(tokens.get("scope", "").split())
    if not set(SCOPE.split()) <= granted:
        sys.exit(f"Google granted only {sorted(granted)}; tick every box on the consent page and retry")
    _write_secret(args.out, client_id, client_secret, tokens["refresh_token"], mailbox)
    print(f"granted mailbox: {mailbox}")
    print(f"wrote {os.path.relpath(args.out)} (sops-encrypted, new revision annotation). "
          "Commit and merge it; ArgoCD syncs the Secret and the annotation change restarts the hub.")


if __name__ == "__main__":
    main()
