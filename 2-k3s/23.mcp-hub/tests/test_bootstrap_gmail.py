import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import warnings

HUB = Path(__file__).resolve().parents[1]


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = load(HUB / "tools/bootstrap-gmail.py")
complete_scope_set = {
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
}
EMAIL = "fixture@example.test"
CLIENT = "synthetic-desktop-client"
CLIENT_SECRET = "synthetic-client-secret"
REFRESH = "synthetic-fresh-refresh"
ACCESS = "synthetic-access"


class BootstrapGmailTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.client_json = Path(self.temp) / "desktop.json"
        self.client_json.write_text(json.dumps({"installed": {"client_id": CLIENT, "client_secret": CLIENT_SECRET}}))
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.stdout))
        self.stack.enter_context(contextlib.redirect_stderr(self.stderr))
        self.calls, self.urls, self.requests = [], [], []
        self.callback = "valid"
        self.traffic = ["callback"]
        self.clock = 0
        self.handled = 0
        self.closed = False
        self.opened = True
        self.tokens = {"access_token": ACCESS, "refresh_token": REFRESH, "scope": " ".join(sorted(complete_scope_set))}
        self.profile = EMAIL
        self.existing = None
        self.mutation_error = False
        self.get_error = False
        self.http_error = False
        self.stack.enter_context(patch.object(bootstrap.http.server, "HTTPServer", self.server))
        self.stack.enter_context(patch.object(bootstrap.webbrowser, "open", self.browser))
        self.stack.enter_context(patch.object(bootstrap.urllib.request, "urlopen", self.urlopen))
        self.stack.enter_context(patch.object(bootstrap.subprocess, "run", self.kubectl))
        self.prompt = self.stack.enter_context(patch("getpass.getpass", return_value=EMAIL))
        self.stack.enter_context(patch("time.monotonic", self.monotonic))
        self.sops = self.stack.enter_context(patch.object(bootstrap, "_write_secret"))
        self.file_output = self.stack.enter_context(patch.object(bootstrap.tempfile, "mkstemp", side_effect=AssertionError("no output files")))

    def monotonic(self):
        self.clock += 1 if self.traffic else 301
        return self.clock

    def server(self, address, handler):
        case = self

        class Server:
            server_port = 54321
            timeout = None

            def handle_request(self):
                case.handled += 1
                if not case.traffic:
                    return
                kind = case.traffic.pop(0)
                request = handler.__new__(handler)
                state = urllib.parse.parse_qs(urllib.parse.urlparse(case.urls[-1]).query)["state"][0]
                query = {"state": state, "code": "synthetic-code"}
                if case.callback == "denied":
                    query = {"state": state, "error": "synthetic-sensitive-denial"}
                elif case.callback == "wrong-state":
                    query["state"] = "wrong"
                request.path = "/favicon.ico" if kind == "favicon" else "/?" + urllib.parse.urlencode(query)
                request.wfile = io.BytesIO()
                request.send_response = lambda *_: None
                request.send_header = lambda *_: None
                request.end_headers = lambda: None
                request.do_GET()

            def server_close(self):
                case.closed = True

        self.assertEqual(address, ("127.0.0.1", 0))
        return Server()

    def browser(self, url):
        if "select_account" in url:
            self.assertTrue(self.prompt.called)
        self.urls.append(url)
        return self.opened

    def urlopen(self, request, timeout):
        self.requests.append(request)
        if self.http_error:
            raise urllib.error.HTTPError(request.full_url, 400, "synthetic-sensitive-http", {}, io.BytesIO(b"synthetic-sensitive-body"))
        result = self.tokens if request.full_url == bootstrap.TOKEN_URL else {"emailAddress": self.profile}
        return io.StringIO(json.dumps(result))

    def kubectl(self, args, **kwargs):
        if args[0] == sys.executable:
            return subprocess.CompletedProcess(args, int(not self.browser(kwargs["input"])))
        self.calls.append((args, kwargs))
        if "get" in args:
            if self.get_error:
                return subprocess.CompletedProcess(args, 1, "synthetic-sensitive-output", "synthetic-sensitive-error")
            return subprocess.CompletedProcess(args, 0, json.dumps(self.existing) if self.existing else "", "")
        return subprocess.CompletedProcess(args, int(self.mutation_error), "synthetic-sensitive-output", "synthetic-sensitive-error")

    def run_main(self, args=None):
        argv = args if args is not None else ["--instance", "homelab", "--context", "fixture", "--client-json", str(self.client_json)]
        with patch.object(sys, "argv", ["bootstrap-gmail.py", *argv]):
            bootstrap.main()

    def mutations(self):
        return [(args, kw) for args, kw in self.calls if "create" in args or "replace" in args]

    def assert_private(self):
        output = self.stdout.getvalue() + self.stderr.getvalue()
        for value in (EMAIL, CLIENT, CLIENT_SECRET, REFRESH, ACCESS, "synthetic-sensitive", "synthetic-code", bootstrap.AUTH_URL):
            self.assertNotIn(value, output)

    def assert_failed(self, message):
        with self.assertRaises(SystemExit) as error:
            self.run_main()
        self.assertEqual(str(error.exception), message)
        self.assertEqual(self.mutations(), [])
        self.assert_private()

    def test_instance_requires_context_and_rejects_work(self):
        for args in (["--instance", "homelab", "--reuse"], ["--instance", "work", "--context", "fixture"],
                     ["--instance", "homelab", "--context", "fixture", "--out", "ignored"],
                     ["--instance", "homelab", "--context", "fixture", "--client-id", CLIENT, "--client-secret", CLIENT_SECRET]):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                self.run_main(args)
            self.assertEqual(self.calls + self.urls + self.requests, [])
            self.prompt.assert_not_called()

    def test_instance_never_reads_legacy_or_writes_sops(self):
        self.run_main()
        for args, kwargs in self.calls:
            self.assertEqual(args[:5], ["kubectl", "--context", "fixture", "-n", "mcp-hub-homelab"])
            self.assertTrue(kwargs["capture_output"])
            self.assertTrue(kwargs["text"])
            self.assertNotIn("--from-literal", " ".join(args))
        mutations = self.mutations()
        self.assertEqual(len(mutations), 1)
        args, kwargs = mutations[0]
        self.assertEqual(args[5:], ["create", "-f", "-"])
        document = json.loads(kwargs["input"])
        self.assertEqual(document["metadata"], {"name": "mcp-hub-gmail", "namespace": "mcp-hub-homelab"})
        data = {key: base64.b64decode(value).decode() for key, value in document["data"].items()}
        self.assertEqual(data, {"client-id": CLIENT, "client-secret": CLIENT_SECRET, "refresh-token": REFRESH, "email": EMAIL})
        self.assertNotIn(ACCESS, kwargs["input"])
        self.sops.assert_not_called()
        self.file_output.assert_not_called()
        self.assertEqual(self.stdout.getvalue(), "homelab Google grant staged; rollout still required\n")
        self.assert_private()

    def test_instance_complete_scopes(self):
        self.run_main()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.urls[0]).query)
        self.assertEqual(set(query["scope"][0].split()), complete_scope_set)
        self.assertEqual(set(load(HUB / "instances/google/seed.py").SCOPES), complete_scope_set)
        self.assertEqual(query["prompt"], ["select_account consent"])
        self.assertEqual(query["access_type"], ["offline"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        form = urllib.parse.parse_qs(self.requests[0].data.decode())
        challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(query["code_challenge"], [challenge])
        self.assertEqual(self.requests[1].get_header("Authorization"), "Bearer " + ACCESS)

    def test_instance_consent_failures_write_nothing(self):
        cases = {
            "denied": "Google consent failed or timed out; nothing written",
            "timeout": "Google consent failed or timed out; nothing written",
            "wrong-state": "Google consent failed or timed out; nothing written",
            "missing-refresh": "Google returned an incomplete grant; nothing written",
            "empty-refresh": "Google returned an incomplete grant; nothing written",
            "partial": "Google grant scopes differ from the requested set; nothing written",
            "broader": "Google grant scopes differ from the requested set; nothing written",
            "wrong-account": "Google account mismatch",
            "http": "homelab Google consent or staging failed; no automatic retry",
        }
        for case, message in cases.items():
            with self.subTest(case=case):
                self.callback, self.traffic, self.clock = "valid", ["callback"], 0
                self.tokens = {"access_token": ACCESS, "refresh_token": REFRESH, "scope": " ".join(sorted(complete_scope_set))}
                self.profile, self.http_error = EMAIL, False
                if case in ("denied", "wrong-state"):
                    self.callback = case
                elif case == "timeout":
                    self.traffic = []
                elif case == "missing-refresh":
                    self.tokens.pop("refresh_token")
                elif case == "empty-refresh":
                    self.tokens["refresh_token"] = ""
                elif case == "partial":
                    self.tokens["scope"] = bootstrap.SCOPE
                elif case == "broader":
                    self.tokens["scope"] += " https://www.googleapis.com/auth/calendar"
                elif case == "wrong-account":
                    self.profile = "other@example.test"
                else:
                    self.http_error = True
                self.assert_failed(message)

    def test_instance_browser_failure_closes_server(self):
        self.opened = False
        self.assert_failed("a local browser is required; nothing written")
        self.assertTrue(self.closed)
        self.assertEqual(self.requests, [])

    def test_instance_failed_browser_never_echoes_auth_url(self):
        launcher = Path(self.temp, "failing-browser")
        launcher.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nprintf "%s\\n" "$@" >&2\nexit 1\n')
        launcher.chmod(0o700)
        code = ("import runpy; bootstrap = runpy.run_path(" + repr(str(HUB / "tools/bootstrap-gmail.py"))
                + "); bootstrap['_authorize_instance']('synthetic-desktop-client', 'synthetic-desktop-secret')")
        env = {**os.environ, "BROWSER": str(launcher) + " %s", "PATH": self.temp,
               "DISPLAY": "", "WAYLAND_DISPLAY": "", "TERM": "", "HOME": self.temp}
        result = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = result.communicate(timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(stdout, b"")
        self.assertEqual(stderr, b"a local browser is required; nothing written\n")
        for private in (bootstrap.AUTH_URL, CLIENT, CLIENT_SECRET, "synthetic-desktop-secret"):
            self.assertNotIn(private, (stdout + stderr).decode())

    def test_instance_requires_non_echoing_prompt(self):
        def unavailable(*args):
            warnings.warn("cannot hide input", bootstrap.getpass.GetPassWarning)
            return EMAIL

        self.prompt.side_effect = unavailable
        self.assert_failed("homelab Google consent or staging failed; no automatic retry")
        self.assertEqual(self.urls, [])

    def test_instance_wrong_desktop_json(self):
        for data in ({"web": {"client_id": CLIENT, "client_secret": CLIENT_SECRET}}, {"installed": {}}, {"installed": {"client_id": CLIENT, "client_secret": ""}}):
            self.client_json.write_text(json.dumps(data))
            self.assert_failed("a Desktop OAuth client JSON is required; nothing written")
            self.assertEqual(self.urls + self.requests + self.calls, [])

    def test_favicon_then_valid_callback(self):
        self.traffic = ["favicon", "callback"]
        self.run_main()
        self.assertEqual(self.handled, 2)
        self.assertEqual(len(self.mutations()), 1)
        self.assertTrue(self.closed)

    def test_favicon_only_times_out(self):
        self.traffic = ["favicon"]
        self.assert_failed("Google consent failed or timed out; nothing written")
        self.assertEqual(self.requests, [])
        self.assertTrue(self.closed)

    def test_instance_write_conflict_is_closed(self):
        self.existing = {"metadata": {"resourceVersion": "42"}, "data": {"refresh-token": base64.b64encode(b"synthetic-old-refresh").decode()}}
        self.mutation_error = True
        with self.assertRaises(SystemExit) as error:
            self.run_main()
        mutations = self.mutations()
        self.assertEqual(len(mutations), 1)
        args, kwargs = mutations[0]
        self.assertEqual(args[5:], ["replace", "-f", "-"])
        self.assertEqual(json.loads(kwargs["input"])["metadata"]["resourceVersion"], "42")
        self.assertEqual(str(error.exception), "Google Secret staging failed; no retry performed")
        self.assert_private()

    def test_instance_read_failure_does_not_create(self):
        self.get_error = True
        self.assert_failed("Google Secret read failed; nothing written")

    def test_reuse_only_selected_client_fields(self):
        self.existing = {"metadata": {"resourceVersion": "42"}, "data": {
            key: base64.b64encode(value.encode()).decode() for key, value in {
                "client-id": CLIENT, "client-secret": CLIENT_SECRET, "refresh-token": "synthetic-old-refresh"}.items()}}
        self.run_main(["--instance", "homelab", "--context", "fixture", "--reuse"])
        for args, _ in self.calls:
            self.assertEqual(args[4], "mcp-hub-homelab")
        proposed = json.loads(self.mutations()[0][1]["input"])
        self.assertEqual(base64.b64decode(proposed["data"]["refresh-token"]).decode(), REFRESH)
        self.assertEqual(len(self.urls), 1)

    def test_legacy_authorization_keeps_original_request(self):
        self.tokens["scope"] = bootstrap.SCOPE
        result = bootstrap._authorize(CLIENT, CLIENT_SECRET)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.urls[0]).query)
        self.assertEqual(query["scope"], [bootstrap.SCOPE])
        self.assertEqual(query["prompt"], ["consent"])
        self.assertEqual(result, self.tokens)
        self.assertIn(self.urls[0], self.stdout.getvalue())
        self.assertTrue(self.closed)

    def test_legacy_explicit_empty_context_is_not_replaced(self):
        self.tokens["scope"] = bootstrap.SCOPE
        self.existing = {"data": {key: base64.b64encode(value.encode()).decode() for key, value in {
            "client-id": CLIENT, "client-secret": CLIENT_SECRET}.items()}}
        with patch.object(bootstrap, "_authorize", return_value=self.tokens):
            self.run_main(["--reuse", "--context", ""])
        self.assertEqual(self.calls[0][0][2], "")

    def test_legacy_default_unchanged(self):
        self.tokens["scope"] = bootstrap.SCOPE
        self.existing = {"data": {key: base64.b64encode(value.encode()).decode() for key, value in {
            "client-id": CLIENT, "client-secret": CLIENT_SECRET}.items()}}
        with patch.object(bootstrap, "_authorize", return_value=self.tokens) as authorize:
            self.run_main(["--reuse"])
        self.assertEqual(self.calls[0][0][:5], ["kubectl", "--context", "epaflix", "-n", "mcp-hub"])
        authorize.assert_called_once_with(CLIENT, CLIENT_SECRET)
        self.assertEqual(bootstrap.SCOPE, "https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/drive")
        self.sops.assert_called_once_with(bootstrap.OUT, CLIENT, CLIENT_SECRET, REFRESH, EMAIL)


if __name__ == "__main__":
    unittest.main()
