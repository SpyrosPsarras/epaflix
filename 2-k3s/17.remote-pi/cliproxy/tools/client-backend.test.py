#!/usr/bin/env python3
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

FILES = Path(__file__).resolve().parents[1] / "files"
sys.path.insert(0, str(FILES))
import payload_lib

SPEC = importlib.util.spec_from_file_location("emitter", FILES / "emit-transcripts.py")
emitter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(emitter)


class BackendTests(unittest.TestCase):
    def test_only_reserved_work_key_prefix_gets_a_public_backend_label(self):
        for header in ("Authorization: Bearer t3-w...abcd", "X-Api-Key: t3-w...abcd"):
            self.assertEqual(payload_lib.client_backend("=== HEADERS ===\n" + header + "\n"), "work")
        for header in ("Authorization: Bearer other...abcd", "User-Agent: t3-w...abcd",
                       "Authorization: Bearer t3-w...abcd EXTRA", "X-Api-Key: t3-w"):
            self.assertEqual(payload_lib.client_backend("=== HEADERS ===\n" + header + "\n"), "")

    def test_loki_record_contains_label_not_header_or_key(self):
        raw = ("=== REQUEST INFO ===\nURL: /v1/messages\n=== HEADERS ===\n"
               "X-Api-Key: t3-w...abcd\n=== REQUEST BODY ===\n"
               "{\"model\":\"fixture\",\"messages\":[{\"role\":\"user\",\"content\":\"synthetic\"}]}\n")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "request-fixture.log"
            path.write_text(raw)
            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                emitter.emit(path.name, str(path))
        output = captured.getvalue()
        record = json.loads(output.split(" ", 1)[1])
        self.assertEqual(record["client_backend"], "work")
        self.assertNotIn("t3-w", output)
        self.assertNotIn("abcd", output)
        self.assertNotIn("X-Api-Key", output)

    def test_dedicated_key_shape_is_redacted_if_quoted_in_a_prompt(self):
        self.assertNotIn("z" * 43, payload_lib.redact("t3-work-" + "z" * 43))


if __name__ == "__main__":
    unittest.main()
