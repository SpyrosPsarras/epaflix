#!/usr/bin/env python3
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

FILES = Path(__file__).resolve().parents[1] / "files"
sys.path.insert(0, str(FILES))
import payload_lib

SPEC = importlib.util.spec_from_file_location("emitter", FILES / "emit-transcripts.py")
emitter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(emitter)
BACKENDS = (("t3-w", "t3-work-", "work"), ("t3-h", "t3-homelab-", "homelab"))


class BackendTests(unittest.TestCase):
    def emit(self, header, body=None):
        request = body or {"model": "fixture", "messages": [{"role": "user", "content": "synthetic"}]}
        raw = ("=== REQUEST INFO ===\nURL: /v1/messages\n=== HEADERS ===\n" + header
               + "\n=== REQUEST BODY ===\n" + json.dumps(request) + "\n")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "request-fixture.log"
            path.write_text(raw)
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                emitter.emit(path.name, str(path))
        output = captured.getvalue()
        return json.loads(output.split(" ", 1)[1]), output

    def test_only_reserved_backend_key_prefixes_get_public_labels(self):
        for masked, _, backend in BACKENDS:
            for header in (f"Authorization: Bearer {masked}...abcd", f"X-Api-Key: {masked}...abcd"):
                self.assertEqual(payload_lib.client_backend("=== HEADERS ===\n" + header + "\n"), backend)
            for header in ("Authorization: Bearer other...abcd", f"User-Agent: {masked}...abcd",
                           f"Authorization: Bearer {masked}...abcd EXTRA", f"X-Api-Key: {masked}"):
                self.assertEqual(payload_lib.client_backend("=== HEADERS ===\n" + header + "\n"), "")

    def test_loki_record_contains_label_not_header_or_key(self):
        for masked, _, backend in BACKENDS:
            record, output = self.emit(f"X-Api-Key: {masked}...abcd")
            self.assertEqual(record["client_backend"], backend)
            self.assertNotIn(masked, output)
            self.assertNotIn("abcd", output)
            self.assertNotIn("X-Api-Key", output)

    def test_dedicated_keys_quoted_in_prompts_are_redacted(self):
        for _, prefix, _ in BACKENDS:
            record, output = self.emit("Authorization: Bearer other...abcd", {
                "model": "fixture", "messages": [{"role": "user", "content": prefix + "z" * 43}]})
            self.assertNotIn("z" * 43, output)
            self.assertIn("<REDACTED>", record["turn"])

    def test_unmasked_credentials_are_never_used_as_log_labels(self):
        for _, prefix, _ in BACKENDS:
            record, output = self.emit("Authorization: Bearer " + prefix + "secret" * 10)
            self.assertEqual(record["client_backend"], "")
            self.assertNotIn("secret", output)

    def test_body_or_upstream_headers_cannot_select_backend_label(self):
        for masked, _, _ in BACKENDS:
            record, _ = self.emit("Authorization: Bearer other...abcd", {
                "model": "fixture", "messages": [{"role": "user", "content": f"Authorization: Bearer {masked}...abcd"}]})
            self.assertEqual(record["client_backend"], "")
            record, _ = self.emit("Authorization: Bearer other...abcd\n=== API REQUEST 1 ===\n"
                                  + f"Authorization: Bearer {masked}...abcd")
            self.assertEqual(record["client_backend"], "")

    def test_client_map_controls_generic_four_character_prefixes(self):
        with patch.dict(payload_lib.CLIENT_BACKENDS, {"unit": "fixture"}):
            record, output = self.emit("Authorization: Bearer unit...abcd")
        self.assertEqual(record["client_backend"], "fixture")
        self.assertNotIn("abcd", output)


if __name__ == "__main__":
    unittest.main()
