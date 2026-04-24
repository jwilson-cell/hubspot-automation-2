"""Send the action-items digest email via Gmail.

Used by the hubspot-actions-digest skill in place of the Gmail MCP
create_draft call. The MCP path only creates drafts — on the server we want
actual sends so the digest lands in reviewer inboxes, not Drafts. This
helper mirrors scripts/send_customer_reply.py's Gmail auth + alias handling
but sends in one call (no draft/promote step).

Usage:
    cat payload.json | py scripts/send_digest_email.py            # dry-run (default, safer)
    cat payload.json | py scripts/send_digest_email.py --send     # live send

Payload (stdin, UTF-8 JSON):
    {
      "to_emails":  ["lconner@gopackn.com", "chansen@gopackn.com"],
      "subject":    "[HubSpot Digest] N tickets to review ...",
      "body_plain": "...",
      "body_html":  "<div>...</div>"   # optional; auto-derived from plain if absent
    }

From / Reply-To behavior: reads `auto_send` block from config/settings.yaml.
When `use_send_as_alias: true` is set (Pack'N's current config points at
customercare@gopackn.com), the From header is that alias. Gmail's verified
"Send mail as" alias makes it send cleanly with no "on behalf of"
disclosure, assuming SPF/DKIM are set up for the domain (they are for
gopackn.com per the Apr 23 verification).

Exit codes:
    0  success — stdout is JSON `{"sent_message_id": "...", "from": "...", "to": [...]}`
    2  bad args / bad payload
    3  Gmail token missing at config/.secrets/token.json
    4  Gmail API error
"""
from __future__ import annotations

import base64
import json
import sys
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

import yaml

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

ROOT = Path(__file__).resolve().parent.parent
GMAIL_TOKEN_PATH = ROOT / "config" / ".secrets" / "token.json"
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def _load_settings() -> dict:
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _gmail_service() -> object | None:
    if not GMAIL_TOKEN_PATH.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_PATH), GMAIL_SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        GMAIL_TOKEN_PATH.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _gmail_owner_email(service) -> str:
    profile = service.users().getProfile(userId="me").execute()
    return profile.get("emailAddress", "")


def _plain_to_html(text: str) -> str:
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return "<div>" + escaped.replace("\n", "<br>") + "</div>"


def _build_mime(payload: dict, auto_send_cfg: dict, owner_email: str) -> tuple[EmailMessage, str]:
    """Return (EmailMessage, from_address_used). Mirrors send_customer_reply.py."""
    display = auto_send_cfg.get("display_name") or ""
    if auto_send_cfg.get("use_send_as_alias") and auto_send_cfg.get("send_as_address"):
        from_addr = auto_send_cfg["send_as_address"]
    else:
        from_addr = owner_email
    reply_to = auto_send_cfg.get("reply_to") or ""

    to_emails = payload["to_emails"]

    msg = EmailMessage()
    msg["From"] = formataddr((display, from_addr)) if display else from_addr
    msg["To"] = ", ".join(to_emails)
    msg["Subject"] = payload["subject"]
    if reply_to:
        msg["Reply-To"] = reply_to

    body_plain = payload["body_plain"]
    body_html = payload.get("body_html") or _plain_to_html(body_plain)
    msg.set_content(body_plain)
    msg.add_alternative(body_html, subtype="html")
    return msg, from_addr


def _validate_payload(p: dict) -> str | None:
    if not isinstance(p.get("to_emails"), list) or not p["to_emails"]:
        return "to_emails must be a non-empty list of email addresses"
    if not p.get("subject"):
        return "subject is required"
    if not p.get("body_plain"):
        return "body_plain is required"
    return None


def main() -> int:
    send_flag = "--send" in sys.argv[1:]

    try:
        raw_stdin = sys.stdin.buffer.read().decode("utf-8")
        payload = json.loads(raw_stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"invalid stdin (expect UTF-8 JSON): {e}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("payload must be a JSON object", file=sys.stderr)
        return 2
    err = _validate_payload(payload)
    if err:
        print(err, file=sys.stderr)
        return 2

    settings = _load_settings()
    # Digest uses the same alias config as auto-send by design — sender identity
    # should match so replies to the digest (if any) route the same way.
    auto_send_cfg = settings.get("auto_send") or {}

    if not GMAIL_TOKEN_PATH.exists():
        print(f"Gmail token missing at {GMAIL_TOKEN_PATH}", file=sys.stderr)
        return 3

    gmail = _gmail_service()
    if gmail is None:
        print("could not build Gmail service", file=sys.stderr)
        return 3

    owner_email = _gmail_owner_email(gmail)
    msg, from_addr = _build_mime(payload, auto_send_cfg, owner_email)

    if not send_flag:
        body_plain = payload["body_plain"]
        print(json.dumps({
            "mode": "dry-run",
            "preview": {
                "from": msg["From"],
                "to": msg["To"],
                "reply_to": msg["Reply-To"],
                "subject": msg["Subject"],
                "body_plain_preview": body_plain[:300],
                "body_plain_chars": len(body_plain),
                "mime_bytes": len(msg.as_bytes()),
            },
        }, indent=2))
        return 0

    try:
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        resp = gmail.users().messages().send(userId="me", body={"raw": raw}).execute()
    except HttpError as e:
        print(f"Gmail API error: {e}", file=sys.stderr)
        return 4

    print(json.dumps({
        "sent_message_id": resp.get("id", ""),
        "from": from_addr,
        "to": payload["to_emails"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
