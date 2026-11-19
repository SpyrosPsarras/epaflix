import json
import os
import re
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROUTES = {
    "radarr": ("radarr.servarr.svc.cluster.local", 7878, "Radarr"),
    "sonarr": ("sonarr.servarr.svc.cluster.local", 8989, "Sonarr"),
}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def forward(self):
        app, slash, path = self.path.removeprefix("/").partition("/")
        if not slash or app not in ROUTES or path.startswith("/"):
            self.send_error(404)
            return
        target = "/" + path
        host, port, service = ROUTES[app]
        media = "movie" if app == "radarr" else "series"
        route = urlsplit(target).path
        body = None
        if self.command == "GET":
            if route not in ("/api/v3/parse", f"/api/v3/{media}/lookup") and not re.fullmatch(r"/api/v3/command/[0-9]+", route):
                self.send_error(404)
                return
        else:
            if target != "/api/v3/command":
                self.send_error(404)
                return
            try:
                size = int(self.headers["Content-Length"])
                if not 0 < size <= 1024:
                    raise ValueError
                body = self.rfile.read(size)
                command = json.loads(body)
                if app == "radarr":
                    [item] = command["movieIds"]
                    expected = {"name": "RefreshMovie", "movieIds": [item]}
                else:
                    item = command["seriesId"]
                    expected = {"name": "RefreshSeries", "seriesId": item}
                if command != expected or type(item) is not int or item < 1:
                    raise ValueError
                body = json.dumps(expected).encode()
            except (ValueError, TypeError, KeyError):
                self.send_error(400)
                return
        key = os.environ.get(f"{app.upper()}_API_KEY")
        if not key:
            self.send_error(503)
            return
        connection = HTTPConnection(host, port, timeout=15)
        try:
            connection.request(self.command, target, body, {"X-Api-Key": key, "Content-Type": "application/json"})
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise ValueError
            record = json.loads(response.read(1024 * 1024))
            if route == "/api/v3/parse":
                result = {media: self.item(record.get(media) or {})}
            elif route.endswith("/lookup"):
                result = [self.item(record[0])] if record else []
            elif self.command == "POST":
                result = self.item(record)
            else:
                status = record.get("status")
                result = {"status": status if status in ("queued", "started", "completed", "failed", "aborted", "cancelled") else "unknown"}
        except (OSError, HTTPException, ValueError, TypeError, KeyError, AttributeError):
            print(f"{service} notification request failed", flush=True)
            self.send_error(502)
            return
        finally:
            connection.close()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    @staticmethod
    def item(record):
        item = record.get("id")
        return {"id": item} if type(item) is int else {}

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 18268), Handler).serve_forever()
