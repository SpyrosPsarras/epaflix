#!/usr/bin/env python3
"""SearXNG search tool for the T3 Code providers, stdio MCP without the SDK.

The pod image has no `mcp` package. The protocol needed here is three
requests, so a hand-rolled loop is smaller than a dependency.
"""
import json
import sys
from urllib.parse import urlencode
from urllib.request import urlopen

URL = "https://searxng.epaflix.com/search"
INSTRUCTIONS = (
    "Use searxng_search for all web searches. Do not use other search engines, "
    "including through a browser, shell, or page-fetch tool. Direct fetching of "
    "known URLs is allowed for reading pages. If SearXNG fails, report the error "
    "rather than falling back to another search provider."
)
TOOL = {
    "name": "searxng_search",
    "description": "Use this tool for ALL web searches, including searches otherwise done in a "
                   "browser or shell. Returns URLs and snippets. Direct fetching of known URLs "
                   "is allowed for reading pages. Report outages; never use another search provider.",
    "inputSchema": {"type": "object", "required": ["query"],
                    "properties": {"query": {"type": "string"},
                                   "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 20}}},
}


def searxng_search(query, limit=10):
    if not query.strip():
        raise ValueError("query must not be empty")
    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    with urlopen(URL + "?" + urlencode({"q": query, "format": "json"}), timeout=30) as response:
        data = json.load(response)
    return {"results": [{k: r.get(k, "") for k in ("title", "url", "content")} for r in data["results"][:limit]],
            "unresponsive_engines": data.get("unresponsive_engines", [])}


def handle(req):
    method, params = req.get("method"), req.get("params") or {}
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "searxng", "version": "1"},
                "instructions": INSTRUCTIONS}
    if method == "tools/list":
        return {"tools": [TOOL]}
    if method == "tools/call" and params.get("name") == "searxng_search":
        args = params.get("arguments") or {}
        try:
            result = searxng_search(args.get("query", ""), int(args.get("limit", 10)))
            return {"content": [{"type": "text", "text": json.dumps(result)}]}
        except Exception as exc:  # report, never fall back
            return {"content": [{"type": "text", "text": f"searxng error: {exc}"}], "isError": True}
    if method == "ping":
        return {}
    raise LookupError(method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        if "id" not in req:  # notification
            continue
        try:
            msg = {"jsonrpc": "2.0", "id": req["id"], "result": handle(req)}
        except LookupError as exc:
            msg = {"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": f"unknown method {exc}"}}
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
