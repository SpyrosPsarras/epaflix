#!/usr/bin/env python3
"""Render one cliproxy request-log payload file (on stdin) as a readable transcript.

Called by cliproxy-payload.sh. The parsing itself lives in
../files/payload_lib.py, shared with the emit-transcripts.py sidecar, so the
human view and the Loki view can never disagree about what was said.

Unlike the sidecar this prints the WHOLE conversation, not just the last turn -
when you are reading one call by hand you usually want the history that led to it.
"""

import os
import signal
import sys

signal.signal(signal.SIGPIPE, signal.SIG_DFL)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "files"))
import payload_lib  # noqa: E402

MAX_MSG = 2000
MAX_ASSISTANT = 6000


def main():
    raw = sys.stdin.read()
    try:
        parsed = payload_lib.parse(raw)
    except ValueError as exc:
        sys.exit("%s - use `raw` and read it by hand" % exc)

    for key in ("model", "stream", "url", "upstream_url", "upstream_auth",
                "upstream_status", "requested_at"):
        value = parsed.get(key)
        if value not in (None, ""):
            print("%-14s: %s" % (key, value))
    print()

    if parsed["system"]:
        body = parsed["system"]
        print("--- system (%d chars, first 300) ---" % len(body))
        print(body[:300].rstrip())
        print()

    for message in parsed["messages"]:
        body = payload_lib.flatten(message.get("content"))
        print("--- %s (%d chars) ---" % (message.get("role"), len(body)))
        if len(body) <= MAX_MSG:
            print(body)
        else:
            print(body[:MAX_MSG] + "\n[... truncated, use `raw` for all of it]")
        print()

    reply = parsed["reply"]
    if reply:
        print("--- assistant (from %s) ---" % parsed["reply_source"])
        print(reply[:MAX_ASSISTANT])
        if len(reply) > MAX_ASSISTANT:
            print("[... truncated, use `raw` for all of it]")
    else:
        print("--- no response body captured (check `raw`) ---")


if __name__ == "__main__":
    main()
