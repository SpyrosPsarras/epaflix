#!/usr/bin/env python3
"""Probe the Claude subscription through CLIProxyAPI with one tiny request.

The model comes from the live catalog, never from config. A pinned name failed
every run from 2026-09-22 once claude-haiku-4-5-20251001 left the catalog. The
cause was a hand-set excluded_models entry on the credential, not the
subscription; reconcile-config.psql now keeps Haiku enabled.

owned_by "anthropic" selects it. In CLIProxyAPI v7.3.15 two channels set that
owner: claude (OAuth or claude-api-key, service_models.go) and devin, for the
Claude models it resells (model_definitions.go). openai-compatibility entries
carry the provider name, so OpenRouter never matches. No devin credential is
configured; if one is added, this probe may land on it instead.

An empty match fails the run: the Claude credential is gone, which is what this
job exists to catch.

The probe also opens the subscription's 5-hour usage window. The daytime runs are
5h apart, so a probe that lands a few seconds early falls inside the window it
should replace and that reset is lost. Each probe targets 24s per Oslo hour past
the hour (05:02, 10:04, 15:06, 20:08), so consecutive targets are 5h2m apart. The
probe is sent at most 60s after its target and gives up after 30s, so it reaches
Anthropic at most 90s late and the next window still opens 30s clear of it. A run
that is later than that, or running outside its scheduled hours, fails without
probing; the failed job raises KubeJobFailedLastRun. CLIProxyAPI drops Anthropic's
ratelimit headers (checked 2026-10-08), so the window end cannot be read back
instead.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BASE = os.environ["CLIPROXY_URL"]
KEY = os.environ["CLIPROXY_API_KEY"]


def get_json(path, headers=None, body=None, timeout=120):
    request = urllib.request.Request(
        BASE + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"authorization": f"Bearer {KEY}", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
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
oslo = ZoneInfo("Europe/Oslo")
now = datetime.now(oslo)
if now.hour not in (5, 10, 15, 20):
    sys.exit(f"running at {now:%H:%M}, outside the 05/10/15/20 schedule: skipping so no reset moves")
target = now.replace(minute=0, second=0, microsecond=0) + timedelta(seconds=24 * now.hour)
print(f"probing {model} (1 of {len(models)} anthropic-owned) at {target:%H:%M:%S}")
time.sleep(max(0, (target - now).total_seconds()))
late = (datetime.now(oslo) - target).total_seconds()
if late > 60:
    sys.exit(f"{late:.0f}s past the probe target: skipping so the next run's reset survives")

reply = get_json(
    "/v1/messages",
    {"x-api-key": KEY, "content-type": "application/json", "anthropic-version": "2023-06-01"},
    {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": "just reply pong"}]},
    timeout=30,
)
if reply.get("type") != "message":
    sys.exit(f"unexpected response: {json.dumps(reply)[:400]}")
print(json.dumps(reply)[:400])
