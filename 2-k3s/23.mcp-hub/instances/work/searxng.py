#!/usr/bin/env python3
import hashlib
import os
import re

import mcp_hub
import searxng_mcp
import uvicorn


def app(token):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token) or len(token) < 16:
        raise ValueError("An independent search upstream credential is required")
    return mcp_hub.build_app({"work-search": hashlib.sha256(token.encode()).hexdigest()},
                             modules=[("/mcp", searxng_mcp.server)], ups=[])


if __name__ == "__main__":
    uvicorn.run(app(os.environ["SEARCH_UPSTREAM_TOKEN"]), host="0.0.0.0", port=8000, access_log=False)
