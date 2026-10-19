import json
import logging
import os
from pathlib import Path
from urllib.parse import quote

import gmail_mcp


def register_send_draft(server, credential_path: Path) -> None:
    from fastmcp.exceptions import ToolError

    try:
        grant = json.loads(credential_path.read_text())
        if not isinstance(grant, dict) or not all(
            isinstance(grant.get(key), str) and grant[key].strip()
            for key in ("client_id", "client_secret", "refresh_token", "token_uri")
        ) or grant["token_uri"] != "https://oauth2.googleapis.com/token" or not (
            isinstance(grant.get("scopes"), list) and grant["scopes"]
            and all(isinstance(scope, str) and scope.strip() for scope in grant["scopes"])
        ):
            raise ValueError
    except (OSError, ValueError, TypeError):
        raise ValueError("Google draft credentials unavailable") from None
    gmail_mcp.AUTH = gmail_mcp.Auth(grant["client_id"], grant["client_secret"], grant["refresh_token"])

    @server.tool()
    def gmail_send_draft(draft_id: str, user_google_email: str | None = None) -> str:
        """Send an existing Gmail draft immediately. Sent mail cannot be recalled."""
        if not draft_id.strip() or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in draft_id):
            raise ToolError("A nonempty draft_id without control characters is required")
        if not isinstance(gmail_mcp.AUTH, gmail_mcp.Auth):
            raise ToolError("Google draft credentials unavailable")
        try:
            return json.dumps(gmail_mcp._send_draft(draft_id))
        except (RuntimeError, ValueError, KeyError, OSError, TypeError):
            raise ToolError("Google draft send failed") from None


def main() -> None:
    try:
        email = os.environ["USER_GOOGLE_EMAIL"]
        directory = os.environ["WORKSPACE_MCP_CREDENTIALS_DIR"]
        if not email.strip() or not directory.strip():
            raise ValueError
        credential_path = Path(directory) / (quote(email, safe="@._-") + ".json")
    except (KeyError, ValueError):
        raise SystemExit("Google draft credentials unavailable") from None
    logging.basicConfig(level=logging.CRITICAL)
    import main as pinned_main

    original_filter = pinned_main.filter_server_tools

    def filter_and_register(server):
        tools_removed = original_filter(server)
        try:
            register_send_draft(server, credential_path)
        except ValueError:
            raise SystemExit("Google draft credentials unavailable") from None
        return tools_removed

    pinned_main.filter_server_tools = filter_and_register
    pinned_main.main()


if __name__ == "__main__":
    main()
