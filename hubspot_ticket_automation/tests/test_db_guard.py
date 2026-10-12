"""The helper suite's database tests run only against the local dev Postgres.

Phase 93, plan 14 (orchestrator ruling on checker CRON-B1; runbook tag
R-LOCAL-DB-ONLY). The guard itself is in packn_os_hubspot_client/tests/conftest.py:
the pure function `assert_local_dev_database` and a session-scoped autouse fixture
that aborts the session when the function refuses.

This file is in the pure suite because it opens no connection. The pure cases call
the function with a URL; the end-to-end case starts a child pytest on the helper
suite with a URL on the reserved host `db.example.invalid` and reads how it ended.
Every user, password and database name below is made up.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from packn_os_hubspot_client.tests import conftest as helper_conftest
from packn_os_hubspot_client.tests.conftest import (
    DB_GUARD_EXIT_CODE,
    DB_GUARD_REFUSAL_TOKEN,
    LOCAL_DB_HOSTS,
    LOCAL_DB_PORT,
    NonLocalDatabaseError,
    assert_local_dev_database,
)

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # hubspot_ticket_automation/

USER = "guard_user_zq"
PASSWORD = "guard_secret_pw_zq"
DATABASE = "guard_db_zq"
NO_ENV: dict = {}


def _url(host_port: str, scheme: str = "postgres", tail: str = "") -> str:
    return f"{scheme}://{USER}:{PASSWORD}@{host_port}/{DATABASE}{tail}"


def _refusal(url, environ=None) -> str:
    with pytest.raises(NonLocalDatabaseError) as caught:
        assert_local_dev_database(url, NO_ENV if environ is None else environ)
    return str(caught.value)


def _assert_says_nothing_private(message: str) -> None:
    assert USER not in message
    assert PASSWORD not in message
    assert DATABASE not in message


# ------------------------------------------------------------------ the constants


def test_the_rule_is_the_dev_docker_postgres() -> None:
    assert LOCAL_DB_HOSTS == ("localhost", "127.0.0.1")
    assert LOCAL_DB_PORT == 5433
    assert DB_GUARD_REFUSAL_TOKEN == "REFUSING_NON_LOCAL_DATABASE"
    assert DB_GUARD_EXIT_CODE != 0


def test_the_suites_own_default_url_passes() -> None:
    assert assert_local_dev_database(helper_conftest.DEFAULT_DB_URL, NO_ENV) is None


# ------------------------------------------------------------------ accepted


@pytest.mark.parametrize(
    "url",
    [
        _url("localhost:5433"),
        _url("127.0.0.1:5433"),
        _url("localhost:5433", scheme="postgresql"),
        _url("LOCALHOST:5433"),
        _url("localhost:5433", tail="?sslmode=disable"),
        "postgres://localhost:5433/anything",
    ],
)
def test_a_local_dev_url_is_accepted(url: str) -> None:
    assert assert_local_dev_database(url, NO_ENV) is None


# ------------------------------------------------------------------ refused


@pytest.mark.parametrize(
    ("host_port", "host", "port"),
    [
        ("db.example.invalid:5432", "db.example.invalid", "5432"),
        ("db.example.invalid:5433", "db.example.invalid", "5433"),
        ("db.example.invalid:6543", "db.example.invalid", "6543"),
        ("10.0.0.7:5433", "10.0.0.7", "5433"),
        ("localhost:5432", "localhost", "5432"),
        ("localhost:6543", "localhost", "6543"),
        ("127.0.0.1:5432", "127.0.0.1", "5432"),
        ("localhost.example.invalid:5433", "localhost.example.invalid", "5433"),
        ("[::1]:5433", "::1", "5433"),
    ],
)
def test_any_other_host_or_port_is_refused_and_named(host_port: str, host: str, port: str) -> None:
    message = _refusal(_url(host_port))
    assert message.startswith(DB_GUARD_REFUSAL_TOKEN)
    assert host in message
    assert port in message
    _assert_says_nothing_private(message)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "db.example.invalid"])
def test_a_url_with_no_port_is_refused(host: str) -> None:
    message = _refusal(_url(host))
    assert message.startswith(DB_GUARD_REFUSAL_TOKEN)
    assert host in message
    assert "no port" in message
    _assert_says_nothing_private(message)


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "   ",
        12345,
        b"postgres://localhost:5433/db",
        "not a url",
        "localhost:5433",
        f"host=localhost port=5433 user={USER} password={PASSWORD} dbname={DATABASE}",
        f"postgres://{USER}:{PASSWORD}@localhost:notaport/{DATABASE}",
        f"postgres://{USER}:{PASSWORD}@localhost:99999/{DATABASE}",
        f"postgres://{USER}:{PASSWORD}@/{DATABASE}",
        f"mysql://{USER}:{PASSWORD}@localhost:5433/{DATABASE}",
        f"postgres://{USER}:pa/ss{PASSWORD}@localhost:5433/{DATABASE}",
        f"postgres://{USER}:{PASSWORD}@localhost:5433,db.example.invalid:5433/{DATABASE}",
    ],
)
def test_a_url_that_cannot_be_read_is_refused_without_echoing_it(url) -> None:
    message = _refusal(url)
    assert message.startswith(DB_GUARD_REFUSAL_TOKEN)
    assert "host unknown" in message and "port unknown" in message
    _assert_says_nothing_private(message)


def test_a_host_hidden_behind_the_user_part_is_the_host_that_is_judged() -> None:
    """Everything before the last @ is the user part; the host after it decides."""
    message = _refusal(f"postgres://localhost:5433@db.example.invalid:5433/{DATABASE}")
    assert "db.example.invalid" in message
    assert DATABASE not in message


@pytest.mark.parametrize(
    "tail",
    [
        "?host=db.example.invalid",
        "?hostaddr=10.0.0.7",
        "?port=5432",
        "?service=prod",
        "?sslmode=require&HOST=db.example.invalid",
    ],
)
def test_a_query_parameter_that_redirects_the_connection_is_refused(tail: str) -> None:
    message = _refusal(_url("localhost:5433", tail=tail))
    assert message.startswith(DB_GUARD_REFUSAL_TOKEN)
    assert "db.example.invalid" not in message and "10.0.0.7" not in message  # names, not values
    _assert_says_nothing_private(message)


@pytest.mark.parametrize("variable", ["PGHOSTADDR", "PGSERVICE"])
def test_an_environment_variable_that_redirects_the_connection_is_refused(variable: str) -> None:
    message = _refusal(_url("localhost:5433"), environ={variable: "10.0.0.7"})
    assert message.startswith(DB_GUARD_REFUSAL_TOKEN)
    assert variable in message
    assert "10.0.0.7" not in message
    _assert_says_nothing_private(message)


def test_an_empty_redirecting_variable_is_not_a_redirect() -> None:
    assert assert_local_dev_database(_url("localhost:5433"), {"PGHOSTADDR": "", "PGSERVICE": ""}) is None


def test_the_refusal_is_not_an_assertion_error() -> None:
    """The fixture catches exactly this type; a plain assert would be compiled out under -O."""
    assert issubclass(NonLocalDatabaseError, RuntimeError)
    assert not issubclass(NonLocalDatabaseError, AssertionError)


# ------------------------------------------------------------------ end to end


def test_a_child_process_on_a_non_local_url_aborts_before_any_test() -> None:
    """The whole session stops: non-zero exit, the token, nothing passed, nothing skipped.

    The host is under the reserved `.invalid` domain. It can only end this fast,
    with this token and no driver error, if nothing tried to connect.
    """
    environ = dict(os.environ)
    environ["PACKN_OS_DATABASE_URL"] = _url("db.example.invalid:5432")
    started = time.monotonic()
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "packn_os_hubspot_client/tests/test_drafts.py",
            "-p",
            "no:cacheprovider",
        ],
        cwd=str(PACKAGE_ROOT),
        env=environ,
        capture_output=True,
        text=True,
        timeout=120,
    )
    elapsed = time.monotonic() - started
    output = child.stdout + child.stderr

    assert child.returncode == DB_GUARD_EXIT_CODE, output[-2000:]
    assert DB_GUARD_REFUSAL_TOKEN in output
    assert "db.example.invalid" in output and "5432" in output
    assert re.search(r"\b\d+ passed\b", output) is None, output[-2000:]
    assert re.search(r"\b\d+ skipped\b", output) is None, output[-2000:]
    assert "PASSED" not in output and "SKIPPED" not in output
    assert "OperationalError" not in output  # the driver was never asked to connect
    assert elapsed < 10, f"the child took {elapsed:.1f}s"
    _assert_says_nothing_private(output)
