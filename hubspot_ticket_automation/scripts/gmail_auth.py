"""One-time Gmail OAuth consent. Produces config/.secrets/token.json.

Run once from the project root:
    py scripts/gmail_auth.py

Opens a browser; after you allow, writes the refresh token to disk.
Because the Workspace OAuth client is Internal, the refresh token does not expire.
"""
from pathlib import Path
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
ROOT = Path(__file__).resolve().parent.parent
CREDS_PATH = ROOT / "config" / ".secrets" / "credentials.json"
TOKEN_PATH = ROOT / "config" / ".secrets" / "token.json"


def main() -> int:
    if not CREDS_PATH.exists():
        print(f"credentials.json not found at {CREDS_PATH}", file=sys.stderr)
        return 1

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if creds and creds.valid:
        print(f"Token already valid at {TOKEN_PATH}")
        return 0

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
        creds = flow.run_local_server(port=0)

    TOKEN_PATH.write_text(creds.to_json())
    print(f"Token written to {TOKEN_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
