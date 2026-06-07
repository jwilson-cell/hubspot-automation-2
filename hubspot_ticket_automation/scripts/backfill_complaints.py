#!/usr/bin/env python3
"""Phase 20 Plan 20-05 — historical complaint backfill (one-shot, resumable).

WHAT IT DOES
------------
Sweeps ALL historical mispack tickets out of HubSpot into Pack'N OS
`customer_complaints` (classification='mispack') with brand attribution. This
closes the gap the live writer (scripts/write_complaints.py) can't cover: the
CSV mirror only spans the automation's lifetime (since ~2026-04-23), and old
tickets carry NULL tracking_number ticket PROPERTIES — their real tracking value
lives in the first email body's HTML blockquote (memory
reference_hubspot_form_vs_property). probe-1 (Plan 20-02 SUMMARY) CONFIRMED the
search source: topic_of_ticket EQ "Mispack (Provide Order Number)" → HTTP 200,
total=35 historical tickets.

It is a READ-ONLY pass against HubSpot (search + associations + companies +
optional email-body fetch) that writes ONLY to Postgres via the same idempotent
path the live writer uses: client.write_complaints → ON CONFLICT
(tenant_id, hubspot_ticket_id) WHERE deleted_at IS NULL DO NOTHING. The HubSpot
search key-space (the ticket id, as text) is identical to the CSV writer's, so
the two paths dedupe against each other — re-running either is free.

RESUMABLE + IDEMPOTENT
----------------------
A watermark (config/.complaints_backfill_state.json, gitignored) holds the max
createdate epoch-ms of the last fully-committed page. A killed run resumes from
the watermark. Deleting the watermark file re-runs the WHOLE sweep as a free
reconciliation pass (every INSERT is ON CONFLICT DO NOTHING). The watermark is
written AFTER each page's DB commit, so an interruption never advances past
unwritten rows; a page-boundary re-fetch overlap is free.

NEVER ASSUME (Pitfall 6 / backfill-placed-at posture)
-----------------------------------------------------
A ticket whose createdate is unparseable is SKIPPED + counted, never stamped
now(). A ticket with no associated company → brand NULL. A ticket with no
property tracking and no blockquote match → tracking NULL. Nothing is defaulted.

RATE LIMITING (shared 3 req/s HubSpot budget; T-20-05-01)
---------------------------------------------------------
Before EVERY HubSpot call the script attempts the cross-process rate-limit token
(packn_os_hubspot_client.rate_limit) AND sleeps a hard 0.34s floor. The token
DEGRADES TO PASSTHROUGH without REDIS_URL (interactive shells lack it), so the
unconditional sleep is the real guard.

OPERATOR RUNBOOK (execution deferred to Plan 20-08 — run AFTER the migration +
GRANT + cutover land; this is a droplet-side one-shot immune to OS deploys)
--------------------------------------------------------------------------------
Prerequisites (Plan 20-06 / 20-07):
  - migration 0073 (customer_complaints.brand column) APPLIED on prod.
  - GRANT SELECT, INSERT ON customer_complaints TO packn_os_existing_automation.
  - PACKN_OS_DATABASE_URL present in the interactive env (probe preflight: YES).
  - config/.secrets/hubspot_token.txt populated (the shared private-app token).

On the droplet (packn@167.99.229.91), from /opt/packn/hubspot_ticket_automation:
  1. Dry run (pages + counts; NO DB writes, NO watermark):
       .venv/bin/python scripts/backfill_complaints.py --dry-run
  2. Real run (resumable; survives disconnect via nohup):
       nohup .venv/bin/python scripts/backfill_complaints.py \
         >> outputs/runs/backfill-complaints.log 2>&1 &
  3. Resume after a kill: just re-run the real command — it picks up at the
     watermark automatically.
  4. Reconciliation re-sweep (re-check ALL history, free / idempotent):
       rm config/.complaints_backfill_state.json
       .venv/bin/python scripts/backfill_complaints.py
  5. Skip the per-ticket email-body fallback (faster, fewer calls; loses
     blockquote-only tracking):
       .venv/bin/python scripts/backfill_complaints.py --no-email-fetch

Secrets: this script reads the HubSpot token from the token FILE and the DB URL
from the environment. It NEVER prints either; this runbook carries zero secret
values.

Stdlib + the existing packn_os_hubspot_client module only — no new dependencies
(sibling CLAUDE.md invariant).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator, Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from packn_os_hubspot_client import client  # noqa: E402
from packn_os_hubspot_client.db import close_pool  # noqa: E402

# -- constants ---------------------------------------------------------------

HUBSPOT_BASE = "https://api.hubapi.com"
TOKEN_PATH = ROOT / "config" / ".secrets" / "hubspot_token.txt"
WATERMARK_PATH = ROOT / "config" / ".complaints_backfill_state.json"

# The exact mispack topic value probe-1 confirmed is searchable (total=35).
MISPACK_TOPIC = "Mispack (Provide Order Number)"
CLASSIFICATION = "mispack"

# Properties hydrated per ticket from the search response.
SEARCH_PROPERTIES = [
    "createdate",
    "subject",
    "topic_of_ticket",
    "tracking_number",
    "order_number",
]

PAGE_LIMIT = 200  # HubSpot search max page size.
BATCH_LIMIT = 100  # v4 association / v3 company batch-read input cap (RESEARCH A5).
RATE_FLOOR_S = 0.34  # hard per-call sleep floor: time.sleep(0.34) before EVERY HubSpot call (REDIS may be absent → token degrades to passthrough, so this sleep is the real guard).
RETRY_SLEEP_S = 2.0  # one-time backoff on 429/5xx for this interactive one-shot.

# Exit codes (interactive one-shot — MAY fail loudly, unlike the cron writer).
EXIT_OK = 0
EXIT_TOKEN_MISSING = 3
EXIT_HTTP_FATAL = 4


def _log(msg: str) -> None:
    print(f"[backfill_complaints] {msg}", file=sys.stderr)


# ===========================================================================
# PURE LAYER (Task 1 — unit-tested, no HTTP/DB). Do not add side effects here.
# ===========================================================================


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
        "limit": PAGE_LIMIT,
    }
    if after is not None:
        body["after"] = after
    return body


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

    Returns the alnum run following a "Tracking Number" label. A missing label,
    or a label with no value (empty / only tags before the next field), returns
    None — never a guess, never a partial.
    """
    if not html:
        return None
    m = _TRACKING_RE.search(html)
    if not m:
        return None
    value = m.group(1).strip()
    return value or None


def chunk(items: list, size: int) -> Iterator[list]:
    """Yield successive `size`-length slices of `items` (last may be shorter)."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


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


# ===========================================================================
# I/O LAYER (Task 2 — HTTP paging + hydration + watermark + dry-run).
# ===========================================================================


def _read_token() -> str:
    """Read the shared HubSpot private-app token from the token file.

    Exits EXIT_TOKEN_MISSING (3) with a clear message if missing/empty — a
    backfill is interactive and SHOULD fail loudly on a fatal config error
    (unlike the never-fail cron writer).
    """
    if not TOKEN_PATH.exists():
        _log(f"token missing at config/.secrets/hubspot_token.txt")
        sys.exit(EXIT_TOKEN_MISSING)
    token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    if not token:
        _log(f"token file at config/.secrets/hubspot_token.txt is empty")
        sys.exit(EXIT_TOKEN_MISSING)
    return token


def _rate_gate() -> None:
    """Throttle BEFORE every HubSpot call: attempt the cross-process token, then
    sleep the hard floor unconditionally.

    The token DEGRADES TO PASSTHROUGH without REDIS_URL (interactive shells lack
    it — and rate_limit._get_redis() raises KeyError on a missing REDIS_URL),
    so the unconditional 0.34s sleep is the real guard. Any token failure is
    swallowed; the sleep always runs.
    """
    try:
        from packn_os_hubspot_client import rate_limit

        rate_limit.acquire_hubspot_token()
    except Exception as exc:  # noqa: BLE001 — token is best-effort; sleep is the guard.
        _log(f"rate-limit token unavailable ({exc!r}); relying on sleep floor")
    time.sleep(RATE_FLOOR_S)


def _post(path: str, body: dict, token: str) -> dict:
    """POST a JSON body to HubSpot. Rate-gated; ONE retry on 429/5xx then fatal.

    Returns the parsed JSON dict on 2xx. Exits EXIT_HTTP_FATAL (4) loudly on
    401/403 (auth) or after the single retry — this is an interactive one-shot,
    not the never-fail cron writer.
    """
    return _request("POST", path, token, body=body)


def _get(path: str, token: str) -> Optional[dict]:
    """GET from HubSpot. Rate-gated; ONE retry on 429/5xx. Returns None on 404."""
    return _request("GET", path, token)


def _request(
    method: str, path: str, token: str, body: Optional[dict] = None
) -> Optional[dict]:
    url = f"{HUBSPOT_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    for attempt in (1, 2):  # at most one retry
        _rate_gate()
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            code = e.code
            if code == 404 and method == "GET":
                return None
            if code in (401, 403):
                err = e.read().decode("utf-8", errors="replace")[:300]
                _log(f"HTTP {code} on {method} {path} (auth) — aborting: {err}")
                sys.exit(EXIT_HTTP_FATAL)
            if code == 429 or 500 <= code < 600:
                if attempt == 1:
                    _log(f"HTTP {code} on {method} {path} — retry in {RETRY_SLEEP_S}s")
                    time.sleep(RETRY_SLEEP_S)
                    continue
                err = e.read().decode("utf-8", errors="replace")[:300]
                _log(f"HTTP {code} on {method} {path} after retry — aborting: {err}")
                sys.exit(EXIT_HTTP_FATAL)
            err = e.read().decode("utf-8", errors="replace")[:300]
            _log(f"HTTP {code} on {method} {path} — aborting: {err}")
            sys.exit(EXIT_HTTP_FATAL)
        except urllib.error.URLError as e:
            if attempt == 1:
                _log(f"network error on {method} {path} ({e!r}) — retry in {RETRY_SLEEP_S}s")
                time.sleep(RETRY_SLEEP_S)
                continue
            _log(f"network error on {method} {path} after retry — aborting: {e!r}")
            sys.exit(EXIT_HTTP_FATAL)
    # Unreachable (both attempts either return or sys.exit), but satisfy typing.
    return None


def _hydrate_brands(ticket_ids: list[str], token: str) -> dict[str, Optional[str]]:
    """Resolve brand for each ticket via ticket→company assoc + company name.

    Chunks ids ≤100 for both batch reads. Returns {ticket_id: brand|None}.
    """
    brands: dict[str, Optional[str]] = {tid: None for tid in ticket_ids}
    for ids in chunk(ticket_ids, BATCH_LIMIT):
        assoc = _post(
            "/crm/v4/associations/tickets/companies/batch/read",
            {"inputs": [{"id": tid} for tid in ids]},
            token,
        )
        # Collect the first company id per ticket so we can batch-read names.
        company_ids: list[str] = []
        for result in assoc.get("results", []):
            to_list = result.get("to") or []
            if to_list:
                first = to_list[0]
                cid = str(first.get("toObjectId") or first.get("id") or "")
                if cid:
                    company_ids.append(cid)
        companies: dict = {"results": []}
        if company_ids:
            # Dedupe company ids; still bounded by ≤100 tickets per chunk.
            uniq = list(dict.fromkeys(company_ids))
            companies = _post(
                "/crm/v3/objects/companies/batch/read",
                {"inputs": [{"id": cid} for cid in uniq], "properties": ["name"]},
                token,
            )
        for tid in ids:
            brands[tid] = brand_from_batches(assoc, companies, tid)
    return brands


def _tracking_for_ticket(
    ticket: dict, token: str, email_fetch: bool
) -> Optional[str]:
    """tracking_number property if non-empty; else (when email_fetch) the first
    associated email's hs_email_html blockquote; else None."""
    props = ticket.get("properties") or {}
    prop_tracking = client.normalize_optional(props.get("tracking_number"))
    if prop_tracking:
        return prop_tracking
    if not email_fetch:
        return None
    ticket_id = str(ticket.get("id") or "")
    if not ticket_id:
        return None
    assoc = _get(f"/crm/v4/objects/tickets/{ticket_id}/associations/emails", token)
    if not assoc:
        return None
    results = assoc.get("results") or []
    if not results:
        return None
    first_email_id = str(results[0].get("toObjectId") or results[0].get("id") or "")
    if not first_email_id:
        return None
    email = _get(
        f"/crm/v3/objects/emails/{first_email_id}?properties=hs_email_html", token
    )
    if not email:
        return None
    html = (email.get("properties") or {}).get("hs_email_html")
    return extract_tracking_from_blockquote(html)


def run(token: str, dry_run: bool, email_fetch: bool) -> dict:
    """Page the full mispack-ticket history → customer_complaints, resumably.

    Returns the summary counts dict.
    """
    watermark = load_watermark(WATERMARK_PATH)
    _log(
        f"start: watermark={watermark} dry_run={dry_run} email_fetch={email_fetch}"
    )

    pages = tickets_seen = inserted = conflict_skipped = 0
    skipped_bad = no_brand = no_tracking = email_fetches = 0
    after: Optional[str] = None

    while True:
        body = build_search_body(watermark_ms=watermark, after=after)
        resp = _post("/crm/v3/objects/tickets/search", body, token)
        results = resp.get("results") or []
        if not results:
            break
        pages += 1
        tickets_seen += len(results)

        ticket_ids = [str(t.get("id") or "") for t in results if t.get("id")]
        brands = _hydrate_brands(ticket_ids, token)

        page_rows: list[dict] = []
        page_max_created = watermark
        for t in results:
            ticket_id = str(t.get("id") or "")
            props = t.get("properties") or {}

            created_raw = props.get("createdate")
            complained_at = client.parse_complained_at(created_raw)
            if complained_at is None:
                # NEVER default complained_at — skip + count (Pitfall 6).
                skipped_bad += 1
                continue

            brand = brands.get(ticket_id)
            if brand is None:
                no_brand += 1

            tracking = _tracking_for_ticket(t, token, email_fetch)
            if email_fetch and not client.normalize_optional(
                props.get("tracking_number")
            ):
                email_fetches += 1
            if tracking is None:
                no_tracking += 1

            page_rows.append(
                {
                    "hubspot_ticket_id": ticket_id,
                    "classification": CLASSIFICATION,
                    "shipment_tracking_number": tracking,
                    "brand": brand,
                    "complained_at": complained_at,
                }
            )

            # Track the max createdate epoch-ms for the watermark advance.
            try:
                created_ms = _createdate_ms(created_raw)
                if created_ms is not None and created_ms > page_max_created:
                    page_max_created = created_ms
            except Exception:  # noqa: BLE001 — watermark math never aborts the run.
                pass

        if dry_run:
            # No DB write, no watermark write — just count.
            inserted += len(page_rows)
            _log(
                f"page={pages} tickets={len(results)} would_insert={len(page_rows)} (dry-run)"
            )
        else:
            page_result = client.write_complaints(page_rows) if page_rows else {
                "inserted": 0,
                "conflict_skipped": 0,
            }
            inserted += page_result["inserted"]
            conflict_skipped += page_result["conflict_skipped"]
            # Persist the watermark AFTER the page's DB commit returns.
            if page_max_created > watermark:
                watermark = page_max_created
                save_watermark(WATERMARK_PATH, watermark)
            _log(
                f"page={pages} tickets={len(results)} "
                f"inserted={page_result['inserted']} "
                f"conflict_skipped={page_result['conflict_skipped']}"
            )

        paging = resp.get("paging") or {}
        after = ((paging.get("next") or {}).get("after")) or None
        if after is None:
            break

    summary = {
        "pages": pages,
        "tickets_seen": tickets_seen,
        "inserted": inserted,
        "conflict_skipped": conflict_skipped,
        "skipped_bad": skipped_bad,
        "no_brand": no_brand,
        "no_tracking": no_tracking,
        "email_fetches": email_fetches,
    }
    _log(
        "done: "
        f"pages={pages} tickets_seen={tickets_seen} inserted={inserted} "
        f"conflict_skipped={conflict_skipped} skipped_bad={skipped_bad} "
        f"no_brand={no_brand} no_tracking={no_tracking} "
        f"email_fetches={email_fetches}"
    )
    return summary


def _createdate_ms(value: Optional[str]) -> Optional[int]:
    """HubSpot createdate → epoch-ms int for the watermark, or None.

    HubSpot returns createdate either as an ISO-8601 string or an epoch-ms
    string depending on the API surface; handle both. Never raises out (callers
    treat None as "could not advance watermark from this row").
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.isdigit():
        n = int(raw)
        # 13-digit → already ms; 10-digit → s → ms.
        if len(raw) == 13:
            return n
        if len(raw) == 10:
            return n * 1000
        return None
    iso = client.parse_complained_at(raw)
    if iso is None:
        return None
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return int(dt.timestamp() * 1000)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-shot resumable historical mispack-complaint backfill."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="page + count only; write NO rows and NO watermark.",
    )
    parser.add_argument(
        "--no-email-fetch",
        action="store_true",
        help="skip the per-ticket email-body blockquote tracking fallback "
        "(default is fetch-on — tracking-matched rows feed /scorecards).",
    )
    args = parser.parse_args(argv)

    token = _read_token()
    try:
        run(token, dry_run=args.dry_run, email_fetch=not args.no_email_fetch)
    finally:
        close_pool()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
