"""Send a customer-facing reply via Gmail and log it on the HubSpot ticket.

Used by the hubspot-tickets skill for auto-send on form Mispack / Carrier Issue
tickets. Conversations API cannot create new threads for form-sourced tickets,
so we send via Gmail (reusing the existing OAuth token) and log the send as an
email engagement on the ticket for visibility in the HubSpot timeline.

The engagement is created via the legacy v1 engagements endpoint
(/engagements/v1/engagements) rather than v3 /crm/v3/objects/emails, because
v3 does not correctly parse a synthetic hs_email_headers blob into the
from/to metadata that HubSpot's ticket timeline requires to render the
engagement. v1 accepts explicit from/to objects that populate reliably.

Usage:
    cat payload.json | py scripts/send_customer_reply.py            # dry-run
    cat payload.json | py scripts/send_customer_reply.py --send     # live

Payload (stdin):
    {
      "ticket_id":   "44708236804",
      "to_email":    "jwilson@gopackn.com",
      "to_name":     "Jacob Wilson",
      "subject":     "Re: Carrier Issue — Order 28931",
      "body_plain":  "Hi Jacob, ...",
      "body_html":   "<p>Hi Jacob, ...</p>"
    }

Exit codes:
    0  success (both Gmail send + HubSpot log OK, or dry-run)
    2  bad args / bad payload
    3  Gmail token missing
    4  Gmail API error
    5  HubSpot token missing
    6  HubSpot API error (before Gmail send)
    7  PARTIAL — Gmail send succeeded but HubSpot log failed. Customer got the
       email; HubSpot has no record. Operator must log manually.
"""
from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
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
HUBSPOT_TOKEN_PATH = ROOT / "config" / ".secrets" / "hubspot_token.txt"
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
HUBSPOT_API_BASE = "https://api.hubapi.com"


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


def _build_mime(payload: dict, auto_send_cfg: dict, owner_email: str) -> tuple[EmailMessage, str]:
    """Return (EmailMessage, from_address_used)."""
    display = auto_send_cfg.get("display_name") or ""
    if auto_send_cfg.get("use_send_as_alias") and auto_send_cfg.get("send_as_address"):
        from_addr = auto_send_cfg["send_as_address"]
    else:
        from_addr = owner_email
    reply_to = auto_send_cfg.get("reply_to") or ""

    to_email = payload["to_email"]
    to_name = payload.get("to_name") or ""

    msg = EmailMessage()
    msg["From"] = formataddr((display, from_addr)) if display else from_addr
    msg["To"] = formataddr((to_name, to_email)) if to_name else to_email
    msg["Subject"] = payload["subject"]
    if reply_to:
        msg["Reply-To"] = reply_to

    body_plain = payload["body_plain"]
    body_html = payload.get("body_html") or _plain_to_html(body_plain)
    msg.set_content(body_plain)
    msg.add_alternative(body_html, subtype="html")
    return msg, from_addr


def _plain_to_html(text: str) -> str:
    escaped = (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return "<div>" + escaped.replace("\n", "<br>") + "</div>"


def _gmail_send(service, msg: EmailMessage) -> str:
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    resp = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    return resp.get("id", "")


def _gmail_owner_email(service) -> str:
    profile = service.users().getProfile(userId="me").execute()
    return profile.get("emailAddress", "")


def _hubspot_request(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{HUBSPOT_API_BASE}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:
        raw = resp.read().decode("utf-8") or "{}"
        return json.loads(raw)


def _hubspot_lookup_contact_id(token: str, ticket_id: str) -> int | None:
    """Return the first contactId associated with the ticket, or None."""
    try:
        data = _hubspot_request(
            "GET",
            f"/crm/v4/objects/tickets/{ticket_id}/associations/contacts",
            token,
        )
    except urllib.error.HTTPError:
        return None
    results = data.get("results") or []
    if not results:
        return None
    return int(results[0]["toObjectId"])


def _split_name(to_name: str) -> tuple[str, str]:
    if not to_name:
        return "", ""
    parts = to_name.strip().split(None, 1)
    first = parts[0]
    last = parts[1] if len(parts) > 1 else ""
    return first, last


def _hubspot_log_email(
    token: str,
    ticket_id: str,
    from_addr: str,
    from_display: str,
    to_email: str,
    to_name: str,
    subject: str,
    body_plain: str,
    body_html: str,
    sent_at: datetime,
) -> str:
    """Create an EMAIL engagement on the ticket + contact (technical audit).

    Goes to the v1 engagements endpoint (better metadata parsing than v3).
    NOTE: Help Desk ticket timelines in HubSpot UI do NOT render engagement
    records — only conversation-thread messages. So this email engagement is
    an audit-trail artifact that is reachable via API / contact timeline, but
    the reviewer will not see it on the ticket itself. See _hubspot_log_note
    below for the timeline-visible record.
    """
    ts_ms = int(sent_at.timestamp() * 1000)
    from_first, from_last = _split_name(from_display)
    to_first, to_last = _split_name(to_name)

    associations: dict = {"ticketIds": [int(ticket_id)]}
    contact_id = _hubspot_lookup_contact_id(token, ticket_id)
    if contact_id is not None:
        associations["contactIds"] = [contact_id]

    body = {
        "engagement": {"active": True, "type": "EMAIL", "timestamp": ts_ms},
        "associations": associations,
        "metadata": {
            "from": {"email": from_addr, "firstName": from_first, "lastName": from_last},
            "to": [{"email": to_email, "firstName": to_first, "lastName": to_last}],
            "cc": [],
            "bcc": [],
            "subject": subject,
            "html": body_html,
            "text": body_plain,
        },
    }
    resp = _hubspot_request("POST", "/engagements/v1/engagements", token, body=body)
    return str(resp["engagement"]["id"])


def _hubspot_log_note(
    token: str,
    ticket_id: str,
    from_addr: str,
    to_email: str,
    subject: str,
    body_plain: str,
    sent_at: datetime,
    gmail_message_id: str,
    metadata_block: str = "",
) -> str:
    """Create a note engagement on the ticket summarizing the auto-send.

    This is the timeline-visible record of the auto-send. HubSpot Help Desk
    ticket timelines render note engagements but NOT email engagements (which
    are sidelined because form tickets have no conversation thread). The note
    is the reviewer's only visual audit of the auto-sent reply.

    If `metadata_block` is provided (a PACKN_METADATA_V1 block from the
    caller), it is appended verbatim to the note body. The remote digest
    parses this block to reconstruct classifier output + action items
    without a local queue file.
    """
    ts_ms = int(sent_at.timestamp() * 1000)
    note_body = (
        f"[AUTO-SENT TO CUSTOMER] Pack'N automation sent this reply on "
        f"{sent_at.strftime('%Y-%m-%d %H:%M UTC')}.\n\n"
        f"To: {to_email}\n"
        f"From: {from_addr}\n"
        f"Subject: {subject}\n"
        f"Gmail message id: {gmail_message_id}\n\n"
        f"---\n"
        f"{body_plain}\n"
        f"---\n"
    )
    if metadata_block:
        note_body += f"\n{metadata_block}\n"
    associations: dict = {"ticketIds": [int(ticket_id)]}
    contact_id = _hubspot_lookup_contact_id(token, ticket_id)
    if contact_id is not None:
        associations["contactIds"] = [contact_id]
    body = {
        "engagement": {"active": True, "type": "NOTE", "timestamp": ts_ms},
        "associations": associations,
        "metadata": {"body": note_body},
    }
    resp = _hubspot_request("POST", "/engagements/v1/engagements", token, body=body)
    return str(resp["engagement"]["id"])


def _validate_payload(p: dict) -> str | None:
    required = ["ticket_id", "to_email", "subject", "body_plain"]
    for k in required:
        if not p.get(k):
            return f"missing or empty field: {k}"
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
    auto_send_cfg = settings.get("auto_send") or {}

    if not GMAIL_TOKEN_PATH.exists():
        print(f"Gmail token missing at {GMAIL_TOKEN_PATH}", file=sys.stderr)
        return 3
    if not HUBSPOT_TOKEN_PATH.exists():
        print(f"HubSpot token missing at {HUBSPOT_TOKEN_PATH}", file=sys.stderr)
        return 5

    gmail = _gmail_service()
    if gmail is None:
        print("could not build Gmail service", file=sys.stderr)
        return 3
    owner_email = _gmail_owner_email(gmail)
    msg, from_addr = _build_mime(payload, auto_send_cfg, owner_email)

    hubspot_token = HUBSPOT_TOKEN_PATH.read_text(encoding="utf-8").strip()

    if not send_flag:
        body_plain = payload["body_plain"]
        body_html = payload.get("body_html") or _plain_to_html(body_plain)
        print(json.dumps({
            "mode": "dry-run",
            "gmail_preview": {
                "from": msg["From"],
                "to": msg["To"],
                "reply_to": msg["Reply-To"],
                "subject": msg["Subject"],
                "body_plain_preview": body_plain[:200],
                "mime_bytes": len(msg.as_bytes()),
            },
            "hubspot_engagement_preview": {
                "endpoint": "POST /engagements/v1/engagements",
                "ticket_id": payload["ticket_id"],
                "contact_id_lookup": _hubspot_lookup_contact_id(hubspot_token, payload["ticket_id"]),
                "from_email": from_addr,
                "from_display": auto_send_cfg.get("display_name") or "",
                "to_email": payload["to_email"],
                "to_name": payload.get("to_name") or "",
                "subject": payload["subject"],
            },
        }, indent=2))
        return 0

    try:
        gmail_msg_id = _gmail_send(gmail, msg)
    except HttpError as e:
        print(f"Gmail API error: {e}", file=sys.stderr)
        return 4

    sent_at = datetime.now(timezone.utc)
    body_plain = payload["body_plain"]
    body_html = payload.get("body_html") or _plain_to_html(body_plain)

    email_engagement_id = ""
    note_engagement_id = ""
    hubspot_errors: list[str] = []

    try:
        email_engagement_id = _hubspot_log_email(
            hubspot_token,
            payload["ticket_id"],
            from_addr,
            auto_send_cfg.get("display_name") or "",
            payload["to_email"],
            payload.get("to_name") or "",
            payload["subject"],
            body_plain,
            body_html,
            sent_at,
        )
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        hubspot_errors.append(f"email_engagement: HTTP {e.code}: {err_body[:300]}")

    try:
        note_engagement_id = _hubspot_log_note(
            hubspot_token,
            payload["ticket_id"],
            from_addr,
            payload["to_email"],
            payload["subject"],
            body_plain,
            sent_at,
            gmail_msg_id,
            metadata_block=payload.get("metadata_block") or "",
        )
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        hubspot_errors.append(f"note_engagement: HTTP {e.code}: {err_body[:300]}")

    # Timeline visibility requires the note. If the note failed, the reviewer
    # has no visual record on the ticket — surface as partial failure so the
    # operator can log manually.
    if not note_engagement_id:
        print(json.dumps({
            "partial_failure": True,
            "gmail_message_id": gmail_msg_id,
            "email_engagement_id": email_engagement_id,
            "note_engagement_id": "",
            "hubspot_errors": hubspot_errors,
            "remediation": "email was sent to customer; the timeline-visible note failed to post — add manually on the ticket",
        }, indent=2), file=sys.stderr)
        return 7

    result = {
        "gmail_message_id": gmail_msg_id,
        "note_engagement_id": note_engagement_id,
        "email_engagement_id": email_engagement_id,
        "from": from_addr,
        "to": payload["to_email"],
        "sent_at": sent_at.isoformat(),
    }
    if hubspot_errors:
        result["non_blocking_hubspot_errors"] = hubspot_errors
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
