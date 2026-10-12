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


# ============================================================================
# Phase 93 (plan 14) — replace_pending_draft, write_cron_draft,
# rollback_cycle_entries.
#
# One pending draft per ticket, decided by one transaction under a per-ticket
# advisory lock. Every row these tests write has a `pytest-` routine name, and
# every ticket they point at is made (and removed) by the `seeded_ticket`
# fixtures. The tests connect as the table owner, so a privilege the cron's
# role lacks cannot show up here: the supersede's column list is therefore
# checked on the statement actually sent.
# ============================================================================

import contextlib  # noqa: E402
import inspect  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402
from typing import Iterator  # noqa: E402

from psycopg.rows import dict_row  # noqa: E402

ROUTINE = "pytest-tickets-process"
ON_DEMAND_ROUTINE = "pytest-packn_os.task_reply"
BASE_VERSION = "v3.3.0"
MODEL = "claude-sonnet-5"
GRANTED_COLUMNS = {"state", "superseded_at", "superseded_by_draft_id", "updated_at"}


@pytest.fixture
def drafts_db(
    db_conn: psycopg.Connection, cleanup_test_data: None
) -> Iterator[psycopg.Connection]:
    """The owner connection for seeding and reading, with a fresh helper pool."""
    close_pool()
    yield db_conn
    close_pool()


def _rows(conn: psycopg.Connection, sql: str, params: tuple = ()) -> list[dict]:
    """Run one statement, hand back its rows, and end the transaction.

    A statement that fails is rolled back before the error is passed on, so the
    connection is usable by the fixtures that clean up after the test.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    return rows


def _drafts_of(conn: psycopg.Connection, ticket: dict) -> list[dict]:
    """Every draft of the ticket, by text key or by OS id, oldest first."""
    return _rows(
        conn,
        """
        SELECT id::text AS id, routine_name, ticket_id, os_ticket_id::text AS os_ticket_id,
               prompt_version, state::text AS state, superseded_at,
               superseded_by_draft_id::text AS superseded_by_draft_id,
               updated_at, created_at, deleted_at, draft_body
          FROM automation_drafts
         WHERE tenant_id = %s AND (ticket_id = %s OR os_ticket_id = %s::uuid)
         ORDER BY created_at, id
        """,
        (TENANT_ID, ticket["draft_key"], ticket["id"]),
    )


def _pending_count(conn: psycopg.Connection, ticket: dict) -> int:
    """A direct count of the ticket's live pending drafts."""
    rows = _rows(
        conn,
        """
        SELECT count(*) AS n FROM automation_drafts
         WHERE tenant_id = %s AND state = 'pending' AND deleted_at IS NULL
           AND (ticket_id = %s OR os_ticket_id = %s::uuid)
        """,
        (TENANT_ID, ticket["draft_key"], ticket["id"]),
    )
    return rows[0]["n"]


def _draft(conn: psycopg.Connection, draft_id: str) -> dict:
    rows = _rows(
        conn,
        """
        SELECT id::text AS id, routine_name, ticket_id, os_ticket_id::text AS os_ticket_id,
               prompt_version, state::text AS state, superseded_at,
               superseded_by_draft_id::text AS superseded_by_draft_id, updated_at,
               deleted_at, draft_body
          FROM automation_drafts WHERE id = %s::uuid
        """,
        (draft_id,),
    )
    assert len(rows) == 1, f"no draft {draft_id}"
    return rows[0]


def _seed_draft(
    conn: psycopg.Connection,
    *,
    ticket_key: str,
    os_ticket_id,
    prompt_version: str,
    state: str = "pending",
    routine: str = ROUTINE,
    deleted: bool = False,
) -> str:
    """Insert one draft row directly (the owner connection), in any state."""
    rows = _rows(
        conn,
        """
        INSERT INTO automation_drafts
          (tenant_id, routine_name, ticket_id, os_ticket_id, draft_body, model,
           prompt_version, hubspot_ticket_snapshot, state, deleted_at)
        VALUES (%s, %s, %s, %s::uuid, %s, %s, %s, %s::jsonb, %s::automation_draft_state,
                CASE WHEN %s THEN now() ELSE NULL END)
        RETURNING id::text AS id
        """,
        (
            TENANT_ID,
            routine,
            ticket_key,
            os_ticket_id,
            f"seeded {state}",
            MODEL,
            prompt_version,
            json.dumps(_make_snapshot()),
            state,
            deleted,
        ),
    )
    return rows[0]["id"]


def _replace(ticket: dict, cycle_key: str, **overrides) -> dict:
    arguments = dict(
        draft_key=ticket["draft_key"],
        os_ticket_id=ticket["id"],
        routine_name=ROUTINE,
        draft_body=f"body for {cycle_key}",
        model=MODEL,
        base_prompt_version=BASE_VERSION,
        cycle_key=cycle_key,
        hubspot_ticket_snapshot=_make_snapshot(),
    )
    arguments.update(overrides)
    return client.replace_pending_draft(**arguments)


def _cron_write(ticket_key: str, body: str = "cron body") -> str:
    """write_cron_draft with the positional arguments the skill passes."""
    return client.write_cron_draft(
        ticket_key, ROUTINE, body, MODEL, BASE_VERSION, _make_snapshot()
    )


def _use_context(monkeypatch: pytest.MonkeyPatch, tmp_path, context) -> None:
    """Point PACKN_RUN_CONTEXT at a file holding `context` (written as JSON)."""
    path = tmp_path / f"run-context-{uuid.uuid4().hex[:8]}.json"
    path.write_text(json.dumps(context), encoding="utf-8")
    monkeypatch.setenv(client.RUN_CONTEXT_ENV, str(path))


@pytest.fixture
def recorded(
    monkeypatch: pytest.MonkeyPatch, test_db_url: str
) -> Iterator[list]:
    """Every statement the client sends, as (text, params), in order.

    The client is handed a pool of one real connection whose cursors record
    before they execute, so what is compared is what reached the server.
    """
    statements: list = []

    class RecordingCursor(psycopg.Cursor):
        def execute(self, query, params=None, **kwargs):
            statements.append((str(query), None if params is None else tuple(params)))
            return super().execute(query, params, **kwargs)

    conn = psycopg.connect(test_db_url, row_factory=dict_row, cursor_factory=RecordingCursor)

    class OneConnectionPool:
        @contextlib.contextmanager
        def connection(self):
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()

    monkeypatch.setattr(client, "get_pool", lambda: OneConnectionPool())
    try:
        yield statements
    finally:
        conn.close()


def _is_drafts_update(statement: str) -> bool:
    return re.match(r"\s*UPDATE\s+automation_drafts\b", statement, re.IGNORECASE) is not None


def _assigned_columns(statement: str) -> list[str]:
    """The columns an `UPDATE automation_drafts SET … WHERE …` assigns, in order."""
    match = re.search(
        r"\bUPDATE\s+automation_drafts\s+SET\b(.*?)\bWHERE\b",
        statement,
        re.IGNORECASE | re.DOTALL,
    )
    assert match is not None, "not an UPDATE automation_drafts … SET … WHERE statement"
    parts: list[str] = []
    depth = 0
    current = ""
    for char in match.group(1):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return [part.split("=", 1)[0].strip().strip('"').lower() for part in parts if part.strip()]


# ------------------------------------------------------------------ names and the lock key


def test_the_lock_key_is_the_string_the_os_route_hashes() -> None:
    """src/app/api/tasks/reply-draft/route.ts: `task_reply_draft:${TENANT_ID}:${draftKey}`
    through hashtextextended(…, 0). A different string would not serialise with it."""
    assert client.DRAFT_LOCK_PREFIX == "task_reply_draft"
    assert client.draft_lock_key("48211900123") == f"task_reply_draft:{TENANT_ID}:48211900123"
    assert client.DRAFT_LOCK_SQL == "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"


def test_the_cycle_and_context_names() -> None:
    assert client.ROLLBACK_CYCLE_PREFIX == "hubspot"
    assert client.CYCLE_SEPARATOR == "#"
    assert client.RUN_CONTEXT_ENV == "PACKN_RUN_CONTEXT"
    assert inspect.signature(client.rollback_cycle_entries).parameters[
        "routine_name"
    ].default == "tickets-process"


def test_write_cron_draft_takes_write_drafts_positional_parameters() -> None:
    plain = list(inspect.signature(client.write_draft).parameters)
    assert plain == [
        "ticket_id",
        "routine_name",
        "draft_body",
        "model",
        "prompt_version",
        "hubspot_ticket_snapshot",
    ]
    assert list(inspect.signature(client.write_cron_draft).parameters) == plain


def test_replace_pending_draft_is_keyword_only() -> None:
    parameters = inspect.signature(client.replace_pending_draft).parameters
    assert list(parameters) == [
        "draft_key",
        "os_ticket_id",
        "routine_name",
        "draft_body",
        "model",
        "base_prompt_version",
        "cycle_key",
        "hubspot_ticket_snapshot",
    ]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters.values())


# ------------------------------------------------------------------ replace: the passes


def test_replace_first_pass_inserts_one_pending_cycle_keyed_row(
    drafts_db: psycopg.Connection, seeded_ticket: dict
) -> None:
    result = _replace(seeded_ticket, "A")

    assert result["created"] is True
    assert result["superseded"] == 0
    uuid.UUID(result["draft_id"])
    rows = _drafts_of(drafts_db, seeded_ticket)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == result["draft_id"]
    assert row["state"] == "pending"
    assert row["prompt_version"] == "v3.3.0#A"
    assert row["ticket_id"] == seeded_ticket["hubspot_ticket_id"]
    assert row["os_ticket_id"] == seeded_ticket["id"]
    assert row["routine_name"] == ROUTINE
    assert row["draft_body"] == "body for A"
    assert row["superseded_at"] is None and row["superseded_by_draft_id"] is None


def test_replace_second_pass_supersedes_the_first_and_points_it_at_the_new_row(
    drafts_db: psycopg.Connection, seeded_ticket: dict
) -> None:
    """SPEC R12 acceptance: two passes, different cycles."""
    first = _replace(seeded_ticket, "A")
    before = _draft(drafts_db, first["draft_id"])
    second = _replace(seeded_ticket, "B")

    assert second["created"] is True
    assert second["superseded"] == 1
    assert second["draft_id"] != first["draft_id"]
    old = _draft(drafts_db, first["draft_id"])
    new = _draft(drafts_db, second["draft_id"])
    assert old["state"] == "superseded"
    assert old["superseded_at"] is not None
    assert old["superseded_by_draft_id"] == second["draft_id"]
    assert old["updated_at"] > before["updated_at"]
    assert old["draft_body"] == "body for A"  # nothing else about the old row moved
    assert new["state"] == "pending"
    assert new["prompt_version"] == "v3.3.0#B"
    assert _pending_count(drafts_db, seeded_ticket) == 1
    assert len(_drafts_of(drafts_db, seeded_ticket)) == 2


def test_replace_same_cycle_again_is_a_replay(
    drafts_db: psycopg.Connection, seeded_ticket: dict
) -> None:
    first = _replace(seeded_ticket, "A")
    second = _replace(seeded_ticket, "B")
    snapshot = _drafts_of(drafts_db, seeded_ticket)

    again = _replace(seeded_ticket, "B", draft_body="a different body on the replay")

    assert again == {"draft_id": second["draft_id"], "created": False, "superseded": 0}
    assert _drafts_of(drafts_db, seeded_ticket) == snapshot
    assert _draft(drafts_db, first["draft_id"])["superseded_by_draft_id"] == second["draft_id"]
    assert _pending_count(drafts_db, seeded_ticket) == 1


def test_replace_a_rerun_cycle_with_no_newer_signal_still_yields_a_fresh_draft(
    drafts_db: psycopg.Connection, seeded_ticket: dict
) -> None:
    """SPEC Edge (adjacency R12): cycle key `rerun:<request id>`."""
    signal = _replace(seeded_ticket, str(uuid.uuid4()))
    rerun_key = f"rerun:{uuid.uuid4()}"

    rerun = _replace(seeded_ticket, rerun_key)

    assert rerun["created"] is True and rerun["superseded"] == 1
    assert _draft(drafts_db, signal["draft_id"])["state"] == "superseded"
    fresh = _draft(drafts_db, rerun["draft_id"])
    assert fresh["state"] == "pending"
    assert fresh["prompt_version"] == f"v3.3.0#{rerun_key}"
    assert _pending_count(drafts_db, seeded_ticket) == 1


# ------------------------------------------------------------------ replace: what it leaves alone


def test_replace_leaves_sent_approved_and_rejected_drafts_untouched(
    drafts_db: psycopg.Connection, seeded_ticket: dict
) -> None:
    """SPEC Edge (empty R12)."""
    kept = {
        state: _seed_draft(
            drafts_db,
            ticket_key=seeded_ticket["draft_key"],
            os_ticket_id=seeded_ticket["id"],
            prompt_version=f"v3.3.0#old-{state}",
            state=state,
        )
        for state in ("sent", "approved", "rejected", "superseded")
    }
    before = {state: _draft(drafts_db, draft_id) for state, draft_id in kept.items()}

    result = _replace(seeded_ticket, "A")

    assert result["superseded"] == 0
    for state, draft_id in kept.items():
        assert _draft(drafts_db, draft_id) == before[state], state
    assert _pending_count(drafts_db, seeded_ticket) == 1


def test_replace_leaves_a_soft_deleted_pending_draft_untouched(
    drafts_db: psycopg.Connection, seeded_ticket: dict
) -> None:
    deleted = _seed_draft(
        drafts_db,
        ticket_key=seeded_ticket["draft_key"],
        os_ticket_id=seeded_ticket["id"],
        prompt_version="v3.3.0#deleted",
        deleted=True,
    )
    before = _draft(drafts_db, deleted)

    result = _replace(seeded_ticket, "A")

    assert result["superseded"] == 0
    assert _draft(drafts_db, deleted) == before


def test_replace_leaves_another_tickets_pending_draft_untouched(
    drafts_db: psycopg.Connection, seeded_tickets
) -> None:
    ours = seeded_tickets()
    theirs = seeded_tickets()
    other = _replace(theirs, "A")
    # A draft of a third ticket written the old way: no OS id at all. A supersede
    # that compared a missing OS id as "equal" would sweep this row up.
    unlinked = _seed_draft(
        drafts_db,
        ticket_key=f"pytest-unlinked-{uuid.uuid4().hex[:8]}",
        os_ticket_id=None,
        prompt_version=BASE_VERSION,
    )
    before = (_draft(drafts_db, other["draft_id"]), _draft(drafts_db, unlinked))

    _replace(ours, "A")
    _replace(ours, "B")
    _replace(ours, "C", os_ticket_id=None)

    assert (_draft(drafts_db, other["draft_id"]), _draft(drafts_db, unlinked)) == before
    assert _pending_count(drafts_db, theirs) == 1
    assert _pending_count(drafts_db, ours) == 1


# ------------------------------------------------------------------ replace: what it does replace


def test_replace_supersedes_a_pending_on_demand_draft_of_the_same_ticket(
    drafts_db: psycopg.Connection, seeded_ticket: dict
) -> None:
    """Q6: a cron pass replaces an operator's generated-but-unsent draft."""
    on_demand = _seed_draft(
        drafts_db,
        ticket_key=seeded_ticket["draft_key"],
        os_ticket_id=seeded_ticket["id"],
        prompt_version="packn_os.task_reply.v1#cycle-1",
        routine=ON_DEMAND_ROUTINE,
    )

    result = _replace(seeded_ticket, "A")

    assert result["superseded"] == 1
    old = _draft(drafts_db, on_demand)
    assert old["state"] == "superseded"
    assert old["superseded_by_draft_id"] == result["draft_id"]
    assert old["routine_name"] == ON_DEMAND_ROUTINE
    assert _pending_count(drafts_db, seeded_ticket) == 1


def test_replace_supersedes_a_draft_written_the_old_way(
    drafts_db: psycopg.Connection, seeded_ticket: dict
) -> None:
    """HubSpot id as the text key, no OS id, the bare prompt version."""
    old_id = client.write_draft(
        seeded_ticket["hubspot_ticket_id"], ROUTINE, "old way", MODEL, BASE_VERSION, _make_snapshot()
    )
    assert _draft(drafts_db, old_id)["os_ticket_id"] is None

    result = _replace(seeded_ticket, "A")

    assert result["superseded"] == 1
    old = _draft(drafts_db, old_id)
    assert old["state"] == "superseded"
    assert old["superseded_by_draft_id"] == result["draft_id"]
    assert old["prompt_version"] == BASE_VERSION
    assert _pending_count(drafts_db, seeded_ticket) == 1


def test_replace_supersedes_a_draft_linked_by_the_os_id_alone(
    drafts_db: psycopg.Connection, seeded_ticket: dict
) -> None:
    """A draft keyed by the OS id as text (written before the ticket had a
    HubSpot id) is the same ticket's: the OS sibling says so."""
    by_os_id = _seed_draft(
        drafts_db,
        ticket_key=seeded_ticket["id"],
        os_ticket_id=seeded_ticket["id"],
        prompt_version="packn_os.task_reply.v1#cycle-1",
        routine=ON_DEMAND_ROUTINE,
    )

    result = _replace(seeded_ticket, "A")

    assert result["superseded"] == 1
    assert _draft(drafts_db, by_os_id)["superseded_by_draft_id"] == result["draft_id"]


def test_replace_for_a_ticket_with_no_hubspot_id_keys_the_draft_by_the_os_id(
    drafts_db: psycopg.Connection, seeded_ticket_without_hubspot_id: dict
) -> None:
    """SPEC R11 / Phase 92.2 R10: the text key is the OS id as text."""
    ticket = seeded_ticket_without_hubspot_id
    assert ticket["draft_key"] == ticket["id"]

    first = _replace(ticket, "A")
    second = _replace(ticket, "B")

    row = _draft(drafts_db, second["draft_id"])
    assert row["ticket_id"] == ticket["id"]
    assert row["os_ticket_id"] == ticket["id"]
    assert _draft(drafts_db, first["draft_id"])["state"] == "superseded"
    assert _pending_count(drafts_db, ticket) == 1


# ------------------------------------------------------------------ replace: refused input


@pytest.mark.parametrize("missing", ["category", "captured_at"])
def test_replace_refuses_a_snapshot_missing_a_required_key(
    drafts_db: psycopg.Connection, seeded_ticket: dict, missing: str
) -> None:
    snapshot = _make_snapshot()
    del snapshot[missing]

    with pytest.raises(ValueError, match=missing):
        _replace(seeded_ticket, "A", hubspot_ticket_snapshot=snapshot)

    assert _drafts_of(drafts_db, seeded_ticket) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"draft_key": ""},
        {"draft_key": None},
        {"cycle_key": ""},
        {"cycle_key": None},
        {"base_prompt_version": ""},
        {"base_prompt_version": "v3.3.0#already-suffixed"},
        {"os_ticket_id": "not-a-uuid"},
        {"os_ticket_id": "48211900123"},
    ],
)
def test_replace_refuses_arguments_it_cannot_key_a_draft_by(
    drafts_db: psycopg.Connection, seeded_ticket: dict, overrides: dict
) -> None:
    arguments = {"cycle_key": "A"}
    arguments.update(overrides)
    cycle_key = arguments.pop("cycle_key")

    with pytest.raises(ValueError):
        _replace(seeded_ticket, cycle_key, **arguments)

    assert _drafts_of(drafts_db, seeded_ticket) == []


# ------------------------------------------------------------------ the lock


def test_replace_waits_for_the_tickets_advisory_lock(
    drafts_db: psycopg.Connection, seeded_ticket: dict, test_db_url: str
) -> None:
    """Checker CRON-W1. This case fails when the lock statement is removed.

    The test holds the ticket's advisory lock in its own transaction, with the
    statement and key the function uses. A replace started on a thread must not
    finish, and must write nothing another connection can see, until the test
    ends that transaction.
    """
    cycle = "blocked-cycle"
    outcome: dict = {}

    def work() -> None:
        try:
            outcome["result"] = _replace(seeded_ticket, cycle)
        except BaseException as error:  # noqa: BLE001 - reported by the test thread
            outcome["error"] = error

    def visible_rows(watcher: psycopg.Connection) -> int:
        return watcher.execute(
            "SELECT count(*) AS n FROM automation_drafts "
            "WHERE tenant_id = %s AND ticket_id = %s AND prompt_version = %s",
            (TENANT_ID, seeded_ticket["draft_key"], f"{BASE_VERSION}#{cycle}"),
        ).fetchone()["n"]

    holder = psycopg.connect(test_db_url)
    watcher = psycopg.connect(test_db_url, autocommit=True, row_factory=dict_row)
    thread = threading.Thread(target=work, daemon=True)
    try:
        holder.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"task_reply_draft:{TENANT_ID}:{seeded_ticket['draft_key']}",),
        )
        thread.start()
        thread.join(0.5)

        assert thread.is_alive(), f"the replace did not wait for the lock: {outcome}"
        assert visible_rows(watcher) == 0

        holder.rollback()  # ends the transaction, which releases the lock
        thread.join(5)

        assert not thread.is_alive(), "the replace did not finish after the lock was released"
        assert "error" not in outcome, outcome.get("error")
        assert outcome["result"]["created"] is True
        assert visible_rows(watcher) == 1
    finally:
        holder.close()  # releases the lock whatever happened above
        thread.join(5)
        watcher.close()


def test_replace_takes_the_lock_before_it_writes(
    recorded: list, drafts_db: psycopg.Connection, seeded_ticket: dict
) -> None:
    _replace(seeded_ticket, "A")

    assert recorded[0] == (
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"task_reply_draft:{TENANT_ID}:{seeded_ticket['draft_key']}",),
    )
    assert "INSERT INTO automation_drafts" in recorded[1][0]


def test_two_overlapping_passes_leave_exactly_one_pending_draft(
    drafts_db: psycopg.Connection, seeded_ticket: dict
) -> None:
    """SPEC Edge (concurrency) and R15 acceptance, ten times over."""
    # Build the helper's pool before the threads start. Its lazy init is not
    # thread-safe (the cron is one thread), and two threads racing to build it
    # would each make a pool; that is a fact about this test, not the replace.
    client.get_pool()
    for repetition in range(10):
        barrier = threading.Barrier(2)
        errors: list = []

        def work(cycle_key: str) -> None:
            try:
                barrier.wait(timeout=10)
                _replace(seeded_ticket, cycle_key)
            except BaseException as error:  # noqa: BLE001 - reported by the test thread
                errors.append(error)

        threads = [
            threading.Thread(target=work, args=(f"race-{repetition}-{side}",), daemon=True)
            for side in ("left", "right")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)

        assert not any(thread.is_alive() for thread in threads), f"repetition {repetition} hung"
        assert errors == [], f"repetition {repetition}: {errors}"
        assert _pending_count(drafts_db, seeded_ticket) == 1, f"repetition {repetition}"
        assert len(_drafts_of(drafts_db, seeded_ticket)) == 2 * (repetition + 1)


# ------------------------------------------------------------------ the supersede's columns


def test_the_parser_counts_a_fifth_column() -> None:
    """Checker CRON-W2: the check below is only as good as this parser."""
    five = (
        "UPDATE automation_drafts\n"
        "   SET state = 'superseded', superseded_at = now(),\n"
        "       superseded_by_draft_id = %s, updated_at = now(),\n"
        "       draft_body = coalesce(draft_body, 'x, y')\n"
        " WHERE tenant_id = %s AND id <> %s"
    )
    assert _assigned_columns(five) == [
        "state",
        "superseded_at",
        "superseded_by_draft_id",
        "updated_at",
        "draft_body",
    ]
    assert len(_assigned_columns(five)) == 5
    assert set(_assigned_columns(five)) != GRANTED_COLUMNS


def test_the_supersede_assigns_only_the_four_granted_columns(
    recorded: list, drafts_db: psycopg.Connection, seeded_ticket: dict
) -> None:
    """Checker CRON-W2, on the statement actually sent. The cron's role holds
    UPDATE on these four columns and no other (OS migration 0181)."""
    _replace(seeded_ticket, "A")
    del recorded[:]

    result = _replace(seeded_ticket, "B")

    assert result["superseded"] == 1
    updates = [text for text, _params in recorded if _is_drafts_update(text)]
    assert len(updates) == 1
    assigned = _assigned_columns(updates[0])
    assert set(assigned) == GRANTED_COLUMNS
    assert len(assigned) == 4
    assert not any(re.search(r"\bRETURNING\b", text, re.IGNORECASE) for text in updates)
    assert not any(re.match(r"\s*DELETE\b", text, re.IGNORECASE) for text, _ in recorded)


def test_a_replay_sends_no_update(
    recorded: list, drafts_db: psycopg.Connection, seeded_ticket: dict
) -> None:
    _replace(seeded_ticket, "A")
    _replace(seeded_ticket, "B")
    del recorded[:]

    again = _replace(seeded_ticket, "B")

    assert again["created"] is False
    assert len(recorded) >= 2  # the lock and the insert were still sent
    assert not any(_is_drafts_update(text) for text, _params in recorded)


def test_the_module_has_one_update_on_the_drafts_table() -> None:
    source = inspect.getsource(client)
    assert len(re.findall(r"UPDATE\s+automation_drafts\b", source)) == 1


# ------------------------------------------------------------------ write_cron_draft: no cycle


def _context_shapes(tmp_path, ticket_key: str) -> dict:
    """Every way a run context can fail to name a cycle for this ticket."""
    missing = tmp_path / "no-such-file.json"
    not_json = tmp_path / "not-json.json"
    not_json.write_text("{this is not json", encoding="utf-8")
    shapes = {"env_unset": None, "file_missing": str(missing), "file_not_json": str(not_json)}
    for name, content in {
        "not_an_object": ["a", "list"],
        "empty_object": {},
        "another_ticket": {"pytest-some-other-ticket": {"os_ticket_id": None, "cycle_key": "X"}},
        "null_cycle_key": {ticket_key: {"os_ticket_id": None, "cycle_key": None}},
        "empty_cycle_key": {ticket_key: {"os_ticket_id": None, "cycle_key": ""}},
        "cycle_key_not_text": {ticket_key: {"os_ticket_id": None, "cycle_key": 7}},
        "no_cycle_key_at_all": {ticket_key: {"os_ticket_id": None}},
        "entry_not_an_object": {ticket_key: "X"},
        "entry_null": {ticket_key: None},
    }.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(content), encoding="utf-8")
        shapes[name] = str(path)
    return shapes


CONTEXT_SHAPE_NAMES = [
    "env_unset",
    "file_missing",
    "file_not_json",
    "not_an_object",
    "empty_object",
    "another_ticket",
    "null_cycle_key",
    "empty_cycle_key",
    "cycle_key_not_text",
    "no_cycle_key_at_all",
    "entry_not_an_object",
    "entry_null",
]


@pytest.mark.parametrize("shape", CONTEXT_SHAPE_NAMES)
def test_write_cron_draft_with_no_cycle_sends_exactly_write_drafts_statements(
    recorded: list,
    drafts_db: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    shape: str,
) -> None:
    """Byte for byte (checker CRON-B2): the same statement texts, in the same
    order, with the same parameters, and no other statement, on a first write
    and on a replay. A cron deployed before the owner runs the grant step
    therefore cannot break a HubSpot-mode draft write."""
    ticket_key = f"pytest-plain-{uuid.uuid4().hex[:8]}"
    arguments = (ticket_key, ROUTINE, "hello", MODEL, BASE_VERSION, _make_snapshot())
    path = _context_shapes(tmp_path, ticket_key)[shape]
    monkeypatch.delenv(client.RUN_CONTEXT_ENV, raising=False)

    plain_id = client.write_draft(*arguments)
    plain_first = list(recorded)
    del recorded[:]
    assert client.write_draft(*arguments) == plain_id
    plain_replay = list(recorded)
    del recorded[:]
    _rows(drafts_db, "DELETE FROM automation_drafts WHERE id = %s::uuid", (plain_id,))

    if path is not None:
        monkeypatch.setenv(client.RUN_CONTEXT_ENV, path)
    cron_id = client.write_cron_draft(*arguments)
    cron_first = list(recorded)
    del recorded[:]
    assert client.write_cron_draft(*arguments) == cron_id
    cron_replay = list(recorded)

    assert len(plain_first) == 1 and len(plain_replay) == 2
    assert cron_first == plain_first
    assert cron_replay == plain_replay
    uuid.UUID(cron_id)
    row = _draft(drafts_db, cron_id)
    assert row["prompt_version"] == BASE_VERSION
    assert row["os_ticket_id"] is None
    assert row["state"] == "pending"


# ------------------------------------------------------------------ write_cron_draft: a cycle


def test_write_cron_draft_with_a_cycle_goes_through_the_replace(
    drafts_db: psycopg.Connection,
    seeded_ticket: dict,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    signal_id = str(uuid.uuid4())
    earlier = _replace(seeded_ticket, "earlier-cycle")
    _use_context(
        monkeypatch,
        tmp_path,
        {
            seeded_ticket["draft_key"]: {
                "os_ticket_id": seeded_ticket["id"],
                "cycle_key": signal_id,
                "signal_at": "2026-10-09T14:07:03.120Z",
                "signal_id": signal_id,
            }
        },
    )

    draft_id = _cron_write(seeded_ticket["draft_key"])

    row = _draft(drafts_db, draft_id)
    assert row["prompt_version"] == f"v3.3.0#{signal_id}"
    assert row["os_ticket_id"] == seeded_ticket["id"]
    assert row["ticket_id"] == seeded_ticket["hubspot_ticket_id"]
    assert row["state"] == "pending"
    assert _draft(drafts_db, earlier["draft_id"])["superseded_by_draft_id"] == draft_id
    assert _pending_count(drafts_db, seeded_ticket) == 1
    assert _cron_write(seeded_ticket["draft_key"]) == draft_id  # the same cycle is a replay


def test_write_cron_draft_passes_a_bad_os_id_on_as_an_error(
    drafts_db: psycopg.Connection,
    seeded_ticket: dict,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A context entry that names a cycle but a broken OS id is not quietly
    written as a plain draft: that would be a second pending draft."""
    _use_context(
        monkeypatch,
        tmp_path,
        {seeded_ticket["draft_key"]: {"os_ticket_id": "not-a-uuid", "cycle_key": "A"}},
    )

    with pytest.raises(ValueError):
        _cron_write(seeded_ticket["draft_key"])

    assert _drafts_of(drafts_db, seeded_ticket) == []


# ------------------------------------------------------------------ rollback_cycle_entries


def test_rollback_entries_name_a_ticket_with_a_cycle_keyed_pending_draft(
    drafts_db: psycopg.Connection, seeded_ticket: dict
) -> None:
    _replace(seeded_ticket, str(uuid.uuid4()))
    run_id = str(uuid.uuid4())

    entries = client.rollback_cycle_entries(run_id, ROUTINE)

    assert entries == {
        seeded_ticket["draft_key"]: {
            "os_ticket_id": seeded_ticket["id"],
            "cycle_key": f"hubspot:{run_id}",
            "signal_at": None,
            "signal_id": None,
        }
    }


def test_rollback_entries_carry_a_missing_os_id_as_none(
    drafts_db: psycopg.Connection,
) -> None:
    ticket_key = f"pytest-unlinked-{uuid.uuid4().hex[:8]}"
    _seed_draft(drafts_db, ticket_key=ticket_key, os_ticket_id=None, prompt_version="v3.3.0#c1")

    entries = client.rollback_cycle_entries("run-1", ROUTINE)

    assert entries == {
        ticket_key: {
            "os_ticket_id": None,
            "cycle_key": "hubspot:run-1",
            "signal_at": None,
            "signal_id": None,
        }
    }


def test_rollback_entries_skip_everything_that_is_not_a_live_cycle_keyed_pending_cron_draft(
    drafts_db: psycopg.Connection, seeded_tickets
) -> None:
    bare = seeded_tickets()
    client.write_draft(bare["draft_key"], ROUTINE, "bare", MODEL, BASE_VERSION, _make_snapshot())
    for state in ("superseded", "sent", "approved", "rejected"):
        ticket = seeded_tickets()
        _seed_draft(
            drafts_db,
            ticket_key=ticket["draft_key"],
            os_ticket_id=ticket["id"],
            prompt_version=f"v3.3.0#{state}-cycle",
            state=state,
        )
    other_routine = seeded_tickets()
    _seed_draft(
        drafts_db,
        ticket_key=other_routine["draft_key"],
        os_ticket_id=other_routine["id"],
        prompt_version="packn_os.task_reply.v1#cycle-1",
        routine=ON_DEMAND_ROUTINE,
    )
    soft_deleted = seeded_tickets()
    _seed_draft(
        drafts_db,
        ticket_key=soft_deleted["draft_key"],
        os_ticket_id=soft_deleted["id"],
        prompt_version="v3.3.0#deleted-cycle",
        deleted=True,
    )
    live = seeded_tickets()
    _replace(live, "the-one-live-cycle")

    entries = client.rollback_cycle_entries("run-1", ROUTINE)

    assert list(entries) == [live["draft_key"]]


def test_rollback_entries_are_one_select_and_no_write(
    recorded: list, drafts_db: psycopg.Connection, seeded_ticket: dict
) -> None:
    _replace(seeded_ticket, "A")
    del recorded[:]

    entries = client.rollback_cycle_entries("run-1", ROUTINE)

    assert len(entries) == 1
    assert len(recorded) == 1
    text, params = recorded[0]
    assert re.match(r"\s*SELECT\b", text, re.IGNORECASE)
    assert not re.search(r"\b(INSERT|UPDATE|DELETE)\b", text, re.IGNORECASE)
    assert TENANT_ID in params and ROUTINE in params


@pytest.mark.parametrize("run_id", ["", None, 7])
def test_rollback_entries_refuse_a_run_id_that_is_not_text(run_id) -> None:
    with pytest.raises(ValueError):
        client.rollback_cycle_entries(run_id, ROUTINE)


# ------------------------------------------------------------------ across modes


def _hubspot_mode_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path, ticket: dict, run_id: str
) -> str:
    """What a HubSpot-mode run does: read the rollback entries once, put them in
    the run context, then write the ticket's draft by its HubSpot id."""
    _use_context(monkeypatch, tmp_path, client.rollback_cycle_entries(run_id, ROUTINE))
    return _cron_write(ticket["hubspot_ticket_id"], body=f"hubspot-mode body {run_id}")


def test_a_flip_back_to_hubspot_keeps_one_pending_draft(
    drafts_db: psycopg.Connection,
    seeded_ticket: dict,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Checker CRON-B2, the acceptance case: an `os`-mode write, then a
    `hubspot`-mode write on the same ticket."""
    os_mode = _replace(seeded_ticket, str(uuid.uuid4()))
    run_id = str(uuid.uuid4())

    new_id = _hubspot_mode_write(monkeypatch, tmp_path, seeded_ticket, run_id)

    assert _pending_count(drafts_db, seeded_ticket) == 1
    old = _draft(drafts_db, os_mode["draft_id"])
    assert old["state"] == "superseded"
    assert old["superseded_by_draft_id"] == new_id
    new = _draft(drafts_db, new_id)
    assert new["state"] == "pending"
    assert new["prompt_version"] == f"v3.3.0#hubspot:{run_id}"
    assert new["os_ticket_id"] == seeded_ticket["id"]


def test_one_pending_draft_after_every_step_of_a_sequence_across_modes(
    drafts_db: psycopg.Connection,
    seeded_ticket: dict,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """os, hubspot, hubspot again in a later run, then os for a newer signal."""
    _replace(seeded_ticket, str(uuid.uuid4()))
    assert _pending_count(drafts_db, seeded_ticket) == 1

    _hubspot_mode_write(monkeypatch, tmp_path, seeded_ticket, str(uuid.uuid4()))
    assert _pending_count(drafts_db, seeded_ticket) == 1

    _hubspot_mode_write(monkeypatch, tmp_path, seeded_ticket, str(uuid.uuid4()))
    assert _pending_count(drafts_db, seeded_ticket) == 1

    last = _replace(seeded_ticket, str(uuid.uuid4()))
    assert _pending_count(drafts_db, seeded_ticket) == 1

    rows = _drafts_of(drafts_db, seeded_ticket)
    assert len(rows) == 4
    assert [row["state"] for row in rows].count("superseded") == 3
    assert _draft(drafts_db, last["draft_id"])["state"] == "pending"


def test_without_the_rollback_entries_a_second_pending_draft_appears(
    drafts_db: psycopg.Connection,
    seeded_ticket: dict,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """The residual, written down: this is what the rollback entries prevent.
    A HubSpot-mode write with an empty run context inserts a bare-version row
    beside the cycle-keyed one."""
    _replace(seeded_ticket, str(uuid.uuid4()))
    _use_context(monkeypatch, tmp_path, {})

    _cron_write(seeded_ticket["hubspot_ticket_id"])

    assert _pending_count(drafts_db, seeded_ticket) == 2
