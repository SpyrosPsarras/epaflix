import json
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    tdarr_port = 8265

    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def parse_request(self):
        if not super().parse_request():
            return False
        if self.requestline.split()[1] not in ("/status", "/queue"):
            self.send_error(404)
            return False
        if self.command != "POST":
            self.send_response(405)
            self.send_header("Allow", "POST")
            self.end_headers()
            return False
        return True

    def do_POST(self):
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if (len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal()
                    or "Transfer-Encoding" in self.headers or self.headers.get_content_type() != "application/json"):
                raise ValueError
            size = int(lengths[0])
            if not 0 < size <= 16 * 1024:
                raise ValueError
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError
            request = json.loads(raw)
            if not isinstance(request, dict) or request.keys() != {"path"} or not isinstance(request["path"], str):
                raise ValueError
            path = request["path"]
            parts = path.split("/")
            if (len(parts) < 4 or parts[:2] != ["", "media"] or parts[2] not in ("movies", "tvshows", "animes")
                    or any(part in ("", ".", "..") for part in parts[3:])
                    or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in path)):
                raise ValueError
        except (ValueError, RecursionError, TimeoutError):
            self.send_error(400)
            return

        try:
            record = self.lookup("FileJSONDB", path)
            if record.get("_id") != path or record.get("DB") != parts[2]:
                self.send_error(404, "File is not indexed in the expected Tdarr library")
                return
            if self.path == "/queue":
                self.post_tdarr("/api/v2/bulk-update-files", {
                    "fileIds": [path], "updatedObj": {"TranscodeDecisionMaker": "Queued", "holdUntil": 0},
                })
                result = {"queued": True}
            else:
                decision = record.get("TranscodeDecisionMaker")
                date = record.get("lastTranscodeDate")
                result = {
                    "path": path,
                    "TranscodeDecisionMaker": decision if isinstance(decision, str) else None,
                    "lastTranscodeDate": date if type(date) in (int, str) else None,
                }
                if self.lookup("StagedJSONDB", path).get("status") == "requireReview":
                    result["requireReview"] = True
        except (OSError, HTTPException, ValueError, RecursionError):
            print("Tdarr media request failed", flush=True)
            self.send_error(502)
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def lookup(self, collection, path):
        body = self.post_tdarr("/api/v2/cruddb", {"collection": collection, "mode": "getById", "docID": path, "obj": {}})
        record = json.loads(body) if body else {}
        if not isinstance(record, dict):
            raise ValueError
        return record

    def post_tdarr(self, route, data):
        connection = HTTPConnection("127.0.0.1", self.tdarr_port, timeout=5)
        try:
            connection.request("POST", route, json.dumps({"data": data}), {"Content-Type": "application/json"})
            response = connection.getresponse()
            limit = 1024 * 1024
            if response.status != 200:
                raise ValueError
            body = response.read(limit + 1)
            if len(body) > limit:
                raise ValueError
            return body
        finally:
            connection.close()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8267), Handler).serve_forever()
