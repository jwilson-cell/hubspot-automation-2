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
    report_routine_schedule(routine_name, cron_marker) -> Optional[str]
        (2026-05-19: reports the actual droplet crontab back to Pack'N OS so
         the UI never drifts from reality. Caller passes a marker substring
         that uniquely identifies its own crontab line — e.g. the wrapper
         script path or the slash-command name.)
    claim_pending_manual_run() -> Optional[dict]
        (2026-05-19 Phase 3: atomic SELECT+UPDATE of a single pending
         automation_run_requests row. Race-safe via FOR UPDATE SKIP LOCKED
         so the regular 12h cron and the 5-min manual-run poller cannot
         double-claim. Returns the claimed row dict OR None when nothing
         pending. Caller is responsible for invoking the routine and
         calling mark_manual_run_completed.)
    mark_manual_run_completed(run_request_id, resulting_run_id) -> None
        (Phase 3: link the claimed manual-run request to the
         automation_runs row produced by the invocation. Idempotent.)

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
import subprocess
from datetime import datetime, timezone
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


# ---------------------------------------------------------------------------
# Phase 20 (D-01/D-04) — complaint-mirror writer helpers.
#
# The deterministic post-run writer (scripts/write_complaints.py) consumes the
# skill's accumulated outputs/kpi/mispack_log.csv and INSERTs customer_complaints
# rows through write_complaints() below. ZERO new dependencies, ZERO HubSpot
# calls — this is a pure DB-side mirror of the mispack CSV.
# ---------------------------------------------------------------------------


def normalize_optional(value: Optional[str]) -> Optional[str]:
    """'' / whitespace-only / None → None; else the stripped string.

    Empty strings must NEVER reach customer_complaints: they break the
    `IS NOT NULL` partial-index predicates (the tenant_brand / tenant_tracking
    indexes) and the NOT-EXISTS complement semantics on the Pack'N OS read side
    (an '' tracking would never match a shipment yet would not count as "no
    tracking", silently corrupting the unattributed bucket).
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def parse_complained_at(value: Optional[str]) -> Optional[str]:
    """Strict dual-format parse → ISO-8601 string with timezone, or None.

    Accepts:
      - ISO-8601 with a 'Z' suffix or an explicit offset ('Z' is normalized to
        '+00:00' before datetime.fromisoformat — the live on-disk format is
        ISO-8601-Z per Plan 20-02 probe-3).
      - integer epoch: 13 digits → milliseconds, 10 digits → seconds, both
        interpreted as UTC.

    NEVER defaults — an unparseable/empty/None timestamp returns None and the
    CALLER skips the row (D-06: complained_at carries "when the customer filed",
    NOT "when the writer ran"; defaulting to now() would corrupt the rolling-30d
    and monthly SLA windows on the OS read side).
    """
    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None

    # Integer epoch branch (all-digits, optional leading sign disallowed —
    # complaint timestamps are always positive).
    if raw.isdigit():
        try:
            num = int(raw)
        except ValueError:
            return None
        if len(raw) == 13:
            seconds = num / 1000.0
        elif len(raw) == 10:
            seconds = float(num)
        else:
            # Ambiguous digit count — refuse rather than guess the unit.
            return None
        try:
            dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (ValueError, OverflowError, OSError):
            return None
        return dt.isoformat()

    # ISO-8601 branch (normalize a trailing 'Z' to '+00:00' for fromisoformat).
    iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt.isoformat()


def write_complaints(rows: list[dict]) -> dict:
    """Batch INSERT mispack complaint rows. One connection, one transaction.

    Each row dict:
        {"hubspot_ticket_id": str, "classification": str,
         "shipment_tracking_number": str | None, "brand": str | None,
         "complained_at": str  # ISO-8601, pre-validated by the caller}

    Returns {"inserted": int, "conflict_skipped": int}.

    Idempotency: customer_complaints' unique index is PARTIAL
    (`customer_complaints_tenant_hubspot_uniq … WHERE deleted_at IS NULL`,
    migration 0052), so the ON CONFLICT target MUST repeat the WHERE clause or
    Postgres raises 42P10 (InvalidColumnReference) on EVERY row (memory
    project_packn_os_partial_index_onconflict_targetwhere — the same trap that
    killed emit.ts and Phase 18 inflight_orders in prod while passing
    build/tests). The pytest idempotency test is the regression guard.
    """
    inserted = 0
    conflict_skipped = 0
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    """
                    INSERT INTO customer_complaints
                      (tenant_id, hubspot_ticket_id, classification,
                       shipment_tracking_number, brand, complained_at)
                    VALUES (%s, %s, %s, %s, %s, %s::timestamptz)
                    ON CONFLICT (tenant_id, hubspot_ticket_id) WHERE deleted_at IS NULL
                      DO NOTHING
                    RETURNING id
                    """,
                    (
                        TENANT_ID,
                        row["hubspot_ticket_id"],
                        row["classification"],
                        normalize_optional(row.get("shipment_tracking_number")),
                        normalize_optional(row.get("brand")),
                        row["complained_at"],
                    ),
                )
                if cur.fetchone():
                    inserted += 1
                else:
                    conflict_skipped += 1
        conn.commit()
    return {"inserted": inserted, "conflict_skipped": conflict_skipped}


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


def _detect_cron_schedule(marker: str) -> Optional[str]:
    """Run `crontab -l` and return the first-5-fields schedule of the line
    containing `marker`.

    Returns None on any failure (crontab missing, marker not found, line too
    short, subprocess timeout, exception). Callers MUST treat None as
    "could not determine — skip the report" rather than as an error.

    Handles two cron line shapes:
        */30 * * * * /opt/.../script.sh    -> "*/30 * * * *"
        @hourly /opt/.../script.sh         -> "@hourly"
    """
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        for raw_line in result.stdout.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if marker not in line:
                continue
            if line.startswith("@"):
                first = line.split(maxsplit=1)
                return first[0] if first else None
            parts = line.split(maxsplit=5)
            if len(parts) >= 5:
                return " ".join(parts[:5])
        return None
    except Exception:
        return None


def report_routine_schedule(
    routine_name: str, cron_marker: str
) -> Optional[str]:
    """Detect the droplet's current crontab schedule for this routine and
    UPDATE automation_routines so the Pack'N OS UI never shows a stale value.

    The helper:
      1. Reads `crontab -l` for the current user.
      2. Finds the line containing `cron_marker` (e.g. 'cron_tickets.sh' for
         tickets-process or '/packn-digest' for the digest skill).
      3. Parses the first 5 fields as the cron schedule.
      4. UPDATEs cron_schedule + last_schedule_report_at = NOW().

    Designed for run-EVERY-tick use (operator confirmed 2026-05-19): the
    UPDATE is unconditional but cheap (single row, indexed PK), and the
    function ONLY logs when the schedule CHANGES or detection fails — so the
    runs log doesn't fill up with "still */30 * * * *" lines.

    Required DB grant on packn_os_existing_automation role:
        GRANT UPDATE (cron_schedule, last_schedule_report_at)
          ON public.automation_routines TO packn_os_existing_automation;

    Returns:
        The reported cron_schedule string on success, or None when detection
        or the DB write failed. Never raises (fail-quiet by design).
    """
    detected = _detect_cron_schedule(cron_marker)
    if detected is None:
        logger.warning(
            "report_routine_schedule: could not detect cron line for marker; skipping",
            extra={"routine": routine_name, "marker": cron_marker},
        )
        return None
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                # 2-step so we can log only on actual schedule changes
                # (operator wants zero log noise on the happy unchanged path).
                cur.execute(
                    "SELECT cron_schedule FROM automation_routines "
                    "WHERE tenant_id = %s AND name = %s",
                    (TENANT_ID, routine_name),
                )
                row = cur.fetchone()
                previous = row["cron_schedule"] if row else None
                cur.execute(
                    """
                    UPDATE automation_routines
                    SET cron_schedule = %s,
                        last_schedule_report_at = NOW()
                    WHERE tenant_id = %s AND name = %s
                    """,
                    (detected, TENANT_ID, routine_name),
                )
                conn.commit()
                if previous != detected:
                    logger.info(
                        "report_routine_schedule: cron changed %r -> %r",
                        previous,
                        detected,
                        extra={"routine": routine_name},
                    )
    except Exception as e:
        logger.warning(
            "report_routine_schedule: DB write failed: %s",
            e,
            extra={"routine": routine_name, "schedule": detected},
        )
        return None
    return detected


def claim_pending_manual_run() -> Optional[dict]:
    """Atomically claim the OLDEST pending, non-expired manual-run request.

    Returns the claimed row as a dict (id, routine_name, requested_by,
    requested_at) on success, or None when nothing is pending.

    Race-safe: uses FOR UPDATE SKIP LOCKED inside a subquery so two
    concurrent callers (e.g. two overlapping poll-cron invocations, or the
    poll cron racing with the regular 12h cron) cannot claim the same row.
    The successful caller's UPDATE marks processed=true + processed_at=NOW;
    the loser's subquery returns zero rows and the caller receives None.

    Returns [] / None on exception (fail-quiet — a DB outage should not
    crash the poll wrapper; the next tick retries).

    Required DB grants on packn_os_existing_automation role:
        GRANT SELECT, UPDATE (processed, processed_at, resulting_run_id)
          ON public.automation_run_requests TO packn_os_existing_automation;
    """
    try:
        with get_pool().connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    UPDATE automation_run_requests
                    SET processed = true,
                        processed_at = NOW()
                    WHERE id = (
                        SELECT id FROM automation_run_requests
                        WHERE tenant_id = %s
                          AND processed = false
                          AND expires_at > NOW()
                        ORDER BY requested_at ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    RETURNING id, routine_name, requested_by, requested_at
                    """,
                    (TENANT_ID,),
                )
                row = cur.fetchone()
                conn.commit()
                return row if row else None
    except Exception as e:
        logger.warning(
            "claim_pending_manual_run failed: %s",
            e,
        )
        return None


def mark_manual_run_completed(
    run_request_id: str, resulting_run_id: Optional[str]
) -> None:
    """Link the claimed manual-run request to the automation_runs row that
    came out of the invocation. Called by the poll-cron wrapper AFTER the
    routine returns.

    `resulting_run_id` may be None when the routine crashed before writing
    its own automation_runs row — the wrapper still calls this so the audit
    trail shows the claim was attempted.

    Idempotent: UPDATE-by-id with no precondition; repeated calls are
    no-ops with respect to processed=true (already set by claim).

    Required DB grant: UPDATE (processed_at, resulting_run_id) on
    automation_run_requests (covered by the GRANT in claim_pending_manual_run
    docstring).
    """
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE automation_run_requests
                    SET resulting_run_id = %s
                    WHERE id = %s
                    """,
                    (resulting_run_id, run_request_id),
                )
                conn.commit()
    except Exception as e:
        logger.warning(
            "mark_manual_run_completed failed: %s",
            e,
            extra={
                "run_request_id": run_request_id,
                "resulting_run_id": resulting_run_id,
            },
        )
