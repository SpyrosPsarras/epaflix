#!/usr/bin/env python3
"""Create or renew the hub's Notion grant (../docs/adr/0001-*.md), on a
workstation with a browser and kubectl access to the cluster.

  bootstrap-notion.py [--context epaflix]

Hosted Notion MCP is OAuth-only. This script:
  1. reads the authorization-server metadata of https://mcp.notion.com;
  2. reuses the client_id already in Secret mcp-hub/mcp-hub-notion-grant, or
     registers a new public client (dynamic client registration) whose
     redirect URI is a fixed loopback port;
  3. runs the PKCE authorization-code flow in your browser: log in with the
     Notion account whose permissions every hub client will act with;
  4. writes access and refresh token straight into the Secret with kubectl
     (not git: the hub rotates the refresh token itself on every refresh).
The hub picks the new grant up on its next request; no restart, no commit.

Run it again when Notion tools answer "Notion grant expired": grants last at
most 180 days from consent (30 days without use). Nothing secret is printed.
Stdlib only.
"""

import argparse
import base64
import datetime
import hashlib
import http.server
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

ISSUER = "https://mcp.notion.com"
RESOURCE = "https://mcp.notion.com/mcp"
PORT = 53682  # fixed so the registered redirect URI, and with it the client_id, stays reusable
REDIRECT = f"http://127.0.0.1:{PORT}/callback"
NS, NAME = "mcp-hub", "mcp-hub-notion-grant"


def _json(url, data=None, form=False):
    # Cloudflare in front of Notion answers 403 to Python-urllib's default UA.
    headers = {"Accept": "application/json", "User-Agent": "epaflix-mcp-hub/1"}
    if data is not None:
        if form:
            body, headers["Content-Type"] = urllib.parse.urlencode(data).encode(), "application/x-www-form-urlencoded"
        else:
            body, headers["Content-Type"] = json.dumps(data).encode(), "application/json"
    else:
        body = None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"{url} -> HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")


def _kubectl(ctx, *args, stdin=None):
    return subprocess.run(["kubectl", "--context", ctx, "-n", NS, *args], input=stdin, capture_output=True,
                          text=True)


def _existing(ctx):
    r = _kubectl(ctx, "get", "secret", NAME, "-o", "json")
    if r.returncode != 0:
        return None
    return {k: base64.b64decode(v).decode() for k, v in json.loads(r.stdout).get("data", {}).items()}


def _register(meta):
    reg = _json(meta["registration_endpoint"], {
        "client_name": "epaflix mcp-hub", "redirect_uris": [REDIRECT],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })
    return reg["client_id"], reg.get("client_secret", "")


def _authorize(meta, client_id):
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state, got = secrets.token_urlsafe(16), {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            got.update({k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"mcp-hub: Notion authorization received, you can close this tab.")

        def log_message(self, *_):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "resource": RESOURCE,
    })
    print("Opening the Notion consent page. If no browser appears, open this URL:\n" + url + "\n")
    webbrowser.open(url)
    srv.timeout, deadline = 30, time.time() + 300
    while "state" not in got and time.time() < deadline:
        srv.handle_request()  # stray requests such as /favicon.ico carry no state; keep waiting
    srv.server_close()
    if got.get("state") != state:
        sys.exit("no valid OAuth callback within 5 minutes (or state mismatch); aborting")
    if "code" not in got:
        sys.exit(f"consent failed: {got.get('error_description') or got.get('error', 'no code returned')}")
    return got["code"], verifier


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--context", default="epaflix", help="kubectl context of the cluster running the hub")
    a = p.parse_args()

    meta = _json(ISSUER + "/.well-known/oauth-authorization-server")
    old = _existing(a.context)
    exists, old = old is not None, old or {}
    if old.get("client_id"):
        client_id, client_secret = old["client_id"], old.get("client_secret", "")
        print("reusing the registered Notion OAuth client")
    else:
        client_id, client_secret = _register(meta)
        print("registered a new Notion OAuth client")
    code, verifier = _authorize(meta, client_id)
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": client_id,
            "code_verifier": verifier, "resource": RESOURCE}
    if client_secret:
        form["client_secret"] = client_secret
    tokens = _json(meta["token_endpoint"], form, form=True)
    if "refresh_token" not in tokens:
        sys.exit("Notion returned no refresh_token; nothing written")

    today = datetime.date.today()
    data = {"token_endpoint": meta["token_endpoint"], "client_id": client_id, "client_secret": client_secret,
            "refresh_token": tokens["refresh_token"], "access_token": tokens["access_token"],
            "expires_at": str(int(time.time() + int(tokens.get("expires_in", 3600)))),
            "consented_at": today.isoformat()}
    secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
              "metadata": {"name": NAME, "namespace": NS, "labels": {"app": "mcp-hub"}},
              "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}}
    # replace/create, not apply: apply would copy the tokens into its last-applied annotation.
    verb = "replace" if exists else "create"
    r = _kubectl(a.context, verb, "-f", "-", stdin=json.dumps(secret))
    if r.returncode != 0:
        sys.exit(f"kubectl {verb} failed: {r.stderr.strip()}")
    print(f"wrote Secret {NS}/{NAME}. Hard expiry: {today + datetime.timedelta(days=180)} "
          "(earlier after 30 days without use). Re-run this script before then.")


if __name__ == "__main__":
    main()
