#!/usr/bin/env python3
"""Capture verification output without printing source rows or secrets."""
from pathlib import Path
import subprocess
import sys
import hashlib
import json
import migrate

root = Path(__file__).resolve().parent
with (root / "test-output.txt").open("w") as log:
    result = subprocess.run([sys.executable, "-m", "unittest", "-v", "test_migrate"],
                            cwd=root, stdout=log, stderr=subprocess.STDOUT)
    print("configured_schema_hash_length", len(migrate.SCHEMA_SHA256), file=log)
    for source in migrate.SOURCES:
        snapshot = root / "private/snapshots" / source / "state.sqlite"
        if snapshot.exists():
            c = migrate.ro(snapshot)
            actual = hashlib.sha256(json.dumps(migrate.schema(c), separators=(",", ":")).encode()).hexdigest()
            print(source, "schema_matches", actual == migrate.SCHEMA_SHA256,
                  "threads", c.execute("select count(*) from projection_threads").fetchone()[0], file=log)
            try:
                migrate.validate_source(c)
                print(source, "projection preflight PASS", file=log)
            except migrate.Invalid as exc:
                print(source, "projection preflight BLOCKED:", str(exc), file=log)
            finally:
                c.close()
print("Tests exit:", result.returncode, "Captured in test-output.txt")
sys.exit(result.returncode)
