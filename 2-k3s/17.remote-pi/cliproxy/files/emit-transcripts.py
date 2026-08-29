#!/usr/bin/env python3
"""Emit one JSON line per cliproxy request so the messages land in Loki.

Runs as a sidecar next to cliproxy. Watches the request-log directory that
`request-log: true` fills, and for every completed payload file prints a single
line to stdout. promtail already scrapes every container in the pod, so that line
reaches Loki with no promtail or Loki change - one place to look, which was the
whole point (#1016).

WHY A DELTA AND NOT THE FILE
An agent replays the entire conversation on every call, so a payload file is
~1.1MB and ~99% of it is history Loki already has. At the observed rate (106
`/v1/messages` in one hour) shipping files whole would be ~2.8GB/day against a
15Gi Loki PVC - dead inside a week. Only the last message is new information, so
that plus the reply is what gets emitted: ~2-6KB per request, ~190MB per 31-day
retention window.

WHY THE LINE STARTS WITH A TIMESTAMP
promtail's multiline stage uses `firstline: '^\\d{4}-\\d{2}-\\d{2}|^[A-Z]{1}\\d{4}'`
(promtail-values.yaml). A bare `{...}` does not match it and would be appended to
whatever log entry came before, corrupting both. The leading ISO date is
load-bearing, not decoration.

Everything emitted passes through payload_lib.redact() first. Bearer values are
already truncated by CLIProxyAPI itself, but a prompt or tool result can quote a
secret, and #602 is what happens when one reaches a retained transcript.

Env:
  LOGS_DIR              default /var/lib/cliproxy/logs
  POLL_SECONDS          default 2
  STABLE_SECONDS        default 2      file must stop changing before it is read
  MAX_ATTEMPTS          default 5      re-reads before a file is abandoned
  MAX_USER_CHARS        default 2000
  MAX_REPLY_CHARS       default 4000
  MAX_TOOL_RESULT_CHARS default 500
  EMIT_BACKLOG          default 0      1 = also emit files already present at start
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import payload_lib  # noqa: E402


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


LOGS_DIR = os.environ.get("LOGS_DIR", "/var/lib/cliproxy/logs")
POLL_SECONDS = env_int("POLL_SECONDS", 2)
STABLE_SECONDS = env_int("STABLE_SECONDS", 2)
MAX_USER_CHARS = env_int("MAX_USER_CHARS", 2000)
MAX_REPLY_CHARS = env_int("MAX_REPLY_CHARS", 4000)
MAX_TOOL_RESULT_CHARS = env_int("MAX_TOOL_RESULT_CHARS", 500)
MAX_ATTEMPTS = env_int("MAX_ATTEMPTS", 5)
EMIT_BACKLOG = env_int("EMIT_BACKLOG", 0) == 1


def log(message):
    """Operational chatter, in the same date-first shape promtail expects."""
    print("%s [emit-transcripts] %s" % (stamp(), message), flush=True)


def stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def request_id(name):
    base = name[:-4] if name.endswith(".log") else name
    return base.rsplit("-", 1)[-1]


def candidates():
    try:
        names = os.listdir(LOGS_DIR)
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        if not name.endswith(".log") or name == "main.log":
            continue
        path = os.path.join(LOGS_DIR, name)
        try:
            info = os.stat(path)
        except OSError:
            continue
        if not os.path.isfile(path):
            continue
        out.append((name, path, info.st_mtime, info.st_size))
    return out


def emit(name, path):
    with open(path, "r", errors="replace") as handle:
        raw = handle.read()

    parsed = payload_lib.parse(raw)
    turn = payload_lib.last_turn(parsed, user_chars=MAX_USER_CHARS,
                                 tool_result_chars=MAX_TOOL_RESULT_CHARS)
    reply = parsed["reply"]

    account = ""
    for part in (parsed.get("upstream_auth") or "").split(","):
        part = part.strip()
        if part.startswith("label="):
            account = part[len("label="):]

    record = {
        "kind": "transcript",
        "request_id": request_id(name),
        "url": parsed.get("url"),
        "model": parsed.get("model"),
        "stream": parsed.get("stream"),
        "upstream_status": parsed.get("upstream_status"),
        "account": account,
        "message_count": parsed.get("message_count"),
        "prompt_chars": parsed.get("prompt_chars"),
        "turn_role": turn.get("role"),
        "turn_tools": turn.get("tools"),
        "turn": payload_lib.redact(turn.get("text")),
        "turn_truncated": bool(turn.get("truncated")),
        "reply_tools": parsed.get("reply_tools"),
        "reply": payload_lib.redact(reply[:MAX_REPLY_CHARS]),
        "reply_truncated": len(reply) > MAX_REPLY_CHARS,
        "reply_source": parsed.get("reply_source"),
        "payload_file": name,
        "payload_bytes": os.path.getsize(path),
    }
    print("%s %s" % (stamp(), json.dumps(record, ensure_ascii=False)), flush=True)


def main():
    seen = set()
    attempts = {}

    if not EMIT_BACKLOG:
        backlog = [name for name, _, _, _ in candidates()]
        seen.update(backlog)
        log("watching %s (skipped %d pre-existing payload file(s); "
            "set EMIT_BACKLOG=1 to emit them)" % (LOGS_DIR, len(backlog)))
    else:
        log("watching %s (emitting backlog too)" % LOGS_DIR)

    while True:
        now = time.time()
        for name, path, mtime, size in candidates():
            if name in seen:
                continue
            if size == 0 or (now - mtime) < STABLE_SECONDS:
                continue
            try:
                emit(name, path)
                seen.add(name)
                attempts.pop(name, None)
            except Exception as exc:  # noqa: BLE001 - a bad file must not kill the tail
                count = attempts.get(name, 0) + 1
                attempts[name] = count
                if count >= MAX_ATTEMPTS:
                    seen.add(name)
                    attempts.pop(name, None)
                    log("giving up on %s after %d attempts: %r"
                        % (name, count, exc))

        if len(seen) > 5000:
            present = {name for name, _, _, _ in candidates()}
            seen.intersection_update(present)
            for name in list(attempts):
                if name not in present:
                    attempts.pop(name, None)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
