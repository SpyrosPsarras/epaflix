#!/usr/bin/env python3
import hashlib
import os
import re

import mcp_hub
from fastmcp import FastMCP
from fastmcp.skills.provider import SkillsDirectoryProvider
from starlette.responses import PlainTextResponse
import uvicorn


def app(token, root):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token) or len(token) < 16:
        raise ValueError("An independent skills upstream credential is required")
    server = FastMCP("skills", providers=[SkillsDirectoryProvider(root, reload=True)])

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request):
        return PlainTextResponse("ok")

    return mcp_hub.BearerAuth(server.http_app(path="/mcp"), {"skills": hashlib.sha256(token.encode()).hexdigest()})


if __name__ == "__main__":
    uvicorn.run(app(os.environ["SKILLS_UPSTREAM_TOKEN"], os.environ["SKILLS_ROOT"]),
                host="0.0.0.0", port=8000, access_log=False)
