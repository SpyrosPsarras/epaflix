#!/usr/bin/env python3
"""Web search for the MCP hub over the in-cluster SearXNG (2-k3s/14.searxng).

Env: SEARXNG_URL (default http://searxng.searxng.svc.cluster.local:8080).
The tool name, arguments and instructions are the ones every client already
knows from the old stdio script; the workspace rules name searxng_search.
"""

import json
import os
import urllib.parse
import urllib.request

INSTRUCTIONS = (
    "Use searxng_search for all web searches. Do not use other search engines, "
    "including through a browser, shell, or page-fetch tool. Direct fetching of "
    "known URLs is allowed for reading pages. If SearXNG fails, report the error "
    "rather than falling back to another search provider."
)


def _search(base, query, limit):
    if not query.strip():
        raise ValueError("query must not be empty")
    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    url = base.rstrip("/") + "/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.load(resp)
    return {"results": [{k: r.get(k, "") for k in ("title", "url", "content")} for r in data["results"][:limit]],
            "unresponsive_engines": data.get("unresponsive_engines", [])}


def server():
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    base = os.environ.get("SEARXNG_URL", "http://searxng.searxng.svc.cluster.local:8080")
    mcp = MCPServer("searxng", instructions=INSTRUCTIONS)

    @mcp.tool()
    def searxng_search(query: str, limit: int = 10) -> str:
        """Use this tool for ALL web searches, including searches otherwise done in a browser or shell. Returns URLs and snippets. Direct fetching of known URLs is allowed for reading pages. Report outages; never use another search provider."""
        try:
            return json.dumps(_search(base, query, limit))
        except (ValueError, OSError, KeyError) as e:  # report, never fall back
            raise ToolError(f"searxng error: {e}") from None

    return mcp
