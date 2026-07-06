"""Raw HubSpot REST helpers for the ticket pipeline.

Mirrors what the agent's MCP calls fetch (SKILL.md steps 1 + 2a), using the
same private-app token file and the same cross-process rate-limit posture as
scripts/backfill_complaints.py: attempt the Redis token bucket, then a hard
0.34s sleep floor before EVERY call (the token degrades to passthrough
without REDIS_URL, so the sleep is the real guard — T-20-05-01).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent

HUBSPOT_BASE = "https://api.hubapi.com"
TOKEN_PATH = ROOT / "config" / ".secrets" / "hubspot_token.txt"
RATE_FLOOR_S = 0.34
RETRY_SLEEP_S = 2.0

# Standard properties the agent's Step-1 search always returns, on top of
# settings.ticket_custom_properties. hs_file_upload is deliberately absent
# (attachment invariant — CLAUDE.md #7).
TICKET_BASE_PROPERTIES = [
    "subject",
    "content",
    "hs_pipeline_stage",
    "hs_ticket_priority",
    "hs_lastmodifieddate",
    "createdate",
    "source_type",
    "hs_last_message_from_visitor",
]

EMAIL_PROPERTIES = [
    "hs_email_subject",
    "hs_email_text",
    "hs_email_html",
    "hs_email_direction",
    "hs_email_from_email",
    "hs_email_to_email",
    "hs_createdate",
]

CONTACT_PROPERTIES = ["firstname", "lastname", "email", "phone"]
COMPANY_PROPERTIES = ["name", "domain"]


def _log(msg: str) -> None:
    print(f"[pipeline.hubspot] {msg}", file=sys.stderr)


def read_token() -> Optional[str]:
    try:
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        return token or None
    except OSError:
        return None


def _acquire_rate_token() -> None:
    try:
        from packn_os_hubspot_client.rate_limit import acquire_hubspot_token

        acquire_hubspot_token()
    except Exception:
        pass
    time.sleep(RATE_FLOOR_S)


def _request(
    method: str, path: str, token: str, body: Optional[dict] = None
) -> Optional[dict]:
    """One HubSpot call with rate-limit floor + single retry on 429/5xx.

    Returns parsed JSON, or None on failure (callers treat None as
    "enrichment unavailable" and continue — parity with the agent's
    log-and-continue failure posture)."""
    url = f"{HUBSPOT_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    for attempt in (1, 2):
        _acquire_rate_token()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            _log(f"{method} {path} -> HTTP {e.code} (attempt {attempt})")
            if e.code == 429 or e.code >= 500:
                time.sleep(RETRY_SLEEP_S)
                continue
            return None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            _log(f"{method} {path} transport error (attempt {attempt}): {e!r}")
            time.sleep(RETRY_SLEEP_S)
    return None


def search_tickets(body: dict, token: str) -> Optional[dict]:
    return _request("POST", "/crm/v3/objects/tickets/search", token, body)


def get_associated_ids(
    ticket_id: str, to_object: str, token: str, limit: int = 50
) -> list[str]:
    """GET /crm/v4/objects/tickets/{id}/associations/{toObject} -> object ids."""
    resp = _request(
        "GET",
        f"/crm/v4/objects/tickets/{ticket_id}/associations/{to_object}?limit={limit}",
        token,
    )
    if not resp:
        return []
    return [str(r.get("toObjectId")) for r in resp.get("results", []) if r.get("toObjectId")]


def batch_read(
    object_type: str, ids: list[str], properties: list[str], token: str
) -> list[dict]:
    """POST /crm/v3/objects/{type}/batch/read (input cap 100 — callers here
    never exceed it: emails are capped by conversation_email_limit, contacts/
    companies by association fan-out)."""
    if not ids:
        return []
    resp = _request(
        "POST",
        f"/crm/v3/objects/{object_type}/batch/read",
        token,
        {"properties": properties, "inputs": [{"id": i} for i in ids[:100]]},
    )
    return (resp or {}).get("results", [])
