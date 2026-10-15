#!/usr/bin/env python3
"""Stage local client grants, configure Pi, or let the owner copy a token to the vault."""

import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import sys
import urllib.error
import urllib.request

import hub_clients
import hub_services


def command(argv, stdin=None):
    return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=60)


def hub_url(environment):
    return "https://mcp-" + environment + ".epaflix.com"


def client_secret_name(machine):
    return "mcp-hub-client-" + machine


def hub_namespace(environment):
    return "mcp-hub-" + environment


def kubectl(context, environment, *args, document=None):
    return command(["kubectl", "--context", context, "-n", hub_namespace(environment), *args],
                   json.dumps(document) if document is not None else None)


def secret(context, environment, name, optional=False):
    result = kubectl(context, environment, "get", "secret", name, "-o", "json")
    if result.returncode:
        if optional and ("NotFound" in result.stderr or "not found" in result.stderr):
            return None
        raise RuntimeError("required Secret is unavailable")
    return json.loads(result.stdout)


def encode(value):
    return base64.b64encode(value.encode()).decode()


def token(document, environment):
    value = base64.b64decode(document["data"]["token"], validate=True).decode()
    if not re.fullmatch(re.escape(environment) + r"\.[A-Za-z0-9_-]{43}", value):
        raise RuntimeError("client Secret has an invalid token")
    return value


def credentials(document):
    value = json.loads(base64.b64decode(document["data"]["credentials.json"], validate=True))
    if not isinstance(value, dict) or set(value) != {"clients", "upstreams"} or not isinstance(value["clients"], dict):
        raise RuntimeError("invalid instance credentials")
    return value


def provision(context, environment, machine):
    instance = secret(context, environment, "mcp-hub-instance")
    existing = credentials(instance)["clients"].get(machine)
    name = client_secret_name(machine)
    staged = secret(context, environment, name, optional=True)
    if existing is not None and staged is None:
        raise RuntimeError("existing client grant has no matching staged token; refusing rotation")
    if staged is None:
        value = environment + "." + secrets.token_urlsafe(32)
        document = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": {
            "name": name, "namespace": hub_namespace(environment)}, "data": {"token": encode(value)}}
        result = kubectl(context, environment, "create", "-f", "-", document=document)
        if result.returncode:
            staged = secret(context, environment, name)
            value = token(staged, environment)
    else:
        value = token(staged, environment)
    digest = hashlib.sha256(value.encode()).hexdigest()
    for _ in range(5):
        instance = secret(context, environment, "mcp-hub-instance")
        updated = credentials(instance)
        existing = updated["clients"].get(machine)
        if existing is not None:
            if existing != digest:
                raise RuntimeError("existing client grant differs; refusing rotation")
            return
        updated["clients"][machine] = digest
        patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": instance["metadata"]["resourceVersion"]},
                 {"op": "add", "path": "/data/credentials.json", "value": encode(json.dumps(updated))}]
        result = kubectl(context, environment, "patch", "secret", "mcp-hub-instance", "--type=json",
                         "--patch-file=/dev/stdin", document=patch)
        if not result.returncode:
            return
    raise RuntimeError("could not append client grant after concurrent updates")


def services(context, environment):
    result = kubectl(context, environment, "get", "secret", "mcp-hub-instance", "-o",
                     r"jsonpath={.data.services\.json}")
    if result.returncode:
        raise RuntimeError("approved service allowlist is unavailable")
    return hub_services.validate_services(json.loads(base64.b64decode(result.stdout, validate=True)), environment)


def configure(context, environment, target):
    selected = services(context, environment)
    before = hub_clients.load_json(str(target))
    after = copy.deepcopy(before)
    servers = after.setdefault("mcpServers", {})
    prefix = environment + "-"
    for name in hub_clients.MANAGED:
        servers.pop(prefix + name, None)
    entries = hub_clients.pi({}, hub_url(environment),
                             "MCP_HUB_" + environment.upper() + "_TOKEN", services=selected)["mcpServers"]
    servers.update({prefix + name: entry for name, entry in entries.items()})
    if after != before:
        hub_clients.write_json(str(target), after)


def vault(context, environment, machine):
    status = command(["bw", "status"])
    if status.returncode or json.loads(status.stdout).get("status") != "unlocked":
        raise RuntimeError("owner must unlock their vault in this shell first")
    title = input("Vault item title: ").strip()
    if not title:
        raise RuntimeError("a vault item title is required")
    value = token(secret(context, environment, client_secret_name(machine)), environment)
    template = command(["bw", "get", "template", "item"])
    if template.returncode:
        raise RuntimeError("vault template unavailable")
    item = json.loads(template.stdout)
    item.update({"type": 1, "name": title, "login": {"username": machine, "password": value,
        "uris": [{"uri": hub_url(environment), "match": None}]}})
    encoded = command(["bw", "encode"], json.dumps(item))
    if encoded.returncode or command(["bw", "create", "item"], encoded.stdout).returncode:
        raise RuntimeError("vault write failed")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def check(context, machine):
    tokens = {environment: token(secret(context, environment, client_secret_name(machine)), environment)
              for environment in hub_services.ENVIRONMENTS}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "local-check", "version": "1"}}}
    for environment, value in tokens.items():
        selected = hub_services.client_servers(services(context, environment))
        if not selected:
            raise RuntimeError("no model-facing route is configured")
        url = hub_url(environment) + next(iter(selected.values()))
        other = next(tokens[peer] for peer in hub_services.ENVIRONMENTS if peer != environment)
        for bearer in (other, value):
            request = urllib.request.Request(url, data=json.dumps(init).encode(), headers={
                "Authorization": "Bearer " + bearer, "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream"})
            try:
                response = opener.open(request, timeout=30)
            except urllib.error.HTTPError as error:
                error.close()
                if bearer == other and error.code == 401:
                    continue
                raise RuntimeError("hub request failed") from None
            with response:
                if bearer == other:
                    raise RuntimeError("hub accepted the other hub's token")
                body = response.read(1024 * 1024 + 1)
                if response.status != 200 or len(body) > 1024 * 1024:
                    raise RuntimeError("invalid initialize response")
                if response.headers.get("content-type", "").startswith("text/event-stream"):
                    body = next(line[5:].strip() for line in body.splitlines() if line.startswith(b"data:"))
                result = json.loads(body)
                if not isinstance(result.get("result"), dict) or "protocolVersion" not in result["result"]:
                    raise RuntimeError("hub upstream did not initialize")
        print("local-clients: " + environment + " initialized; other hub token rejected")


def main():
    actions = {
        "provision": (provision, ("context", "instance", "machine")),
        "configure": (configure, ("context", "instance", "config")),
        "vault": (vault, ("context", "instance", "machine")),
        "check": (check, ("context", "machine")),
    }
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=actions)
    parser.add_argument("--context", required=True, choices=("epaflix",))
    parser.add_argument("--instance", choices=hub_services.ENVIRONMENTS)
    parser.add_argument("--machine", choices=("homepc", "laptop"))
    parser.add_argument("--config", type=Path, default=Path.home() / ".pi/agent/mcp.json")
    args = parser.parse_args()
    action, parameters = actions[args.action]
    for name in parameters:
        if getattr(args, name) is None:
            parser.error("--" + name + " is required for this action")
    try:
        action(*(getattr(args, name) for name in parameters))
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, StopIteration, subprocess.SubprocessError):
        print("local-clients: operation failed; credential details suppressed", file=sys.stderr)
        return 1
    print("local-clients: " + args.action + " succeeded for " + (args.instance or args.machine))
    return 0


if __name__ == "__main__":
    sys.exit(main())
