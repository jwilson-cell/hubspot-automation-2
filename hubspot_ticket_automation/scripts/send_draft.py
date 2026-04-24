"""Promote a Gmail draft to Sent.

Usage:
    py scripts/send_draft.py <draft_id>

Exits 0 on success and prints the sent message id to stdout.
Exits non-zero on any failure and writes the error to stderr.
"""
from pathlib import Path
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = ROOT / "config" / ".secrets" / "token.json"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: send_draft.py <draft_id>", file=sys.stderr)
        return 2
    draft_id = sys.argv[1].strip()
    if not draft_id:
        print("empty draft_id", file=sys.stderr)
        return 2

    if not TOKEN_PATH.exists():
        print(f"token.json missing at {TOKEN_PATH} — run gmail_auth.py first", file=sys.stderr)
        return 3

    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    try:
        resp = service.users().drafts().send(userId="me", body={"id": draft_id}).execute()
    except HttpError as e:
        print(f"gmail api error: {e}", file=sys.stderr)
        return 4

    msg_id = resp.get("id", "")
    print(msg_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
