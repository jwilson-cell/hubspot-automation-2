"""Shared pytest fixtures for Phase 4 helper tests.

Test DB strategy: tests run against the live dev Postgres DB (not a separate
test DB - mirrors `tests/setup.ts` posture from Phase 1 where the same
docker-compose Postgres serves dev + tests). Per-test isolation via SAVEPOINT
rollback in the `db_conn` fixture - each test gets a clean slate without
truncating tables that hold seed data (the 2 automation_routines rows from
Plan 04-02 must persist for read_routine_enabled tests).

Env vars expected (defaults below match docker-compose.yml host mapping):
    PACKN_OS_DATABASE_URL - test/dev Postgres URL
    REDIS_URL             - test/dev Redis URL
"""

import os
from typing import Generator

import psycopg
import pytest
from psycopg.rows import dict_row

# Default points at the dev docker Postgres (port 5433 to match the
# docker-compose host mapping that avoids the native Windows postgres on
# 5432). Same posture as tests/setup.ts in the Phase 1 test harness.
DEFAULT_DB_URL = "postgres://packn:packn_dev@localhost:5433/packn_os"
DEFAULT_REDIS_URL = "redis://localhost:6379"


@pytest.fixture(scope="session", autouse=True)
def _set_test_env() -> None:
    """Ensure PACKN_OS_DATABASE_URL + REDIS_URL are set before any helper
    module imports them. Mirrors tests/setup.ts TEST_ENV_DEFAULTS guard
    pattern - shell env wins, but the defaults make CI work without
    .env.local."""
    if "PACKN_OS_DATABASE_URL" not in os.environ:
        os.environ["PACKN_OS_DATABASE_URL"] = DEFAULT_DB_URL
    if "REDIS_URL" not in os.environ:
        os.environ["REDIS_URL"] = DEFAULT_REDIS_URL
    if "HUBSPOT_RATE_LIMIT_AUTOMATION" not in os.environ:
        os.environ["HUBSPOT_RATE_LIMIT_AUTOMATION"] = "3"


@pytest.fixture
def test_db_url() -> str:
    """Test Postgres URL - defaults to the dev docker Postgres."""
    return os.environ.get("PACKN_OS_DATABASE_URL", DEFAULT_DB_URL)


@pytest.fixture
def test_redis_url() -> str:
    """Test Redis URL - defaults to the dev docker Redis (db 0)."""
    # Use db 15 to avoid clashing with dev/prod traffic on db 0 when redis-py
    # tests run; rate_limit tests in Wave 1+ point at the actual rate keys
    # so use db 0 by default.
    return os.environ.get("REDIS_URL", DEFAULT_REDIS_URL)


@pytest.fixture
def db_conn(test_db_url: str) -> Generator[psycopg.Connection, None, None]:
    """Per-test DB connection wrapped in a SAVEPOINT that rolls back at end.

    Each test gets a fresh transaction; commits inside the test (e.g. from
    `client.write_draft` which calls `conn.commit()` on the helper's pool's
    OWN connection) are isolated from this fixture's connection - the
    fixture's view of the DB still reflects committed inserts because the
    helper's pool runs in autocommit-by-default-then-explicit-commit mode.

    Tests that mutate via `client.*` and need to assert post-state read via
    `db_conn.cursor()` see the committed rows. The `_cleanup` block at fixture
    teardown DELETEs any rows the test inserted by tagging on tenant_id +
    routine_name patterns (see _cleanup_test_data).
    """
    conn = psycopg.connect(test_db_url, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def cleanup_test_data(db_conn: psycopg.Connection) -> Generator[None, None, None]:
    """Cleans up rows inserted by test functions.

    Runs BEFORE the test (clears any leftover from prior failed run) and AFTER
    the test. Targets the test-specific routine names so seed data
    (tickets-process, digest) is preserved.

    Test routine names use the prefix 'pytest-' so the cleanup is targeted.
    """
    _cleanup(db_conn)
    yield
    _cleanup(db_conn)


def _cleanup(conn: psycopg.Connection) -> None:
    """Delete pytest-prefixed rows from automation_* tables.

    Order matters - rerun_requests references draft_id (no FK but logical
    dependency), drafts referenced by drafts_revisions (FK + append-only
    trigger - we rely on test data not creating revisions, otherwise need
    to disable the trigger which is out of scope).
    """
    with conn.cursor() as cur:
        # Order: most-dependent first
        cur.execute(
            "DELETE FROM automation_rerun_requests WHERE routine_name LIKE 'pytest-%'"
        )
        cur.execute(
            "DELETE FROM automation_drafts WHERE routine_name LIKE 'pytest-%'"
        )
        cur.execute(
            "DELETE FROM automation_runs WHERE routine_name LIKE 'pytest-%'"
        )
        cur.execute(
            "DELETE FROM automation_routines WHERE name LIKE 'pytest-%'"
        )
        # Phase 20 (Plan 20-04): complaint-mirror writer/helper tests tag
        # tickets with the same 'pytest-' prefix (no FK to the automation_*
        # tables — independent leaf, safe to delete last).
        cur.execute(
            "DELETE FROM customer_complaints WHERE hubspot_ticket_id LIKE 'pytest-%'"
        )
    conn.commit()
