"""Raw HubSpot REST helpers for the ticket pipeline.

Mirrors what the agent's MCP calls fetch (SKILL.md steps 1 + 2a), using the
same private-app token file and the same cross-process rate-limit posture as
scripts/backfill_complaints.py: attempt the Redis token bucket, then a hard
0.34s sleep floor before EVERY call (the token degrades to passthrough
without REDIS_URL, so the sleep is the real guard — T-20-05-01).

THE ONE CHOKEPOINT, AND ITS COUNTER (Phase 93)
----------------------------------------------
Every HubSpot request made by code goes through `_request` in this module —
the pipeline's, the pre-gate's (scripts/pregate_tickets.py has no request code
of its own) and the `os`-mode steps'. Two numbers are kept per process:

    raw_requests   one per HTTP attempt. A retry is a second request.
    kinds          one per LOGICAL call, under one of CALL_KINDS. One thread
                   fetch is two requests (association list, batch read) and ONE
                   `email_thread` (decision Q7).

`_request` counts raw attempts and nothing else. A kind is recorded once per
logical operation, by the code that knows what the operation is:
`search_tickets` records its own (`search` unless told otherwise); every other
call site calls `record_call(kind)` once before its requests. Read the totals
with `snapshot_calls()`; start a run with `reset_calls()`.

The counter sees requests made by CODE. The agent's HubSpot calls go through
MCP and are invisible here, which is why a run record carries counts for
`os`-mode runs only.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

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

# The kinds of logical HubSpot call, in the order the Pack'N OS card lists them.
CALL_KINDS = (
    "search",
    "ticket_read",
    "contact_read",
    "company_read",
    "related_lookup",
    "rerun_read",
    "email_thread",
    "number_backfill",
)

# Ticket properties code never writes (CLAUDE.md invariant 5: the automation does
# not change a ticket's stage, owner or priority).
PROTECTED_TICKET_PROPERTIES = (
    "hs_pipeline",
    "hs_pipeline_stage",
    "hubspot_owner_id",
    "hs_ticket_priority",
)

# Per-process totals. Read with snapshot_calls(), zero with reset_calls().
_CALLS: dict[str, Any] = {"kinds": {kind: 0 for kind in CALL_KINDS}, "raw_requests": 0}


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


def record_call(kind: str) -> None:
    """Count one LOGICAL HubSpot call of `kind` (one of CALL_KINDS).

    Call it once per operation, at the call site, before the operation's
    requests — never once per request: a thread fetch is one `email_thread`.
    An unknown kind is a ValueError, so a typo cannot open a ninth bucket.
    """
    if kind not in CALL_KINDS:
        raise ValueError(f"not a HubSpot call kind: {kind!r}")
    _CALLS["kinds"][kind] += 1


def snapshot_calls() -> dict:
    """The totals so far, as a copy: {"kinds": {<the eight>: n}, "raw_requests": n}."""
    return {"kinds": dict(_CALLS["kinds"]), "raw_requests": _CALLS["raw_requests"]}


def reset_calls() -> None:
    """Zero every kind and the raw total (the start of a run)."""
    for kind in CALL_KINDS:
        _CALLS["kinds"][kind] = 0
    _CALLS["raw_requests"] = 0


def _default_opener(request: urllib.request.Request, timeout: Optional[float] = None):
    """The real transport. Resolved at call time, so a test can replace it."""
    return urllib.request.urlopen(request, timeout=timeout)


def _request(
    method: str,
    path: str,
    token: str,
    body: Optional[dict] = None,
    *,
    opener: Optional[Callable[..., Any]] = None,
) -> Optional[dict]:
    """One HubSpot call with rate-limit floor + single retry on 429/5xx.

    Returns parsed JSON, or None on failure (callers treat None as
    "enrichment unavailable" and continue — parity with the agent's
    log-and-continue failure posture).

    Each HTTP attempt adds one to the raw request total; the retry is a second
    request. No kind is recorded here — see the module docstring.

    `opener` is the test seam: called as `opener(request, timeout=30)`, it
    answers a context manager with `.read()`. Production code passes none and
    gets urllib's urlopen."""
    send = _default_opener if opener is None else opener
    url = f"{HUBSPOT_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    for attempt in (1, 2):
        _acquire_rate_token()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        _CALLS["raw_requests"] += 1
        try:
            with send(req, timeout=30) as resp:
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


def search_tickets(
    body: dict,
    token: str,
    kind: str = "search",
    *,
    transport: Optional[Callable[..., Any]] = None,
) -> Optional[dict]:
    """POST /crm/v3/objects/tickets/search, recorded as ONE call of `kind`.

    `kind` defaults to `search`, so the pre-gate's and the pipeline's searches
    are counted with no change at their call sites; a search that is really
    something else (a related-ticket lookup) passes its own kind.

    `transport` replaces the request function for this one call:
    `transport(method, path, token, body)` -> the parsed answer or None."""
    record_call(kind)
    request = _request if transport is None else transport
    return request("POST", "/crm/v3/objects/tickets/search", token, body)


def update_ticket_properties(ticket_id: str, properties: dict, token: str) -> Optional[dict]:
    """One PATCH on one ticket: `properties` is the name -> value map to write.

    Returns the parsed answer, or None when the write failed. Records no kind:
    the caller records the logical call (the number backfill is
    `number_backfill`).

    Refused with ValueError before any request: a ticket id that is not a
    HubSpot id (digits — the id goes into the URL path), an empty or non-dict
    `properties`, and any of PROTECTED_TICKET_PROPERTIES."""
    ticket_ref = str(ticket_id) if ticket_id is not None else ""
    if not (ticket_ref.isascii() and ticket_ref.isdigit()):
        raise ValueError("update_ticket_properties needs a HubSpot ticket id (digits)")
    if not isinstance(properties, dict) or not properties:
        raise ValueError("update_ticket_properties needs a non-empty properties object")
    protected = sorted(name for name in properties if name in PROTECTED_TICKET_PROPERTIES)
    if protected:
        raise ValueError(f"code never writes these ticket properties: {protected}")
    return _request(
        "PATCH", f"/crm/v3/objects/tickets/{ticket_ref}", token, {"properties": properties}
    )


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
