"""HUB-04 - write_draft idempotent INSERT cases + Phase 4.1 D-02.b read_pending_drafts.

Activated by Plan 04-03 Task 2. Tests run against the live dev Postgres DB.
Test routine names use the 'pytest-' prefix so the cleanup is targeted.

Phase 4.1 D-02.b adds 6 read_pending_drafts test cases (Plan 04.1-04 Task 2).
"""

import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import psycopg
import pytest

from packn_os_hubspot_client import client
from packn_os_hubspot_client.db import close_pool
from packn_os_hubspot_client.tenant import TENANT_ID


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


# ============================================================================
# Phase 4.1 D-02.b — read_pending_drafts test cases
# Plan 04.1-04 Task 2: digest skill reads pending drafts via this helper
# instead of scraping HubSpot draft engagement PACKN_METADATA_V1 blocks.
# ============================================================================


@pytest.fixture
def seeded_drafts(db_conn: psycopg.Connection, cleanup_test_data: None):
    """Seed a deterministic mix of drafts for read_pending_drafts tests.

    Routine names use the 'pytest-' prefix so cleanup_test_data targets them.
    Tickets are tagged 'pytest-T-N' (also pytest-prefixed so any future
    ticket_id-based cleanup hooks would also catch them).

    Fixture mix:
        T-1: in-window pending  (appears in main query)
        T-2: in-window sent     (state filter excludes)
        T-3: in-window rejected (state filter excludes)
        T-4: in-window superseded (state filter excludes)
        T-5: outside-window pending (since_hours filter excludes)
        T-6: soft-deleted pending (deleted_at filter excludes)
        T-7: cross-tenant pending (tenant_id filter excludes — uses 'other' tenant_id)
        T-8: cross-routine pending (routine_name filter excludes — uses 'pytest-digest')
        T-9: in-window older pending (appears, BEFORE T-1 in ASC order)
    """
    fixtures = [
        # (state, deleted_at, hours_offset, tenant, routine, ticket_id, body)
        ("pending",    None,                                  -1,  TENANT_ID, "pytest-tickets-process", "pytest-T-1", "in-window pending"),
        ("sent",       None,                                  -1,  TENANT_ID, "pytest-tickets-process", "pytest-T-2", "in-window sent"),
        ("rejected",   None,                                  -1,  TENANT_ID, "pytest-tickets-process", "pytest-T-3", "in-window rejected"),
        ("superseded", None,                                  -1,  TENANT_ID, "pytest-tickets-process", "pytest-T-4", "in-window superseded"),
        ("pending",    None,                                  -72, TENANT_ID, "pytest-tickets-process", "pytest-T-5", "outside window pending"),
        ("pending",    datetime.now(timezone.utc),            -1,  TENANT_ID, "pytest-tickets-process", "pytest-T-6", "soft-deleted pending"),
        ("pending",    None,                                  -1,  "other",   "pytest-tickets-process", "pytest-T-7", "cross-tenant pending"),
        ("pending",    None,                                  -1,  TENANT_ID, "pytest-digest",          "pytest-T-8", "cross-routine pending"),
        ("pending",    None,                                  -2,  TENANT_ID, "pytest-tickets-process", "pytest-T-9", "in-window older pending"),
    ]

    close_pool()
    for state, deleted_at, hours_offset, tenant, routine, ticket_id, body in fixtures:
        snapshot = {
            "subject": f"Subject for {ticket_id}",
            "body": body,
            "category": "tracking",
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        with db_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO automation_drafts (tenant_id, routine_name, ticket_id, draft_body,
                                               model, prompt_version, hubspot_ticket_snapshot,
                                               state, deleted_at, created_at)
                VALUES (%s, %s, %s, %s, 'claude-sonnet-4-5', 'v3.2.1', %s::jsonb,
                        %s, %s, NOW() + (%s || ' hours')::interval)
                """,
                (tenant, routine, ticket_id, body, json.dumps(snapshot),
                 state, deleted_at, str(hours_offset)),
            )
        db_conn.commit()
    yield
    # Cleanup of pytest-tickets-process + pytest-digest rows is handled by
    # cleanup_test_data; cross-tenant 'other' rows are also pytest-prefixed
    # routine_name so they're caught by the same DELETE.
    with db_conn.cursor() as cur:
        # Belt-and-suspenders: ensure the 'other'-tenant row is removed too.
        cur.execute("DELETE FROM automation_drafts WHERE ticket_id LIKE 'pytest-T-%'")
    db_conn.commit()


def test_read_pending_drafts_only_pending_within_window(seeded_drafts) -> None:
    """Only in-window, non-deleted, packn-tenant, matching-routine, pending rows return."""
    close_pool()
    rows = client.read_pending_drafts("pytest-tickets-process", since_hours=48)
    close_pool()
    # Filter to pytest-prefixed tickets to ignore any seed rows from other tests
    rows = [r for r in rows if r["ticket_id"].startswith("pytest-T-")]
    # T-9 (older pending @ -2h) THEN T-1 (newer pending @ -1h) — ASC by created_at
    ticket_ids = [r["ticket_id"] for r in rows]
    assert ticket_ids == ["pytest-T-9", "pytest-T-1"], (
        f"Expected only [pytest-T-9, pytest-T-1] in ASC order, got {ticket_ids}"
    )


def test_read_pending_drafts_returns_empty_on_db_unreachable() -> None:
    """When get_pool() raises (e.g., Postgres unreachable), returns [] (fail-closed)."""
    close_pool()
    with patch("packn_os_hubspot_client.client.get_pool") as mock_pool:
        mock_pool.side_effect = psycopg.OperationalError("connection refused")
        rows = client.read_pending_drafts("pytest-tickets-process")
    assert rows == [], f"Expected [] on DB unreachable, got {rows}"


def test_read_pending_drafts_filters_by_tenant_id(seeded_drafts) -> None:
    """Cross-tenant ('other') row excluded by tenant_id filter."""
    close_pool()
    rows = client.read_pending_drafts("pytest-tickets-process", since_hours=48)
    close_pool()
    assert all(r.get("ticket_id") != "pytest-T-7" for r in rows), (
        "Cross-tenant row pytest-T-7 (tenant_id='other') leaked into results"
    )


def test_read_pending_drafts_filters_by_routine_name(seeded_drafts) -> None:
    """Cross-routine ('pytest-digest') row excluded by routine_name filter."""
    close_pool()
    rows = client.read_pending_drafts("pytest-tickets-process", since_hours=48)
    close_pool()
    assert all(r.get("ticket_id") != "pytest-T-8" for r in rows), (
        "Cross-routine row pytest-T-8 (routine='pytest-digest') leaked into results"
    )


def test_read_pending_drafts_returns_parsed_jsonb_for_snapshot(seeded_drafts) -> None:
    """hubspot_ticket_snapshot returns as parsed dict (not JSON string) thanks to dict_row + jsonb."""
    close_pool()
    rows = client.read_pending_drafts("pytest-tickets-process", since_hours=48)
    close_pool()
    pytest_rows = [r for r in rows if r["ticket_id"].startswith("pytest-T-")]
    assert len(pytest_rows) >= 1, "Need at least one pytest-prefixed row to validate snapshot shape"
    for r in pytest_rows:
        snap = r["hubspot_ticket_snapshot"]
        assert isinstance(snap, dict), f"Expected dict for snapshot, got {type(snap).__name__}"
        assert "subject" in snap
        assert "category" in snap


def test_read_pending_drafts_orders_by_created_at_asc(seeded_drafts) -> None:
    """Rows ordered by created_at ASC (oldest first) for consistent digest rendering."""
    close_pool()
    rows = client.read_pending_drafts("pytest-tickets-process", since_hours=48)
    close_pool()
    timestamps = [r["created_at"] for r in rows if r["ticket_id"].startswith("pytest-T-")]
    assert timestamps == sorted(timestamps), (
        f"Expected ASC order, got: {timestamps}"
    )
