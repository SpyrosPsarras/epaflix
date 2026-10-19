import json
import os
from pathlib import Path
from urllib.parse import quote

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
]


def seed(directory: Path, email: str, client_id: str, client_secret: str, refresh_token: str) -> Path:
    if not all(isinstance(value, str) and value.strip() for value in (email, client_id, client_secret, refresh_token)):
        raise ValueError("Google credential seed failed")
    credential_path = directory / (quote(email, safe="@._-") + ".json")
    grant = {"refresh_token": refresh_token, "token_uri": "https://oauth2.googleapis.com/token",
             "client_id": client_id, "client_secret": client_secret, "scopes": SCOPES}
    mask = os.umask(0o077)
    try:
        with credential_path.open("w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(grant, stream)
    except (OSError, ValueError):
        raise ValueError("Google credential seed failed") from None
    finally:
        os.umask(mask)
    return credential_path


if __name__ == "__main__":
    try:
        seed(Path(os.environ["WORKSPACE_MCP_CREDENTIALS_DIR"]), os.environ["USER_GOOGLE_EMAIL"],
             os.environ["GOOGLE_OAUTH_CLIENT_ID"], os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
             os.environ["GOOGLE_REFRESH_TOKEN"])
    except (KeyError, ValueError, OSError):
        raise SystemExit("Google credential seed failed") from None
