import hashlib
import os
import re

import mcp_hub
import notion_grant
from starlette.applications import Starlette
from upstream import Upstream
import uvicorn

GRANT = "mcp-hub-outlook-grant"
BOOTSTRAP = "2-k3s/23.mcp-hub/tools/bootstrap-outlook.py"


def app(token: str, grant, *, transport=None) -> Starlette:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", token) or len(token) < 16:
        raise ValueError("An independent Outlook upstream credential is required")
    up = Upstream("outlook", "http://127.0.0.1:3000/mcp", grant.headers, on_unauthorized=grant.invalidate,
                  transport=transport)
    return mcp_hub.build_app({"outlook": hashlib.sha256(token.encode()).hexdigest()}, modules=(), ups=[("/mcp", up)])


if __name__ == "__main__":
    store = notion_grant.SecretStore(os.environ["HUB_NAMESPACE"], GRANT, label="Microsoft",
                                     bootstrap=BOOTSTRAP + " --context <approved-context>")
    uvicorn.run(app(os.environ["OUTLOOK_UPSTREAM_TOKEN"], notion_grant.Grant(store)),
                host="0.0.0.0", port=8000, access_log=False)
