#!/usr/bin/env python3
"""Offline search/configuration checks. Uses the installed Codex config writer."""
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import tomllib
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit


def load(filename):
    spec = importlib.util.spec_from_file_location(filename, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


search = load("searxng_mcp.py")
config = load("configure-search.py")
with patch.object(search, "urlopen", return_value=io.StringIO(json.dumps({
    "results": [{"title": "A", "url": "https://example.org", "content": "snippet"}] * 3,
    "unresponsive_engines": [["bing", "timeout"]],
}))) as request:
    result = search.searxng_search("a & β", 2)
    assert len(result["results"]) == 2
    assert result["unresponsive_engines"] == [["bing", "timeout"]]
    assert parse_qs(urlsplit(request.call_args.args[0]).query)["q"] == ["a & β"]
for query, limit in [(" ", 1), ("test", 0), ("test", 21)]:
    try:
        search.searxng_search(query, limit)
        raise AssertionError("invalid arguments accepted")
    except ValueError:
        pass
with patch.object(search, "urlopen", side_effect=HTTPError(search.URL, 503, "unavailable", {}, None)):
    try:
        search.searxng_search("test")
        raise AssertionError("failure hidden")
    except HTTPError:
        pass

with tempfile.TemporaryDirectory() as tmp:
    home = Path(tmp)
    codex = home / ".codex"
    codex.mkdir()
    (codex / "config.toml").write_text('web_search = "live"\n[projects."/work"]\ntrust_level = "trusted"\n')
    (home / ".claude.json").write_text('{"mcpServers":{"keepass":{"command":"keepass-mcp"}}}')
    oc = home / ".config/opencode/opencode.json"
    oc.parent.mkdir(parents=True)
    oc.write_text('{"permission":"allow","model":"existing/model"}')
    with patch.dict(os.environ, {"HOME": tmp, "CODEX_HOME": str(codex)}):
        config.configure(home)
        before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
        config.configure(home)
        assert before == {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    cc = tomllib.loads((codex / "config.toml").read_text())
    assert cc["web_search"] == "disabled"
    assert cc["projects"]["/work"]["trust_level"] == "trusted"
    assert cc["mcp_servers"]["searxng"]["command"] == "/usr/local/bin/searxng-mcp"
    assert "keepass" in json.loads((home / ".claude.json").read_text())["mcpServers"]
    assert "WebSearch" in json.loads((home / ".claude/settings.json").read_text())["permissions"]["deny"]
    cfg = json.loads(oc.read_text())
    assert cfg["permission"] == {"*": "allow", "websearch": "deny"}
    assert cfg["model"] == "existing/model"
print("PASS: search encoding, bounds, outage propagation, configuration preservation and idempotence")
