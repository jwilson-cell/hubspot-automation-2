"""Post a customer-facing reply to a HubSpot help desk ticket.

Usage:
    echo "reply body" | py scripts/send_hubspot_reply.py <ticket_id>           # dry-run
    echo "reply body" | py scripts/send_hubspot_reply.py <ticket_id> --send    # live

Dry-run (default):
    Looks up the Conversations thread(s) associated with the ticket, fetches the
    most recent outgoing agent message as a payload template, prints the payload
    we would POST, and exits 0. No writes.

Live (--send):
    POSTs the message to the Conversations API. Exits 0 and prints the new
    message id on success. Non-zero on failure with an error to stderr.

Exit codes:
    0  success
    2  bad args / empty body
    3  token file missing
    4  HubSpot API error (HTTP non-2xx)
    5  no conversation thread associated with ticket
    6  could not derive a send-payload template from the thread
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = ROOT / "config" / ".secrets" / "hubspot_token.txt"
API_BASE = "https://api.hubapi.com"


def _read_token() -> str:
    if not TOKEN_PATH.exists():
        print(f"token missing at {TOKEN_PATH}", file=sys.stderr)
        sys.exit(3)
    return TOKEN_PATH.read_text(encoding="utf-8").strip()


def _request(method: str, path: str, token: str, body: dict | None = None) -> dict:
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code} on {method} {path}: {err_body}", file=sys.stderr)
        sys.exit(4)


def _list_threads_for_ticket(token: str, ticket_id: str) -> list[dict]:
    qs = urllib.parse.urlencode({"associatedTicketIds": ticket_id, "limit": 10})
    data = _request("GET", f"/conversations/v3/conversations/threads?{qs}", token)
    return data.get("results", [])


def _get_thread(token: str, thread_id: str) -> dict:
    return _request("GET", f"/conversations/v3/conversations/threads/{thread_id}", token)


def _list_messages(token: str, thread_id: str, limit: int = 20) -> list[dict]:
    qs = urllib.parse.urlencode({"limit": limit})
    data = _request(
        "GET",
        f"/conversations/v3/conversations/threads/{thread_id}/messages?{qs}",
        token,
    )
    return data.get("results", [])


def _derive_payload_template(thread: dict, messages: list[dict]) -> dict | None:
    """Derive channel/sender/recipients for an outgoing reply.

    Preferred: copy from the most recent OUTGOING agent message on this thread.
    Fallback (first-reply case): invert the most recent INCOMING customer
    message — its recipients become our senders, its senders become our recipients.
    """
    for msg in reversed(messages):
        if msg.get("direction") == "OUTGOING" and msg.get("type") == "MESSAGE":
            return {
                "source": "prior_outgoing",
                "channelId": msg.get("channelId"),
                "channelAccountId": msg.get("channelAccountId"),
                "senders": msg.get("senders"),
                "recipients": msg.get("recipients"),
                "subject": msg.get("subject"),
            }
    for msg in reversed(messages):
        if msg.get("direction") == "INCOMING" and msg.get("type") == "MESSAGE":
            inbound_senders = msg.get("senders") or []
            inbound_recipients = msg.get("recipients") or []
            if not inbound_senders or not inbound_recipients:
                continue
            subj = msg.get("subject") or ""
            if subj and not subj.lower().startswith("re:"):
                subj = f"Re: {subj}"
            return {
                "source": "inverted_incoming",
                "channelId": msg.get("channelId"),
                "channelAccountId": msg.get("channelAccountId"),
                # agents (original recipients) become our senders
                "senders": inbound_recipients,
                # customer (original sender) becomes our recipient
                "recipients": inbound_senders,
                "subject": subj or None,
            }
    return None


def main() -> int:
    argv = sys.argv[1:]
    send_flag = "--send" in argv
    pos = [a for a in argv if not a.startswith("--")]
    if len(pos) != 1 or not pos[0].strip():
        print("usage: send_hubspot_reply.py <ticket_id> [--send]", file=sys.stderr)
        return 2
    ticket_id = pos[0].strip()

    body_text = sys.stdin.read().strip()
    if not body_text:
        print("empty body on stdin", file=sys.stderr)
        return 2

    token = _read_token()

    threads = _list_threads_for_ticket(token, ticket_id)
    if not threads:
        print(f"no conversation thread associated with ticket {ticket_id}", file=sys.stderr)
        return 5

    # Prefer the most recently active thread.
    threads.sort(key=lambda t: t.get("latestMessageTimestamp") or "", reverse=True)
    thread = threads[0]
    thread_id = thread["id"]

    messages = _list_messages(token, thread_id)
    template = _derive_payload_template(thread, messages)
    if not template:
        print(
            f"could not derive send payload — no prior OUTGOING MESSAGE on thread {thread_id}",
            file=sys.stderr,
        )
        return 6

    payload = {
        "type": "MESSAGE",
        "text": body_text,
        "richText": f"<div>{body_text.replace(chr(10), '<br>')}</div>",
        "channelId": template["channelId"],
        "channelAccountId": template["channelAccountId"],
        "senders": template["senders"],
        "recipients": template["recipients"],
    }
    if template.get("subject"):
        payload["subject"] = template["subject"]

    if not send_flag:
        print(json.dumps({
            "mode": "dry-run",
            "ticket_id": ticket_id,
            "thread_id": thread_id,
            "thread_meta": {
                "status": thread.get("status"),
                "latestMessageTimestamp": thread.get("latestMessageTimestamp"),
                "inboxId": thread.get("inboxId"),
            },
            "template_source": template.get("source"),
            "template_source_message_count": len(messages),
            "payload_preview": payload,
        }, indent=2))
        return 0

    resp = _request(
        "POST",
        f"/conversations/v3/conversations/threads/{thread_id}/messages",
        token,
        body=payload,
    )
    msg_id = resp.get("id", "")
    print(msg_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
