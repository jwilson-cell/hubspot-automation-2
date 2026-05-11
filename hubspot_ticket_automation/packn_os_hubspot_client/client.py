"""Pack'N OS HubSpot helper - DB I/O surface for the existing automation.

Public functions (called by the existing automation's SKILL.md cron skill):
    read_routine_enabled(routine_name) -> bool
    write_run_record(routine_name, status, error_summary, tickets_processed,
                     drafts_created, started_at_iso, finished_at_iso) -> None
    write_draft(ticket_id, routine_name, draft_body, model, prompt_version,
                hubspot_ticket_snapshot) -> str  (draft_id, idempotent on D-05 triple)
    read_pending_drafts(routine_name, since_hours=48) -> list[dict]
        (Phase 4.1 D-02.b - digest skill reads pending drafts via this helper
         instead of scraping HubSpot draft engagement PACKN_METADATA_V1 blocks)
    read_pending_rerun_requests(routine_name) -> list[dict]
    mark_rerun_processed(rerun_request_id, resulting_draft_id) -> None

All functions use `with pool.connection() as conn:` for connection lifecycle
(Pitfall 4). All INSERTs/SELECTs include tenant_id (TENANT_ID constant -
mirrors src/lib/tenant.ts). The helper is fail-closed on routine reads
(missing row OR exception OR DB unreachable -> returns False) so the cron
NEVER proceeds without explicit enabled=true affirmation (CONTEXT D-01).

Threat-register notes (T-04-* in the plan):
    - psycopg %s placeholders prevent SQL injection (T-04-snapshot-injection)
    - 'category' + 'captured_at' validation forces structured snapshot
      (T-04-snapshot-injection D-07 scope guard)
    - rerun mark-processed is idempotent (T-04-rerun-replay)
"""

import json
import logging
from pathlib import Path
from typing import Optional

from psycopg.rows import dict_row

from .db import get_pool
from .tenant import TENANT_ID

logger = logging.getLogger(__name__)

# Phase 4.1 D-02.a / RESEARCH Open Question 5 (option B) - action_items continue
# to flow through pending_actions.json in the sibling-repo's config/ dir; the
# canonical Pack'N OS source has no equivalent file (this constant is exported
# for symmetry with the sibling-repo mirror but is unused in the Pack'N OS-only
# code paths). When the helper is mirrored to the sibling repo, this constant
# resolves to <sibling-repo>/hubspot_ticket_automation/config/pending_actions.json.
PENDING_ACTIONS_PATH = Path(__file__).resolve().parent.parent / "config" / "pending_actions.json"


def read_routine_enabled(routine_name: str) -> bool:
    """Return True iff automation_routines row exists with enabled=true.

    Fail-closed safety per CONTEXT D-01: returns False on missing row OR
    exception OR DB unreachable. The existing automation never proceeds
    without explicit enabled=true affirmation.
    """
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT enabled FROM automation_routines "
                    "WHERE tenant_id = %s AND name = %s",
                    (TENANT_ID, routine_name),
                )
                row = cur.fetchone()
                return bool(row and row["enabled"])
    except Exception as e:
        logger.warning(
            "read_routine_enabled failed - defaulting False: %s",
            e,
            extra={"routine": routine_name},
        )
        return False


def write_draft(
    ticket_id: str,
    routine_name: str,
    draft_body: str,
    model: str,
    prompt_version: str,
    hubspot_ticket_snapshot: dict,
) -> str:
    """INSERT into automation_drafts; idempotent on the D-05 triple.

    Idempotency key: (tenant_id, routine_name, ticket_id, prompt_version) -
    enforced by the unique index `automation_drafts_idem_uniq` (Plan 04-02).

    Returns:
        draft_id (UUID string) - new on first call, EXISTING on replay.

    Raises:
        ValueError: if hubspot_ticket_snapshot lacks 'category' (D-07
            similarity scope guard) or 'captured_at' (UI-side review-time
            staleness check).
    """
    if "category" not in hubspot_ticket_snapshot:
        raise ValueError(
            "hubspot_ticket_snapshot must include 'category' key for D-07 similarity scope"
        )
    if "captured_at" not in hubspot_ticket_snapshot:
        raise ValueError(
            "hubspot_ticket_snapshot must include 'captured_at' ISO timestamp"
        )

    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO automation_drafts
                  (tenant_id, routine_name, ticket_id, draft_body, model, prompt_version,
                   hubspot_ticket_snapshot, state)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, 'pending')
                ON CONFLICT (tenant_id, routine_name, ticket_id, prompt_version)
                  DO NOTHING
                RETURNING id
                """,
                (
                    TENANT_ID,
                    routine_name,
                    ticket_id,
                    draft_body,
                    model,
                    prompt_version,
                    json.dumps(hubspot_ticket_snapshot),
                ),
            )
            row = cur.fetchone()
            if row:
                conn.commit()
                return str(row["id"])
            # Replay path - the unique constraint blocked the INSERT; fetch existing.
            cur.execute(
                """
                SELECT id FROM automation_drafts
                WHERE tenant_id = %s AND routine_name = %s
                  AND ticket_id = %s AND prompt_version = %s
                """,
                (TENANT_ID, routine_name, ticket_id, prompt_version),
            )
            existing = cur.fetchone()
            return str(existing["id"]) if existing else ""


def read_pending_drafts(routine_name: str, since_hours: int = 48) -> list[dict]:
    """Read all pending drafts for this routine in the last N hours.

    Returns each row as a dict with the columns the digest needs to render:
        id, ticket_id, draft_body, hubspot_ticket_snapshot (parsed JSON), created_at

    Used by the hubspot-actions-digest skill (Phase 4.1 D-02.b) instead of
    scraping HubSpot draft engagement notes for PACKN_METADATA_V1 blocks.

    Returns [] on exception (fail-closed: missing drafts < silent crash).

    Per Phase 4.1 D-04: requires SELECT on automation_drafts for the
    packn_os_existing_automation role (granted via Phase 4.1 D-04.c).

    Filters applied:
        - tenant_id = TENANT_ID (multi-tenant invariant)
        - routine_name = <param>
        - state = 'pending' (state machine handles natural dedup; once a draft
          transitions to approved/sent/rejected/superseded it's no longer surfaced)
        - deleted_at IS NULL (soft-delete invariant)
        - created_at > NOW() - INTERVAL since_hours (lookback window)

    Ordering: created_at ASC so the digest renders oldest-first (consistent
    with the pre-Phase-4.1 HubSpot search ASC ordering).
    """
    try:
        with get_pool().connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT id, ticket_id, draft_body, hubspot_ticket_snapshot, created_at
                    FROM automation_drafts
                    WHERE tenant_id = %s
                      AND routine_name = %s
                      AND state = 'pending'
                      AND deleted_at IS NULL
                      AND created_at > NOW() - (%s || ' hours')::interval
                    ORDER BY created_at ASC
                    """,
                    (TENANT_ID, routine_name, str(since_hours)),
                )
                return cur.fetchall()
    except Exception as e:
        logger.warning(
            "read_pending_drafts failed: %s",
            e,
            extra={"routine": routine_name},
        )
        return []


def write_run_record(
    routine_name: str,
    status: str,  # 'success' | 'failure' | 'skipped' | 'partial'
    error_summary: Optional[str],
    tickets_processed: int,
    drafts_created: int,
    started_at_iso: str,
    finished_at_iso: Optional[str],
) -> None:
    """INSERT into automation_runs.

    Truncates error_summary at 8KB per UI-SPEC discretionary item 9.
    Truncation prefers the LAST 8KB (root cause is usually at traceback end
    per Assumption A5).

    Returns None - signature is fire-and-forget; failures raise.
    """
    if error_summary and len(error_summary) > 8000:
        error_summary = "...[truncated]\n" + error_summary[-8000:]
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO automation_runs
                  (tenant_id, routine_name, status, started_at, finished_at,
                   tickets_processed, drafts_created, error_summary)
                VALUES (%s, %s, %s, %s::timestamptz, %s::timestamptz, %s, %s, %s)
                """,
                (
                    TENANT_ID,
                    routine_name,
                    status,
                    started_at_iso,
                    finished_at_iso,
                    tickets_processed,
                    drafts_created,
                    error_summary,
                ),
            )
            conn.commit()


def read_pending_rerun_requests(routine_name: str) -> list[dict]:
    """Read unprocessed, non-expired rerun requests for this routine.

    Existing automation processes each + then calls mark_rerun_processed().
    Returns [] on exception (fail-closed: missing reruns < silent crash).

    Result rows include id, ticket_id, requested_by, requested_at - the
    minimum surface the cron needs to re-run the ticket-process pipeline
    against the right ticket.
    """
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, ticket_id, requested_by, requested_at
                    FROM automation_rerun_requests
                    WHERE tenant_id = %s AND routine_name = %s
                      AND processed = false AND expires_at > NOW()
                    ORDER BY requested_at ASC
                    """,
                    (TENANT_ID, routine_name),
                )
                return cur.fetchall()
    except Exception as e:
        logger.warning(
            "read_pending_rerun_requests failed: %s",
            e,
            extra={"routine": routine_name},
        )
        return []


def mark_rerun_processed(rerun_request_id: str, resulting_draft_id: str) -> None:
    """UPDATE automation_rerun_requests; called after a rerun produces a draft.

    Idempotent: re-running with the same rerun_request_id sets processed=true
    again with the same result (T-04-rerun-replay mitigation).
    """
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE automation_rerun_requests
                SET processed = true,
                    processed_at = NOW(),
                    resulting_draft_id = %s
                WHERE id = %s
                """,
                (resulting_draft_id, rerun_request_id),
            )
            conn.commit()
