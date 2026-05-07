"""HUB-01 - read_routine_enabled tests.

Activated by Plan 04-03 Task 2. Tests run against the live dev Postgres DB
via the conftest db_conn + cleanup_test_data fixtures. Test routine names
use the 'pytest-' prefix so the cleanup is targeted.
"""

import psycopg

from packn_os_hubspot_client import client
from packn_os_hubspot_client.db import close_pool
from packn_os_hubspot_client.tenant import TENANT_ID


def _insert_routine(conn: psycopg.Connection, name: str, enabled: bool) -> None:
    """Helper - INSERT a test routine row."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO automation_routines (tenant_id, name, enabled, cron_schedule)
            VALUES (%s, %s, %s, %s)
            """,
            (TENANT_ID, name, enabled, "*/30 * * * *"),
        )
    conn.commit()


def test_read_routine_enabled_returns_true_when_row_exists_and_enabled(
    db_conn: psycopg.Connection, cleanup_test_data: None
) -> None:
    """read_routine_enabled returns True when row enabled=true."""
    _insert_routine(db_conn, "pytest-enabled-routine", enabled=True)
    # Helper uses its own connection pool - close it first so we get a fresh
    # connection that sees the committed insert.
    close_pool()
    assert client.read_routine_enabled("pytest-enabled-routine") is True
    close_pool()


def test_read_routine_enabled_returns_false_when_row_exists_and_paused(
    db_conn: psycopg.Connection, cleanup_test_data: None
) -> None:
    """read_routine_enabled returns False when row enabled=false."""
    _insert_routine(db_conn, "pytest-paused-routine", enabled=False)
    close_pool()
    assert client.read_routine_enabled("pytest-paused-routine") is False
    close_pool()


def test_read_routine_enabled_fail_closed_on_missing_row(
    db_conn: psycopg.Connection, cleanup_test_data: None
) -> None:
    """read_routine_enabled returns False (fail-closed) on missing row."""
    close_pool()
    # Use a name that definitely doesn't exist - the cleanup ensures pytest-*
    # rows are gone, and 'pytest-nonexistent' isn't seeded by Plan 04-02.
    assert client.read_routine_enabled("pytest-nonexistent-routine") is False
    close_pool()


def test_read_routine_enabled_uses_TENANT_ID_packn_constant(
    db_conn: psycopg.Connection, cleanup_test_data: None
) -> None:
    """read_routine_enabled hardcodes TENANT_ID = 'packn'.

    Asserts the constant is 'packn' (mirrors src/lib/tenant.ts) AND that a
    row with a different tenant_id is NOT returned by the SELECT (multi-
    tenant isolation invariant).
    """
    assert TENANT_ID == "packn"

    # Insert a routine with a different tenant_id - it must NOT be visible
    # to read_routine_enabled (which scopes to TENANT_ID='packn').
    with db_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO automation_routines (tenant_id, name, enabled, cron_schedule)
            VALUES (%s, %s, %s, %s)
            """,
            ("other-tenant", "pytest-other-tenant-routine", True, "*/30 * * * *"),
        )
    db_conn.commit()
    close_pool()
    # Even though the row exists with enabled=true, the helper scopes to
    # tenant_id='packn' and finds no row - fail-closed False.
    assert client.read_routine_enabled("pytest-other-tenant-routine") is False
    close_pool()

    # Cleanup the cross-tenant row (cleanup fixture only matches tenant_id='packn')
    with db_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM automation_routines "
            "WHERE tenant_id = 'other-tenant' AND name LIKE 'pytest-%'"
        )
    db_conn.commit()
