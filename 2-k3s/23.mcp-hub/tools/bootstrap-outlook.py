#!/usr/bin/env python3
"""Stage the work hub's Microsoft grant for the Outlook backend.

  bootstrap-outlook.py --context <approved-context> --tenant <tenant-id> --client-id <app-id>

Runs the device-code sign-in against the owner's Entra app registration
(public client, delegated mail scopes, consented for the owner only). Open
the printed Microsoft page in any browser, enter the code and sign in as the
expected account. The script checks the account through a hidden prompt and
the granted scopes, then writes the grant straight into
mcp-hub-work/mcp-hub-outlook-grant with kubectl. The Outlook gateway
refreshes and rotates it from there. A running gateway keeps its cached
access token until near its expiry (up to 90 minutes); restart outlook-mcp
to switch at once. Nothing secret is printed or written to
git. Run it again when Outlook tools answer "Microsoft grant expired".
See ../instances/README.md. Stdlib only.
"""

import argparse
import base64
import datetime
import getpass
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings

NAMESPACE, NAME = "mcp-hub-work", "mcp-hub-outlook-grant"
SCOPES = ("Mail.ReadWrite", "Mail.Send", "MailboxSettings.Read", "MailboxSettings.ReadWrite", "User.Read")
SCOPE = " ".join(SCOPES + ("offline_access",))
ME_URL = "https://graph.microsoft.com/v1.0/me?$select=userPrincipalName"


def post(url, form):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(),
                                 headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except ValueError:
            return {"error": f"HTTP {e.code}"}


def sign_in(tenant, client_id, post=post, sleep=time.sleep, clock=time.monotonic):
    base = f"https://login.microsoftonline.com/{urllib.parse.quote(tenant, safe='')}/oauth2/v2.0"
    code = post(base + "/devicecode", {"client_id": client_id, "scope": SCOPE})
    if not code.get("device_code") or not code.get("message"):
        sys.exit("Microsoft refused the device-code request; nothing written: " + str(code.get("error", "?")))
    print(code["message"], flush=True)
    interval, deadline = int(code.get("interval", 5)), clock() + int(code.get("expires_in", 900))
    while clock() < deadline:
        sleep(interval)
        tokens = post(base + "/token", {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                        "client_id": client_id, "device_code": code["device_code"]})
        error = tokens.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error or not all(isinstance(tokens.get(key), str) and tokens[key] for key in
                            ("access_token", "refresh_token", "scope")):
            sys.exit("Microsoft sign-in failed; nothing written: " + str(error or "incomplete grant"))
        granted = {scope.rsplit("/", 1)[-1].lower() for scope in tokens["scope"].split()}
        if not {scope.lower() for scope in SCOPES} <= granted:
            sys.exit("Microsoft granted fewer scopes than requested; nothing written")
        return base + "/token", tokens
    sys.exit("Microsoft sign-in timed out; nothing written")


def account(access_token):
    req = urllib.request.Request(ME_URL, headers={"Authorization": "Bearer " + access_token})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["userPrincipalName"]


def write_secret(context, data, run=subprocess.run):
    command = ["kubectl", "--context", context, "-n", NAMESPACE]
    result = run(command + ["get", "secret", NAME, "--ignore-not-found", "-o", "json"],
                 capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit("Microsoft grant Secret read failed; nothing written")
    metadata = {"name": NAME, "namespace": NAMESPACE}
    verb = "create"
    if result.stdout.strip():
        metadata["resourceVersion"] = json.loads(result.stdout)["metadata"]["resourceVersion"]
        verb = "replace"
    document = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": metadata,
                "data": {key: base64.b64encode(value.encode()).decode() for key, value in data.items()}}
    # replace/create, not apply: apply would copy the tokens into its last-applied annotation.
    result = run(command + [verb, "-f", "-"], input=json.dumps(document), capture_output=True, text=True)
    if result.returncode != 0:
        sys.exit("Microsoft grant Secret staging failed; no retry performed")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--context", required=True, help="kubectl context of the cluster running the work hub")
    p.add_argument("--tenant", required=True, help="Entra tenant ID")
    p.add_argument("--client-id", required=True, help="application (client) ID of the owner's app registration")
    args = p.parse_args()
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        expected = getpass.getpass("Expected Microsoft account (UPN), hidden: ").strip().lower()
    if not expected:
        sys.exit("an expected Microsoft account is required")
    endpoint, tokens = sign_in(args.tenant, args.client_id)
    try:
        if account(tokens["access_token"]).lower() != expected:
            sys.exit("Microsoft account mismatch; nothing written")
    except (OSError, KeyError, ValueError):
        sys.exit("Microsoft account check failed; nothing written")
    write_secret(args.context, {
        "token_endpoint": endpoint, "client_id": args.client_id, "client_secret": "", "scope": SCOPE,
        "refresh_token": tokens["refresh_token"], "access_token": tokens["access_token"],
        "expires_at": str(int(time.time() + int(tokens.get("expires_in", 3600)))),
        "consented_at": datetime.date.today().isoformat()})
    print(f"wrote Secret {NAMESPACE}/{NAME}; a running gateway switches within 90 minutes, or restart outlook-mcp")


if __name__ == "__main__":
    main()
