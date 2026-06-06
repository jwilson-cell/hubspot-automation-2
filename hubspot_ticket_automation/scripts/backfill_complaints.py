#!/usr/bin/env python3
"""Phase 20 Plan 20-05 — pure-helper layer (Task 1).

The HTTP-paging + hydration + watermark + dry-run assembly lands in Task 2; this
file currently holds ONLY the pure, stdlib-only, unit-testable core:

  - build_search_body         exact HubSpot tickets/search request body
  - extract_tracking_from_blockquote   form-vs-property tracking fallback parse
  - chunk                     ≤100 input-id chunking for batch reads
  - load_watermark / save_watermark    resumable createdate watermark (corrupt → 0)
  - brand_from_batches        ticket→company association → company name (ER-3)

GATE (probe-1, machine-enforced upstream): Plan 20-02's SUMMARY records
`probe-1 … CONFIRMED` (HTTP 200, total=35) — `topic_of_ticket` EQ
"Mispack (Provide Order Number)" IS searchable, so the search-based backfill
source below is VALID. If that verdict were CONTRADICTED the backfill would have
to be re-planned (CSV-only + email-content search fallback); it is not.

Stdlib only (sibling CLAUDE.md invariant: no new Python deps).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator, Optional

# The exact mispack topic value probe-1 confirmed is searchable (total=35).
MISPACK_TOPIC = "Mispack (Provide Order Number)"

# Properties hydrated per ticket from the search response.
SEARCH_PROPERTIES = [
    "createdate",
    "subject",
    "topic_of_ticket",
    "tracking_number",
    "order_number",
]


# ---------------------------------------------------------------------------
# build_search_body — exact HubSpot /crm/v3/objects/tickets/search body
# ---------------------------------------------------------------------------


def build_search_body(watermark_ms: int = 0, after: Optional[str] = None) -> dict:
    """Build the tickets/search request body.

    filterGroups: topic_of_ticket EQ MISPACK_TOPIC AND createdate GT watermark_ms.
    No pipeline-stage filter — closed tickets are exactly what a backfill wants.
    Sorted createdate ASCENDING (so the watermark advances monotonically), 200/page.
    The "after" key is OMITTED on the first page (after=None) and PRESENT otherwise.
    """
    body: dict = {
        "filterGroups": [
            {
                "filters": [
                    {
                        "propertyName": "topic_of_ticket",
                        "operator": "EQ",
                        "value": MISPACK_TOPIC,
                    },
                    {
                        "propertyName": "createdate",
                        "operator": "GT",
                        "value": watermark_ms,
                    },
                ]
            }
        ],
        "properties": list(SEARCH_PROPERTIES),
        "sorts": [{"propertyName": "createdate", "direction": "ASCENDING"}],
        "limit": 200,
    }
    if after is not None:
        body["after"] = after
    return body


# ---------------------------------------------------------------------------
# extract_tracking_from_blockquote — form-vs-property fallback parse
# ---------------------------------------------------------------------------

# Old form tickets carry NULL tracking_number ticket PROPERTIES; the real value
# lives in the first email body's HTML blockquote (memory
# reference_hubspot_form_vs_property). Match the "Tracking Number" label
# (case-insensitive, tolerating &nbsp; and HTML tags between words), then skip
# any HTML tags / whitespace / colon, then capture a [A-Z0-9]{8,} run.
# Separator that may appear between the label words and before the value:
# HTML tags, &nbsp; entities, colons, or whitespace (any run, including none).
_SEP = r"(?:<[^>]*>|&nbsp;|&#160;|[\s:])*"
_TRACKING_RE = re.compile(
    r"tracking" + _SEP + r"number"  # "Tracking" <sep> "Number" (tags/nbsp/ws tolerant)
    + _SEP  # tags / nbsp / colon / whitespace between label & value
    + r"([A-Za-z0-9]{8,})",  # the tracking value (>= 8 alnum chars)
    re.IGNORECASE,
)


def extract_tracking_from_blockquote(html: Optional[str]) -> Optional[str]:
    """Pull a tracking number out of an email-body HTML blockquote, or None.

    Returns the uppercased-as-found alnum run following a "Tracking Number"
    label. A missing label, or a label with no value (empty / only tags before
    the next field), returns None — never a guess, never a partial.
    """
    if not html:
        return None
    m = _TRACKING_RE.search(html)
    if not m:
        return None
    value = m.group(1).strip()
    return value or None


# ---------------------------------------------------------------------------
# chunk — ≤100 input-id chunking for batch reads
# ---------------------------------------------------------------------------


def chunk(items: list, size: int) -> Iterator[list]:
    """Yield successive `size`-length slices of `items` (last may be shorter)."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ---------------------------------------------------------------------------
# watermark — resumable createdate state (corrupt/missing → 0 = free re-sweep)
# ---------------------------------------------------------------------------


def load_watermark(path: Path) -> int:
    """Read {"last_createdate": <epoch-ms>} → int, or 0 on any problem.

    A missing file, corrupt JSON, or a missing/non-int last_createdate all
    return 0 — losing the watermark costs only a free idempotent full re-sweep
    (ON CONFLICT DO NOTHING), so tolerance is the correct posture (and deleting
    the file is the documented reconciliation pass).
    """
    try:
        if not path.exists():
            return 0
        data = json.loads(path.read_text(encoding="utf-8") or "null")
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(data, dict):
        return 0
    value = data.get("last_createdate")
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def save_watermark(path: Path, last_createdate: int) -> None:
    """Persist {"last_createdate": <epoch-ms>}; OSError-tolerant (a lost
    watermark only costs a free re-sweep, never correctness)."""
    try:
        path.write_text(
            json.dumps({"last_createdate": last_createdate}), encoding="utf-8"
        )
    except OSError:
        # Non-fatal: the DB partial-unique is the real idempotency arbiter.
        pass


# ---------------------------------------------------------------------------
# brand_from_batches — ticket→company association → company name (ER-3)
# ---------------------------------------------------------------------------


def brand_from_batches(
    assoc_response: dict, companies_response: dict, ticket_id: str
) -> Optional[str]:
    """Resolve a ticket's brand = its first associated company's name, or None.

    `assoc_response`  = POST /crm/v4/associations/tickets/companies/batch/read.
    `companies_response` = POST /crm/v3/objects/companies/batch/read (props name).
    Returns None when: the ticket has no entry in the assoc batch, no associated
    company, the company is absent from the companies batch, or its name is
    empty/whitespace (no-name ⇒ unattributed, never a guess).
    """
    company_id: Optional[str] = None
    for result in assoc_response.get("results", []):
        if str((result.get("from") or {}).get("id")) != str(ticket_id):
            continue
        to_list = result.get("to") or []
        if not to_list:
            return None
        first = to_list[0]
        company_id = str(first.get("toObjectId") or first.get("id") or "") or None
        break

    if company_id is None:
        return None

    for company in companies_response.get("results", []):
        if str(company.get("id")) != str(company_id):
            continue
        name = (company.get("properties") or {}).get("name")
        if name is None:
            return None
        name = name.strip()
        return name or None

    return None
