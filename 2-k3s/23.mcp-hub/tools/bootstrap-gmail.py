#!/usr/bin/env python3
"""Stage a homelab Google grant from a workstation with a browser.

Uses PKCE consent for the complete Gmail and Drive scope set, verifies the
expected account through a hidden prompt and writes directly to Kubernetes.
Nothing secret is printed or written to git.

Usage:
  bootstrap-gmail.py --instance homelab --context <approved-context> --reuse
  bootstrap-gmail.py --instance homelab --context <approved-context> --client-json <private-client-file>

Requires an approved Desktop OAuth client, enabled Gmail and Drive APIs and
a consent screen published to production. See ../instances/README.md.
"""

import argparse
import base64
import hashlib
import getpass
import http.server
import json
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import warnings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
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


def _client(args: argparse.Namespace, namespace: str) -> tuple[str, str]:
    if args.reuse:
        r = subprocess.run(["kubectl", "--context", args.context, "-n", namespace, "get", "secret", "mcp-hub-gmail",
                            "-o", "json"], capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit("Google client read failed; nothing written")
        data = json.loads(r.stdout)["data"]
        return (base64.b64decode(data["client-id"]).decode(), base64.b64decode(data["client-secret"]).decode())
    with open(args.client_json) as f:
        data = json.load(f)
    c = data.get("installed")
    if not isinstance(c, dict) or not all(isinstance(c.get(key), str) and c[key].strip()
                                          for key in ("client_id", "client_secret")):
        sys.exit("a Desktop OAuth client JSON is required; nothing written")
    return c["client_id"], c["client_secret"]


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
        browser = subprocess.run(
            [sys.executable, "-c", "import sys, webbrowser; sys.exit(not webbrowser.open(sys.stdin.read()))"],
            input=url, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if browser.returncode != 0:
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


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--reuse", action="store_true",
                   help="reuse the OAuth client in the selected instance Secret, needs kubectl")
    p.add_argument("--context", help="kubectl context for --reuse or --instance")
    p.add_argument("--instance", choices=("homelab",), required=True)
    p.add_argument("--client-json", help="client_secret_*.json downloaded from the Google Cloud console")
    # Parsed only so argparse never echoes a pasted client secret as an unrecognized argument.
    p.add_argument("--client-secret", help=argparse.SUPPRESS)
    args = p.parse_args()
    if not args.context:
        p.error("--instance requires an explicit --context")
    if args.client_secret is not None:
        p.error("pass the OAuth client with --client-json or --reuse")
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
        tokens = _authorize_instance(client_id, client_secret)
        mailbox = _profile(tokens["access_token"])
        if mailbox != expected:
            sys.exit("Google account mismatch")
        _write_instance_secret(args.context, {"client-id": client_id, "client-secret": client_secret,
                                             "refresh-token": tokens["refresh_token"], "email": mailbox})
    except Exception:
        sys.exit("homelab Google consent or staging failed; no automatic retry")
    print("homelab Google grant staged; rollout still required")


if __name__ == "__main__":
    main()
