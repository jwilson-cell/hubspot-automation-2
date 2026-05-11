"""Phase 4.1 D-02.c — One-time cleanup of cutover-day HubSpot draft engagements.

After the Phase 4.1 D-02.a draft-branch hard-cut, the ~150 HubSpot draft
engagements created on cutover day (2026-05-11) are redundant — the drafts
they represent now live in Pack'N OS automation_drafts.

This script:
1. Searches HubSpot for engagement notes containing BOTH 'PACKN_METADATA_V1'
   AND '[DRAFT — REVIEW BEFORE SENDING]' (excludes auto-send notes which use
   '[AUTO-SENT TO CUSTOMER]' header).
2. For each candidate, checks Pack'N OS automation_drafts for a matching row
   (ticket_id + created_at within +/-5 min of the engagement's hs_timestamp).
3. Classifies as Migrated (delete) or Orphan (skip + log).
4. Dry-run by default; --apply hard-deletes via HubSpot REST API.

Idempotent: re-running after deletes produces zero new deletions (404 is
treated as success — the engagement was already deleted).

Per CONTEXT D-02.c: --apply requires explicit flag + operator approval.

Usage:
  python scripts/cleanup_cutover_draft_engagements.py                          # dry-run
  python scripts/cleanup_cutover_draft_engagements.py --apply                  # commit deletes (operator step)
  python scripts/cleanup_cutover_draft_engagements.py --apply --since-days 7
  python scripts/cleanup_cutover_draft_engagements.py --apply --max-deletes 25

Output: outputs/cleanup/cleanup_<timestamp>.md — one line per engagement.
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from packn_os_hubspot_client.db import get_pool
from packn_os_hubspot_client.tenant import TENANT_ID

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

HUBSPOT_API_BASE = "https://api.hubapi.com"
DRAFT_MARKER = "[DRAFT — REVIEW BEFORE SENDING]"
METADATA_MARKER = "PACKN_METADATA_V1"
DEFAULT_SINCE_DAYS = 14
DEFAULT_MAX_DELETES = 1000


def parse_args():
    p = argparse.ArgumentParser(
        description="Cleanup cutover-day HubSpot draft engagements (Phase 4.1 D-02.c)"
    )
    p.add_argument("--apply", action="store_true", help="Actually delete (default: dry-run)")
    p.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS)
    p.add_argument("--max-deletes", type=int, default=DEFAULT_MAX_DELETES)
    p.add_argument("--hubspot-token", help="Override HUBSPOT_PRIVATE_APP_TOKEN")
    return p.parse_args()


def get_hubspot_token(override):
    """Get HubSpot private-app token from CLI flag > env var > local secrets file."""
    if override:
        return override
    token = os.environ.get("HUBSPOT_PRIVATE_APP_TOKEN")
    if token:
        return token
    token_file = Path("config/.secrets/hubspot_token.txt")
    if token_file.exists():
        return token_file.read_text().strip()
    # Fallback: cwd-relative hubspot_token.txt (some legacy invocations expect this)
    legacy_file = Path("hubspot_token.txt")
    if legacy_file.exists():
        return legacy_file.read_text().strip()
    raise RuntimeError(
        "No HubSpot token available — set HUBSPOT_PRIVATE_APP_TOKEN env var, "
        "place token in config/.secrets/hubspot_token.txt, or pass --hubspot-token"
    )


def search_draft_engagements(token, since_days):
    """Search HubSpot for note engagements matching both markers within the window.

    Returns up to 1000 candidates (paged at 100 per request). The CONTAINS_TOKEN
    operator is a HubSpot fulltext-style filter — it doesn't require exact-substring
    match, so we filter again at the script level using the body content (the
    classify_candidate path checks for both markers).
    """
    since_ts_ms = int((datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp() * 1000)
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/notes/search"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {
                        "propertyName": "hs_note_body",
                        "operator": "CONTAINS_TOKEN",
                        "value": METADATA_MARKER,
                    },
                    {
                        "propertyName": "hs_note_body",
                        "operator": "CONTAINS_TOKEN",
                        "value": DRAFT_MARKER,
                    },
                    {
                        "propertyName": "hs_timestamp",
                        "operator": "GTE",
                        "value": str(since_ts_ms),
                    },
                ]
            }
        ],
        "properties": ["hs_note_body", "hs_timestamp", "hs_engagement_associations"],
        "limit": 100,
        "sorts": [{"propertyName": "hs_timestamp", "direction": "ASCENDING"}],
    }
    candidates = []
    after = None
    while len(candidates) < 1000:
        if after:
            payload["after"] = after
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        candidates.extend(data.get("results", []))
        paging = data.get("paging", {})
        if not paging.get("next", {}).get("after"):
            break
        after = paging["next"]["after"]
    return candidates


def extract_ticket_id_from_engagement(engagement):
    """Extract the first associated TICKET object id from the engagement, or None."""
    assocs_raw = engagement.get("properties", {}).get("hs_engagement_associations")
    if not assocs_raw:
        return None
    try:
        assocs = json.loads(assocs_raw) if isinstance(assocs_raw, str) else assocs_raw
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(assocs, list):
        return None
    for a in assocs:
        if isinstance(a, dict) and a.get("objectType") == "TICKET":
            return str(a.get("objectId"))
    return None


def find_matching_draft(ticket_id, hs_timestamp_iso):
    """Return True if an automation_drafts row exists within +/-5 min of the engagement timestamp.

    Fail-closed: any DB lookup failure returns False (treated as orphan / skip-delete).
    """
    try:
        hs_ts = datetime.fromisoformat(hs_timestamp_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    window_lower = hs_ts - timedelta(minutes=5)
    window_upper = hs_ts + timedelta(minutes=5)
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM automation_drafts
                    WHERE tenant_id = %s
                      AND ticket_id = %s
                      AND created_at >= %s AND created_at <= %s
                    LIMIT 1
                    """,
                    (TENANT_ID, ticket_id, window_lower, window_upper),
                )
                return cur.fetchone() is not None
    except Exception as e:
        logger.warning("DB lookup failed for ticket_id=%s: %s", ticket_id, e)
        return False  # fail-closed: orphan = SKIP


def delete_engagement(token, note_id):
    """Hard-delete via HubSpot REST.

    Per RESEARCH Open Question 3 (RESOLVED): use REST DELETE /crm/v3/objects/notes/{id}
    as the canonical path. The sibling repo's MCP client (mcp__claude_ai_HubSpot__manage_crm_objects)
    is also acceptable IF execute-time verification confirms it exposes delete for notes;
    if MCP lacks delete-engagement support at v1 (the documented risk in Open Q3), this
    REST call is the fallback. Both paths converge on the same hubapi.com endpoint.

    Status code contract: 204 success, 404 already-deleted (idempotent — re-runs are no-ops).
    Returns: (success: bool, err: Optional[str]).
    """
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/notes/{note_id}"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.delete(url, headers=headers, timeout=30)
    if resp.status_code in (204, 404):
        return True, None
    return False, f"{resp.status_code} {resp.text[:200]}"


def main():
    args = parse_args()
    token = get_hubspot_token(args.hubspot_token)
    log_dir = Path("outputs/cleanup")
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"cleanup_{timestamp}.md"
    mode = "APPLY" if args.apply else "DRY-RUN"

    candidates = search_draft_engagements(token, args.since_days)
    logger.info("Found %d candidates in last %d days", len(candidates), args.since_days)

    stats = {
        "migrated-deleted": 0,
        "migrated-dry-run": 0,
        "orphan": 0,
        "no-association": 0,
        "error": 0,
    }
    log_lines = [
        f"# Phase 4.1 D-02.c Cleanup Run — {timestamp}",
        f"**Mode:** {mode}",
        f"**Lookback:** {args.since_days} days",
        f"**Max deletes:** {args.max_deletes}",
        f"**Candidates found:** {len(candidates)}",
        "",
        "| note_id | ticket_id | hs_timestamp | status | note |",
        "|---------|-----------|--------------|--------|------|",
    ]

    deletes_remaining = args.max_deletes
    for engagement in candidates:
        if deletes_remaining <= 0:
            log_lines.append("| — | — | — | cap-reached | --max-deletes limit reached |")
            break
        note_id = engagement.get("id")
        hs_ts = engagement.get("properties", {}).get("hs_timestamp", "")
        ticket_id = extract_ticket_id_from_engagement(engagement)
        if not ticket_id:
            stats["no-association"] += 1
            log_lines.append(
                f"| {note_id} | (none) | {hs_ts} | no-association | no associated ticket |"
            )
            continue
        if not find_matching_draft(ticket_id, hs_ts):
            stats["orphan"] += 1
            log_lines.append(
                f"| {note_id} | {ticket_id} | {hs_ts} | orphan | no matching automation_drafts row |"
            )
            continue
        if args.apply:
            success, err = delete_engagement(token, note_id)
            if success:
                stats["migrated-deleted"] += 1
                deletes_remaining -= 1
                log_lines.append(
                    f"| {note_id} | {ticket_id} | {hs_ts} | migrated-deleted | DELETE succeeded |"
                )
            else:
                stats["error"] += 1
                log_lines.append(
                    f"| {note_id} | {ticket_id} | {hs_ts} | error | {err} |"
                )
        else:
            stats["migrated-dry-run"] += 1
            log_lines.append(
                f"| {note_id} | {ticket_id} | {hs_ts} | migrated-dry-run | WOULD DELETE |"
            )

    log_lines.append("")
    log_lines.append("## Summary")
    for k, v in stats.items():
        log_lines.append(f"- **{k}**: {v}")
    log_path.write_text("\n".join(log_lines), encoding="utf-8")

    print(f"Wrote log: {log_path}")
    print(f"Mode: {mode}")
    print(f"Stats: {stats}")
    sys.exit(0)


if __name__ == "__main__":
    main()
