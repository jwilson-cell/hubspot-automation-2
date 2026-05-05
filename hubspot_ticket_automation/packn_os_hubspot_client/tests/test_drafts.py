"""HUB-04 - write_draft idempotent INSERT cases.

Activated by Plan 04-03 Task 2. Tests run against the live dev Postgres DB.
Test routine names use the 'pytest-' prefix so the cleanup is targeted.
"""

import json
import uuid

import psycopg
import pytest

from packn_os_hubspot_client import client
from packn_os_hubspot_client.db import close_pool


def _make_snapshot() -> dict:
    """Helper - make a valid snapshot dict (has the required keys)."""
    return {
        "subject": "test subject",
        "body": "test body",
        "category": "shipping",
        "captured_at": "2026-05-03T14:00:00Z",
        "contact": {"name": "Test User", "email": "test@example.com"},
    }


def test_write_draft_inserts_pending_row_returns_draft_id(
    db_conn: psycopg.Connection, cleanup_test_data: None
) -> None:
    """write_draft inserts state='pending' row and returns the new draft_id."""
    close_pool()
    draft_id = client.write_draft(
        ticket_id="pytest-ticket-123",
        routine_name="pytest-tickets-process",
        draft_body="hello",
        model="claude-sonnet-4-5",
        prompt_version="v3.2.1",
        hubspot_ticket_snapshot=_make_snapshot(),
    )
    close_pool()
    # Should be a valid UUID
    assert draft_id
    uuid.UUID(draft_id)  # raises ValueError if not a UUID

    # SELECT to verify state='pending'
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, state, draft_body FROM automation_drafts WHERE id = %s",
            (draft_id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["state"] == "pending"
    assert row["draft_body"] == "hello"


def test_write_draft_idempotent_on_routine_ticket_promptversion_triple(
    db_conn: psycopg.Connection, cleanup_test_data: None
) -> None:
    """Second write_draft with same triple returns SAME draft_id, no second row."""
    close_pool()
    snap = _make_snapshot()
    first_id = client.write_draft(
        ticket_id="pytest-ticket-456",
        routine_name="pytest-tickets-process",
        draft_body="hello-v1",
        model="claude-sonnet-4-5",
        prompt_version="v3.2.1",
        hubspot_ticket_snapshot=snap,
    )
    second_id = client.write_draft(
        ticket_id="pytest-ticket-456",
        routine_name="pytest-tickets-process",
        # Different body - would be a NEW row if not for the unique idempotency
        # constraint. The replay path returns the existing id.
        draft_body="hello-v2-replay-body",
        model="claude-sonnet-4-5",
        prompt_version="v3.2.1",
        hubspot_ticket_snapshot=snap,
    )
    close_pool()

    assert first_id == second_id

    # Confirm only ONE row exists for this triple
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) AS n FROM automation_drafts
            WHERE routine_name = 'pytest-tickets-process'
              AND ticket_id = 'pytest-ticket-456'
              AND prompt_version = 'v3.2.1'
            """,
        )
        row = cur.fetchone()
    assert row is not None
    assert row["n"] == 1


def test_write_draft_raises_when_snapshot_missing_category(
    db_conn: psycopg.Connection, cleanup_test_data: None
) -> None:
    """ValueError when hubspot_ticket_snapshot lacks 'category' (D-07 scope)."""
    close_pool()
    bad_snap = {"subject": "x", "body": "y", "captured_at": "2026-05-03T00:00:00Z"}
    with pytest.raises(ValueError, match="category"):
        client.write_draft(
            ticket_id="pytest-ticket-789",
            routine_name="pytest-tickets-process",
            draft_body="x",
            model="claude-sonnet-4-5",
            prompt_version="v3.2.1",
            hubspot_ticket_snapshot=bad_snap,
        )
    close_pool()


def test_write_draft_persists_full_snapshot_in_jsonb_column(
    db_conn: psycopg.Connection, cleanup_test_data: None
) -> None:
    """write_draft serializes the snapshot dict via json.dumps and stores in
    hubspot_ticket_snapshot column."""
    close_pool()
    snap = _make_snapshot()
    snap["custom_properties"] = {"hs_internal_field": "abc"}
    draft_id = client.write_draft(
        ticket_id="pytest-ticket-snap",
        routine_name="pytest-tickets-process",
        draft_body="hi",
        model="claude-sonnet-4-5",
        prompt_version="v3.2.1",
        hubspot_ticket_snapshot=snap,
    )
    close_pool()

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT hubspot_ticket_snapshot FROM automation_drafts WHERE id = %s",
            (draft_id,),
        )
        row = cur.fetchone()
    assert row is not None
    # psycopg + jsonb returns dict directly
    stored = row["hubspot_ticket_snapshot"]
    if isinstance(stored, str):
        # If row_factory returns string, decode
        stored = json.loads(stored)
    assert stored["category"] == "shipping"
    assert stored["custom_properties"]["hs_internal_field"] == "abc"
    assert stored["captured_at"] == "2026-05-03T14:00:00Z"
