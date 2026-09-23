#!/usr/bin/env python3
"""Probe the Claude subscription through CLIProxyAPI with one tiny request.

The model comes from the live catalog, never from config. A pinned name failed
every run from 2026-09-22 once the subscription stopped serving
claude-haiku-4-5-20251001.

owned_by "anthropic" selects it. In CLIProxyAPI v7.3.15 two channels set that
owner: claude (OAuth or claude-api-key, service_models.go) and devin, for the
Claude models it resells (model_definitions.go). openai-compatibility entries
carry the provider name, so OpenRouter never matches. No devin credential is
configured; if one is added, this probe may land on it instead.

An empty match fails the run: the Claude credential is gone, which is what this
job exists to catch.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ["CLIPROXY_URL"]
KEY = os.environ["CLIPROXY_API_KEY"]


def get_json(path, headers=None, body=None):
    request = urllib.request.Request(
        BASE + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"authorization": f"Bearer {KEY}", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        sys.exit(f"{path}: http {error.code} {error.read()[:400].decode(errors='replace')}")
    except urllib.error.URLError as error:
        sys.exit(f"{path}: {error.reason}")


# Without anthropic-version, CLIProxyAPI answers in the OpenAI list shape, whose
# ids are the routable model names. The Anthropic shape rewrites ids.
catalog = get_json("/v1/models")["data"]
models = sorted(m["id"] for m in catalog if m.get("owned_by") == "anthropic")
if not models:
    sys.exit(f"no anthropic-owned model among {len(catalog)} catalog entries: "
             "Claude credential missing or expired")
model = models[0]
print(f"probing {model} (1 of {len(models)} anthropic-owned)")

reply = get_json(
    "/v1/messages",
    {"x-api-key": KEY, "content-type": "application/json", "anthropic-version": "2023-06-01"},
    {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "just reply pong"}]},
)
if reply.get("type") != "message":
    sys.exit(f"unexpected response: {json.dumps(reply)[:400]}")
print(json.dumps(reply)[:400])
