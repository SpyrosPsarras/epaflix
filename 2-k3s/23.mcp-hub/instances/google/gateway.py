import hashlib
import os
import re

import mcp_hub
from starlette.applications import Starlette
from upstream import Upstream
import uvicorn


def app(token: str, *, transport=None) -> Starlette:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token) or len(token) < 16:
        raise ValueError("An independent Google upstream credential is required")
    return mcp_hub.build_app(
        {"google": hashlib.sha256(token.encode()).hexdigest()}, modules=(),
        ups=[("/mcp", Upstream("google", "http://127.0.0.1:8001/mcp", transport=transport))],
    )


if __name__ == "__main__":
    uvicorn.run(app(os.environ["GOOGLE_UPSTREAM_TOKEN"]), host="0.0.0.0", port=8000, access_log=False)
