#!/usr/bin/env python3
"""SearXNG JSON guard for the Pi harness web search (epaflix#1038).

Pi's web search reaches SearXNG unauthenticated only while Pi-hole resolves
searxng.epaflix.com to the internal route (192.168.10.102, traefik's
`internal` entrypoint). If that resolution ever falls through to public DNS,
the same request lands on the Authentik-gated public route and the harness
receives an HTML 302 where it expects JSON, which reads as a bad or quiet
search rather than a broken route.

The harness therefore points searxngBaseUrl at this guard - either the
in-cluster deployment (search-guard/ manifests, LoadBalancer IP on the LAN)
or the localhost install (setup-search-guard.sh). The guard forwards the
query to the upstream name and asserts the answer is JSON before the harness
trusts it. A 3xx, a non-JSON content type, an unparsable body or an
unreachable upstream becomes a hard configuration error naming this issue,
instead of an empty or mistaken result set. The guard does not prevent the
break; it stops the break from being silent.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

PORT = 8893
BIND = os.environ.get("SEARCH_GUARD_BIND", "127.0.0.1")
UPSTREAM = "https://searxng.epaflix.com"
TIMEOUT = 30  # seconds, matches pi-web-access's own search timeout
ISSUE = "epaflix#1038"


class NoRedirect(HTTPRedirectHandler):
    """A 3xx from the search endpoint is itself the failure signature."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


opener = build_opener(NoRedirect)


def fetch(url):
    """Return (status, content-type, body) without following redirects."""
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            return response.status, response.headers.get("content-type", ""), response.read()
    except HTTPError as error:
        return error.code, error.headers.get("content-type", ""), error.read()


class Guard(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path.split("?", 1)[0] != "/search":
            self.fail(404, ISSUE + " guard: only /search is served")
            return
        url = UPSTREAM + self.path
        try:
            status, ctype, body = fetch(url)
        except (URLError, OSError) as error:
            self.refuse(
                "could not reach searxng.epaflix.com (%s). SearXNG may be "
                "down, or the name resolved to a host that refuses the "
                "connection; check the Pi-hole 10-epaflix.conf record." % error)
            return
        media_type = (ctype or "").split(";")[0].strip().lower()
        if status != 200 or media_type != "application/json":
            observed = "HTTP %s (%s)" % (status, media_type or "no content type")
            self.refuse(
                "searxng.epaflix.com answered %s instead of 200 JSON. A "
                "redirect or HTML body means the name resolved to the "
                "Authentik-gated public route; check the Pi-hole 10-epaflix.conf "
                "record and the resolver chain." % observed)
            return
        try:
            data = json.loads(body)
        except ValueError as error:
            self.refuse(
                "searxng.epaflix.com answered 200 JSON but the body does "
                "not parse (%s); the name may be answering from a captive portal "
                "or proxy. Check what it resolves to: dig +short "
                "searxng.epaflix.com" % error,
            )
            return
        payload = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.log_message("ok: %s", self.path)

    def refuse(self, message):
        self.fail(502, ISSUE + ": " + message)

    def fail(self, status, message):
        payload = message.encode()
        self.log_message("refusing: %s", message)
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        print(format % args, flush=True)


def main():
    server = ThreadingHTTPServer((BIND, PORT), Guard)
    print("search-guard on http://%s:%d -> %s (%s)" % (BIND, PORT, UPSTREAM, ISSUE), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
