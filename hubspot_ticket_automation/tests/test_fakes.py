"""Self-tests for the cron test fakes and for the pure-suite database guard
(Phase 93, plan 12).

The fakes are tested here so that a later plan's green result cannot come from
a fake that silently returns nothing: each one is shown to record what it was
given and to replay what it was told to.

No test in this file reaches a database, the network, a secret file or the
tracked config/state.json. The guard tests replace the pool class with a spy,
so they stay harmless even with the guard missing.
"""
from __future__ import annotations

import ast
import inspect
import json
import logging
import subprocess
import sys
import types
from pathlib import Path

import pytest

import tests.conftest as pure_suite
from packn_os_hubspot_client import client, db, tickets_api
from packn_os_hubspot_client.tickets_api import TicketsApiError
from tests.helpers import fakes
from ticket_pipeline import hubspot_api

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # hubspot_ticket_automation/
GIT_ROOT = PACKAGE_ROOT.parent
TRACKED_STATE = PACKAGE_ROOT / "config" / "state.json"
TRACKED_STATE_GIT_PATH = "hubspot_ticket_automation/config/state.json"

MARKER = "ZZ-FAKES-MARKER"
OS_FUNCTIONS = [
    "fetch_unprocessed",
    "fetch_ticket",
    "post_classification",
    "post_source_shadow_observations",
]


def positional_names(fn) -> list[str]:
    """Positional parameter names, in order. `self` is already gone on a bound method."""
    return [
        p.name
        for p in inspect.signature(fn).parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]


# ------------------------------------------------------ FakeHubSpotTransport


def test_fake_hubspot_transport_records_every_request_as_method_and_path() -> None:
    transport = fakes.FakeHubSpotTransport()
    transport("POST", "/crm/v3/objects/tickets/search", "token", {"limit": 25})
    transport("GET", "/crm/v4/objects/tickets/42/associations/emails?limit=50", "token")
    assert transport.calls == [
        ("POST", "/crm/v3/objects/tickets/search"),
        ("GET", "/crm/v4/objects/tickets/42/associations/emails?limit=50"),
    ]
    assert transport.bodies == [{"limit": 25}, None]


def test_fake_hubspot_transport_replays_a_canned_response_by_path_prefix() -> None:
    transport = fakes.FakeHubSpotTransport({"/crm/v3/objects/tickets/search": {"total": 2}})
    assert transport("POST", "/crm/v3/objects/tickets/search", "token", {}) == {"total": 2}


def test_fake_hubspot_transport_prefers_the_longest_matching_prefix() -> None:
    transport = fakes.FakeHubSpotTransport({"/crm/v3/objects/": {"which": "short"}})
    transport.respond("/crm/v3/objects/emails/batch/read", {"which": "long"})
    assert transport("POST", "/crm/v3/objects/emails/batch/read", "token", {}) == {"which": "long"}
    assert transport("POST", "/crm/v3/objects/contacts/batch/read", "token", {}) == {"which": "short"}


def test_fake_hubspot_transport_answers_none_for_a_path_with_no_canned_response() -> None:
    transport = fakes.FakeHubSpotTransport({"/crm/v3/objects/tickets/search": {"total": 0}})
    assert transport("GET", "/crm/v4/objects/tickets/42/associations/companies", "token") is None
    assert transport.calls == [("GET", "/crm/v4/objects/tickets/42/associations/companies")]


def test_fake_hubspot_transport_calls_matching_filters_by_prefix() -> None:
    transport = fakes.FakeHubSpotTransport()
    transport("POST", "/crm/v3/objects/tickets/search", "token", {})
    transport("GET", "/crm/v4/objects/tickets/42/associations/emails?limit=50", "token")
    transport("POST", "/crm/v3/objects/emails/batch/read", "token", {})
    assert transport.calls_matching("/crm/v3/objects/tickets/search") == [
        ("POST", "/crm/v3/objects/tickets/search")
    ]
    assert transport.calls_matching("/crm/v3/objects/") == [
        ("POST", "/crm/v3/objects/tickets/search"),
        ("POST", "/crm/v3/objects/emails/batch/read"),
    ]
    assert transport.calls_matching("/crm/v3/objects/contacts") == []


def test_fake_hubspot_transport_hands_out_a_copy_of_the_canned_response() -> None:
    transport = fakes.FakeHubSpotTransport({"/crm/v3/objects/tickets/search": {"results": []}})
    first = transport("POST", "/crm/v3/objects/tickets/search", "token", {})
    first["results"].append("mutated")
    assert transport("POST", "/crm/v3/objects/tickets/search", "token", {}) == {"results": []}


def test_fake_hubspot_transport_has_the_real_request_signature() -> None:
    assert positional_names(fakes.FakeHubSpotTransport()) == positional_names(hubspot_api._request)


def test_fake_hubspot_transport_stands_in_for_the_real_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = fakes.FakeHubSpotTransport(
        {
            "/crm/v3/objects/tickets/search": {"total": 1, "results": [{"id": "42"}]},
            "/crm/v4/objects/tickets/42/associations/emails": {
                "results": [{"toObjectId": 7}, {"toObjectId": 8}]
            },
            "/crm/v3/objects/emails/batch/read": {"results": [{"id": "7"}, {"id": "8"}]},
        }
    )
    monkeypatch.setattr(hubspot_api, "_request", transport)
    assert hubspot_api.search_tickets({"limit": 25}, "token") == {"total": 1, "results": [{"id": "42"}]}
    assert hubspot_api.get_associated_ids("42", "emails", "token") == ["7", "8"]
    assert hubspot_api.batch_read("emails", ["7", "8"], ["hs_email_text"], "token") == [
        {"id": "7"},
        {"id": "8"},
    ]
    # a lookup with nothing canned fails the way the real client does
    assert hubspot_api.get_associated_ids("42", "contacts", "token") == []
    assert len(transport.calls) == 4
    assert transport.calls_matching("/crm/v3/objects/tickets/search") == [
        ("POST", "/crm/v3/objects/tickets/search")
    ]


# ------------------------------------------------------------------ FakeOsApi


def test_fake_os_api_answers_an_empty_list_until_told_otherwise() -> None:
    api = fakes.FakeOsApi()
    answer = api.fetch_unprocessed(25)
    assert answer["tickets"] == []
    assert answer["contract_version"] == "1"
    assert api.calls == [("fetch_unprocessed", (25,))]


def test_fake_os_api_replays_the_canned_list() -> None:
    listing = {"contract_version": "1", "generated_at": "2026-10-09T14:07:03.120Z", "tickets": [{"id": "t1"}]}
    api = fakes.FakeOsApi(unprocessed=listing)
    assert api.fetch_unprocessed(100) == listing
    assert api.fetch_unprocessed(5) == listing  # a fake holds no rule: it does not cut the list
    assert api.calls == [("fetch_unprocessed", (100,)), ("fetch_unprocessed", (5,))]


def test_fake_os_api_fetch_ticket_replays_by_ref_and_answers_none_when_unknown() -> None:
    single = {"contract_version": "1", "ticket": {"id": "os-1"}, "unprocessed": True}
    api = fakes.FakeOsApi(tickets={"os-1": single, "123456": single})
    assert api.fetch_ticket("os-1") == single
    assert api.fetch_ticket("123456") == single
    assert api.fetch_ticket("999") is None  # the real client's answer to a 404
    assert api.calls == [
        ("fetch_ticket", ("os-1",)),
        ("fetch_ticket", ("123456",)),
        ("fetch_ticket", ("999",)),
    ]


def test_fake_os_api_post_classification_records_the_payload_and_replays_a_result() -> None:
    api = fakes.FakeOsApi()
    payload = {"category": "wismo_tracking", "outcome": "completed"}
    assert api.post_classification("os-1", payload) == {"status": 200, "error": None, "replayed": False}
    api.classification_results["os-2"] = {"status": 409, "error": "conflict", "replayed": None}
    assert api.post_classification("os-2", payload) == {"status": 409, "error": "conflict", "replayed": None}
    assert api.calls == [
        ("post_classification", ("os-1", payload)),
        ("post_classification", ("os-2", payload)),
    ]


def test_fake_os_api_post_source_shadow_observations_records_and_replays() -> None:
    api = fakes.FakeOsApi()
    payload = {"contract_version": "1", "observations": []}
    assert api.post_source_shadow_observations(payload) == {"status": 200, "error": None, "replayed": False}
    api.source_shadow_result = {"status": 413, "error": "payload_too_large", "replayed": None}
    assert api.post_source_shadow_observations(payload) == {
        "status": 413,
        "error": "payload_too_large",
        "replayed": None,
    }
    assert api.calls_to("post_source_shadow_observations") == [(payload,), (payload,)]


def test_fake_os_api_raises_the_given_code_on_the_next_list_fetch_only() -> None:
    api = fakes.FakeOsApi()
    api.fail_next_list("signature_rejected", 401)
    with pytest.raises(TicketsApiError) as caught:
        api.fetch_unprocessed(25)
    assert (caught.value.code, caught.value.http_status) == ("signature_rejected", 401)
    assert api.fetch_unprocessed(25)["tickets"] == []  # the failure was for one fetch
    assert api.calls == [("fetch_unprocessed", (25,)), ("fetch_unprocessed", (25,))]


def test_fake_os_api_can_fail_any_one_of_its_four_functions_once() -> None:
    api = fakes.FakeOsApi()
    api.fail_next("post_classification", "list_timeout")
    with pytest.raises(TicketsApiError) as caught:
        api.post_classification("os-1", {})
    assert (caught.value.code, caught.value.http_status) == ("list_timeout", None)
    assert api.post_classification("os-1", {})["status"] == 200
    with pytest.raises(ValueError):
        api.fail_next("not_a_function", "list_error")


def test_fake_os_api_hands_out_a_copy_of_what_it_was_given() -> None:
    api = fakes.FakeOsApi(unprocessed={"contract_version": "1", "tickets": [{"id": "t1"}]})
    api.fetch_unprocessed(25)["tickets"].append({"id": "mutated"})
    assert api.fetch_unprocessed(25)["tickets"] == [{"id": "t1"}]


@pytest.mark.parametrize("name", OS_FUNCTIONS)
def test_fake_os_api_has_the_real_positional_parameters(name: str) -> None:
    real = getattr(tickets_api, name)
    fake = getattr(fakes.FakeOsApi(), name)
    assert positional_names(fake) == positional_names(real)
    # the fake takes nothing else: the real function's test seams are keyword-only
    assert list(inspect.signature(fake).parameters) == positional_names(real)


@pytest.mark.parametrize("name", OS_FUNCTIONS)
def test_the_real_function_keeps_its_test_seams_keyword_only(name: str) -> None:
    parameters = inspect.signature(getattr(tickets_api, name)).parameters.values()
    seams = [p.name for p in parameters if p.kind is p.KEYWORD_ONLY]
    assert seams == ["base_url", "secret", "now", "opener"]


# --------------------------------------------------------------- FakeLauncher


def test_fake_launcher_records_the_command_and_environment_and_returns_the_exit_code() -> None:
    launcher = fakes.FakeLauncher(3)
    env = {"PACKN_RUN_ID": "run-1", "PACKN_SOURCE_MODE": "os"}
    assert launcher(["claude", "-p", "/packn-tickets"], env) == 3
    assert launcher.calls == [(["claude", "-p", "/packn-tickets"], env)]
    env["PACKN_RUN_ID"] = "changed-after-the-call"
    assert launcher.calls[0][1]["PACKN_RUN_ID"] == "run-1"  # what it was given, not a live reference


def test_fake_launcher_takes_the_command_then_the_environment() -> None:
    assert positional_names(fakes.FakeLauncher(0)) == ["command", "env"]


def test_fake_launcher_writes_the_result_to_the_path_named_in_the_environment(tmp_path: Path) -> None:
    result = {"status": "success", "tickets_processed": 3, "drafts_created": 2}
    launcher = fakes.FakeLauncher(0, result)
    target = tmp_path / "os" / "run-1" / "result.json"
    assert fakes.RESULT_PATH_ENV == "PACKN_RESULT_PATH"
    assert launcher(["claude"], {"PACKN_RESULT_PATH": str(target)}) == 0
    assert json.loads(target.read_text(encoding="utf-8")) == result


def test_fake_launcher_writes_nothing_without_a_result(tmp_path: Path) -> None:
    target = tmp_path / "result.json"
    assert fakes.FakeLauncher(1)(["claude"], {"PACKN_RESULT_PATH": str(target)}) == 1
    assert not target.exists()


def test_fake_launcher_writes_nothing_when_the_environment_names_no_path(tmp_path: Path) -> None:
    launcher = fakes.FakeLauncher(0, {"status": "success"})
    assert launcher(["claude"], {"PACKN_RUN_ID": "run-1"}) == 0
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------ temp_state_file


def test_temp_state_file_writes_under_the_pytest_temp_directory(tmp_path: Path) -> None:
    state = {"last_run_at": "2026-10-09T14:07:03Z", "ticket_fingerprints": {"42": {"num_notes": 1}}}
    path = fakes.temp_state_file(tmp_path, state)
    assert isinstance(path, Path)
    assert tmp_path in path.parents
    assert path != TRACKED_STATE
    assert json.loads(path.read_text(encoding="utf-8")) == state


def test_temp_state_file_leaves_the_tracked_state_file_as_committed(tmp_path: Path) -> None:
    before = TRACKED_STATE.read_bytes()
    path = fakes.temp_state_file(tmp_path, {"last_run_at": "2026-10-09T14:07:03Z"})
    path.write_text(json.dumps({"last_run_at": "rewritten by the code under test"}), encoding="utf-8")
    after = TRACKED_STATE.read_bytes()
    assert after == before
    # and it is what HEAD holds. This checkout converts line endings on Windows,
    # so the comparison is on content with the line endings normalised.
    committed = subprocess.run(
        ["git", "show", f"HEAD:{TRACKED_STATE_GIT_PATH}"],
        cwd=GIT_ROOT,
        capture_output=True,
        check=True,
    ).stdout
    assert committed, "git show returned nothing for the tracked state file"
    assert after.replace(b"\r\n", b"\n") == committed.replace(b"\r\n", b"\n")


# ------------------------------------------------------- assert_marker_absent


def test_assert_marker_absent_passes_when_nothing_was_emitted(caplog, capfd) -> None:
    caplog.set_level(logging.DEBUG)
    logging.getLogger("fakes.selftest").warning("a line with no ticket text")
    print("a line with no ticket text", file=sys.stderr)
    print("a line with no ticket text")
    fakes.assert_marker_absent(MARKER, caplog, capfd)


def test_assert_marker_absent_fails_on_a_marker_logged_through_logging(caplog, capfd) -> None:
    caplog.set_level(logging.DEBUG)
    logging.getLogger("fakes.selftest").warning("body was %s", MARKER)
    with pytest.raises(AssertionError, match="logging"):
        fakes.assert_marker_absent(MARKER, caplog, capfd)


def test_assert_marker_absent_fails_on_a_marker_in_a_log_record_field(caplog, capfd) -> None:
    caplog.set_level(logging.DEBUG)
    logging.getLogger("fakes.selftest").warning("draft written", extra={"draft_body": MARKER})
    with pytest.raises(AssertionError, match="logging"):
        fakes.assert_marker_absent(MARKER, caplog, capfd)


def test_assert_marker_absent_fails_on_a_marker_printed_to_stderr(caplog, capfd) -> None:
    print(f"[pipeline.hubspot] body was {MARKER}", file=sys.stderr)
    with pytest.raises(AssertionError, match="stderr"):
        fakes.assert_marker_absent(MARKER, caplog, capfd)


def test_assert_marker_absent_fails_on_a_marker_printed_to_stdout(caplog, capfd) -> None:
    print(json.dumps({"draft_body": MARKER}))
    with pytest.raises(AssertionError, match="stdout"):
        fakes.assert_marker_absent(MARKER, caplog, capfd)


def test_assert_marker_absent_refuses_an_empty_marker(caplog, capfd) -> None:
    with pytest.raises(ValueError):
        fakes.assert_marker_absent("", caplog, capfd)


# ------------------------------------------------------------ pure-suite guard


class _ConnectionAttempted(Exception):
    """Raised by the pool spy: the code under test tried to build a real pool."""


@pytest.fixture
def pool_spy(monkeypatch: pytest.MonkeyPatch) -> list:
    """Replace the pool class so nothing here can connect, guard or no guard."""
    built: list = []

    def spy(*args, **kwargs):
        built.append((args, kwargs))
        raise _ConnectionAttempted("a connection pool was built")

    monkeypatch.setattr(db, "ConnectionPool", spy)
    monkeypatch.setattr(db, "_pool", None)
    return built


def test_the_pure_suite_cannot_open_the_helper_pool(pool_spy: list) -> None:
    with pytest.raises(RuntimeError, match="pure suite has no database"):
        db.get_pool()
    assert pool_spy == []
    assert db._pool is None


def test_a_client_function_cannot_open_the_pool_either(pool_spy: list) -> None:
    # client.py binds the name with `from .db import get_pool`: patching the db
    # module alone would leave every client function able to connect.
    with pytest.raises(RuntimeError, match="pure suite has no database"):
        client.mark_rerun_processed("pytest-guard-request", "pytest-guard-draft")
    assert pool_spy == []


def test_a_fail_closed_client_read_meets_the_guard_and_not_a_database(
    pool_spy: list, caplog
) -> None:
    # read_routine_enabled turns any failure into False (its own fail-closed
    # rule), so the guard shows in its warning, not as a raised error.
    caplog.set_level(logging.WARNING)
    assert client.read_routine_enabled("tickets-process") is False
    assert "pure suite has no database" in caplog.text
    assert pool_spy == []


def test_the_guard_holds_with_a_database_url_in_the_environment(
    pool_spy: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    # an unroutable placeholder (TEST-NET-1): nothing may even try it
    monkeypatch.setenv("PACKN_OS_DATABASE_URL", "postgres://nobody:nothing@192.0.2.1:1/none")
    with pytest.raises(RuntimeError, match="pure suite has no database"):
        db.get_pool()
    with pytest.raises(RuntimeError, match="pure suite has no database"):
        client.mark_rerun_processed("pytest-guard-request", "pytest-guard-draft")
    assert pool_spy == []
    assert db._pool is None


def test_the_guard_is_an_autouse_fixture_of_this_directory() -> None:
    assert Path(pure_suite.__file__).resolve() == (PACKAGE_ROOT / "tests" / "conftest.py").resolve()
    assert db.get_pool is pure_suite._no_database_pool
    assert client.get_pool is pure_suite._no_database_pool


def test_a_module_that_bound_the_guard_mid_test_gets_the_real_getter_back() -> None:
    # A module first imported while a test runs does `from ...db import get_pool`
    # and binds the guard for good. The guard's teardown hands such a module the
    # real getter, so the helper package's own suite is not left with the guard.
    def real_getter():
        return "the real pool"

    late = types.ModuleType("late_binder_probe")
    late.get_pool = pure_suite._no_database_pool
    untouched = types.ModuleType("other_probe")
    untouched.get_pool = len
    pure_suite._hand_back(real_getter, [late, untouched])
    assert late.get_pool is real_getter
    assert untouched.get_pool is len


def test_the_fakes_depend_on_the_standard_library_and_the_client_error_only() -> None:
    source = (PACKAGE_ROOT / "tests" / "helpers" / "fakes.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"relative import at line {node.lineno}"
            imported.add(node.module or "")
    assert imported, "the parse found no import"
    outside = sorted(
        name
        for name in imported
        if name.split(".")[0] not in sys.stdlib_module_names
        and name != "packn_os_hubspot_client.tickets_api"
    )
    assert outside == []
