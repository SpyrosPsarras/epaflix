#!/usr/bin/env python3
"""Render one cliproxy request-log payload file (on stdin) as a readable transcript.

Called by cliproxy-payload.sh. A separate file rather than inlined in the shell
script because the SSE regexes need both quote characters, which `python3 -c`
inside a single-quoted shell string cannot carry.

Parses the `=== SECTION ===` layout that CLIProxyAPI's
internal/logging/request_logger_format.go writes:

    === REQUEST INFO ===     url, method, timestamps
    === HEADERS ===          downstream headers (bearer values already truncated)
    === REQUEST BODY ===     the client's JSON - prompts live here
    === API REQUEST 1 ===    what was sent upstream, incl. provider + auth label
    === API RESPONSE 1 ===   upstream response, SSE when streaming
    === RESPONSE ===         what was streamed back to the client

A format change upstream shows up as a parse failure here, not as silently wrong
output - the messages are the point, so guessing at them is worse than failing.
"""

import json
import re
import sys

MAX_MSG = 2000
MAX_ASSISTANT = 6000


def section(raw, name):
    m = re.search(r"^=== " + re.escape(name) + r"[^=\n]*===\n(.*?)(?=^=== |\Z)",
                  raw, re.S | re.M)
    return m.group(1) if m else ""


def first_json(text):
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def flatten(content):
    """Anthropic content blocks -> plain text, keeping tool calls visible."""
    if isinstance(content, str):
        return content
    out = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            out.append(part.get("text", ""))
        elif kind == "thinking":
            out.append("[thinking] " + part.get("thinking", ""))
        elif kind == "tool_use":
            out.append("[tool_use %s] %s" % (part.get("name"),
                                             json.dumps(part.get("input"))[:400]))
        elif kind == "tool_result":
            body = part.get("content")
            body = body if isinstance(body, str) else flatten(body)
            out.append("[tool_result] " + body[:400])
        else:
            out.append("[%s]" % kind)
    return "\n".join(x for x in out if x)


def reassemble_sse(text):
    """Rebuild the assistant turn from a `data: {...}` event stream.

    Blocks arrive as start/delta/stop around an index, and the delta type says
    what it is: text_delta, thinking_delta, or input_json_delta for tool args.
    Only text_delta was handled at first, which made a tool-call-only turn look
    like "no response body captured" - the common case for an agent.
    """
    blocks = {}
    order = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload.startswith("{"):
            continue
        try:
            evt = json.loads(payload)
        except json.JSONDecodeError:
            continue
        etype = evt.get("type")
        idx = evt.get("index")
        if etype == "content_block_start":
            block = evt.get("content_block", {}) or {}
            blocks[idx] = {"kind": block.get("type"),
                           "name": block.get("name"),
                           "text": ""}
            order.append(idx)
        elif etype == "content_block_delta":
            delta = evt.get("delta", {}) or {}
            slot = blocks.setdefault(idx, {"kind": None, "name": None, "text": ""})
            if idx not in order:
                order.append(idx)
            for key in ("text", "thinking", "partial_json"):
                if key in delta:
                    slot["text"] += delta[key] or ""
                    if slot["kind"] is None:
                        slot["kind"] = {"text": "text",
                                        "thinking": "thinking",
                                        "partial_json": "tool_use"}[key]
    out = []
    for idx in order:
        slot = blocks.get(idx) or {}
        body = slot.get("text", "")
        if not body:
            continue
        if slot.get("kind") == "thinking":
            out.append("[thinking] " + body)
        elif slot.get("kind") == "tool_use":
            out.append("[tool_use %s] %s" % (slot.get("name"), body[:600]))
        else:
            out.append(body)
    return "\n".join(out)


def main():
    raw = sys.stdin.read()
    if "=== REQUEST INFO ===" not in raw:
        sys.exit("not a cliproxy payload file (no '=== REQUEST INFO ===' header)")

    req = first_json(section(raw, "REQUEST BODY"))
    if req is None:
        sys.exit("could not parse the request body as JSON - use `raw` and read it by hand")

    print("model    :", req.get("model"))
    print("stream   :", req.get("stream"))
    for line in section(raw, "REQUEST INFO").splitlines():
        key, _, value = line.partition(":")
        if key in ("URL", "Timestamp"):
            print("%-9s: %s" % (key.lower(), value.strip()))
    for line in section(raw, "API REQUEST 1").splitlines():
        if line.startswith(("Upstream URL:", "Auth:")):
            key, _, value = line.partition(":")
            print("%-9s: %s" % (key.lower().replace(" ", "-"), value.strip()))
    print()

    sysmsg = req.get("system")
    if sysmsg:
        body = flatten(sysmsg)
        print("--- system (%d chars, first 300) ---" % len(body))
        print(body[:300].rstrip())
        print()

    for msg in req.get("messages", []):
        body = flatten(msg.get("content"))
        print("--- %s (%d chars) ---" % (msg.get("role"), len(body)))
        if len(body) <= MAX_MSG:
            print(body)
        else:
            print(body[:MAX_MSG] + "\n[... truncated, use `raw` for all of it]")
        print()

    # Prefer what the client actually received; fall back to the upstream copy.
    for name in ("RESPONSE", "API RESPONSE 1"):
        body = section(raw, name)
        if not body:
            continue
        text = reassemble_sse(body)
        if not text:
            obj = first_json(body)
            text = flatten(obj.get("content")) if obj else ""
        if text:
            print("--- assistant (from %s) ---" % name)
            print(text[:MAX_ASSISTANT])
            if len(text) > MAX_ASSISTANT:
                print("[... truncated, use `raw` for all of it]")
            return
    print("--- no response body captured (check `raw`) ---")


if __name__ == "__main__":
    main()
