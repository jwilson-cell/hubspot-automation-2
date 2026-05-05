"""HUB-02/06 - write_run_record contract.

Activated by Plan 04-03 Task 2. Tests run against the live dev Postgres DB.
Test routine names use the 'pytest-' prefix so the cleanup is targeted.
"""

import psycopg

from packn_os_hubspot_client import client
from packn_os_hubspot_client.db import close_pool


def test_write_run_record_inserts_with_correct_status_enum(
    db_conn: psycopg.Connection, cleanup_test_data: None
) -> None:
    """status accepts 'success', 'failure', 'skipped', 'partial' - matches
    automationRunStatusEnum."""
    close_pool()
    for status in ("success", "failure", "skipped", "partial"):
        client.write_run_record(
            routine_name=f"pytest-routine-{status}",
            status=status,
            error_summary=None,
            tickets_processed=0,
            drafts_created=0,
            started_at_iso="2026-05-03T14:00:00+00:00",
            finished_at_iso="2026-05-03T14:00:30+00:00",
        )
    close_pool()

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT routine_name, status FROM automation_runs
            WHERE routine_name LIKE 'pytest-routine-%'
            ORDER BY routine_name
            """,
        )
        rows = cur.fetchall()
    assert len(rows) == 4
    statuses = {r["status"] for r in rows}
    assert statuses == {"success", "failure", "skipped", "partial"}


def test_write_run_record_truncates_error_summary_at_8KB_with_marker(
    db_conn: psycopg.Connection, cleanup_test_data: None
) -> None:
    """Long error_summary truncated at 8000 chars with '...[truncated]\\n'
    prefix. Truncation prefers the LAST 8000 chars (root cause is at
    traceback end)."""
    # Build a 12KB error string with distinctive start + end markers
    payload_start = "STARTSTART" + ("A" * 1990)  # 2000 chars
    payload_middle = "M" * 8000
    payload_end = ("Z" * 1990) + "ENDENDENDD"  # 2000 chars; ends with 'ENDD' (last 4)
    full = payload_start + payload_middle + payload_end
    assert len(full) == 12000

    close_pool()
    client.write_run_record(
        routine_name="pytest-truncate-routine",
        status="failure",
        error_summary=full,
        tickets_processed=0,
        drafts_created=0,
        started_at_iso="2026-05-03T14:00:00+00:00",
        finished_at_iso="2026-05-03T14:00:30+00:00",
    )
    close_pool()

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT error_summary FROM automation_runs "
            "WHERE routine_name = 'pytest-truncate-routine'",
        )
        row = cur.fetchone()
    assert row is not None
    stored = row["error_summary"]
    assert stored is not None

    # Length: '...[truncated]\n' (15 chars) + 8000 chars
    assert stored.startswith("...[truncated]\n")
    assert len(stored) == 15 + 8000

    # The last 8000 chars of the input should be preserved (root cause at
    # traceback end). The end of `full` is "ENDD" so stored must end with
    # "ENDD".
    assert stored.endswith("ENDD")
    # The middle 'M' chars should appear (they're in the last 8000)
    assert "MMMMMMMMMM" in stored
    # The very-start 'STARTSTART' must NOT appear (it's in the first 4000
    # which got truncated)
    assert "STARTSTART" not in stored


def test_write_run_record_returns_None_on_success(
    db_conn: psycopg.Connection, cleanup_test_data: None
) -> None:
    """Function signature returns None (insert is fire-and-forget)."""
    close_pool()
    result = client.write_run_record(
        routine_name="pytest-return-none-routine",
        status="success",
        error_summary=None,
        tickets_processed=1,
        drafts_created=1,
        started_at_iso="2026-05-03T14:00:00+00:00",
        finished_at_iso="2026-05-03T14:00:30+00:00",
    )
    close_pool()
    assert result is None
