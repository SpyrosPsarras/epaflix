import json
import os
import runpy
import subprocess
import sys
import threading
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent


class FlowTests(unittest.TestCase):
    def setUp(self):
        path = HERE / "flow-lg-c1.json"
        self.assertTrue(path.is_file(), "the LG C1 flow export is missing")
        flow = json.loads(path.read_text())
        self.assertEqual(flow["_id"], "lg-c1-compat")
        self.nodes = {node["id"]: node for node in flow["flowPlugins"]}
        self.edges = {}
        for edge in flow["flowEdges"]:
            source, target = edge["source"], edge["target"]
            self.assertIn(source, self.nodes)
            self.assertIn(target, self.nodes)
            key = (source, edge["sourceHandle"])
            self.assertNotIn(key, self.edges)
            self.edges[key] = target

    def route(self, **outputs):
        node = "input"
        visited = []
        while node is not None:
            self.assertNotIn(node, visited, "flows must not loop")
            visited.append(node)
            node = self.edges.get((node, str(outputs.get(node, 1))))
        return visited

    def inputs(self, node):
        return self.nodes[node]["inputsDB"]

    def test_routes_untouched_files_to_end(self):
        self.assertEqual(self.nodes["input"]["pluginName"], "inputFile")
        self.assertEqual(self.nodes["audio"]["pluginName"], "checkAudioCodec")
        self.assertEqual(self.inputs("audio")["codec"], "dts")
        self.assertEqual(self.edges["audio", "2"], "hdr")
        for hdr in (2, 3, 4):
            with self.subTest(hdr=hdr):
                self.assertEqual(self.route(audio=2, hdr=hdr, has_dts=2), ["input", "audio", "hdr", "has_dts"])
        self.assertEqual(self.route(audio=2, profile=5, mkv=2, has_dts=2)[-1], "has_dts")
        self.assertNotIn("profile", self.route(audio=2, hdr=4, has_dts=2))

    def test_dts_conversion_requires_codec_match(self):
        incoming = [(source, output) for (source, output), target in self.edges.items() if target == "begin_dts"]
        self.assertEqual(len(incoming), 1)
        source, output = incoming[0]
        self.assertEqual(output, "1")
        self.assertEqual(self.nodes[source]["sourceRepo"], "Community")
        self.assertEqual(self.nodes[source]["pluginName"], "checkAudioCodec")
        self.assertEqual(self.inputs(source), {"codec": "dts"})
        self.assertNotIn((source, "2"), self.edges)

    def test_no_template_gate_for_conversion(self):
        ancestors = set()
        pending = ["execute", "replace"]
        while pending:
            node = pending.pop()
            for (source, _), target in self.edges.items():
                if target == node and source not in ancestors:
                    ancestors.add(source)
                    pending.append(source)
        variable_checks = {node for node in ancestors if self.nodes[node]["pluginName"] == "checkFlowVariable"}
        self.assertEqual(variable_checks, {"known_duration"})
        self.assertEqual(self.inputs("known_duration")["variable"], "{{{args.originalLibraryFile.ffProbeData.format.duration}}}")

    def test_dv_profiles(self):
        self.assertEqual(self.nodes["hdr"]["pluginName"], "checkHDRType")
        self.assertEqual(self.nodes["profile"]["pluginName"], "checkDoViProfile")
        for profile in (3, 5):
            self.assertIn("strip_args", self.route(audio=2, profile=profile))
        for profile in (1, 2, 4):
            for audio in (1, 2):
                with self.subTest(profile=profile, audio=audio):
                    route = self.route(audio=audio, profile=profile)
                    self.assertEqual(self.nodes[route[-1]]["pluginName"], "requireReview")
                    self.assertNotIn("execute", route)
                    self.assertNotIn("replace", route)

    def test_mkv_only_strip(self):
        self.assertEqual(self.nodes["mkv"]["pluginName"], "checkFileExtension")
        self.assertEqual(self.inputs("mkv")["extensions"], "mkv")
        for profile in (3, 5):
            self.assertEqual(self.edges["profile", str(profile)], "mkv")
        for audio in (1, 2):
            self.assertNotIn("strip_args", self.route(audio=audio, profile=5, mkv=2, has_dts=audio))

    def test_ffmpeg_args(self):
        for node in ("dts_args", "strip_args"):
            self.assertEqual(self.nodes[node]["pluginName"], "ffmpegCommandCustomArguments")
            args = self.inputs(node)["outputArguments"]
            self.assertIn("-c:v copy -c:s copy", args)
            self.assertIn("{{{args.variables.user.dtsArgs}}}", args)
            tokens = args.split()
            filters = [tokens[index:index + 2] for index, token in enumerate(tokens) if token.startswith("-bsf")]
            self.assertEqual(filters, [["-bsf:v:0", "dovi_rpu=strip=1"]] if node == "strip_args" else [])
            self.assertNotIn("-c:a eac3", args)
        self.assertEqual(self.inputs("mark_dts"), {
            "codecType": "audio", "propertyToCheck": "codec_name", "valuesToRemove": "dts", "condition": "equals",
        })
        self.assertIn("{{#removed}} -c:{{index}} eac3 -b:{{index}} 640k{{/removed}}", self.inputs("remember_dts")["value"])
        self.assertNotIn("mark_dts", self.route(audio=2))
        self.assertEqual(self.route(hdr=2)[2:8], ["scout", "mark_dts", "remember_dts", "hdr", "has_dts", "begin_dts"])
        for route in (self.route(hdr=3), self.route(profile=5), self.route(audio=2, profile=3)):
            actual = route[route.index("execute") - 2:route.index("execute")]
            self.assertEqual(self.nodes[actual[0]]["pluginName"], "ffmpegCommandStart")
            self.assertIn(actual[1], ("dts_args", "strip_args"))

    def test_replace_guarded(self):
        self.assertEqual(self.nodes["ratio"]["pluginName"], "compareFileDurationRatio")
        self.assertEqual(self.inputs("ratio"), {"greaterThan": "99", "lessThan": "101"})
        self.assertEqual(self.nodes["health"]["pluginName"], "runHealthCheck")
        self.assertEqual(self.inputs("health"), {"type": "thorough", "gpuAcceleration": "none"})
        self.assertEqual(self.nodes["replace"]["pluginName"], "replaceOriginalFile")
        self.assertEqual({key for key, target in self.edges.items() if target == "replace"}, {("health", "1")})
        self.assertEqual({key for key, target in self.edges.items() if target == "health"}, {("ratio", "1")})
        for output in (2, 3):
            self.assertNotIn("replace", self.route(hdr=3, ratio=output))
        self.assertNotIn("replace", self.route(hdr=3, known_duration=2))
        for library_output, service in ((1, "radarr"), (2, "sonarr")):
            notify = self.route(hdr=3, movie=library_output)[-1]
            self.assertEqual(self.nodes[notify]["pluginName"], "notifyRadarrOrSonarr")
            self.assertEqual(self.inputs(notify)["arr"], service)
            self.assertEqual(self.inputs(notify)["arr_host"], f"http://127.0.0.1:18268/{service}")


class SeedTests(unittest.TestCase):
    def test_readiness_and_seed_handle_bad_http_responses(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def reply(self, body):
                if body is not None:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(body)

            def do_GET(self):
                requests.append(self.path)
                self.reply(first_status if len(requests) == 1 else b'{"status":"good"}')

            def do_POST(self):
                requests.append(self.path)
                self.reply(crud_response)

            def log_message(self, *args):
                pass

        with TemporaryDirectory(dir=Path.cwd()) as tmp, ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                ready_file = Path(tmp) / "ready"
                command = [sys.executable, str(HERE / "seed.py"), base]
                for wait_for_ready, first_status, crud_response in (
                    (False, None, None),
                    (False, None, b"null"),
                    (True, None, None),
                    (True, b"starting", None),
                    (True, b"{}", None),
                    (True, b"null", None),
                ):
                    with self.subTest(wait_for_ready=wait_for_ready, first_status=first_status, crud_response=crud_response):
                        requests.clear()
                        args = ["--ready-file", str(ready_file)] if wait_for_ready else []
                        expected = ["/api/v2/status"] * 2 if wait_for_ready else []
                        result = subprocess.run(command + args, capture_output=True, text=True, timeout=10)
                        self.assertEqual(requests, expected + ["/api/v2/cruddb"])
                        self.assertEqual(result.returncode, 1)
                        self.assertEqual(result.stderr.strip(), "Tdarr configuration seed failed")
                        self.assertFalse(ready_file.exists())
            finally:
                server.shutdown()
                worker.join()

    def test_seed_idempotent(self):
        script = HERE / "seed.py"
        self.assertTrue(script.is_file(), "the seed entry point is missing")
        docs = {("LibrarySettingsJSONDB", "unmanaged"): {"_id": "unmanaged"}}
        writes = []
        defaults = {
            "folderWatching": False, "useFsEvents": False, "folderWatchScanInterval": 300,
            "holdNewFiles": True, "holdFor": 600, "holdForDisplayUnit": "minutes",
            "scheduledScanFindNew": False, "scanOnStart": False, "processTranscodes": False,
        }
        owner_settings = {
            "folderWatching": True, "useFsEvents": True, "folderWatchScanInterval": 600,
            "holdNewFiles": False, "holdFor": 3600, "holdForDisplayUnit": "hours",
            "scheduledScanFindNew": True, "scanOnStart": True, "processTranscodes": True,
            "totalTranscodeCount": 12,
        }

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["data"]
                if self.path != "/api/v2/cruddb":
                    self.send_error(404)
                    return
                collection, mode, doc_id, value = (request[key] for key in ("collection", "mode", "docID", "obj"))
                key = (collection, doc_id)
                result = b""
                if mode == "getById":
                    if key in docs:
                        result = json.dumps(docs[key]).encode()
                elif mode == "getAll":
                    result = json.dumps([doc for (name, _), doc in docs.items() if name == collection]).encode()
                elif mode == "insert" and key not in docs or mode == "update" and key in docs:
                    docs[key] = {**docs.get(key, {}), **value}
                    writes.append((collection, mode, doc_id))
                else:
                    self.send_error(400)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(result)

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            worker = threading.Thread(target=server.serve_forever)
            worker.start()
            try:
                base = f"http://127.0.0.1:{server.server_port}"
                command = [sys.executable, str(script), base]
                keys = [("FlowsJSONDB", "lg-c1-compat"), *[("LibrarySettingsJSONDB", name) for name in ("movies", "tvshows", "animes")]]
                for mode in ("insert", "update"):
                    writes.clear()
                    subprocess.run(command, check=True, capture_output=True, text=True, timeout=10)
                    self.assertEqual(writes, [(collection, mode, key) for collection, key in keys])
                    for collection, key in keys[1:]:
                        library = docs[collection, key]
                        self.assertEqual(library["folder"], f"/media/{key}")
                        self.assertEqual(library["cache"], "/temp")
                        self.assertTrue(library["mediaInfoScan"])
                        self.assertEqual(library["flowId"], "lg-c1-compat")
                        self.assertTrue(library["decisionMaker"]["settingsFlows"])
                        self.assertFalse(library["decisionMaker"]["settingsPlugin"])
                        expected = owner_settings if mode == "update" and key == "movies" else defaults
                        with self.subTest(mode=mode, library=key):
                            self.assertEqual({setting: library.get(setting) for setting in expected}, expected)
                    if mode == "insert":
                        docs["LibrarySettingsJSONDB", "movies"].update(owner_settings)
                self.assertEqual(docs["LibrarySettingsJSONDB", "unmanaged"], {"_id": "unmanaged"})
                docs["LibrarySettingsJSONDB", "unmanaged"]["folder"] = "/media/movies/"
                writes.clear()
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(writes, [])
            finally:
                server.shutdown()
                worker.join()


class NotifyProxyTests(unittest.TestCase):
    def test_only_refresh_calls_reach_arr(self):
        script = HERE / "notify-proxy.py"
        self.assertTrue(script.is_file(), "the loopback notification proxy is missing")
        proxy = runpy.run_path(str(script))
        calls = []

        class Arr(BaseHTTPRequestHandler):
            def reply(self):
                calls.append((self.command, self.path, self.headers["X-Api-Key"]))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"movie":{"id":17},"id":17,"other":"discard me"}')

            do_GET = do_POST = reply

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Arr) as arr, ThreadingHTTPServer(("127.0.0.1", 0), proxy["Handler"]) as server:
            proxy["ROUTES"]["radarr"] = ("127.0.0.1", arr.server_port, "Radarr")
            workers = [threading.Thread(target=instance.serve_forever) for instance in (arr, server)]
            for worker in workers:
                worker.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with patch.dict(os.environ, RADARR_API_KEY="test-value"):
                    for path, body in (("/radarr/api/v3/parse?title=scratch", None), ("/radarr/api/v3/command", b'{"name":"RefreshMovie","movieIds":[1]}')):
                        with urlopen(Request(base + path, data=body, headers={"X-Api-Key": "ignored"}), timeout=2) as response:
                            self.assertEqual(json.load(response), {"id": 17} if body else {"movie": {"id": 17}})
                    for path, body in (("/radarr/api/v3/config/host", None), ("/radarr/api/v3/command", b'{"name":"DeleteMovie","movieIds":[1]}')):
                        with self.assertRaises(HTTPError):
                            urlopen(Request(base + path, data=body), timeout=2)
                self.assertEqual(calls, [("GET", "/api/v3/parse?title=scratch", "test-value"), ("POST", "/api/v3/command", "test-value")])
            finally:
                for instance in (arr, server):
                    instance.shutdown()
                for worker in workers:
                    worker.join()


class MediaProxyTests(unittest.TestCase):
    PATH = "/media/movies/A Movie/It’s a file [1080p].mkv"

    def setUp(self):
        script = HERE / "media-proxy.py"
        self.assertTrue(script.is_file(), "the Tdarr media proxy is missing")
        self.calls = []
        self.records = {}
        self.upstream_status = 200
        test = self

        class Tdarr(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                test.calls.append((self.path, body))
                if self.path == "/api/v2/cruddb":
                    request = body["data"]
                    record = test.records.get((request["collection"], request["docID"]))
                    reply = b"" if record is None else json.dumps(record).encode()
                else:
                    reply = b"OK"
                self.send_response(test.upstream_status)
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args):
                pass

        tdarr = self.start_server(Tdarr)
        handler = runpy.run_path(str(script))["Handler"]
        handler.tdarr_port = tdarr.server_port
        self.proxy = self.start_server(handler)

    def start_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(worker.join)
        self.addCleanup(server.shutdown)
        return server

    def request(self, route, body=None, method="POST"):
        connection = HTTPConnection("127.0.0.1", self.proxy.server_port, timeout=2)
        try:
            connection.request(method, route, json.dumps(body), {"Content-Type": "application/json"})
            response = connection.getresponse()
            result = response.read()
            return response.status, json.loads(result) if response.status == 200 else result
        finally:
            connection.close()

    def lookup(self, collection, path):
        return "/api/v2/cruddb", {"data": {"collection": collection, "mode": "getById", "docID": path, "obj": {}}}

    def test_invalid_paths_never_reach_tdarr(self):
        paths = [
            "media/movies/a.mkv", "/media/movies", "/media/movies/", "/media/movies-copy/a.mkv",
            "/media/downloads/a.mkv", "/media/tvshows/../movies/a.mkv", "/media/animes/./a.mkv",
            "/media/movies//a.mkv", "//media/movies/a.mkv", "/media/movies/a.mkv/",
            "/media/movies/a\x00.mkv", "/media/movies/a\nb.mkv", None, [self.PATH],
        ]
        for route in ("/status", "/queue"):
            for body in [{"path": path} for path in paths] + [{}, {"path": self.PATH, "holdUntil": 99}]:
                with self.subTest(route=route, body=body):
                    self.assertEqual(self.request(route, body)[0], 400)
        self.assertEqual(self.calls, [])

    def test_only_two_post_routes(self):
        for method, route, expected in (
            ("POST", "/api/v2/cruddb", 404), ("POST", "/api/v2/bulk-update-files", 404),
            ("POST", "/status?all=1", 404), ("POST", "//status", 404), ("POST", "/queue/", 404),
            ("GET", "/status", 405), ("DELETE", "/queue", 405), ("CUSTOM", "/queue", 405),
        ):
            with self.subTest(method=method, route=route):
                self.assertEqual(self.request(route, {"path": self.PATH}, method)[0], expected)
        self.assertEqual(self.calls, [])

    def test_status_whitelists_record_fields(self):
        for library in ("movies", "tvshows", "animes"):
            path = self.PATH.replace("/movies/", f"/{library}/")
            with self.subTest(library=library):
                self.records["FileJSONDB", path] = {
                    "_id": path, "file": path, "DB": library, "TranscodeDecisionMaker": "Queued",
                    "lastTranscodeDate": 1234, "ffProbeData": {"extra": True},
                }
                self.records["StagedJSONDB", path] = {"_id": path, "status": "requireReview", "job": {"jobId": "secret"}}
                expected = {"path": path, "TranscodeDecisionMaker": "Queued", "lastTranscodeDate": 1234}
                self.calls.clear()
                self.assertEqual(self.request("/status", {"path": path}), (200, {**expected, "requireReview": True}))
                self.assertEqual(self.calls, [self.lookup("FileJSONDB", path), self.lookup("StagedJSONDB", path)])
                self.records["StagedJSONDB", path]["status"] = "success"
                self.assertEqual(self.request("/status", {"path": path}), (200, expected))
                del self.records["StagedJSONDB", path]
                self.assertEqual(self.request("/status", {"path": path}), (200, expected))

    def test_queue_updates_only_one_known_file(self):
        self.records["FileJSONDB", self.PATH] = {"_id": self.PATH, "DB": "movies"}
        self.assertEqual(self.request("/queue", {"path": self.PATH}), (200, {"queued": True}))
        self.assertEqual(self.calls, [self.lookup("FileJSONDB", self.PATH), ("/api/v2/bulk-update-files", {"data": {
            "fileIds": [self.PATH], "updatedObj": {"TranscodeDecisionMaker": "Queued", "holdUntil": 0},
        }})])

    def test_unknown_or_mismatched_file_cannot_be_queued(self):
        for record in (None, {"_id": self.PATH, "DB": "tvshows"}, {"_id": self.PATH + ".other", "DB": "movies"}):
            with self.subTest(record=record):
                self.records["FileJSONDB", self.PATH] = record
                self.calls.clear()
                self.assertEqual(self.request("/queue", {"path": self.PATH})[0], 404)
                self.assertEqual(self.calls, [self.lookup("FileJSONDB", self.PATH)])

    def test_upstream_error_is_not_forwarded(self):
        self.records["FileJSONDB", self.PATH] = {"error": "internal details"}
        self.upstream_status = 500
        status, body = self.request("/queue", {"path": self.PATH})
        self.assertEqual(status, 502)
        self.assertNotIn(b"internal details", body)
        self.assertEqual(self.calls, [self.lookup("FileJSONDB", self.PATH)])


if __name__ == "__main__":
    unittest.main()
