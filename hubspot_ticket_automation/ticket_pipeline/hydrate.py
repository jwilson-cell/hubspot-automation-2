"""Deterministic ticket-context hydration — SKILL.md steps 2a / 2a.4 / 2a.5
ported out of the agent.

Pure helpers (html_to_text, parse_form_fields, build_thread) carry the unit
tests; the `hydrate_ticket` orchestrator does the HubSpot/SSK I/O and is
exercised by the capped live shadow run.
"""
from __future__ import annotations

import html as html_lib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import hubspot_api as hs

ROOT = Path(__file__).resolve().parent.parent
SSK_HELPER = ROOT / "scripts" / "ssk_order_lookup.py"
VENV_PY = ROOT / ".venv" / "bin" / "python"


def _log(msg: str) -> None:
    print(f"[pipeline.hydrate] {msg}", file=sys.stderr)


# ===========================================================================
# PURE LAYER (unit-tested)
# ===========================================================================

_TAG_RE = re.compile(r"<(?:br|/p|/div|/tr|/li)[^>]*>", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")


def html_to_text(html: Optional[str]) -> str:
    """Strip HTML to readable plain text: block-level closers become
    newlines, entities are unescaped, whitespace is collapsed per line."""
    if not html:
        return ""
    text = _TAG_RE.sub("\n", html)
    text = _ANY_TAG_RE.sub(" ", text)
    text = html_lib.unescape(text).replace("\xa0", " ")  # &nbsp; -> plain space
    lines = [_WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


# "Label: value" lines in the form-submission blockquote. Labels are the
# form's display names ("Order Number", "Topic of Ticket", ...); keys are
# normalized to snake_case to match ticket_context.form_fields.* naming.
_FIELD_LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 /()'&-]{2,60}?)\s*:\s*(.+)$")


def _snake(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")


def parse_form_fields(first_email_html: Optional[str]) -> dict:
    """Parse the form-submission blockquote (SKILL 2a: 'its body is an HTML
    blockquote containing the form field values') into a {snake_label: value}
    dict. Unparseable lines are simply skipped — the ticket's native
    properties are merged on top by the caller, so this is best-effort."""
    fields: dict = {}
    for line in html_to_text(first_email_html).splitlines():
        m = _FIELD_LINE_RE.match(line.strip())
        if not m:
            continue
        key = _snake(m.group(1))
        value = m.group(2).strip()
        if key and value and key not in fields:
            fields[key] = value
    return fields


def build_thread(emails: list[dict]) -> list[dict]:
    """Associated `emails` objects -> ticket_context.thread[] entries,
    oldest-first. Prefers hs_email_text; falls back to stripped HTML."""
    entries = []
    for e in emails:
        props = e.get("properties") or {}
        body = (props.get("hs_email_text") or "").strip() or html_to_text(
            props.get("hs_email_html")
        )
        direction = (props.get("hs_email_direction") or "").upper()
        entries.append(
            {
                "from": "customer" if direction in ("INCOMING_EMAIL", "INBOUND", "") else "agent",
                "email": props.get("hs_email_from_email"),
                "date": props.get("hs_createdate"),
                "body_text": body[:4000],
            }
        )
    entries.sort(key=lambda x: x.get("date") or "")
    return entries


def latest_customer_message(thread: list[dict]) -> str:
    for entry in reversed(thread):
        if entry.get("from") == "customer" and entry.get("body_text"):
            return entry["body_text"]
    return thread[-1]["body_text"] if thread else ""


def resolve_merchant(company: dict, form_fields: dict) -> str:
    """Resolve the BRAND/merchant the ticket belongs to, mirroring SKILL 2h's
    company_name rules (brand, never gopackn). Returns "" when unknown —
    callers must NOT guess between brands. Used to org-scope the SSK lookup
    (cross-merchant order-number collision guard, 2026-07-22)."""
    name = ((company or {}).get("name") or "").strip()
    domain = ((company or {}).get("domain") or "").strip()
    if name and "gopackn" not in name.lower() and "gopackn" not in domain.lower():
        return name
    for key in ("company_name", "company", "brand"):
        v = ((form_fields or {}).get(key) or "").strip()
        if v and "gopackn" not in v.lower():
            return v
    return ""


def merchant_hints(contact: dict) -> list[str]:
    """Weak merchant signals for the SSK helper: currently the contact's email
    domain (e.g. va@wkr.gg -> wkr.gg). The helper only uses a hint when it
    matches a configured merchant; gopackn/CX domains are never hints."""
    email = ((contact or {}).get("email") or "").strip()
    if "@" not in email:
        return []
    dom = email.rsplit("@", 1)[-1].lower()
    if not dom or "gopackn" in dom:
        return []
    return [dom]


_ATTACH_HINT_RE = re.compile(
    r"\battach(?:ed|ment|ments)?\b|\bscreenshot\b|\bphoto(?:s)?\b|\bimage(?:s)? (?:below|attached)\b",
    re.IGNORECASE,
)


def detect_attachments(*texts: Optional[str]) -> bool:
    """Presence detection ONLY (attachment invariant): body mentions, never
    file fetches."""
    return any(t and _ATTACH_HINT_RE.search(t) for t in texts)


# ===========================================================================
# I/O LAYER
# ===========================================================================


def _ssk_lookup(
    order_number: Optional[str],
    tracking_number: Optional[str],
    merchant: str = "",
    hints: Optional[list[str]] = None,
) -> Optional[dict]:
    """scripts/ssk_order_lookup.py subprocess — same helper, same contract as
    the agent's step 2a.5. Returns the parsed ssk_state dict, or None when
    the lookup could not run (exit 3/4/2 — the drafter falls back to hedged
    language, exactly like the agent). Exit 3 includes the merchant-scoping
    refusal: known merchant with no configured org key never queries under
    another merchant's key."""
    if not (order_number or tracking_number):
        return None
    payload = json.dumps(
        {
            "order_number": order_number or "",
            "tracking_number": tracking_number or "",
            "merchant": merchant or "",
            "merchant_hints": hints or [],
        }
    )
    python = str(VENV_PY) if VENV_PY.exists() else sys.executable
    try:
        proc = subprocess.run(
            [python, str(SSK_HELPER)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=45,
            cwd=str(ROOT),
        )
    except (subprocess.SubprocessError, OSError) as e:
        _log(f"ssk lookup failed to run: {e!r}")
        return None
    if proc.returncode != 0:
        _log(f"ssk lookup exit {proc.returncode}")
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        _log("ssk lookup returned unparseable stdout")
        return None


def _related_tickets(
    ticket_id: str,
    order_number: str,
    settings: dict,
    token: str,
    portal_id: int,
) -> list[dict]:
    """SKILL 2a.4 cross-ticket continuation lookup (order_number EQ)."""
    cfg = ((settings.get("cross_ticket_lookup") or {}).get("by_order_number")) or {}
    if not cfg.get("enabled") or not (order_number or "").strip():
        return []
    lookback_days = int(cfg.get("lookback_days", 30))
    import time as _time

    since_ms = int(_time.time() * 1000) - lookback_days * 86_400_000
    filters = [
        {"propertyName": "order_number", "operator": "EQ", "value": order_number},
        {"propertyName": "hs_lastmodifieddate", "operator": "GT", "value": since_ms},
        {"propertyName": "hs_object_id", "operator": "NEQ", "value": ticket_id},
    ]
    if not cfg.get("include_closed_stages", True):
        filters.append(
            {
                "propertyName": "hs_pipeline_stage",
                "operator": "IN",
                "values": [str(s) for s in settings.get("active_stages") or []],
            }
        )
    resp = hs.search_tickets(
        {
            "filterGroups": [{"filters": filters}],
            "properties": [
                "subject",
                "hs_pipeline_stage",
                "createdate",
                "hs_lastmodifieddate",
                "topic_of_ticket",
            ],
            "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}],
            "limit": int(cfg.get("max_results", 3)),
        },
        token,
    )
    related = []
    for r in (resp or {}).get("results", []):
        rid = str(r.get("id"))
        props = r.get("properties") or {}
        snippet = None
        email_ids = hs.get_associated_ids(rid, "emails", token, limit=1)
        if email_ids:
            emails = hs.batch_read("emails", email_ids, hs.EMAIL_PROPERTIES, token)
            if emails:
                ep = emails[0].get("properties") or {}
                body = (ep.get("hs_email_text") or "").strip() or html_to_text(
                    ep.get("hs_email_html")
                )
                snippet = body[:200] or None
        related.append(
            {
                "ticket_id": rid,
                "ticket_link": f"https://app.hubspot.com/contacts/{portal_id}/ticket/{rid}",
                "subject": props.get("subject"),
                "stage": props.get("hs_pipeline_stage"),
                "createdate": props.get("createdate"),
                "hs_lastmodifieddate": props.get("hs_lastmodifieddate"),
                "topic_of_ticket": props.get("topic_of_ticket"),
                "snippet": snippet,
            }
        )
    return related


def hydrate_ticket(ticket: dict, settings: dict, token: str) -> dict:
    """Build the ticket_context object (SKILL 2a shape) for one search hit."""
    ticket_id = str(ticket.get("id"))
    props = ticket.get("properties") or {}
    portal_id = int(settings.get("hubspot_portal_id", 0))
    email_limit = int(settings.get("conversation_email_limit", 10))

    email_ids = hs.get_associated_ids(ticket_id, "emails", token, limit=email_limit)
    emails = hs.batch_read("emails", email_ids, hs.EMAIL_PROPERTIES, token)
    emails.sort(key=lambda e: (e.get("properties") or {}).get("hs_createdate") or "")
    thread = build_thread(emails)

    # form_fields: blockquote parse of the FIRST email, then native ticket
    # properties merged ON TOP (properties win — they're structured; the
    # blockquote fills the unmapped gaps, per memory
    # packn-hubspot-form-ticket-data-model / SKILL 2a.6 rationale).
    first_html = (emails[0].get("properties") or {}).get("hs_email_html") if emails else None
    form_fields = parse_form_fields(first_html)
    for prop_key in settings.get("ticket_custom_properties") or []:
        value = props.get(prop_key)
        if value not in (None, ""):
            form_fields[prop_key] = value

    contact = {}
    contact_ids = hs.get_associated_ids(ticket_id, "contacts", token, limit=1)
    if contact_ids:
        rows = hs.batch_read("contacts", contact_ids, hs.CONTACT_PROPERTIES, token)
        if rows:
            cp = rows[0].get("properties") or {}
            name = " ".join(x for x in [cp.get("firstname"), cp.get("lastname")] if x)
            contact = {"name": name or None, "email": cp.get("email"), "phone": cp.get("phone")}
    if not contact.get("email") and props.get("hs_all_associated_contact_emails"):
        contact["email"] = props["hs_all_associated_contact_emails"].split(";")[0].strip()

    company = {}
    company_ids = hs.get_associated_ids(ticket_id, "companies", token, limit=1)
    if company_ids:
        rows = hs.batch_read("companies", company_ids, hs.COMPANY_PROPERTIES, token)
        if rows:
            kp = rows[0].get("properties") or {}
            company = {"name": kp.get("name"), "domain": kp.get("domain")}

    latest_msg = latest_customer_message(thread) or html_to_text(props.get("content")) or (
        form_fields.get("inquiry_description") or ""
    )

    ctx = {
        "ticket_id": ticket_id,
        "ticket_link": f"https://app.hubspot.com/contacts/{portal_id}/ticket/{ticket_id}",
        "subject": props.get("subject"),
        "topic_of_ticket": props.get("topic_of_ticket") or form_fields.get("topic_of_ticket"),
        "source_type": props.get("source_type"),
        "form_fields": form_fields,
        "has_attachments": detect_attachments(latest_msg, props.get("content")),
        "stage": props.get("hs_pipeline_stage"),
        "priority_from_hubspot": props.get("hs_ticket_priority"),
        "createdate": props.get("createdate"),
        "hs_lastmodifieddate": props.get("hs_lastmodifieddate"),
        "contact": contact,
        "company": company,
        "thread": thread,
        "latest_customer_message": latest_msg,
        "related_tickets": [],
    }

    try:
        ctx["related_tickets"] = _related_tickets(
            ticket_id, form_fields.get("order_number") or "", settings, token, portal_id
        )
    except Exception as e:
        _log(f"related-tickets enrichment failed for {ticket_id}: {e!r}")

    # SSK hydration (SKILL 2a.5 'simplest implementation': any form ticket
    # with an order_number OR tracking_number).
    if (settings.get("shipsidekick") or {}).get("enabled"):
        try:
            ssk = _ssk_lookup(
                form_fields.get("order_number"),
                form_fields.get("tracking_number"),
                merchant=resolve_merchant(company, form_fields),
                hints=merchant_hints(contact),
            )
            if ssk is not None:
                ctx["ssk_state"] = ssk
        except Exception as e:
            _log(f"ssk hydration failed for {ticket_id}: {e!r}")

    return ctx
