"""Parse one CLIProxyAPI request-log payload file.

Two consumers, one parser:

  * cliproxy/tools/render-payload.py  - interactive transcript for a human
  * cliproxy/files/emit-transcripts.py - one JSON line per request for Loki

Lives under files/ because that directory is what the kustomize
configMapGenerator mounts into the pod at /scripts; the tools/ script imports it
from the repo path. Keeping a second copy in tools/ would guarantee the two
drift, and a parser that disagrees with itself about what was said is worse than
having no parser.

Payload layout, written by CLIProxyAPI's
internal/logging/request_logger_format.go:

    === REQUEST INFO ===     url, method, timestamps
    === HEADERS ===          downstream headers (bearer values already truncated)
    === REQUEST BODY ===     the client's JSON - prompts live here
    === API REQUEST 1 ===    what went upstream, incl. provider + account label
    === API RESPONSE 1 ===   upstream response, SSE frames when streaming
    === RESPONSE ===         what was streamed back to the client

Nothing here guesses. A section that will not parse is reported as missing, so a
format change upstream surfaces as an empty field rather than as invented text.
"""

import json
import re

_REDACTIONS = [
    (re.compile(r"\b(sk-ant-[A-Za-z0-9_\-]{8,})"), "sk-ant-<REDACTED>"),
    (re.compile(r"\b(sk-[A-Za-z0-9]{20,})"), "sk-<REDACTED>"),
    (re.compile(r"\b(ghp_[A-Za-z0-9]{20,})"), "ghp_<REDACTED>"),
    (re.compile(r"\b(github_pat_[A-Za-z0-9_]{20,})"), "github_pat_<REDACTED>"),
    (re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,})"), "gh_<REDACTED>"),
    (re.compile(r"\b(xox[baprs]-[A-Za-z0-9\-]{10,})"), "xox-<REDACTED>"),
    (re.compile(r"\b(AKIA[0-9A-Z]{16})"), "AKIA<REDACTED>"),
    (re.compile(r"\b(AIza[0-9A-Za-z_\-]{30,})"), "AIza<REDACTED>"),
    (re.compile(r"\b(omp-[A-Za-z0-9]{16,})"), "omp-<REDACTED>"),
    (re.compile(r"(?i)(password\s*=\s*)([^\s;\"']{3,})"), r"\1<REDACTED>"),
    (re.compile(r"(?i)(\"(?:password|token|secret|api[_-]?key)\"\s*:\s*\")([^\"]{3,})"),
     r"\1<REDACTED>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.S), "<REDACTED PRIVATE KEY>"),
]


def redact(text):
    """Blank known credential shapes. Never returns None."""
    if not text:
        return ""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def section(raw, name):
    match = re.search(r"^=== " + re.escape(name) + r"[^=\n]*===\n(.*?)(?=^=== |\Z)",
                      raw, re.S | re.M)
    return match.group(1) if match else ""


def first_json(text):
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


def field(raw, name, key):
    for line in section(raw, name).splitlines():
        head, _, value = line.partition(":")
        if head.strip() == key:
            return value.strip()
    return ""


def flatten(content, tool_input_chars=400, tool_result_chars=400):
    """Anthropic content blocks -> plain text, keeping tool activity visible."""
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
            out.append("[tool_use %s] %s" % (
                part.get("name"), json.dumps(part.get("input"))[:tool_input_chars]))
        elif kind == "tool_result":
            body = part.get("content")
            body = body if isinstance(body, str) else flatten(body)
            out.append("[tool_result] " + body[:tool_result_chars])
        else:
            out.append("[%s]" % kind)
    return "\n".join(x for x in out if x)


def tool_names(content):
    if not isinstance(content, list):
        return []
    return [p.get("name") for p in content
            if isinstance(p, dict) and p.get("type") == "tool_use" and p.get("name")]


def reassemble_sse(text, tool_input_chars=600):
    """Rebuild an assistant turn from a `data: {...}` event stream.

    Blocks arrive as start/delta/stop around an index and the delta type says what
    they are: text_delta, thinking_delta, or input_json_delta for tool arguments.
    Matching only text_delta - the obvious first attempt - reports "no response"
    for a thinking+tool_use turn, which is the commonest shape an agent produces.
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
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        etype = event.get("type")
        index = event.get("index")
        if etype == "content_block_start":
            block = event.get("content_block", {}) or {}
            blocks[index] = {"kind": block.get("type"), "name": block.get("name"),
                             "text": ""}
            order.append(index)
        elif etype == "content_block_delta":
            delta = event.get("delta", {}) or {}
            slot = blocks.setdefault(index, {"kind": None, "name": None, "text": ""})
            if index not in order:
                order.append(index)
            for key, kind in (("text", "text"), ("thinking", "thinking"),
                              ("partial_json", "tool_use")):
                if key in delta:
                    slot["text"] += delta[key] or ""
                    if slot["kind"] is None:
                        slot["kind"] = kind
    out = []
    tools = []
    for index in order:
        slot = blocks.get(index) or {}
        body = slot.get("text", "")
        if not body:
            continue
        if slot.get("kind") == "thinking":
            out.append("[thinking] " + body)
        elif slot.get("kind") == "tool_use":
            if slot.get("name"):
                tools.append(slot["name"])
            out.append("[tool_use %s] %s" % (slot.get("name"), body[:tool_input_chars]))
        else:
            out.append(body)
    return "\n".join(out), tools


def assistant_turn(raw):
    """The reply, preferring what the client received over the upstream copy."""
    for name in ("RESPONSE", "API RESPONSE 1"):
        body = section(raw, name)
        if not body:
            continue
        text, tools = reassemble_sse(body)
        if text:
            return text, tools, name
        obj = first_json(body)
        if obj:
            return flatten(obj.get("content")), tool_names(obj.get("content")), name
    return "", [], ""


def parse(raw):
    """Payload text -> dict. Raises ValueError if this is not a payload file."""
    if "=== REQUEST INFO ===" not in raw:
        raise ValueError("not a cliproxy payload file (no '=== REQUEST INFO ===')")

    request = first_json(section(raw, "REQUEST BODY"))
    if request is None:
        raise ValueError("request body is not parseable JSON")

    messages = request.get("messages") or []
    system = request.get("system")
    reply, reply_tools, reply_source = assistant_turn(raw)

    return {
        "url": field(raw, "REQUEST INFO", "URL"),
        "method": field(raw, "REQUEST INFO", "Method"),
        "requested_at": field(raw, "REQUEST INFO", "Timestamp"),
        "version": field(raw, "REQUEST INFO", "Version"),
        "model": request.get("model"),
        "stream": request.get("stream"),
        "upstream_url": field(raw, "API REQUEST 1", "Upstream URL"),
        "upstream_auth": field(raw, "API REQUEST 1", "Auth"),
        "upstream_status": field(raw, "API RESPONSE 1", "Status"),
        "responded_at": field(raw, "API RESPONSE 1", "Timestamp"),
        "system": flatten(system) if system else "",
        "messages": messages,
        "message_count": len(messages),
        "prompt_chars": len(json.dumps(request)),
        "reply": reply,
        "reply_tools": reply_tools,
        "reply_source": reply_source,
    }


def last_turn(parsed, user_chars=2000, tool_result_chars=500):
    """The NEW information in this call.

    An agent replays the whole conversation on every request, so shipping all
    messages would republish the same history once per call - the thing that made
    raw payloads cost ~1.1MB each. Only the final message is new.
    """
    messages = parsed.get("messages") or []
    if not messages:
        return {"role": "", "text": ""}
    last = messages[-1]
    text = flatten(last.get("content"), tool_result_chars=tool_result_chars)
    return {
        "role": last.get("role", ""),
        "text": text[:user_chars],
        "truncated": len(text) > user_chars,
        "tools": tool_names(last.get("content")),
    }
