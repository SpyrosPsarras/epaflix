#!/usr/bin/env python3
"""Shared SearXNG search tool for the T3 Code providers."""
import json
from urllib.parse import urlencode
from urllib.request import urlopen

URL = "https://searxng.epaflix.com/search"
INSTRUCTIONS = (
    "Use searxng_search for all web searches. Do not use other search engines, "
    "including through a browser, shell, or page-fetch tool. Direct fetching of "
    "known URLs is allowed for reading pages. If SearXNG fails, report the error "
    "rather than falling back to another search provider."
)


def searxng_search(query: str, limit: int = 10) -> dict:
    """Use this tool for ALL web searches, including searches otherwise done in a
    browser or shell. Returns URLs and snippets. Direct fetching of known URLs
    is allowed for reading pages. Report outages; never use another search provider.
    """
    if not query.strip():
        raise ValueError("query must not be empty")
    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    url = URL + "?" + urlencode({"q": query, "format": "json"})
    with urlopen(url, timeout=30) as response:
        data = json.load(response)
    return {
        "results": [
            {key: result.get(key, "") for key in ("title", "url", "content")}
            for result in data["results"][:limit]
        ],
        "unresponsive_engines": data.get("unresponsive_engines", []),
    }


if __name__ == "__main__":
    try:
        from mcp.server.mcpserver import MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP as MCPServer

    server = MCPServer("searxng", instructions=INSTRUCTIONS)
    server.tool()(searxng_search)
    server.run()
