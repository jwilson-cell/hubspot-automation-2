"""The cron's one source setting, and the candidate rule it shares (Phase 93, plan 13).

Three groups:

    PIN      what the cron sends to HubSpot today, written down as literals and run
             green against the untouched code BEFORE any function was moved. A later
             edit that changes one byte of the `hubspot`-mode search turns these red.
    MODE     the mode reader's three honest outcomes (a valid mode, source_unreadable,
             source_unknown) and the absent / valid / unrecognised rule.
    READERS  the pre-gate and the engine-shadow pipeline branching on that one mode.

No test here reaches a database, HubSpot, a model, a secret file or the tracked
config/state.json: the transport, the client read and the model calls are fakes,
and every path the code would open is redirected under the pytest temp directory.
The network is refused outright by an autouse fixture, so a test that forgets a
fake fails instead of sending a request.
"""
from __future__ import annotations

import ast
import contextlib
import inspect
import json
import logging
import socket
import sys
import types
import urllib.request
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # hubspot_ticket_automation/
sys.path.insert(0, str(PACKAGE_ROOT / "scripts"))  # scripts/ is imported flat, as run.py does

import pregate_tickets as pg  # noqa: E402
from packn_os_hubspot_client import client  # noqa: E402
from packn_os_hubspot_client.tenant import TENANT_ID  # noqa: E402
from tests.helpers import fakes  # noqa: E402
from ticket_pipeline import hubspot_api, hydrate, kb, llm, source  # noqa: E402
from ticket_pipeline import run as pipeline_run  # noqa: E402

SEARCH_PATH = "/crm/v3/objects/tickets/search"

NO_NETWORK_MESSAGE = "a test in test_source_modes.py tried to open a network connection"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse every outgoing connection for the length of each test in this file.

    The request builders catch OSError and carry on, so the refusal is an
    AssertionError: it cannot be mistaken for a failed call and swallowed.
    """

    def refuse(*_args, **_kwargs):
        raise AssertionError(NO_NETWORK_MESSAGE)

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def test_the_network_is_refused_in_this_file() -> None:
    with pytest.raises(AssertionError, match="network connection"):
        urllib.request.urlopen("https://example.invalid/")
    with pytest.raises(AssertionError, match="network connection"):
        socket.create_connection(("example.invalid", 443))


# --------------------------------------------------------------------------- PIN
#
# Literals copied from the code as it stood on `main` (063b75f). They are not built
# from the functions under test: a literal is the only thing a moved function can be
# compared with.

PINNED_BODY = {
    "filterGroups": [
        {
            "filters": [
                {"propertyName": "hs_lastmodifieddate", "operator": "GT", "value": 123456},
                {"propertyName": "hs_pipeline_stage", "operator": "IN", "values": ["1", "3"]},
                {
                    "propertyName": "hs_last_message_from_visitor",
                    "operator": "EQ",
                    "value": "true",
                },
            ]
        }
    ],
    "properties": ["hs_lastmodifieddate"],
    "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
    "limit": 25,
}

PINNED_COMPACT_JSON = (
    '{"filterGroups":[{"filters":[{"propertyName":"hs_lastmodifieddate","operator":"GT",'
    '"value":123456},{"propertyName":"hs_pipeline_stage","operator":"IN","values":["1","3"]},'
    '{"propertyName":"hs_last_message_from_visitor","operator":"EQ","value":"true"}]}],'
    '"properties":["hs_lastmodifieddate"],"sorts":[{"propertyName":"hs_lastmodifieddate",'
    '"direction":"ASCENDING"}],"limit":25}'
)

# What goes on the wire: both request builders serialise with json.dumps' defaults.
PINNED_WIRE_JSON = (
    '{"filterGroups": [{"filters": [{"propertyName": "hs_lastmodifieddate", "operator": "GT", '
    '"value": 123456}, {"propertyName": "hs_pipeline_stage", "operator": "IN", "values": '
    '["1", "3"]}, {"propertyName": "hs_last_message_from_visitor", "operator": "EQ", "value": '
    '"true"}]}], "properties": ["hs_lastmodifieddate"], "sorts": [{"propertyName": '
    '"hs_lastmodifieddate", "direction": "ASCENDING"}], "limit": 25}'
)

PINNED_COMPACT_JSON_NO_VISITOR_FILTER = (
    '{"filterGroups":[{"filters":[{"propertyName":"hs_lastmodifieddate","operator":"GT",'
    '"value":123456},{"propertyName":"hs_pipeline_stage","operator":"IN","values":["1","3"]}]}],'
    '"properties":["hs_lastmodifieddate"],"sorts":[{"propertyName":"hs_lastmodifieddate",'
    '"direction":"ASCENDING"}],"limit":25}'
)

# The property list the pipeline asks for, before the settings' custom properties.
PINNED_PIPELINE_BASE_PROPERTIES = [
    "subject",
    "content",
    "hs_pipeline_stage",
    "hs_ticket_priority",
    "hs_lastmodifieddate",
    "createdate",
    "source_type",
    "hs_last_message_from_visitor",
]

# 123456 ms after the epoch, so a state file can reproduce the pinned `GT` value.
LAST_RUN_AT_FOR_PIN = "1970-01-01T00:02:03.456Z"

SETTINGS = {
    "active_stages": ["1", "3"],
    "require_last_message_from_visitor": True,
    "per_run_cap": 25,
    "ticket_custom_properties": ["topic_of_ticket", "order_number"],
    "pipeline": {"shadow_enabled": True, "shadow_limit": 25},
}


def _compact(value) -> str:
    return json.dumps(value, separators=(",", ":"))


def _absent_setting() -> dict:
    return {"found": False, "value": None}


@pytest.fixture
def pipeline_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the pipeline at a settings file, a state file and an output directory
    under the pytest temp directory, and give it a token without a token file."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(SETTINGS), encoding="utf-8")  # JSON is valid YAML
    categories_path = tmp_path / "categories.json"
    categories_path.write_text("{}", encoding="utf-8")
    state_path = fakes.temp_state_file(
        tmp_path, {"last_run_at": LAST_RUN_AT_FOR_PIN, "ticket_fingerprints": {}}
    )
    monkeypatch.setattr(pipeline_run, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(pipeline_run, "CATEGORIES_PATH", categories_path)
    monkeypatch.setattr(pipeline_run, "STATE_PATH", state_path)
    monkeypatch.setattr(pipeline_run, "SHADOW_DIR", tmp_path / "engine-shadow-out")
    monkeypatch.setattr(hubspot_api, "read_token", lambda: "test-token")
    monkeypatch.delenv("PACKN_SOURCE_MODE", raising=False)
    return tmp_path


def test_pin_search_body_is_todays_dict() -> None:
    assert pg.build_search_body(123456, ["1", "3"], True, 25) == PINNED_BODY


def test_pin_search_body_is_todays_compact_json() -> None:
    assert _compact(pg.build_search_body(123456, ["1", "3"], True, 25)) == PINNED_COMPACT_JSON


def test_pin_search_body_is_todays_bytes_on_the_wire() -> None:
    body = pg.build_search_body(123456, ["1", "3"], True, 25)
    assert json.dumps(body).encode("utf-8") == PINNED_WIRE_JSON.encode("utf-8")


def test_pin_search_body_without_the_visitor_filter() -> None:
    body = pg.build_search_body(123456, ["1", "3"], False, 25)
    assert _compact(body) == PINNED_COMPACT_JSON_NO_VISITOR_FILTER


def test_pin_pipeline_base_property_list_is_todays() -> None:
    assert hubspot_api.TICKET_BASE_PROPERTIES == PINNED_PIPELINE_BASE_PROPERTIES


def test_pin_pipeline_search_request_with_the_setting_absent(
    pipeline_files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine-shadow pipeline's one search, end to end, with no source setting stored."""
    # `raising=False`: the reader did not exist when this pin was first run.
    monkeypatch.setattr(client, "read_ticket_source_mode", _absent_setting, raising=False)
    transport = fakes.FakeHubSpotTransport({SEARCH_PATH: {"results": []}})
    monkeypatch.setattr(hubspot_api, "_request", transport)

    assert pipeline_run.run_shadow() == 0

    assert transport.calls == [("POST", SEARCH_PATH)]
    expected = dict(PINNED_BODY)
    expected["properties"] = PINNED_PIPELINE_BASE_PROPERTIES + ["topic_of_ticket", "order_number"]
    assert transport.bodies == [expected]
    assert list(transport.bodies[0]) == list(expected)  # key order is part of the bytes


# -------------------------------------------------------------------------- MODE
#
# One setting, three values. The reader has three outcomes and no fourth: a valid
# mode, `source_unreadable`, `source_unknown`. Only an ABSENT row means `hubspot`.

# The six stored values Plan 93-08's parser reports as unrecognised on the OS side.
UNRECOGNISED_STORED_VALUES = [
    pytest.param(None, id="stored-json-null"),
    pytest.param({}, id="empty-object"),
    pytest.param({"mode": "OS"}, id="upper-case"),
    pytest.param({"mode": 3}, id="number"),
    pytest.param("os", id="bare-string"),
    pytest.param(["os"], id="list"),
]

MARKER = "ZZ-SOURCE-MARKER"


def _reader(found: bool, value=None):
    return lambda: {"found": found, "value": value}


def _raising_reader(message: str = "database is down"):
    def read():
        raise RuntimeError(message)

    return read


def _code_of(call) -> str:
    with pytest.raises(source.SourceModeError) as caught:
        call()
    return caught.value.code


def test_mode_constants() -> None:
    assert source.MODES == ("hubspot", "shadow", "os")
    assert (source.MODE_HUBSPOT, source.MODE_SHADOW, source.MODE_OS) == source.MODES
    assert source.DEFAULT_MODE == "hubspot"
    assert source.SETTING_KEY == "ticket_source_mode"
    assert source.MODE_ENV == "PACKN_SOURCE_MODE"
    assert source.SOURCE_UNREADABLE == "source_unreadable"
    assert source.SOURCE_UNKNOWN == "source_unknown"


def test_source_mode_error_carries_its_code() -> None:
    error = source.SourceModeError("source_unknown")
    assert error.code == "source_unknown"
    assert str(error) == "source_unknown"


def test_source_mode_error_refuses_a_code_outside_the_two() -> None:
    with pytest.raises(ValueError):
        source.SourceModeError("hubspot")


def test_parse_mode_absent_row_means_hubspot() -> None:
    assert source.parse_mode(False, None) == "hubspot"


@pytest.mark.parametrize("mode", ["hubspot", "shadow", "os"])
def test_parse_mode_valid_stored_object(mode: str) -> None:
    assert source.parse_mode(True, {"mode": mode}) == mode


def test_parse_mode_ignores_other_keys_in_the_stored_object() -> None:
    assert source.parse_mode(True, {"mode": "os", "extra": 1}) == "os"


@pytest.mark.parametrize("value", UNRECOGNISED_STORED_VALUES)
def test_parse_mode_unrecognised_stored_value_is_source_unknown(value) -> None:
    assert _code_of(lambda: source.parse_mode(True, value)) == "source_unknown"


@pytest.mark.parametrize(
    "value",
    [{"mode": " os"}, {"mode": "os "}, {"mode": None}, {"mode": ["os"]}, {"Mode": "os"}, True, 0],
)
def test_parse_mode_is_exact_about_the_three_values(value) -> None:
    assert _code_of(lambda: source.parse_mode(True, value)) == "source_unknown"


@pytest.mark.parametrize("mode", ["hubspot", "shadow", "os"])
def test_validate_mode_returns_a_known_mode(mode: str) -> None:
    assert source.validate_mode(mode) == mode


@pytest.mark.parametrize("mode", ["banana", "OS", "", None, 3])
def test_validate_mode_refuses_anything_else(mode) -> None:
    assert _code_of(lambda: source.validate_mode(mode)) == "source_unknown"


def test_resolve_mode_absent_row_is_hubspot() -> None:
    assert source.resolve_mode(_reader(False)) == "hubspot"


@pytest.mark.parametrize("mode", ["hubspot", "shadow", "os"])
def test_resolve_mode_returns_the_stored_mode(mode: str) -> None:
    assert source.resolve_mode(_reader(True, {"mode": mode})) == mode


def test_resolve_mode_reader_raising_is_source_unreadable() -> None:
    assert _code_of(lambda: source.resolve_mode(_raising_reader())) == "source_unreadable"


def test_resolve_mode_unknown_value_is_source_unknown() -> None:
    reader = _reader(True, {"mode": "banana"})
    assert _code_of(lambda: source.resolve_mode(reader)) == "source_unknown"


def test_resolve_mode_stored_null_is_source_unknown_never_hubspot() -> None:
    assert _code_of(lambda: source.resolve_mode(_reader(True, None))) == "source_unknown"


@pytest.mark.parametrize(
    "result",
    [None, {}, {"value": {"mode": "os"}}, {"found": "yes", "value": {"mode": "os"}}, ["found"]],
)
def test_resolve_mode_malformed_reader_result_is_source_unreadable(result) -> None:
    """A reader that does not say whether a row was found has not read the setting."""
    assert _code_of(lambda: source.resolve_mode(lambda: result)) == "source_unreadable"


def test_resolve_mode_default_reader_is_the_client_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def read() -> dict:
        calls.append(1)
        return {"found": True, "value": {"mode": "shadow"}}

    monkeypatch.setattr(client, "read_ticket_source_mode", read)
    assert source.resolve_mode() == "shadow"
    assert calls == [1]


def test_resolve_mode_with_no_database_is_source_unreadable_not_a_default() -> None:
    """The real client read, under the pure-suite guard: the pool getter raises, the
    reader has no handler, and the mode read reports it as unreadable."""
    assert _code_of(source.resolve_mode) == "source_unreadable"


def test_a_failed_mode_read_carries_no_text_of_the_cause(caplog, capfd) -> None:
    caplog.set_level(logging.DEBUG)
    with pytest.raises(source.SourceModeError) as caught:
        source.resolve_mode(_raising_reader(f"connection refused for user {MARKER}"))
    assert caught.value.code == "source_unreadable"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert MARKER not in repr(caught.value)
    fakes.assert_marker_absent(MARKER, caplog, capfd)


def test_mode_from_env_reads_the_given_environment() -> None:
    assert source.mode_from_env({"PACKN_SOURCE_MODE": "shadow"}) == "shadow"


@pytest.mark.parametrize("environ", [{}, {"PACKN_SOURCE_MODE": ""}])
def test_mode_from_env_unset_or_empty_is_none(environ: dict) -> None:
    assert source.mode_from_env(environ) is None
    assert source.current_mode(_reader(True, {"mode": "shadow"}), environ) == "shadow"


@pytest.mark.parametrize("value", ["banana", "OS", " os", "os\n"])
def test_mode_from_env_unknown_value_is_source_unknown(value: str) -> None:
    assert _code_of(lambda: source.mode_from_env({"PACKN_SOURCE_MODE": value})) == "source_unknown"


def test_mode_from_env_defaults_to_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PACKN_SOURCE_MODE", "os")
    assert source.mode_from_env() == "os"


def test_current_mode_takes_the_environment_value_without_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PACKN_SOURCE_MODE", "os")
    assert source.current_mode(_raising_reader()) == "os"


def test_current_mode_unknown_environment_value_is_source_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PACKN_SOURCE_MODE", "banana")
    # the reader would answer `hubspot`: an unknown value is not replaced by a read
    assert _code_of(lambda: source.current_mode(_reader(False))) == "source_unknown"


def test_current_mode_reads_the_setting_when_the_variable_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PACKN_SOURCE_MODE", raising=False)
    assert source.current_mode(_reader(True, {"mode": "shadow"})) == "shadow"
    assert source.current_mode(_reader(False)) == "hubspot"
    assert _code_of(lambda: source.current_mode(_raising_reader())) == "source_unreadable"


# ------------------------------------------------- client.read_ticket_source_mode


class _RecordingCursor:
    def __init__(self, row, error: Exception | None = None) -> None:
        self.row = row
        self.error = error
        self.executed: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def execute(self, sql, params=None) -> None:
        self.executed.append((sql, params))
        if self.error is not None:
            raise self.error

    def fetchone(self):
        return self.row


class _RecordingPool:
    """Stands in for the helper's pool: `with pool.connection() as conn`."""

    def __init__(self, row=None, error: Exception | None = None) -> None:
        self.cursor = _RecordingCursor(row, error)
        self.connections = 0

    @contextlib.contextmanager
    def connection(self):
        self.connections += 1
        yield types.SimpleNamespace(cursor=lambda **_kwargs: self.cursor)


def _one_line(sql: str) -> str:
    return " ".join(sql.split()).lower()


def test_read_ticket_source_mode_runs_one_tenant_qualified_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _RecordingPool(row={"value": {"mode": "os"}})
    monkeypatch.setattr(client, "get_pool", lambda: pool)

    client.read_ticket_source_mode()

    assert pool.connections == 1
    assert len(pool.cursor.executed) == 1
    sql, params = pool.cursor.executed[0]
    assert _one_line(sql) == "select value from app_settings where tenant_id = %s and key = %s"
    assert params == (TENANT_ID, "ticket_source_mode")
    assert params[1] == source.SETTING_KEY


def test_read_ticket_source_mode_absent_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "get_pool", lambda: _RecordingPool(row=None))
    assert client.read_ticket_source_mode() == {"found": False, "value": None}


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param({"mode": "os"}, id="object"),
        pytest.param({"mode": "os", "extra": 1}, id="object-with-other-keys"),
        pytest.param(None, id="stored-json-null"),
        pytest.param("os", id="bare-string"),
    ],
)
def test_read_ticket_source_mode_returns_the_value_as_stored(
    monkeypatch: pytest.MonkeyPatch, stored
) -> None:
    monkeypatch.setattr(client, "get_pool", lambda: _RecordingPool(row={"value": stored}))
    assert client.read_ticket_source_mode() == {"found": True, "value": stored}


def test_a_stored_null_and_a_missing_row_are_told_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "get_pool", lambda: _RecordingPool(row={"value": None}))
    assert _code_of(source.resolve_mode) == "source_unknown"
    monkeypatch.setattr(client, "get_pool", lambda: _RecordingPool(row=None))
    assert source.resolve_mode() == "hubspot"


def test_read_ticket_source_mode_lets_a_database_error_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _RecordingPool(error=LookupError("permission denied for table app_settings"))
    monkeypatch.setattr(client, "get_pool", lambda: pool)
    with pytest.raises(LookupError):
        client.read_ticket_source_mode()


def test_read_ticket_source_mode_has_no_except_clause() -> None:
    text = inspect.getsource(client.read_ticket_source_mode)
    handlers = [
        node for node in ast.walk(ast.parse(text)) if isinstance(node, (ast.Try, ast.ExceptHandler))
    ]
    assert handlers == []
    assert "except" not in text
    assert "app_settings" in text  # a real read, not a stub with nothing to catch


# ---------------------------------------------------- the shared candidate rule


def _hit(ticket_id: str, lastmod: str | None) -> dict:
    return {"id": ticket_id, "properties": {"hs_lastmodifieddate": lastmod}}


def test_the_candidate_functions_live_once_and_the_pregate_re_exports_them() -> None:
    for name in ("iso_to_ms", "parse_last_run_at", "build_search_body", "unprocessed_candidates"):
        assert getattr(pg, name) is getattr(source, name), name
        assert getattr(source, name).__module__ == "ticket_pipeline.source", name
    assert pipeline_run.build_search_body is source.build_search_body
    assert pipeline_run.parse_last_run_at is source.parse_last_run_at
    assert pipeline_run.unprocessed_candidates is source.unprocessed_candidates


def test_the_moved_search_body_is_the_pinned_one() -> None:
    assert source.build_search_body(123456, ["1", "3"], True, 25) == PINNED_BODY
    assert _compact(source.build_search_body(123456, ["1", "3"], True, 25)) == PINNED_COMPACT_JSON
    assert source.iso_to_ms(LAST_RUN_AT_FOR_PIN) == 123456
    assert source.parse_last_run_at(LAST_RUN_AT_FOR_PIN) == 123456
    assert source.parse_last_run_at(None, now_ms=86_400_000 + 5) == 5


def test_a_code_written_fingerprint_is_judged_by_processed_at() -> None:
    """R15 groundwork: `os` mode writes {processed_at, hs_lastmodifieddate, num_notes: null}.
    After a rollback to `hubspot` the ticket is not a candidate until it changes again."""
    fingerprints = {
        "1": {
            "processed_at": "2026-10-01T12:00:00Z",  # T: the os-mode pass finished
            "hs_lastmodifieddate": "2026-10-01T11:00:00Z",  # U: the mirrored value, older
            "num_notes": None,
        }
    }
    before = [_hit("1", "2026-10-01T11:30:00Z")]  # after U, before T
    at = [_hit("1", "2026-10-01T12:00:00Z")]
    after = [_hit("1", "2026-10-01T12:00:01Z")]
    assert source.unprocessed_candidates(before, fingerprints) == []
    assert source.unprocessed_candidates(at, fingerprints) == []
    assert source.unprocessed_candidates(after, fingerprints) == ["1"]


def test_a_code_written_fingerprint_with_no_hubspot_timestamp_still_dedupes() -> None:
    fingerprints = {
        "1": {"processed_at": "2026-10-01T12:00:00Z", "hs_lastmodifieddate": None, "num_notes": None}
    }
    assert source.unprocessed_candidates([_hit("1", "2026-10-01T11:59:59Z")], fingerprints) == []
    assert source.unprocessed_candidates([_hit("1", "2026-10-02T00:00:00Z")], fingerprints) == ["1"]


def test_the_candidate_rule_states_the_code_written_case() -> None:
    doc = " ".join((source.unprocessed_candidates.__doc__ or "").split())
    assert "num_notes" in doc and "processed_at" in doc
    assert "written by code" in doc


# ----------------------------------------------------- READERS: the pre-gate


STATE = {
    "last_run_at": LAST_RUN_AT_FOR_PIN,
    "ticket_fingerprints": {
        "20": {"processed_at": "2026-10-01T12:00:00Z", "hs_lastmodifieddate": None, "num_notes": None},
        "30": {"num_notes": 2, "hs_lastmodifieddate": "2026-10-01T12:00:00Z"},
    },
}

NEW_HIT = _hit("10", "2026-10-02T09:00:00Z")
DEDUPED_HIT = _hit("20", "2026-10-01T11:00:00Z")
OTHER_DEDUPED_HIT = _hit("30", "2026-10-01T12:00:00Z")


def _transport(results=None) -> fakes.FakeHubSpotTransport:
    transport = fakes.FakeHubSpotTransport()
    if results is not None:
        transport.respond(SEARCH_PATH, {"total": len(results), "results": results})
    return transport


def test_hubspot_candidates_sends_exactly_the_pinned_search() -> None:
    transport = _transport([])
    pg.hubspot_candidates("hubspot", SETTINGS, STATE, "token", transport=transport)
    assert transport.calls == [("POST", SEARCH_PATH)]
    assert transport.bodies == [PINNED_BODY]
    assert json.dumps(transport.bodies[0]) == PINNED_WIRE_JSON


def test_hubspot_candidates_with_candidates_runs_the_agent() -> None:
    transport = _transport([NEW_HIT, DEDUPED_HIT])
    assert pg.hubspot_candidates("hubspot", SETTINGS, STATE, "token", transport=transport) == {
        "decision": "run_agent",
        "reason": "candidates",
        "matched": [
            {"id": "10", "hs_lastmodifieddate": "2026-10-02T09:00:00Z", "deduped": False},
            {"id": "20", "hs_lastmodifieddate": "2026-10-01T11:00:00Z", "deduped": True},
        ],
        "candidates": ["10"],
        "page_full": False,
    }


def test_hubspot_candidates_full_page_all_deduped_runs_the_agent() -> None:
    settings = dict(SETTINGS, per_run_cap=2)
    transport = _transport([DEDUPED_HIT, OTHER_DEDUPED_HIT])
    result = pg.hubspot_candidates("hubspot", settings, STATE, "token", transport=transport)
    assert result["decision"] == "run_agent"
    assert result["reason"] == "page_full"
    assert result["candidates"] == []
    assert [hit["deduped"] for hit in result["matched"]] == [True, True]
    assert result["page_full"] is True


def test_hubspot_candidates_full_page_with_a_candidate_reports_both() -> None:
    settings = dict(SETTINGS, per_run_cap=2)
    transport = _transport([NEW_HIT, DEDUPED_HIT])
    result = pg.hubspot_candidates("hubspot", settings, STATE, "token", transport=transport)
    assert (result["decision"], result["reason"], result["page_full"]) == (
        "run_agent",
        "candidates",
        True,
    )


@pytest.mark.parametrize(
    "results",
    [pytest.param([], id="nothing-matched"), pytest.param([DEDUPED_HIT], id="all-deduped")],
)
def test_hubspot_candidates_none_is_no_work(results: list) -> None:
    result = pg.hubspot_candidates("hubspot", SETTINGS, STATE, "token", transport=_transport(results))
    assert (result["decision"], result["reason"]) == ("no_work", "none")
    assert result["candidates"] == []
    assert result["page_full"] is False
    assert len(result["matched"]) == len(results)


def test_hubspot_candidates_failed_search_runs_the_agent_not_no_work() -> None:
    """The pre-gate's fail-open branch: the transport answers nothing."""
    transport = _transport()  # nothing canned: every request answers None
    assert pg.hubspot_candidates("hubspot", SETTINGS, STATE, "token", transport=transport) == {
        "decision": "run_agent",
        "reason": "search_failed",
        "matched": [],
        "candidates": [],
        "page_full": False,
    }
    assert transport.calls == [("POST", SEARCH_PATH)]


@pytest.mark.parametrize("answer", [{}, {"total": 0}, [], "ok"])
def test_hubspot_candidates_an_answer_without_results_is_a_failed_search(answer) -> None:
    transport = fakes.FakeHubSpotTransport({SEARCH_PATH: answer})
    result = pg.hubspot_candidates("hubspot", SETTINGS, STATE, "token", transport=transport)
    assert (result["decision"], result["reason"]) == ("run_agent", "search_failed")


def test_hubspot_candidates_os_mode_makes_no_hubspot_call() -> None:
    transport = _transport([NEW_HIT])
    assert pg.hubspot_candidates("os", SETTINGS, STATE, "token", transport=transport) == {
        "decision": "no_work",
        "reason": "os_mode",
        "matched": [],
        "candidates": [],
        "page_full": False,
    }
    assert transport.calls == []


def test_hubspot_candidates_shadow_mode_searches_as_hubspot_mode_does() -> None:
    in_shadow = _transport([NEW_HIT, DEDUPED_HIT])
    in_hubspot = _transport([NEW_HIT, DEDUPED_HIT])
    assert pg.hubspot_candidates(
        "shadow", SETTINGS, STATE, "token", transport=in_shadow
    ) == pg.hubspot_candidates("hubspot", SETTINGS, STATE, "token", transport=in_hubspot)
    assert in_shadow.bodies == [PINNED_BODY]


@pytest.mark.parametrize("mode", ["banana", None, "OS"])
def test_hubspot_candidates_unknown_mode_is_an_error_and_no_call(mode) -> None:
    transport = _transport([NEW_HIT])

    def call():
        return pg.hubspot_candidates(mode, SETTINGS, STATE, "token", transport=transport)

    assert _code_of(call) == "source_unknown"
    assert transport.calls == []


def test_hubspot_candidates_does_not_change_the_state_it_was_handed() -> None:
    state = json.loads(json.dumps(STATE))
    pg.hubspot_candidates("hubspot", SETTINGS, state, "token", transport=_transport([NEW_HIT]))
    assert state == STATE


class _PregateWorld:
    """Everything `pregate_tickets.main` touches, replaced: the client, the settings and
    state loaders, the token and the candidate decision."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.records: list[dict] = []
        self.candidate_calls: list[tuple] = []
        self.enabled = True
        self.reruns: list[dict] = []
        self.settings: dict | Exception = dict(SETTINGS)
        self.token: str | None = "test-token"
        self.decision: dict = _decision("no_work", "none")
        monkeypatch.delenv("PACKN_SOURCE_MODE", raising=False)
        monkeypatch.setattr(client, "report_routine_schedule", lambda *_a, **_k: None)
        monkeypatch.setattr(client, "read_routine_enabled", lambda _name: self.enabled)
        monkeypatch.setattr(client, "read_pending_rerun_requests", lambda _name: self.reruns)
        monkeypatch.setattr(client, "write_run_record", lambda **kw: self.records.append(kw))
        monkeypatch.setattr(client, "read_ticket_source_mode", _absent_setting)
        monkeypatch.setattr(pg, "_load_settings", self._load_settings)
        monkeypatch.setattr(pg, "_load_state", lambda: json.loads(json.dumps(STATE)))
        monkeypatch.setattr(hubspot_api, "read_token", lambda: self.token)
        monkeypatch.setattr(pg, "hubspot_candidates", self._hubspot_candidates)

    def _load_settings(self) -> dict:
        if isinstance(self.settings, Exception):
            raise self.settings
        return self.settings

    def _hubspot_candidates(self, mode, settings, state, token, transport=None) -> dict:
        self.candidate_calls.append((mode, settings, state, token))
        return self.decision

    def statuses(self) -> list[str]:
        return [record["status"] for record in self.records]


def _decision(decision: str, reason: str) -> dict:
    return {"decision": decision, "reason": reason, "matched": [], "candidates": [], "page_full": False}


@pytest.fixture
def pregate_world(monkeypatch: pytest.MonkeyPatch) -> _PregateWorld:
    return _PregateWorld(monkeypatch)


@pytest.mark.parametrize(
    "decision, exit_code, statuses",
    [
        pytest.param(_decision("run_agent", "candidates"), 0, [], id="candidates"),
        pytest.param(_decision("run_agent", "page_full"), 0, [], id="page-full"),
        pytest.param(_decision("run_agent", "search_failed"), 0, [], id="search-failed"),
        pytest.param(_decision("no_work", "none"), 10, ["success"], id="no-work"),
    ],
)
def test_main_in_hubspot_mode_keeps_todays_exit_codes_and_records(
    pregate_world: _PregateWorld, decision: dict, exit_code: int, statuses: list
) -> None:
    pregate_world.decision = decision
    assert pg.main() == exit_code
    assert pregate_world.statuses() == statuses


def test_main_hands_the_one_mode_the_settings_the_state_and_the_token_over(
    pregate_world: _PregateWorld,
) -> None:
    pg.main()
    assert pregate_world.candidate_calls == [("hubspot", SETTINGS, STATE, "test-token")]


def test_main_in_shadow_mode_decides_as_in_hubspot_mode(
    pregate_world: _PregateWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "read_ticket_source_mode", _reader(True, {"mode": "shadow"}))
    pregate_world.decision = _decision("run_agent", "candidates")
    assert pg.main() == 0
    assert [call[0] for call in pregate_world.candidate_calls] == ["shadow"]
    assert pregate_world.records == []


def test_main_takes_the_mode_from_the_environment_before_the_setting(
    pregate_world: _PregateWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PACKN_SOURCE_MODE", "shadow")
    monkeypatch.setattr(client, "read_ticket_source_mode", _raising_reader())
    pg.main()
    assert [call[0] for call in pregate_world.candidate_calls] == ["shadow"]


def test_main_paused_routine_skips_as_today(pregate_world: _PregateWorld) -> None:
    pregate_world.enabled = False
    assert pg.main() == 11
    assert pregate_world.statuses() == ["skipped"]
    assert pregate_world.candidate_calls == []


def test_main_pending_rerun_runs_the_agent_without_a_search(pregate_world: _PregateWorld) -> None:
    pregate_world.reruns = [{"id": "r1"}]
    assert pg.main() == 0
    assert pregate_world.records == []
    assert pregate_world.candidate_calls == []


@pytest.mark.parametrize(
    "break_it",
    [
        pytest.param(lambda w: setattr(w, "settings", OSError("unreadable")), id="settings-unreadable"),
        pytest.param(lambda w: setattr(w, "settings", {"active_stages": []}), id="no-active-stages"),
        pytest.param(
            lambda w: setattr(w, "settings", dict(SETTINGS, per_run_cap="many")), id="cap-not-a-number"
        ),
        pytest.param(lambda w: setattr(w, "token", None), id="token-missing"),
    ],
)
def test_main_caller_side_doubts_still_fail_open(pregate_world: _PregateWorld, break_it) -> None:
    break_it(pregate_world)
    assert pg.main() == 0
    assert pregate_world.records == []
    assert pregate_world.candidate_calls == []


def test_main_in_os_mode_skips_the_agent_and_never_asks_hubspot(
    pregate_world: _PregateWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "read_ticket_source_mode", _reader(True, {"mode": "os"}))
    assert pg.main() == 11
    assert pregate_world.candidate_calls == []
    assert pregate_world.statuses() == ["skipped"]
    assert pregate_world.records[0]["error_summary"] == "pregate: source_mode_os_not_served"


@pytest.mark.parametrize(
    "reader, code",
    [
        pytest.param(_raising_reader(f"refused {MARKER}"), "source_unreadable", id="unreadable"),
        pytest.param(_reader(True, {"mode": "banana"}), "source_unknown", id="unknown"),
        pytest.param(_reader(True, None), "source_unknown", id="stored-null"),
    ],
)
def test_main_never_folds_a_failed_mode_read_into_a_mode(
    pregate_world: _PregateWorld, monkeypatch: pytest.MonkeyPatch, reader, code: str
) -> None:
    """Neither `hubspot` (launch the agent) nor a quiet success: the run stops with a
    failure record that names the code, and nothing is asked of HubSpot."""
    monkeypatch.setattr(client, "read_ticket_source_mode", reader)
    assert pg.main() == 11
    assert pregate_world.candidate_calls == []
    assert pregate_world.statuses() == ["failure"]
    assert pregate_world.records[0]["error_summary"] == f"pregate: {code}"
    assert pregate_world.records[0]["tickets_processed"] == 0


# ------------------------------------------- READERS: the engine-shadow pipeline


class _Models:
    """The three model calls and the KB load, replaced; records what was classified."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.classified: list[str] = []
        monkeypatch.setattr(llm, "classify", self._classify)
        monkeypatch.setattr(llm, "draft", self._draft)
        monkeypatch.setattr(llm, "extract_actions", self._extract)
        monkeypatch.setattr(kb, "load_kb_context", lambda *_a, **_k: "kb text")

    @staticmethod
    def _usage(usages: list) -> None:
        usages.append({"model": "claude-haiku-4-5", "input_tokens": 10, "output_tokens": 5})

    def _classify(self, ctx, settings, categories_cfg, usages) -> dict:
        self.classified.append(ctx["ticket_id"])
        self._usage(usages)
        return {"category": "wismo_tracking", "confidence": 0.9, "priority": "normal"}

    def _draft(self, ctx, category, kb_context, usages) -> str:
        self._usage(usages)
        return f"draft for {ctx['ticket_id']}"

    def _extract(self, ctx, category, draft_body, usages) -> list:
        self._usage(usages)
        return [{"action_type": "follow_up"}]


def _context(ticket_id: str) -> dict:
    return {
        "ticket_id": ticket_id,
        "ticket_link": f"https://example.invalid/ticket/{ticket_id}",
        "subject": "Where is my order",
        "topic_of_ticket": None,
        "source_type": "EMAIL",
        "related_tickets": [],
    }


@pytest.fixture
def no_hydration(monkeypatch: pytest.MonkeyPatch) -> list:
    """`hydrate_ticket` replaced by a recorder that builds a context from the hit."""
    hydrated: list[str] = []

    def hydrate_ticket(ticket, settings, token) -> dict:
        hydrated.append(str(ticket.get("id")))
        return _context(str(ticket.get("id")))

    monkeypatch.setattr(hydrate, "hydrate_ticket", hydrate_ticket)
    return hydrated


def _hubspot_transport(monkeypatch: pytest.MonkeyPatch, results=None) -> fakes.FakeHubSpotTransport:
    transport = _transport(results)
    monkeypatch.setattr(hubspot_api, "_request", transport)
    return transport


def _artifacts(root: Path) -> dict[str, dict]:
    out_root = root / "engine-shadow-out"
    if not out_root.exists():
        return {}
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(out_root.glob("*/*.json"))
    }


def _forbid_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def read_token():
        raise AssertionError("the HubSpot token was read")

    monkeypatch.setattr(hubspot_api, "read_token", read_token)


def test_run_shadow_os_mode_without_contexts_does_nothing(
    pipeline_files: Path, monkeypatch: pytest.MonkeyPatch, no_hydration: list
) -> None:
    transport = _hubspot_transport(monkeypatch, [NEW_HIT])
    _forbid_token(monkeypatch)
    models = _Models(monkeypatch)

    assert pipeline_run.run_shadow(mode="os") == 0
    assert pipeline_run.run_shadow(mode="os", contexts=[]) == 0

    assert transport.calls == []
    assert no_hydration == []
    assert models.classified == []
    assert _artifacts(pipeline_files) == {}


def test_run_shadow_os_mode_processes_the_contexts_it_was_handed(
    pipeline_files: Path, monkeypatch: pytest.MonkeyPatch, no_hydration: list
) -> None:
    transport = _hubspot_transport(monkeypatch, [NEW_HIT])
    _forbid_token(monkeypatch)
    models = _Models(monkeypatch)

    assert pipeline_run.run_shadow(mode="os", contexts=[_context("900"), _context("901")]) == 0

    assert transport.calls == []  # no HubSpot call of any kind
    assert no_hydration == []  # and no hydrate_ticket call
    assert models.classified == ["900", "901"]
    artifacts = _artifacts(pipeline_files)
    assert sorted(artifacts) == ["900.json", "901.json", "_summary.json"]
    assert artifacts["900.json"]["classification"]["category"] == "wismo_tracking"
    assert artifacts["900.json"]["draft_body"] == "draft for 900"
    assert artifacts["900.json"]["action_items"] == [{"action_type": "follow_up"}]
    assert artifacts["900.json"]["ticket_context"] == _context("900")
    assert artifacts["_summary.json"]["totals"]["ok"] == 2
    assert artifacts["_summary.json"]["totals"]["error"] == 0


def test_run_shadow_os_mode_honours_the_limit(
    pipeline_files: Path, monkeypatch: pytest.MonkeyPatch, no_hydration: list
) -> None:
    _hubspot_transport(monkeypatch)
    models = _Models(monkeypatch)
    contexts = [_context("900"), _context("901"), _context("902")]
    assert pipeline_run.run_shadow(limit=2, mode="os", contexts=contexts) == 0
    assert models.classified == ["900", "901"]


def test_run_shadow_os_mode_does_not_serve_the_single_ticket_search(
    pipeline_files: Path, monkeypatch: pytest.MonkeyPatch, no_hydration: list
) -> None:
    transport = _hubspot_transport(monkeypatch, [NEW_HIT])
    _forbid_token(monkeypatch)
    models = _Models(monkeypatch)
    assert pipeline_run.run_shadow(ticket_id="10", mode="os") == 0
    assert transport.calls == []
    assert models.classified == []


def test_run_shadow_os_mode_keeps_an_artifact_inside_the_run_directory(
    pipeline_files: Path, monkeypatch: pytest.MonkeyPatch, no_hydration: list
) -> None:
    _hubspot_transport(monkeypatch)
    _Models(monkeypatch)
    assert pipeline_run.run_shadow(mode="os", contexts=[_context("../../escaped")]) == 0
    assert not (pipeline_files / "escaped.json").exists()
    assert not (pipeline_files / "engine-shadow-out" / "escaped.json").exists()
    names = [name for name in _artifacts(pipeline_files) if name != "_summary.json"]
    assert len(names) == 1
    assert ".." not in names[0]


def test_run_shadow_os_mode_still_obeys_the_engine_shadow_setting(
    pipeline_files: Path, monkeypatch: pytest.MonkeyPatch, no_hydration: list
) -> None:
    """D-08: `pipeline.shadow_enabled` is the engine shadow's own switch in every mode."""
    disabled = dict(SETTINGS, pipeline={"shadow_enabled": False})
    pipeline_run.SETTINGS_PATH.write_text(json.dumps(disabled), encoding="utf-8")
    models = _Models(monkeypatch)
    assert pipeline_run.run_shadow(mode="os", contexts=[_context("900")]) == 0
    assert models.classified == []


@pytest.mark.parametrize("mode", ["hubspot", "shadow"])
def test_run_shadow_searches_dedupes_and_hydrates_outside_os_mode(
    pipeline_files: Path, monkeypatch: pytest.MonkeyPatch, no_hydration: list, mode: str
) -> None:
    pipeline_run.STATE_PATH.write_text(json.dumps(STATE), encoding="utf-8")
    transport = _hubspot_transport(monkeypatch, [NEW_HIT, DEDUPED_HIT])
    models = _Models(monkeypatch)

    assert pipeline_run.run_shadow(mode=mode) == 0

    expected = dict(PINNED_BODY)
    expected["properties"] = PINNED_PIPELINE_BASE_PROPERTIES + ["topic_of_ticket", "order_number"]
    assert transport.calls == [("POST", SEARCH_PATH)]
    assert transport.bodies == [expected]
    assert no_hydration == ["10"]  # the deduped hit is not hydrated
    assert models.classified == ["10"]
    assert sorted(_artifacts(pipeline_files)) == ["10.json", "_summary.json"]


def test_run_shadow_outside_os_mode_ignores_handed_contexts(
    pipeline_files: Path, monkeypatch: pytest.MonkeyPatch, no_hydration: list
) -> None:
    transport = _hubspot_transport(monkeypatch, [])
    models = _Models(monkeypatch)
    assert pipeline_run.run_shadow(mode="hubspot", contexts=[_context("900")]) == 0
    assert transport.calls == [("POST", SEARCH_PATH)]
    assert models.classified == []


def test_run_shadow_without_a_mode_reads_the_one_setting(
    pipeline_files: Path, monkeypatch: pytest.MonkeyPatch, no_hydration: list
) -> None:
    transport = _hubspot_transport(monkeypatch, [])
    monkeypatch.setattr(client, "read_ticket_source_mode", _reader(True, {"mode": "os"}))
    assert pipeline_run.run_shadow() == 0
    assert transport.calls == []  # the stored mode is `os`: no search

    monkeypatch.setattr(client, "read_ticket_source_mode", _absent_setting)
    assert pipeline_run.run_shadow() == 0
    assert transport.calls == [("POST", SEARCH_PATH)]  # no row: today's search


def test_run_shadow_takes_the_mode_from_the_environment_without_a_read(
    pipeline_files: Path, monkeypatch: pytest.MonkeyPatch, no_hydration: list
) -> None:
    transport = _hubspot_transport(monkeypatch, [])
    monkeypatch.setattr(client, "read_ticket_source_mode", _raising_reader())
    monkeypatch.setenv("PACKN_SOURCE_MODE", "os")
    assert pipeline_run.run_shadow() == 0
    assert transport.calls == []
    monkeypatch.setenv("PACKN_SOURCE_MODE", "hubspot")
    assert pipeline_run.run_shadow() == 0
    assert transport.calls == [("POST", SEARCH_PATH)]


@pytest.mark.parametrize(
    "reader",
    [
        pytest.param(_raising_reader(), id="unreadable"),
        pytest.param(_reader(True, {"mode": "banana"}), id="unknown"),
    ],
)
def test_run_shadow_with_no_usable_mode_asks_hubspot_nothing(
    pipeline_files: Path, monkeypatch: pytest.MonkeyPatch, no_hydration: list, reader
) -> None:
    transport = _hubspot_transport(monkeypatch, [NEW_HIT])
    models = _Models(monkeypatch)
    monkeypatch.setattr(client, "read_ticket_source_mode", reader)
    assert pipeline_run.run_shadow() == 0  # the engine shadow never fails the cron
    assert transport.calls == []
    assert models.classified == []


def test_run_shadow_given_an_unknown_mode_asks_hubspot_nothing(
    pipeline_files: Path, monkeypatch: pytest.MonkeyPatch, no_hydration: list
) -> None:
    transport = _hubspot_transport(monkeypatch, [NEW_HIT])
    assert pipeline_run.run_shadow(mode="banana") == 0
    assert transport.calls == []


def test_the_engine_shadow_names_are_untouched() -> None:
    """D-08: the engine shadow keeps its setting, its log tag and its directory."""
    text = Path(pipeline_run.__file__).read_text(encoding="utf-8")
    assert 'pipeline_cfg.get("shadow_enabled")' in text
    assert 'print(f"[pipeline.shadow] {msg}", file=sys.stderr)' in text
    assert 'SHADOW_DIR = ROOT / "outputs" / "shadow"' in text


# ----------------------------------------------------------- static: one home


def _module_tree(module) -> ast.Module:
    return ast.parse(Path(module.__file__).read_text(encoding="utf-8"))


def _imports_the_source_module(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "ticket_pipeline.source":
            return True
        if isinstance(node, ast.ImportFrom) and node.module == "ticket_pipeline":
            if any(alias.name == "source" for alias in node.names):
                return True
    return False


def _string_constants(node: ast.AST) -> set[str]:
    return {
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    }


@pytest.mark.parametrize("module", [pg, pipeline_run], ids=["pregate", "pipeline"])
def test_each_reader_takes_the_mode_from_the_one_module(module) -> None:
    tree = _module_tree(module)
    assert _imports_the_source_module(tree)
    # no list of the three mode names of its own
    collections = [
        node for node in ast.walk(tree) if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict))
    ]
    for collection in collections:
        assert not {"hubspot", "shadow", "os"} <= _string_constants(collection)
    # and no mode spelled as a literal to compare with: the names come from source
    assert "os" not in _string_constants(tree)
    assert "hubspot" not in _string_constants(tree)


def test_the_source_module_imports_only_the_standard_library_at_load() -> None:
    tree = _module_tree(source)
    top_level = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    names = {
        (node.module or "") if isinstance(node, ast.ImportFrom) else alias.name
        for node in top_level
        for alias in node.names
    }
    assert {name.split(".")[0] for name in names} <= set(sys.stdlib_module_names)
    assert names  # the module does import something: the check is not vacuous


# ======================================================================================
# THE THIRD READER: the skill's guard (Phase 93, plan 15)
#
# The agent skill is the third reader of the one setting. It cannot branch in code, so
# its first step is a code guard: `python -m ticket_pipeline.source --require <modes>`.
# The guard takes the mode from PACKN_SOURCE_MODE when the wrapper exported it and
# otherwise resolves it through the ONE reader, so a hand-run obeys the same switch.
# Every case below injects the environment, the reader and the record writer.
# ======================================================================================

HUBSPOT_SKILL_SERVES = ("hubspot", "shadow")
SKILL_PATH = PACKAGE_ROOT / ".claude" / "skills" / "hubspot-tickets" / "SKILL.md"
GUARD_COMMAND = "py -m ticket_pipeline.source --require hubspot,shadow"


class _GuardWorld:
    """A reader and a record writer for the guard, both recording."""

    def __init__(self, found: bool = False, value=None, read_error: Exception | None = None):
        self.found, self.value, self.read_error = found, value, read_error
        self.reads = 0
        self.records: list[dict] = []
        self.write_error: Exception | None = None

    def reader(self) -> dict:
        self.reads += 1
        if self.read_error is not None:
            raise self.read_error
        return {"found": self.found, "value": self.value}

    def write_record(self, *args, **kwargs) -> None:
        if self.write_error is not None:
            raise self.write_error
        bound = inspect.signature(client.write_run_record).bind(*args, **kwargs)
        bound.apply_defaults()
        self.records.append(dict(bound.arguments))

    def guard(self, env: dict, served=HUBSPOT_SKILL_SERVES):
        return source.skill_guard(served, env=env, reader=self.reader, write_record=self.write_record)


def test_skill_guard_wrapped_takes_the_exported_mode_without_reading() -> None:
    world = _GuardWorld(read_error=AssertionError("the setting must not be read"))
    assert world.guard({"PACKN_SOURCE_MODE": "shadow", "PACKN_RUN_ID": "run-1"}) == (0, "shadow")
    assert world.reads == 0
    assert world.records == []


def test_skill_guard_wrapped_in_os_mode_stops_and_leaves_the_record_to_the_wrapper() -> None:
    world = _GuardWorld(read_error=AssertionError("the setting must not be read"))
    result = world.guard({"PACKN_SOURCE_MODE": "os", "PACKN_RUN_ID": "run-1"})
    assert result == (3, "SOURCE_MODE_NOT_SERVED os")
    assert world.reads == 0
    assert world.records == []  # the wrapper owns the run's one record


@pytest.mark.parametrize(
    "exported, token",
    [
        pytest.param("banana", "SOURCE_MODE_UNKNOWN", id="unknown-export"),
        pytest.param(" os", "SOURCE_MODE_UNKNOWN", id="padded-export"),
    ],
)
def test_skill_guard_wrapped_with_an_unknown_export_stops_without_a_record(exported, token) -> None:
    world = _GuardWorld()
    assert world.guard({"PACKN_SOURCE_MODE": exported, "PACKN_RUN_ID": "run-1"}) == (4, token)
    assert world.reads == 0  # an unknown exported value is never replaced by a read
    assert world.records == []


@pytest.mark.parametrize(
    "found, value, mode",
    [
        pytest.param(False, None, "hubspot", id="absent-row"),
        pytest.param(True, {"mode": "hubspot"}, "hubspot", id="hubspot"),
        pytest.param(True, {"mode": "shadow"}, "shadow", id="shadow"),
    ],
)
def test_skill_guard_unwrapped_reads_the_one_setting_and_serves(found, value, mode) -> None:
    world = _GuardWorld(found, value)
    assert world.guard({}) == (0, mode)
    assert world.reads == 1
    assert world.records == []


def test_skill_guard_unwrapped_in_os_mode_stops_and_writes_one_skipped_record() -> None:
    world = _GuardWorld(True, {"mode": "os"})
    assert world.guard({}) == (3, "SOURCE_MODE_NOT_SERVED os")
    (record,) = world.records
    assert record["routine_name"] == "tickets-process"
    assert record["status"] == "skipped"
    assert record["source_mode"] == "os"
    assert "os" in record["error_summary"].split("_")  # the summary names the mode
    assert record["error_summary"] == "skill: source_mode_os_not_served"
    assert record["failure_reason_code"] is None
    assert record["tickets_processed"] == 0 and record["drafts_created"] == 0


def test_skill_guard_unwrapped_with_an_unrecognised_value_stops_with_a_failure_record() -> None:
    world = _GuardWorld(True, {"mode": "banana"})
    assert world.guard({}) == (4, "SOURCE_MODE_UNKNOWN")
    (record,) = world.records
    assert record["status"] == "failure"
    assert record["failure_reason_code"] == "source_unknown"
    assert record["source_mode"] is None
    assert record["error_summary"] == "skill: source_unknown"


def test_skill_guard_unwrapped_with_an_unreadable_setting_stops_with_a_failure_record(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    world = _GuardWorld(read_error=RuntimeError(f"connection refused {MARKER}"))
    assert world.guard({}) == (4, "SOURCE_MODE_UNREADABLE")
    (record,) = world.records
    assert record["status"] == "failure"
    assert record["failure_reason_code"] == "source_unreadable"
    assert record["source_mode"] is None
    assert MARKER not in json.dumps(record, default=str)
    fakes.assert_marker_absent(MARKER, caplog, capfd)


@pytest.mark.parametrize(
    "found, value, expected",
    [
        pytest.param(True, {"mode": "os"}, (3, "SOURCE_MODE_NOT_SERVED os"), id="not-served"),
        pytest.param(True, None, (4, "SOURCE_MODE_UNKNOWN"), id="unknown"),
    ],
)
def test_skill_guard_still_stops_when_its_record_cannot_be_written(
    capfd: pytest.CaptureFixture, found, value, expected
) -> None:
    world = _GuardWorld(found, value)
    world.write_error = RuntimeError(f"insert refused {MARKER}")
    assert world.guard({}) == expected
    out, err = capfd.readouterr()
    code_lines = [line for line in err.splitlines() if "guard_record_unwritable" in line]
    assert code_lines == ["[source] guard_record_unwritable:RuntimeError"]
    assert MARKER not in out + err


def test_skill_guard_an_empty_run_id_is_not_the_wrapper() -> None:
    world = _GuardWorld(True, {"mode": "os"})
    assert world.guard({"PACKN_RUN_ID": ""}) == (3, "SOURCE_MODE_NOT_SERVED os")
    assert len(world.records) == 1


def test_skill_guard_serves_what_it_is_asked_to_serve() -> None:
    """Plan 93-16's skill uses the same guard with `--require os`."""
    world = _GuardWorld(True, {"mode": "os"})
    assert world.guard({}, served=("os",)) == (0, "os")
    assert world.guard({"PACKN_SOURCE_MODE": "hubspot", "PACKN_RUN_ID": "r"}, served=("os",)) == (
        3,
        "SOURCE_MODE_NOT_SERVED hubspot",
    )
    assert world.records == []
    with pytest.raises(ValueError):
        world.guard({}, served=("hubspot", "banana"))


def test_skill_guard_default_reader_and_writer_are_the_one_reader_and_the_helpers_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records: list[tuple] = []
    monkeypatch.setattr(client, "read_ticket_source_mode", _reader(True, {"mode": "os"}))
    monkeypatch.setattr(client, "write_run_record", lambda *a, **k: records.append((a, k)))
    assert source.skill_guard(HUBSPOT_SKILL_SERVES, env={}) == (3, "SOURCE_MODE_NOT_SERVED os")
    ((args, kwargs),) = records
    assert args[:2] == ("tickets-process", "skipped")
    assert kwargs["source_mode"] == "os"


def test_the_guards_names_match_the_orchestrators() -> None:
    from ticket_pipeline import run_types

    assert source.RUN_ID_ENV == run_types.RUN_ID_ENV == "PACKN_RUN_ID"
    assert source.GUARD_ROUTINE == run_types.ROUTINE == pg.ROUTINE
    assert (source.GUARD_EXIT_SERVED, source.GUARD_EXIT_USAGE) == (0, 2)
    assert (source.GUARD_EXIT_NOT_SERVED, source.GUARD_EXIT_NOT_ESTABLISHED) == (3, 4)


# --- the command line


def test_guard_command_line_prints_the_token_and_returns_the_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("PACKN_RUN_ID", "run-1")
    monkeypatch.setenv("PACKN_SOURCE_MODE", "shadow")
    assert source.main(["--require", "hubspot,shadow"]) == 0
    assert capsys.readouterr().out == "shadow\n"
    monkeypatch.setenv("PACKN_SOURCE_MODE", "os")
    assert source.main(["--require", "hubspot,shadow"]) == 3
    assert capsys.readouterr().out == "SOURCE_MODE_NOT_SERVED os\n"
    assert source.main(["--require", "os"]) == 0
    assert capsys.readouterr().out == "os\n"


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--require", "hubspot,banana"], id="unknown-name"),
        pytest.param(["--require", ""], id="empty-list"),
        pytest.param(["--require"], id="no-list"),
        pytest.param([], id="no-arguments"),
        pytest.param(["hubspot"], id="no-flag"),
        pytest.param(["--require", "hubspot", "extra"], id="extra-argument"),
    ],
)
def test_guard_command_line_usage_error_is_exit_2_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, argv
) -> None:
    touched: list[str] = []
    monkeypatch.delenv("PACKN_RUN_ID", raising=False)
    monkeypatch.delenv("PACKN_SOURCE_MODE", raising=False)
    monkeypatch.setattr(client, "read_ticket_source_mode", lambda: touched.append("read"))
    monkeypatch.setattr(client, "write_run_record", lambda *a, **k: touched.append("write"))
    assert source.main(argv) == 2
    assert touched == []
    assert capsys.readouterr().out == ""


def test_guard_command_line_runs_as_a_module_from_the_project_root() -> None:
    """The exact command the skill runs, with the interpreter of this test run. Under
    the wrapper's two variables the guard reads no setting and writes no record, so
    the child touches no database."""
    import os
    import subprocess

    env = dict(os.environ, PACKN_RUN_ID="run-1", PACKN_SOURCE_MODE="shadow")
    env.pop("PACKN_OS_DATABASE_URL", None)
    served = subprocess.run(
        [sys.executable, "-m", "ticket_pipeline.source", "--require", "hubspot,shadow"],
        cwd=str(PACKAGE_ROOT), env=env, capture_output=True, text=True, timeout=60,
    )
    assert (served.returncode, served.stdout.strip()) == (0, "shadow")
    env["PACKN_SOURCE_MODE"] = "os"
    refused = subprocess.run(
        [sys.executable, "-m", "ticket_pipeline.source", "--require", "hubspot,shadow"],
        cwd=str(PACKAGE_ROOT), env=env, capture_output=True, text=True, timeout=60,
    )
    assert (refused.returncode, refused.stdout.strip()) == (3, "SOURCE_MODE_NOT_SERVED os")


# --- the skill's text


def _skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def _source_mode_block(text: str) -> str:
    start = text.index("## Source mode")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def test_the_skill_states_the_modes_it_serves_and_runs_the_guard() -> None:
    block = _source_mode_block(_skill_text())
    assert GUARD_COMMAND in block
    assert "serves `hubspot` and `shadow`" in block
    assert "does not serve `os`" in block
    assert "FIRST" in block
    # the stop rule
    assert "STOP" in block
    for token in ("SOURCE_MODE_NOT_SERVED", "SOURCE_MODE_UNKNOWN", "SOURCE_MODE_UNREADABLE"):
        assert token in block
    assert "do nothing else" in block


def test_the_guard_comes_before_the_skills_first_hubspot_instruction() -> None:
    text = _skill_text()
    guard_at = text.index(GUARD_COMMAND)
    assert text.count(GUARD_COMMAND) == 1
    # the first instruction that calls HubSpot is the first MCP tool the skill names
    assert guard_at < text.index("mcp__claude_ai_HubSpot__")
    assert guard_at < text.index("## Discovery step")
    assert guard_at < text.index("## Step 0")
    assert guard_at < text.index("## Main loop")


def test_the_skill_never_works_out_the_mode_another_way() -> None:
    text = _skill_text()
    assert "read_ticket_source_mode" not in text
    assert "app_settings" not in text
    assert "ticket_source_mode" not in text
