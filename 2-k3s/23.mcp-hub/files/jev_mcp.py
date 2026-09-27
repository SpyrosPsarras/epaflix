#!/usr/bin/env python3
"""Jev (TypeSafe System One) decisions for the MCP hub, via OpenRouter.

Env: JEV_OPENROUTER_KEY (Secret mcp-hub-jev), JEV_MODEL (default jev-1.13).
Jev returns a typed choice with probabilities, never prose. The instructions
ask the agent to consult it before its first action on a task. Registered for
OpenCode only (hub_clients.OPENCODE_ONLY). Each decision logs one line with
request id, choice, confidence, cost and latency, never the prompt.
"""

import json
import math
import os
import re
import time
import urllib.error
import urllib.request

URL = "https://openrouter.ai/api/v1/systemone"
OPTION = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
INSTRUCTIONS = (
    "Jev is a fast, cheap decision model. It returns one choice from options you define, with a "
    "confidence, not prose. At the start of every new user task, before any other tool call, call "
    "jev_decide with: situation = the user's request plus context you already have; question = "
    "'What should be done first?'; options = 2 to 6 short, concrete first steps you would consider "
    "(for example read_code, inspect_logs, run_tests, ask_user). Follow the choice when confidence is "
    "at least 0.6 and it does not conflict with the user's instructions, safety rules or facts you "
    "verified; otherwise use your own judgement and say why. Report the choice and confidence in one "
    "line. Use jev_decide again at later decision points with discrete options. A Jev choice never "
    "authorizes deployments, deletions, credential access or sending messages."
)


def _validate(situation, question, options):
    if not situation.strip() or len(situation) > 12000:
        raise ValueError("situation must be 1 to 12000 characters")
    if not question.strip() or len(question) > 1000:
        raise ValueError("question must be 1 to 1000 characters")
    if not 2 <= len(options) <= 10:
        raise ValueError("options needs 2 to 10 entries")
    for name, description in options.items():
        if not OPTION.match(name):
            raise ValueError(f"option {name!r} must be snake_case, starting with a letter, up to 40 characters")
        if not description.strip() or len(description) > 500:
            raise ValueError(f"option {name!r} needs a description of 1 to 500 characters")


def _post(key, body):
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise ValueError(f"OpenRouter HTTP {e.code}") from None  # no upstream body: it may echo the prompt


def decide(key, situation, question, options, model="jev-1.13", post=_post):
    _validate(situation, question, options)
    if not key:
        raise ValueError("JEV_OPENROUTER_KEY is not set (Secret mcp-hub-jev)")
    start = time.monotonic()
    data = post(key, {"model": model, "state": situation, "questions": {
        "decision": {"type": "choice", "instructions": question, "criteria": options}}})
    answer = (data.get("answers") or {}).get("decision") or {}
    confidence = answer.get("confidence")
    if answer.get("choice") not in options or not isinstance(confidence, (int, float)) \
            or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("invalid Jev response")
    result = {"choice": answer["choice"], "confidence": confidence,
              "probabilities": answer.get("probabilities", {}), "request_id": data.get("id"),
              "model": data.get("model"), "cost_usd": (data.get("usage") or {}).get("cost"),
              "latency_ms": round((time.monotonic() - start) * 1000)}
    print("jev_decide " + json.dumps({k: result[k] for k in ("request_id", "choice", "confidence", "cost_usd",
                                                               "latency_ms")}))
    return result


def server():
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    mcp = MCPServer("jev", instructions=INSTRUCTIONS)

    @mcp.tool()
    def jev_decide(situation: str, question: str, options: dict[str, str]) -> str:
        """Ask Jev to pick one option. situation: the request and relevant context. question: the decision to make. options: {snake_case_name: what this option means}, 2 to 10 entries. Returns choice, confidence (0-1), probabilities, request_id and cost_usd. Call it first on every new task (see server instructions)."""
        try:
            return json.dumps(decide(os.environ.get("JEV_OPENROUTER_KEY", "").strip(), situation, question, options,
                                     os.environ.get("JEV_MODEL", "jev-1.13")))
        except (ValueError, OSError) as e:  # report, never guess a choice
            raise ToolError(f"jev error: {e}") from None

    return mcp


def _selftest():
    opts = {"read_code": "Read the relevant code", "ask_user": "Ask a clarifying question"}
    sent = []

    def fake(choice, confidence):
        def post(key, body):
            sent.append((key, body))
            return {"id": "gen-1", "model": "typesafe/jev", "usage": {"cost": 0.00002},
                    "answers": {"decision": {"choice": choice, "confidence": confidence,
                                             "probabilities": {choice: confidence}}}}
        return post

    r = decide("k", "Fix the typo in README", "What should be done first?", opts, post=fake("read_code", 0.9))
    assert r["choice"] == "read_code" and r["confidence"] == 0.9 and r["cost_usd"] == 0.00002, r
    key, body = sent[-1]
    assert key == "k" and body["model"] == "jev-1.13" and body["state"] == "Fix the typo in README", body
    assert body["questions"]["decision"] == {"type": "choice", "instructions": "What should be done first?",
                                             "criteria": opts}, body
    for choice, confidence in (("invented", 0.9), ("read_code", 2), ("read_code", float("nan")), ("read_code", None)):
        try:
            decide("k", "s", "q", opts, post=fake(choice, confidence))
            raise AssertionError(f"accepted {choice} {confidence}")
        except ValueError as e:
            assert "invalid Jev response" in str(e)
    calls = len(sent)
    for bad in ({"only": "one"}, {"Bad-Name": "x", "ok": "y"}, {"a": " ", "b": "y"}):
        try:
            decide("k", "s", "q", bad, post=fake("a", 1))
            raise AssertionError(f"accepted options {bad}")
        except ValueError:
            pass
    for args in (("k", " ", "q"), ("k", "s", ""), ("", "s", "q")):
        try:
            decide(*args, opts, post=fake("read_code", 1))
            raise AssertionError(f"accepted {args}")
        except ValueError:
            pass
    assert len(sent) == calls, "invalid input or a missing key must not reach OpenRouter"
    print("jev selftest OK")


if __name__ == "__main__":
    _selftest()
