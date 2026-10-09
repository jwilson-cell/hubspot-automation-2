"""An OS ticket payload, turned into the ticket context the classifier knows.

Phase 93, plan 22. In `os` mode (and for the OS half of the source comparison)
the cron reads its tickets from Pack'N OS, not from HubSpot. This module is the
one place that reshapes an OS payload (the 24 keys pinned by
tests/fixtures/tickets-api-list.v1.json) into the `ticket_context` that
`hydrate.hydrate_ticket` builds from HubSpot, so the same classifier and drafter
see the same shape from either source.

THE MAPPING (asserted by value in tests/test_os_bundle.py)
----------------------------------------------------------
    ticket_id                the HubSpot id when the ticket has one, else the OS
                             id: the same value as `draft_key`
    ticket_link              HubSpot-linked: the HubSpot ticket address built
                             from the settings' portal id. No HubSpot id:
                             <os origin>/tickets/<id>
    subject, source_type     the payload's own
    topic_of_ticket          the payload's, else the form block's
    stage                    hs_pipeline_stage (None with no HubSpot id)
    priority_from_hubspot    None. The OS does not hold HubSpot's value, and its
                             own `priority` is a different field
    createdate               created_at (when the ticket was opened)
    hs_lastmodifieddate      hubspot_updated_at
    has_attachments          the payload's boolean. Nothing is opened or read
    form_fields              the first email's parsed form block; the payload's
                             form_answers over it; then order_number,
                             tracking_number, topic_of_ticket and company_name
                             from the payload when not blank. Structured wins
    contact                  requester null -> {}. Else name (ONE string, used
                             as given), email, and phone None
    company                  company_name null -> {}. Else name, and domain None
    thread                   fetched HubSpot emails -> hydration's thread
                             builder. Otherwise the payload's thread: `opening`
                             and `client_reply` -> customer (the requester's
                             email), `outbound` -> agent (email None), date =
                             the item's `at`, body cut at BODY_TEXT_CAP
    latest_customer_message  hydration's picker over that thread; else the
                             payload's body; else the form block's inquiry
                             description
    related_tickets          each payload `related` item: ticket_id = its id,
                             ticket_link = <os origin>/tickets/<id>, stage = its
                             OS state, createdate = its created_at,
                             hs_lastmodifieddate None, topic_of_ticket = its
                             category, subject and snippet its own
    ssk_state                hydration's own ShipSidekick lookup, when the
                             settings enable it and the form fields hold an
                             order or tracking number. Not a HubSpot call

Not in the context (the HubSpot-built one has no key for them; the caller
carries them beside it on the bundle entry): the OS id, state, priority,
channel, category, signal, merchant and thread_truncated.

HUBSPOT CALLS
-------------
`fetch_thread` is the one HubSpot read this module makes: the association list
and the batch read, recorded as ONE `email_thread` call. `build_context` makes
none. A ticket with no HubSpot id costs nothing.

Owner-visible decision (runbook tag THREAD-FALLBACK): a HubSpot-linked ticket
whose thread fetch returns nothing is still given a context, built from the OS
thread. `thread_source` says so (`os_fallback`) and the caller records it.

LOGGING
-------
Ids, codes and counts only. Never a subject, a body, a name or an address, and
never the text of an exception: a failed lookup is logged by its class name.

THE CATEGORY LIST
-----------------
`config/categories.yaml` keeps the fifteen categories as top-level keys, beside
four top-level keys that are not categories. `category_keys` picks the
categories by an exclude-list on purpose: it needs no knowledge of the list it
is compared with, so tests/test_contract_fixtures.py comparing it with the
contract's slugs is a real check. A fifth non-category key added to the file
comes back from `category_keys` and turns that test red until it is added to
NON_CATEGORY_KEYS deliberately.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

from . import hubspot_api as hs
from . import hydrate

# The top-level keys of config/categories.yaml that are not categories.
NON_CATEGORY_KEYS = (
    "form_topic_mapping",
    "auto_send_form_topics",
    "category_owners",
    "suppress_reply_reminder_categories",
)

# Where a context's thread came from (`thread_source`).
THREAD_HUBSPOT = "hubspot"
THREAD_OS = "os"
THREAD_OS_FALLBACK = "os_fallback"

# OS thread kinds, by who wrote the message. A kind in neither tuple is left out.
CUSTOMER_KINDS = ("opening", "client_reply")
AGENT_KINDS = ("outbound",)

# The cut `hydrate.build_thread` applies to a message body (asserted equal in a test).
BODY_TEXT_CAP = 4000

# Payload keys set over the form fields when they hold a value.
PAYLOAD_OVER_FORM_KEYS = ("order_number", "tracking_number", "topic_of_ticket", "company_name")

# The two numbers the later backfill may write to HubSpot.
BACKFILL_KEYS = ("order_number", "tracking_number")

RERUN_CYCLE_PREFIX = "rerun"
BUNDLE_MODE = "os"
DEFAULT_EMAIL_LIMIT = 10
MAX_EMAIL_LIMIT = 100  # the batch read's input cap


def _log(msg: str) -> None:
    print(f"[pipeline.os_bundle] {msg}", file=sys.stderr)


# ===========================================================================
# The category list
# ===========================================================================


def category_keys(categories_cfg: dict) -> list[str]:
    """The category names of a loaded config/categories.yaml, in file order:
    every top-level key that is not in NON_CATEGORY_KEYS.

    A config that is not a mapping is a TypeError: an unreadable file must not
    read as "no categories"."""
    if not isinstance(categories_cfg, dict):
        raise TypeError("category_keys needs the loaded categories.yaml mapping")
    return [key for key in categories_cfg if key not in NON_CATEGORY_KEYS]


# ===========================================================================
# Small readers
# ===========================================================================


def _text(value: Any) -> Optional[str]:
    """A string that is not blank, exactly as given; anything else is None."""
    if isinstance(value, str) and value.strip():
        return value
    return None


def _present(value: Any) -> bool:
    """False for None and for a blank string: a value that must not win a merge."""
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _payload(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise TypeError("an OS ticket payload is an object")
    if _text(payload.get("id")) is None:
        raise ValueError("an OS ticket payload carries its id")
    return payload


def _origin(os_origin: Any) -> str:
    origin = os_origin.strip().rstrip("/") if isinstance(os_origin, str) else ""
    if not origin.startswith(("http://", "https://")):
        raise ValueError("the OS origin is an http or https origin")
    return origin


def _os_ticket_link(origin: str, ticket_id: str) -> str:
    return f"{origin}/tickets/{quote(ticket_id, safe='')}"


def _email_date(email: dict) -> str:
    return (email.get("properties") or {}).get("hs_createdate") or ""


def _emails(thread: Any) -> list[dict]:
    """The fetched emails, oldest first (hydration's own sort key); [] for none."""
    if not isinstance(thread, list):
        return []
    return sorted((email for email in thread if isinstance(email, dict)), key=_email_date)


def _email_limit(settings: Any) -> int:
    configured = (settings or {}).get("conversation_email_limit", DEFAULT_EMAIL_LIMIT)
    return max(1, min(MAX_EMAIL_LIMIT, int(configured)))


# ===========================================================================
# The two keys
# ===========================================================================


def draft_key(payload: dict) -> str:
    """The text key of the ticket's draft, and the bundle entry's `ref`: the
    HubSpot id when the ticket has one, else the OS ticket id."""
    payload = _payload(payload)
    return _text(payload.get("hubspot_ticket_id")) or payload["id"]


def cycle_key(payload: dict, rerun_request_id: Any = None) -> str:
    """The key of one drafting cycle: the consumed client signal's id, or
    `rerun:<request id>` for an operator rerun.

    A pass with neither has no cycle and is a ValueError: a draft written under
    an invented key could never be matched by a replay."""
    if rerun_request_id is not None:
        request_id = str(rerun_request_id)
        if not request_id.strip():
            raise ValueError("a rerun cycle key needs the rerun request id")
        return f"{RERUN_CYCLE_PREFIX}:{request_id}"
    signal = _payload(payload).get("signal")
    signal_id = _text(signal.get("id")) if isinstance(signal, dict) else None
    if signal_id is None:
        raise ValueError("no client signal and no rerun request: this pass has no cycle key")
    return signal_id


# ===========================================================================
# The thread
# ===========================================================================


def fetch_thread(hubspot_ticket_id: Any, settings: dict, token: Optional[str]) -> list[dict]:
    """The ticket's HubSpot emails, oldest first: the one HubSpot read `os` mode keeps.

    At most two requests (the association list, then the batch read of the
    email properties), recorded as ONE `email_thread` call, capped by the
    settings' `conversation_email_limit`.

    Returns [] and makes no request, and records no call, when the ticket has no
    HubSpot id, when the id is not digits (it goes into a URL path) or when
    there is no token. Returns [] when HubSpot refuses or holds no email. In
    every one of those cases the caller builds the context from the OS thread
    (THREAD-FALLBACK)."""
    ticket_ref = _text(hubspot_ticket_id)
    if ticket_ref is None:
        return []
    if not (ticket_ref.isascii() and ticket_ref.isdigit()):
        _log("thread fetch skipped: code=hubspot_id_not_numeric")
        return []
    if not token:
        _log(f"thread fetch skipped: code=no_hubspot_token ticket={ticket_ref}")
        return []
    limit = _email_limit(settings)
    hs.record_call("email_thread")
    email_ids = hs.get_associated_ids(ticket_ref, "emails", token, limit=limit)
    emails = _emails(hs.batch_read("emails", email_ids[:limit], hs.EMAIL_PROPERTIES, token))
    _log(f"thread fetched: ticket={ticket_ref} emails={len(emails)}")
    return emails


def os_thread(payload: dict) -> list[dict]:
    """The payload's thread in the per-message shape `hydrate.build_thread` makes.

    The payload's order is kept (the OS sends oldest first). An item whose kind
    the contract does not name is left out, so nothing that is not a client or
    an agent message can reach a prompt as one."""
    payload = _payload(payload)
    requester = payload.get("requester")
    requester_email = requester.get("email") if isinstance(requester, dict) else None
    items = payload.get("thread")
    entries: list[dict] = []
    left_out = 0
    for item in items if isinstance(items, list) else []:
        kind = item.get("kind") if isinstance(item, dict) else None
        if kind in CUSTOMER_KINDS:
            sender, email = "customer", requester_email
        elif kind in AGENT_KINDS:
            sender, email = "agent", None
        else:
            left_out += 1
            continue
        body = item.get("body")
        entries.append(
            {
                "from": sender,
                "email": email,
                "date": item.get("at"),
                "body_text": (body if isinstance(body, str) else "")[:BODY_TEXT_CAP],
            }
        )
    if left_out:
        _log(f"thread items of an unknown kind left out: ticket={payload['id']} count={left_out}")
    return entries


def thread_source(payload: dict, thread: Any = None) -> str:
    """Where `build_context(payload, thread, ...)` takes its thread from:
    `hubspot` (fetched emails), `os` (no HubSpot id) or `os_fallback` (a
    HubSpot-linked ticket with no fetched email)."""
    if _emails(thread):
        return THREAD_HUBSPOT
    if _text(_payload(payload).get("hubspot_ticket_id")):
        return THREAD_OS_FALLBACK
    return THREAD_OS


def first_email_html(payload: dict, thread: Any = None) -> Optional[str]:
    """The text the form block is parsed from: the oldest fetched email's HTML
    (as hydration reads it), else the payload's opening body."""
    emails = _emails(thread)
    if emails:
        return (emails[0].get("properties") or {}).get("hs_email_html")
    body = _payload(payload).get("body")
    return body if isinstance(body, str) else None


# ===========================================================================
# The number backfill's source
# ===========================================================================


def backfill_source(payload: dict, first_email_html: Optional[str]) -> dict:
    """The two origins of the order and tracking numbers, NOT merged:

        {"payload":    {"order_number": str | None, "tracking_number": str | None},
         "form_block": {"order_number": str | None, "tracking_number": str | None}}

    The later backfill writes a number to HubSpot only when it was empty in the
    payload and present in the form block. The context's merged form fields
    cannot say which it was, so the pair is handed over as it is. A blank string
    is None."""
    payload = _payload(payload)
    block = hydrate.parse_form_fields(first_email_html if isinstance(first_email_html, str) else None)
    return {
        "payload": {key: _text(payload.get(key)) for key in BACKFILL_KEYS},
        "form_block": {key: _text(block.get(key)) for key in BACKFILL_KEYS},
    }


# ===========================================================================
# The context
# ===========================================================================


def _form_fields(payload: dict, emails: list[dict]) -> dict:
    fields = hydrate.parse_form_fields(first_email_html(payload, emails))
    answers = payload.get("form_answers")
    if isinstance(answers, dict):
        for key, value in answers.items():
            if _present(value):
                fields[key] = value
    for key in PAYLOAD_OVER_FORM_KEYS:
        value = payload.get(key)
        if _present(value):
            fields[key] = value
    return fields


def _contact(payload: dict) -> dict:
    requester = payload.get("requester")
    if not isinstance(requester, dict):
        return {}
    return {"name": requester.get("name"), "email": requester.get("email"), "phone": None}


def _company(payload: dict) -> dict:
    name = _text(payload.get("company_name"))
    return {"name": name, "domain": None} if name is not None else {}


def _related_tickets(payload: dict, origin: str) -> list[dict]:
    items = payload.get("related")
    related = []
    for item in items if isinstance(items, list) else []:
        related_id = _text(item.get("id")) if isinstance(item, dict) else None
        if related_id is None:
            continue
        related.append(
            {
                "ticket_id": related_id,
                "ticket_link": _os_ticket_link(origin, related_id),
                "subject": item.get("subject"),
                "stage": item.get("state"),
                "createdate": item.get("created_at"),
                "hs_lastmodifieddate": None,
                "topic_of_ticket": item.get("category"),
                "snippet": item.get("snippet"),
            }
        )
    return related


def build_context(payload: dict, thread: Any, settings: dict, os_origin: str) -> dict:
    """The `ticket_context` for one OS payload: the keys, key order and nesting
    of `hydrate.hydrate_ticket`, each value by its row of the mapping table in
    the module docstring.

    `thread` is what `fetch_thread` returned for the ticket (HubSpot email
    objects), or None / [] to build the thread from the payload. `os_origin` is
    the OS's scheme and host (`tickets_api.read_os_origin()`).

    Makes no HubSpot request and opens no file or address."""
    payload = _payload(payload)
    settings = settings if isinstance(settings, dict) else {}
    origin = _origin(os_origin)
    emails = _emails(thread)

    hubspot_id = _text(payload.get("hubspot_ticket_id"))
    if hubspot_id is not None:
        portal_id = int(settings.get("hubspot_portal_id", 0))
        ticket_id = hubspot_id
        ticket_link = f"https://app.hubspot.com/contacts/{portal_id}/ticket/{hubspot_id}"
    else:
        ticket_id = payload["id"]
        ticket_link = _os_ticket_link(origin, ticket_id)

    built_thread = hydrate.build_thread(emails) if emails else os_thread(payload)
    form_fields = _form_fields(payload, emails)
    contact = _contact(payload)
    company = _company(payload)

    latest_msg = (
        hydrate.latest_customer_message(built_thread)
        or _text(payload.get("body"))
        or form_fields.get("inquiry_description")
        or ""
    )
    if not isinstance(latest_msg, str):
        latest_msg = str(latest_msg)

    ctx = {
        "ticket_id": ticket_id,
        "ticket_link": ticket_link,
        "subject": payload.get("subject"),
        "topic_of_ticket": payload.get("topic_of_ticket") or form_fields.get("topic_of_ticket"),
        "source_type": payload.get("source_type"),
        "form_fields": form_fields,
        "has_attachments": payload.get("has_attachments") is True,
        "stage": payload.get("hs_pipeline_stage"),
        "priority_from_hubspot": None,
        "createdate": payload.get("created_at"),
        "hs_lastmodifieddate": payload.get("hubspot_updated_at"),
        "contact": contact,
        "company": company,
        "thread": built_thread,
        "latest_customer_message": latest_msg,
        "related_tickets": _related_tickets(payload, origin),
    }

    ssk_settings = settings.get("shipsidekick")
    if isinstance(ssk_settings, dict) and ssk_settings.get("enabled"):
        order_number = form_fields.get("order_number")
        tracking_number = form_fields.get("tracking_number")
        if order_number or tracking_number:
            try:
                ssk = hydrate._ssk_lookup(
                    order_number,
                    tracking_number,
                    merchant=hydrate.resolve_merchant(company, form_fields),
                )
                if ssk is not None:
                    ctx["ssk_state"] = ssk
            except Exception as e:
                _log(f"ssk lookup failed: ticket={ticket_id} error={type(e).__name__}")

    return ctx


# ===========================================================================
# The bundle file
# ===========================================================================


def _now_iso() -> str:
    now = datetime.now(timezone.utc)
    return f"{now:%Y-%m-%dT%H:%M:%S}.{now.microsecond // 1000:03d}Z"


def write_bundle(
    path: Any,
    run_id: str,
    entries: list,
    *,
    mode: str = BUNDLE_MODE,
    generated_at: Optional[str] = None,
) -> Path:
    """Write the run's bundle to `path` and return the path:

        {"run_id": ..., "mode": "os", "generated_at": <ISO, ms, Z>, "tickets": entries}

    UTF-8 JSON with non-ASCII text kept as it is. The directory is created. The
    bundle is serialised before anything is created, written beside the target
    and moved over it, so a reader never sees half a file. It holds ticket text:
    the file is made owner-only where the platform allows."""
    if not isinstance(entries, list):
        raise TypeError("a bundle's entries are a list")
    bundle = {
        "run_id": run_id,
        "mode": mode,
        "generated_at": generated_at or _now_iso(),
        "tickets": entries,
    }
    # A lone surrogate half cannot be written as UTF-8: it becomes "?" rather
    # than failing the whole run on one ticket's text.
    data = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8", errors="replace")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    partial.write_bytes(data)
    try:
        os.chmod(partial, 0o600)
    except OSError:
        pass
    os.replace(partial, target)
    return target
