#!/usr/bin/env python3
"""The hub's OAuth grant with hosted Notion MCP (../docs/adr/0001-*.md).

Stored in Secret mcp-hub/mcp-hub-notion-grant, outside git, created by
../tools/bootstrap-notion.py. Keys: token_endpoint, client_id, client_secret
(may be empty: public client), refresh_token, access_token, expires_at (epoch
seconds), consented_at (ISO date).

Notion rotates the refresh token on every refresh and revokes the whole grant
if a rotated-out one is replayed, so this process is the only refresher: one
lock, and each new token pair is written back to the Secret right away. If
that write fails, the pair is kept in memory and the write retried on every
request (at most every FLUSH_EVERY seconds) until it lands. A restart inside
that window makes the hub refresh with the previous refresh token, which
Notion's docs say stays valid next to the current one ("at most two valid at
once"); the retry keeps the window short rather than relying on that.
"""

import base64
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from upstream import UpstreamError

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
FLUSH_EVERY = 30
EXPIRED = ("Notion grant expired or revoked (consented {consented}; grants last at most 180 days, "
           "30 without use). Re-run 2-k3s/23.mcp-hub/tools/bootstrap-notion.py.")


class SecretStore:
    """read() -> (dict, resourceVersion); write(dict, resourceVersion). Uses the pod ServiceAccount."""

    def __init__(self, namespace, name, api="https://kubernetes.default.svc"):
        self.url = f"{api}/api/v1/namespaces/{namespace}/secrets/{name}"

    def _call(self, method, body=None):
        import ssl

        with open(f"{SA}/token") as f:
            token = f.read().strip()
        req = urllib.request.Request(self.url, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": f"Bearer {token}",
                                              "Content-Type": "application/merge-patch+json"})
        ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            return json.load(resp)

    def read(self):
        try:
            s = self._call("GET")
            data = {k: base64.b64decode(v).decode() for k, v in (s.get("data") or {}).items()}
            return data, s["metadata"]["resourceVersion"]
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise UpstreamError("no Notion grant yet: run 2-k3s/23.mcp-hub/tools/bootstrap-notion.py") from None
            raise UpstreamError(f"cannot read the Notion grant Secret (HTTP {e.code})") from None
        except (OSError, KeyError, ValueError) as e:  # URLError is an OSError; no SA token also lands here
            raise UpstreamError(f"cannot read the Notion grant Secret: {type(e).__name__}: {e}") from None

    def write(self, data, resource_version):
        self._call("PATCH", {"metadata": {"resourceVersion": resource_version},
                             "data": {k: base64.b64encode(str(v).encode()).decode() for k, v in data.items()}})


def post_form(url, form):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "Accept": "application/json",
                                          # Cloudflare in front of Notion answers 403 to Python-urllib's UA.
                                          "User-Agent": "epaflix-mcp-hub/1"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return 200, json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {}
    except OSError as e:
        raise UpstreamError(f"Notion token endpoint unreachable: {e}") from None


class Grant:
    def __init__(self, store, post=post_form, clock=time.time):
        self.store, self.post, self.clock = store, post, clock
        self._lock = threading.Lock()
        self._token, self._expires, self._rejected = None, 0.0, None
        self._unsaved = None  # (refresh token still in the Secret, rotated pair not yet written back)
        self._next_flush = 0.0

    def headers(self):
        return {"Authorization": f"Bearer {self.access_token()}"}

    def invalidate(self):
        """Upstream said 401: never reuse this access token."""
        with self._lock:
            self._rejected, self._token = self._token, None

    def _flush(self):
        """Retry writing an unsaved rotated pair; drop it if a bootstrap replaced the grant meanwhile."""
        if not self._unsaved or self.clock() < self._next_flush:
            return
        self._next_flush = self.clock() + FLUSH_EVERY
        try:
            data, rv = self.store.read()
            base, pair = self._unsaved
            if data.get("refresh_token") == base:
                self.store.write(pair, rv)
            self._unsaved = None
        except Exception as e:
            print(f"notion: rotated grant still unsaved, retrying: {e}", file=sys.stderr)

    def access_token(self):
        with self._lock:
            self._flush()
            if self._token and self.clock() < self._expires - 300:
                return self._token
            data, rv = self.store.read()
            if self._unsaved:
                base, pair = self._unsaved
                if data.get("refresh_token") == base:  # our last rotation never reached the Secret; it is newer
                    data = {**data, **pair}
                else:  # a bootstrap replaced the grant meanwhile
                    self._unsaved = None
            token, expires = data.get("access_token"), float(data.get("expires_at") or 0)
            if token and token != self._rejected and self.clock() < expires - 300:
                self._token, self._expires = token, expires
                return token
            return self._refresh(data, rv)

    def _refresh(self, data, rv):
        if not data.get("refresh_token"):
            raise UpstreamError(EXPIRED.format(consented=data.get("consented_at", "?")))
        if not (data.get("client_id") and data.get("token_endpoint")):
            raise UpstreamError("the Notion grant Secret is incomplete; re-run 2-k3s/23.mcp-hub/tools/bootstrap-notion.py")
        form = {"grant_type": "refresh_token", "refresh_token": data["refresh_token"], "client_id": data["client_id"]}
        if data.get("client_secret"):
            form["client_secret"] = data["client_secret"]
        status, body = self.post(data["token_endpoint"], form)
        if status != 200 or "access_token" not in body:
            if body.get("error") == "invalid_grant":
                raise UpstreamError(EXPIRED.format(consented=data.get("consented_at", "?")))
            raise UpstreamError(f"Notion token refresh failed (HTTP {status}): {json.dumps(body)[:300]}")
        new = {"access_token": body["access_token"],
               "expires_at": str(int(self.clock() + int(body.get("expires_in", 3600)))),
               "refresh_token": body.get("refresh_token", data["refresh_token"])}
        stored = self._unsaved[0] if self._unsaved else data["refresh_token"]
        self._unsaved = (stored, new)
        try:
            self.store.write(new, rv)
            self._unsaved = None
        except Exception as e:  # keep serving; _flush retries the write
            self._next_flush = self.clock() + FLUSH_EVERY
            print(f"notion: could not save the rotated grant, retrying: {e}", file=sys.stderr)
        self._token, self._expires, self._rejected = new["access_token"], float(new["expires_at"]), None
        return self._token


def _selftest():
    class Store:
        def __init__(self, data):
            self.data, self.rv, self.writes, self.fail = dict(data), "1", 0, False

        def read(self):
            return dict(self.data), self.rv

        def write(self, data, rv):
            if self.fail:
                raise OSError("api down")
            assert rv == self.rv, "stale resourceVersion"
            self.data.update(data)
            self.rv, self.writes = str(int(self.rv) + 1), self.writes + 1

    now = [1000.0]
    calls = []

    def post(url, form):
        calls.append(form["refresh_token"])
        if form["refresh_token"] == "dead":
            return 400, {"error": "invalid_grant"}
        n = len(calls)
        return 200, {"access_token": f"a{n}", "refresh_token": f"r{n}", "expires_in": 28800}

    base = {"token_endpoint": "https://x/token", "client_id": "c", "refresh_token": "r0",
            "access_token": "a0", "expires_at": "2000", "consented_at": "2026-09-24"}
    store = Store(base)
    g = Grant(store, post=post, clock=lambda: now[0])
    assert g.access_token() == "a0" and not calls, "valid stored token is used without refreshing"
    now[0] = 1800  # within 5 min of expiry
    assert g.access_token() == "a1" and calls == ["r0"] and store.data["refresh_token"] == "r1"
    assert g.access_token() == "a1" and len(calls) == 1, "cached"
    g.invalidate()
    assert g.access_token() == "a2" and calls[-1] == "r1", "a rejected token forces a refresh even if unexpired"
    store.fail = True
    g.invalidate()
    assert g.access_token() == "a3" and store.data["refresh_token"] == "r2", "write failed, Secret still old"
    store.fail = False
    assert g.access_token() == "a3" and store.data["refresh_token"] == "r2", "flush waits FLUSH_EVERY"
    now[0] += FLUSH_EVERY
    assert g.access_token() == "a3" and store.data["refresh_token"] == "r3", "flushed on a cached request"
    store.fail = True
    g.invalidate()
    assert g.access_token() == "a4" and store.data["refresh_token"] == "r3"
    store.fail = False
    g.invalidate()
    assert g.access_token() == "a5" and calls[-1] == "r4", "must refresh with the unsaved newest token, not r3"
    assert store.data["refresh_token"] == "r5"
    store.fail = True
    g.invalidate()
    assert g.access_token() == "a6"  # unsaved r6; Secret keeps r5
    store.fail = False
    store.data.update(refresh_token="boot", access_token="fresh", expires_at="999999")  # bootstrap ran
    g.invalidate()
    assert g.access_token() == "fresh", "a bootstrapped grant wins over an unsaved rotation of the old one"
    broken = Grant(Store({"refresh_token": "x", "expires_at": "0"}), post=post, clock=lambda: now[0])
    try:
        broken.access_token()
        raise AssertionError("an incomplete Secret must raise UpstreamError")
    except UpstreamError as e:
        assert "incomplete" in str(e), e
    dead = Grant(Store({**base, "refresh_token": "dead", "expires_at": "0"}), post=post, clock=lambda: now[0])
    try:
        dead.access_token()
        raise AssertionError("invalid_grant must raise")
    except UpstreamError as e:
        assert "bootstrap-notion.py" in str(e) and "2026-09-24" in str(e), e
    print("notion grant selftest OK")


if __name__ == "__main__":
    _selftest()
