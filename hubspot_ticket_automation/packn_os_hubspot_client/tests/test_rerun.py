"""HUB-03 - read_pending_rerun_requests + mark_rerun_processed flow.

Activated by Plan 04-03 Task 2. Tests run against the live dev Postgres DB.
Test routine names use the 'pytest-' prefix so the cleanup is targeted.
"""

import uuid

import psycopg

from packn_os_hubspot_client import client
from packn_os_hubspot_client.db import close_pool
from packn_os_hubspot_client.tenant import TENANT_ID


def _insert_rerun(
    conn: psycopg.Connection,
    routine_name: str,
    ticket_id: str,
    *,
    requested_at_offset_minutes: int = 0,
    processed: bool = False,
    expires_at_offset_hours: float = 24.0,
) -> str:
    """INSERT a rerun_request and return its id (UUID string).

    requested_at = now() + offset (minutes); use negatives for "older".
    expires_at = now() + offset (hours); use negatives for "expired".
    """
    rid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO automation_rerun_requests
              (id, tenant_id, routine_name, ticket_id, requested_by,
               requested_at, processed, expires_at)
            VALUES (%s, %s, %s, %s, %s,
                    NOW() + (%s || ' minutes')::interval,
                    %s,
                    NOW() + (%s || ' hours')::interval)
            """,
            (
                rid,
                TENANT_ID,
                routine_name,
                ticket_id,
                "test@example.com",
                str(requested_at_offset_minutes),
                processed,
                str(expires_at_offset_hours),
            ),
        )
    conn.commit()
    return rid


def test_read_pending_rerun_requests_returns_only_unprocessed_within_24h(
    db_conn: psycopg.Connection, cleanup_test_data: None
) -> None:
    """Excludes processed=true rows; excludes expires_at < NOW() rows."""
    # 1 processed (excluded), 1 expired (excluded), 1 active (included)
    _insert_rerun(
        db_conn, "pytest-tickets-process", "pytest-ticket-A", processed=True
    )
    _insert_rerun(
        db_conn,
        "pytest-tickets-process",
        "pytest-ticket-B",
        expires_at_offset_hours=-1.0,  # expired 1h ago
    )
    active_id = _insert_rerun(
        db_conn, "pytest-tickets-process", "pytest-ticket-C"
    )

    close_pool()
    pending = client.read_pending_rerun_requests("pytest-tickets-process")
    close_pool()

    assert len(pending) == 1
    assert str(pending[0]["id"]) == active_id
    assert pending[0]["ticket_id"] == "pytest-ticket-C"


def test_read_pending_rerun_requests_orders_by_requested_at_asc(
    db_conn: psycopg.Connection, cleanup_test_data: None
) -> None:
    """Oldest rerun request first (FIFO)."""
    # Insert 3 with decreasing offsets - latest first, oldest last
    _insert_rerun(
        db_conn,
        "pytest-tickets-process",
        "pytest-ticket-newest",
        requested_at_offset_minutes=0,
    )
    _insert_rerun(
        db_conn,
        "pytest-tickets-process",
        "pytest-ticket-middle",
        requested_at_offset_minutes=-30,
    )
    _insert_rerun(
        db_conn,
        "pytest-tickets-process",
        "pytest-ticket-oldest",
        requested_at_offset_minutes=-60,
    )

    close_pool()
    pending = client.read_pending_rerun_requests("pytest-tickets-process")
    close_pool()

    assert len(pending) == 3
    ticket_order = [r["ticket_id"] for r in pending]
    assert ticket_order == [
        "pytest-ticket-oldest",
        "pytest-ticket-middle",
        "pytest-ticket-newest",
    ]


def test_mark_rerun_processed_sets_processed_true_and_records_resulting_draft_id(
    db_conn: psycopg.Connection, cleanup_test_data: None
) -> None:
    """After mark_rerun_processed: processed=true, processed_at=NOW(),
    resulting_draft_id matches."""
    rid = _insert_rerun(
        db_conn, "pytest-tickets-process", "pytest-ticket-mark"
    )

    # Need a real draft_id to point at - insert a minimal draft first.
    snap_id = uuid.uuid4()
    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO automation_drafts
              (id, tenant_id, routine_name, ticket_id, draft_body, model,
               prompt_version, hubspot_ticket_snapshot, state)
            VALUES (%s, %s, 'pytest-tickets-process', 'pytest-ticket-mark',
                    'body', 'model', 'v1',
                    '{"category":"x","captured_at":"2026-05-03T00:00:00Z"}'::jsonb,
                    'pending')
            """,
            (str(snap_id), TENANT_ID),
        )
    db_conn.commit()

    close_pool()
    client.mark_rerun_processed(rid, str(snap_id))
    close_pool()

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT processed, processed_at, resulting_draft_id "
            "FROM automation_rerun_requests WHERE id = %s",
            (rid,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["processed"] is True
    assert row["processed_at"] is not None
    assert str(row["resulting_draft_id"]) == str(snap_id)
