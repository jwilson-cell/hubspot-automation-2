"""The run record carries the run id, the source mode, the failure reason code
and the HubSpot call counts (Phase 93, plan 14; SPEC R14 groundwork).

Three things are pinned here:

    TODAY    called with its seven positional arguments, write_run_record sends
             the statement and the parameters it sent before this plan. The
             literal below was copied from the function before it was edited.
    NEW      the four keyword-only values reach the row, a second record for the
             same run id is refused by the primary key, and nothing reads the
             table back (the cron's role can insert into it, not select from it).
    OLDER    against a database that does not have the new columns yet, the
             record is still written: the old column list, plus the id.

Tests run against the local dev Postgres (the conftest refuses anything else).
Every row has a `pytest-` routine name, which is what the cleanup removes.
"""

import contextlib
import inspect
import json
import re
import uuid
from typing import Iterator, Optional

import psycopg
import pytest
from psycopg.rows import dict_row

from packn_os_hubspot_client import client
from packn_os_hubspot_client.db import close_pool
from packn_os_hubspot_client.tenant import TENANT_ID

ROUTINE = "pytest-run-records"
STARTED = "2026-10-09T14:00:00Z"
FINISHED = "2026-10-09T14:00:42Z"

# Copied from write_run_record as it stood before this plan (cron repo c800bd3),
# whitespace included. Not built from the function under test.
TODAYS_STATEMENT = (
    "\n"
    "                INSERT INTO automation_runs\n"
    "                  (tenant_id, routine_name, status, started_at, finished_at,\n"
    "                   tickets_processed, drafts_created, error_summary)\n"
    "                VALUES (%s, %s, %s, %s::timestamptz, %s::timestamptz, %s, %s, %s)\n"
    "                "
)

CALLS = {
    "kinds": {
        "search": 0,
        "ticket_read": 0,
        "contact_read": 2,
        "company_read": 1,
        "related_lookup": 0,
        "rerun_read": 0,
        "email_thread": 3,
        "number_backfill": 1,
    },
    "raw_requests": 7,
}

NEW_COLUMNS = ("source_mode", "failure_reason_code", "hubspot_calls")


@pytest.fixture
def runs_db(
    db_conn: psycopg.Connection, cleanup_test_data: None
) -> Iterator[psycopg.Connection]:
    """The owner connection for reading rows back, with a fresh helper pool."""
    close_pool()
    yield db_conn
    close_pool()


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch, test_db_url: str) -> Iterator[list]:
    """Every statement the client sends, as (text, params), in order."""
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


def _runs(conn: psycopg.Connection, routine: str = ROUTINE) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text AS id, routine_name, status::text AS status, started_at,
                   finished_at, tickets_processed, drafts_created, error_summary,
                   source_mode, failure_reason_code, hubspot_calls
              FROM automation_runs
             WHERE tenant_id = %s AND routine_name = %s
             ORDER BY created_at, id
            """,
            (TENANT_ID, routine),
        )
        rows = cur.fetchall()
    conn.commit()
    return rows


def _write(status: str = "success", error_summary: Optional[str] = None, **keywords) -> None:
    client.write_run_record(ROUTINE, status, error_summary, 4, 3, STARTED, FINISHED, **keywords)


def _inserts(statements: list) -> list:
    return [s for s in statements if re.match(r"\s*INSERT\s+INTO\s+automation_runs\b", s[0])]


def _preflights(statements: list) -> list:
    return [s for s in statements if "information_schema" in s[0]]


# ------------------------------------------------------------------ the signature


def test_the_seven_positional_parameters_are_unchanged_and_four_are_keyword_only() -> None:
    parameters = inspect.signature(client.write_run_record).parameters
    assert list(parameters) == [
        "routine_name",
        "status",
        "error_summary",
        "tickets_processed",
        "drafts_created",
        "started_at_iso",
        "finished_at_iso",
        "run_id",
        "source_mode",
        "failure_reason_code",
        "hubspot_calls",
    ]
    added = ["run_id", "source_mode", "failure_reason_code", "hubspot_calls"]
    assert all(parameters[name].kind is inspect.Parameter.KEYWORD_ONLY for name in added)
    assert all(parameters[name].default is None for name in added)


# ------------------------------------------------------------------ TODAY


def test_called_the_old_way_it_sends_todays_statement_and_parameters(
    recorded: list, runs_db: psycopg.Connection
) -> None:
    client.write_run_record(ROUTINE, "success", None, 4, 3, STARTED, FINISHED)

    assert recorded == [
        (TODAYS_STATEMENT, (TENANT_ID, ROUTINE, "success", STARTED, FINISHED, 4, 3, None))
    ]
    rows = _runs(runs_db)
    assert len(rows) == 1
    assert [rows[0][column] for column in NEW_COLUMNS] == [None, None, None]


def test_the_four_keywords_passed_as_none_are_still_the_old_way(
    recorded: list, runs_db: psycopg.Connection
) -> None:
    _write(run_id=None, source_mode=None, failure_reason_code=None, hubspot_calls=None)

    assert recorded == [
        (TODAYS_STATEMENT, (TENANT_ID, ROUTINE, "success", STARTED, FINISHED, 4, 3, None))
    ]


# ------------------------------------------------------------------ NEW


def test_a_record_carries_the_run_id_the_mode_and_the_call_counts(
    runs_db: psycopg.Connection,
) -> None:
    run_id = str(uuid.uuid4())

    _write(run_id=run_id, source_mode="os", hubspot_calls=CALLS)

    rows = _runs(runs_db)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == run_id
    assert row["source_mode"] == "os"
    assert row["hubspot_calls"] == CALLS
    assert row["failure_reason_code"] is None
    assert row["status"] == "success"
    assert (row["tickets_processed"], row["drafts_created"]) == (4, 3)


def test_a_failure_record_carries_its_reason_code(runs_db: psycopg.Connection) -> None:
    run_id = str(uuid.uuid4())

    _write(
        status="failure",
        error_summary="list: list_timeout",
        run_id=run_id,
        source_mode="os",
        failure_reason_code="list_timeout",
    )

    row = _runs(runs_db)[0]
    assert row["id"] == run_id
    assert row["status"] == "failure"
    assert row["failure_reason_code"] == "list_timeout"
    assert row["source_mode"] == "os"
    assert row["hubspot_calls"] is None
    assert row["error_summary"] == "list: list_timeout"


def test_a_run_id_alone_becomes_the_records_id(runs_db: psycopg.Connection) -> None:
    run_id = str(uuid.uuid4())

    _write(run_id=run_id)

    row = _runs(runs_db)[0]
    assert row["id"] == run_id
    assert [row[column] for column in NEW_COLUMNS] == [None, None, None]


def test_a_mode_with_no_run_id_gets_a_generated_id(runs_db: psycopg.Connection) -> None:
    _write(source_mode="hubspot")

    row = _runs(runs_db)[0]
    uuid.UUID(row["id"])
    assert row["source_mode"] == "hubspot"


def test_a_second_record_for_the_same_run_id_is_refused(runs_db: psycopg.Connection) -> None:
    """One record per run: the primary key decides."""
    run_id = str(uuid.uuid4())
    _write(run_id=run_id, source_mode="os", hubspot_calls=CALLS)
    first = _runs(runs_db)

    with pytest.raises(psycopg.errors.UniqueViolation):
        _write(status="failure", run_id=run_id, source_mode="os", failure_reason_code="agent_failed")

    assert _runs(runs_db) == first
    assert len(first) == 1


def test_a_new_record_is_one_insert_with_no_read_back(
    recorded: list, runs_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cron's role can insert into the run table, not select from it."""
    monkeypatch.setattr(client, "_RUN_RECORD_PHASE93_COLUMNS", True)

    _write(run_id=str(uuid.uuid4()), source_mode="os", hubspot_calls=CALLS)

    assert len(recorded) == 1
    text, params = recorded[0]
    assert re.match(r"\s*INSERT\s+INTO\s+automation_runs\b", text)
    assert not re.search(r"\bRETURNING\b", text, re.IGNORECASE)
    assert not re.search(r"\bFROM\s+automation_runs\b", text, re.IGNORECASE)
    assert json.dumps(CALLS) in params  # the counts travel as one JSON parameter


def test_the_column_check_is_asked_once_per_process(
    recorded: list, runs_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "_RUN_RECORD_PHASE93_COLUMNS", None)

    _write(run_id=str(uuid.uuid4()), source_mode="os")
    first = list(recorded)
    del recorded[:]
    _write(run_id=str(uuid.uuid4()), source_mode="os")

    assert len(_preflights(first)) == 1
    assert _preflights(first)[0][1] == ("source_mode",)
    assert "automation_runs" in _preflights(first)[0][0]
    assert len(_inserts(first)) == 1
    assert _preflights(recorded) == []
    assert len(_inserts(recorded)) == 1
    assert client._RUN_RECORD_PHASE93_COLUMNS is True  # the dev database has the columns
    assert len(_runs(runs_db)) == 2


def test_the_old_way_never_asks_the_column_check(
    recorded: list, runs_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "_RUN_RECORD_PHASE93_COLUMNS", None)

    client.write_run_record(ROUTINE, "success", None, 4, 3, STARTED, FINISHED)

    assert _preflights(recorded) == []
    assert client._RUN_RECORD_PHASE93_COLUMNS is None


# ------------------------------------------------------------------ OLDER


def test_without_the_new_columns_the_record_is_the_old_statement_plus_the_id(
    recorded: list, runs_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deploy-order guard: a cron that reaches a database the OS has not
    migrated yet still writes its record, without the three values."""
    monkeypatch.setattr(client, "_RUN_RECORD_PHASE93_COLUMNS", False)
    run_id = str(uuid.uuid4())

    _write(
        status="failure",
        run_id=run_id,
        source_mode="os",
        failure_reason_code="list_error",
        hubspot_calls=CALLS,
    )

    assert len(recorded) == 1
    text, params = recorded[0]
    assert re.match(r"\s*INSERT\s+INTO\s+automation_runs\b", text)
    assert not any(column in text for column in NEW_COLUMNS)
    assert re.search(r"\(\s*id\s*,\s*tenant_id\b", text)
    assert params == (run_id, TENANT_ID, ROUTINE, "failure", STARTED, FINISHED, 4, 3, None)
    row = _runs(runs_db)[0]
    assert row["id"] == run_id
    assert row["status"] == "failure"
    assert [row[column] for column in NEW_COLUMNS] == [None, None, None]


def test_without_the_new_columns_and_without_a_run_id_it_is_todays_statement(
    recorded: list, runs_db: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "_RUN_RECORD_PHASE93_COLUMNS", False)

    _write(source_mode="os", hubspot_calls=CALLS)

    assert recorded == [
        (TODAYS_STATEMENT, (TENANT_ID, ROUTINE, "success", STARTED, FINISHED, 4, 3, None))
    ]
    assert len(_runs(runs_db)) == 1


# ------------------------------------------------------------------ failures and limits


@pytest.mark.parametrize("keywords", [{}, {"run_id": "RUN_ID", "source_mode": "os"}])
def test_an_insert_failure_raises(runs_db: psycopg.Connection, keywords: dict) -> None:
    """The caller turns this into a non-zero exit; nothing is swallowed here."""
    keywords = {k: (str(uuid.uuid4()) if v == "RUN_ID" else v) for k, v in keywords.items()}

    with pytest.raises(psycopg.Error):
        _write(status="not-a-status", **keywords)

    assert _runs(runs_db) == []


@pytest.mark.parametrize(
    "keywords",
    [
        {"run_id": "not-a-uuid"},
        {"run_id": 7},
        {"source_mode": 3},
        {"failure_reason_code": ["list_timeout"]},
        {"hubspot_calls": "seven"},
        {"hubspot_calls": [1, 2]},
    ],
)
def test_a_value_of_the_wrong_kind_is_refused_before_any_statement(
    recorded: list, runs_db: psycopg.Connection, keywords: dict
) -> None:
    with pytest.raises(ValueError):
        _write(**keywords)

    assert recorded == []
    assert _runs(runs_db) == []


def test_a_long_error_summary_is_still_cut_to_its_tail(runs_db: psycopg.Connection) -> None:
    summary = ("A" * 9000) + "ROOT_CAUSE_AT_THE_END"

    _write(
        status="failure",
        error_summary=summary,
        run_id=str(uuid.uuid4()),
        source_mode="os",
        failure_reason_code="agent_failed",
    )

    stored = _runs(runs_db)[0]["error_summary"]
    assert stored.startswith("...[truncated]\n")
    assert stored.endswith("ROOT_CAUSE_AT_THE_END")
    assert len(stored) == len("...[truncated]\n") + 8000
