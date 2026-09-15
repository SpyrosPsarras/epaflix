#!/usr/bin/env python3
"""Offline checks for the SearXNG search guard (epaflix#1038).

Starts the real guard handler on an ephemeral loopback port and feeds it a
patched upstream fetch, so no network is involved. The guard must pass a
200 JSON answer through and turn every other answer - a 3xx redirect, a
non-JSON body, an upstream error status, an unparsable body, an unreachable
upstream - into a 502 whose text names the issue.
"""
import importlib.util
import json
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.request import urlopen

spec = importlib.util.spec_from_file_location(
    "search_guard", Path(__file__).with_name("search-guard.py"))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

server = guard.ThreadingHTTPServer(("127.0.0.1", 0), guard.Guard)
port = server.server_address[1]
Thread(target=server.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % port


def fake_fetch(status=200, ctype="application/json", body=b"", error=None):
    calls = []

    def fetch(url):
        calls.append(url)
        if error is not None:
            raise error
        return status, ctype, body

    fetch.calls = calls
    return fetch


def get(path):
    """GET from the guard; return (status, text) without raising on 4xx/5xx."""
    try:
        with urlopen(base + path, timeout=10) as response:
            return response.status, response.read().decode()
    except OSError as error:
        return error.status if hasattr(error, "status") else None, error.read().decode()


def expect_refusal(name):
    status, text = get("/search?q=test&format=json")
    assert status == 502, "%s: got %s, wanted 502" % (name, status)
    assert "1038" in text, "%s: refusal does not name the issue: %r" % (name, text)


fetch = fake_fetch(200, "application/json", b'{"results":[{"title":"a"}],"answers":[]}')
with patch.object(guard, "fetch", fetch):
    with urlopen(base + "/search?q=a%20%26%20b&format=json", timeout=10) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "application/json"
        body = json.load(response)
    assert body == {"results": [{"title": "a"}], "answers": []}
    assert fetch.calls == [guard.UPSTREAM + "/search?q=a%20%26%20b&format=json"]
print("ok - JSON answer passes through, query forwarded verbatim")

for name, fetcher in [
    ("redirect", fake_fetch(302, "text/html; charset=utf-8", b'<a href="https://auth.epaflix.com/">')),
    ("upstream error status", fake_fetch(503, "text/html", b"boom")),
    ("html with 200", fake_fetch(200, "text/html; charset=utf-8", b"<!DOCTYPE html>")),
    ("wrong json media type", fake_fetch(200, "application/x-yaml", b"{}")),
    ("unparsable body", fake_fetch(200, "application/json", b"<not json>")),
    ("upstream unreachable", fake_fetch(error=OSError("connection refused"))),
]:
    with patch.object(guard, "fetch", fetcher):
        expect_refusal(name)
    print("ok - %s refused as configuration error" % name)

status, text = get("/health")
assert status == 404 and "only /search" in text, (status, text)
print("ok - non-search paths rejected")

print("all search-guard checks passed")
