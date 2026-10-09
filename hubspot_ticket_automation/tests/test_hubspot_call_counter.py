"""Every HubSpot request code makes is counted in one place (Phase 93, plan 13).

`ticket_pipeline/hubspot_api.py` is the one chokepoint. Two numbers are kept:

    raw_requests   one per HTTP attempt — a retry is a second request
    kinds          one per LOGICAL call, under one of eight names; a thread fetch
                   is two requests and one `email_thread` (decision Q7)

The tests drive the real request function through its opener seam, so the retry,
the bytes on the wire and the raw total are the real ones. The opener is a fake
that answers from a queue and fails the test on a request it was not told to
expect. No test opens a connection: an autouse fixture refuses the network, the
rate limiter is replaced (it would look for Redis) and the retry sleep is zero.
"""
from __future__ import annotations

import ast
import inspect
import io
import json
import logging
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # hubspot_ticket_automation/
sys.path.insert(0, str(PACKAGE_ROOT / "scripts"))  # scripts/ is imported flat, as run.py does

import pregate_tickets as pg  # noqa: E402
from packn_os_hubspot_client import client  # noqa: E402
from tests.helpers import fakes  # noqa: E402
from ticket_pipeline import hubspot_api  # noqa: E402
from ticket_pipeline import run as pipeline_run  # noqa: E402

BASE = "https://api.hubapi.com"
SEARCH_PATH = "/crm/v3/objects/tickets/search"
TOKEN = "test-token"
TOKEN_MARKER = "ZZ-TOKEN-MARKER"

EIGHT_KINDS = (
    "search",
    "ticket_read",
    "contact_read",
    "company_read",
    "related_lookup",
    "rerun_read",
    "email_thread",
    "number_backfill",
)

# The pre-gate's search as it goes on the wire (pinned in tests/test_source_modes.py
# against the code as it stood before this phase).
PINNED_WIRE_JSON = (
    '{"filterGroups": [{"filters": [{"propertyName": "hs_lastmodifieddate", "operator": "GT", '
    '"value": 123456}, {"propertyName": "hs_pipeline_stage", "operator": "IN", "values": '
    '["1", "3"]}, {"propertyName": "hs_last_message_from_visitor", "operator": "EQ", "value": '
    '"true"}]}], "properties": ["hs_lastmodifieddate"], "sorts": [{"propertyName": '
    '"hs_lastmodifieddate", "direction": "ASCENDING"}], "limit": 25}'
)

SETTINGS = {
    "active_stages": ["1", "3"],
    "require_last_message_from_visitor": True,
    "per_run_cap": 25,
    "ticket_custom_properties": ["topic_of_ticket", "order_number"],
    "pipeline": {"shadow_enabled": True, "shadow_limit": 25},
}
STATE = {"last_run_at": "1970-01-01T00:02:03.456Z", "ticket_fingerprints": {}}
NEW_HIT = {"id": "10", "properties": {"hs_lastmodifieddate": "2026-10-02T09:00:00Z"}}


def _zero(**overrides: int) -> dict:
    kinds = {kind: 0 for kind in EIGHT_KINDS}
    raw = overrides.pop("raw_requests", 0)
    kinds.update(overrides)
    return {"kinds": kinds, "raw_requests": raw}


# ------------------------------------------------------------------ the harness


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse every outgoing connection for the length of each test in this file.

    The request function catches OSError and retries, so the refusal is an
    AssertionError: it cannot be mistaken for a failed call and swallowed.
    """

    def refuse(*_args, **_kwargs):
        raise AssertionError("a test in test_hubspot_call_counter.py tried to open a network connection")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(autouse=True)
def _quiet_chokepoint(monkeypatch: pytest.MonkeyPatch):
    """No rate-limiter lookup, no retry sleep, and a counter at zero before and after."""
    monkeypatch.setattr(hubspot_api, "_acquire_rate_token", lambda: None)
    monkeypatch.setattr(hubspot_api, "RETRY_SLEEP_S", 0)
    monkeypatch.delenv("REDIS_URL", raising=False)  # no rate limiter can find a Redis
    hubspot_api.reset_calls()
    yield
    hubspot_api.reset_calls()


class _Response:
    def __init__(self, payload) -> None:
        self._raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def read(self) -> bytes:
        return self._raw


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(BASE, code, "error", hdrs=None, fp=io.BytesIO(b""))


class FakeOpener:
    """Stands in for urlopen: `opener(request, timeout=...)`.

    Answers from a queue, in order. An item is a JSON-able payload (a 200), raw
    bytes, or an exception to raise. A request with nothing queued fails the test.
    """

    def __init__(self, *answers) -> None:
        self._answers = list(answers)
        self.requests: list[urllib.request.Request] = []
        self.timeouts: list = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if not self._answers:
            raise AssertionError(
                f"unexpected HubSpot request: {request.get_method()} {request.full_url}"
            )
        answer = self._answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return _Response(answer)

    def sent(self) -> list[tuple[str, str]]:
        return [(request.get_method(), request.full_url) for request in self.requests]


def _open_with(monkeypatch: pytest.MonkeyPatch, *answers) -> FakeOpener:
    opener = FakeOpener(*answers)
    monkeypatch.setattr(hubspot_api, "_default_opener", opener)
    return opener


def test_the_network_is_refused_in_this_file() -> None:
    with pytest.raises(AssertionError, match="network connection"):
        urllib.request.urlopen("https://example.invalid/")
    with pytest.raises(AssertionError, match="network connection"):
        socket.create_connection(("example.invalid", 443))


def test_the_fake_opener_fails_on_a_request_it_was_not_told_to_expect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open_with(monkeypatch)  # nothing queued
    with pytest.raises(AssertionError, match="unexpected HubSpot request"):
        hubspot_api.search_tickets({"limit": 1}, TOKEN)


def test_the_default_opener_is_urlopen() -> None:
    """With no seam used, a request goes to urllib's urlopen — here, the refusal."""
    with pytest.raises(AssertionError, match="network connection"):
        hubspot_api._default_opener(urllib.request.Request(BASE + SEARCH_PATH), timeout=30)


# -------------------------------------------------------------- the counter API


def test_call_kinds_are_the_eight_in_card_order() -> None:
    assert hubspot_api.CALL_KINDS == EIGHT_KINDS


def test_snapshot_on_a_fresh_process_is_all_zero() -> None:
    code = (
        "import json; from ticket_pipeline import hubspot_api; "
        "print(json.dumps(hubspot_api.snapshot_calls()))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=PACKAGE_ROOT, capture_output=True, text=True, check=True
    )
    assert json.loads(proc.stdout) == _zero()
    assert list(json.loads(proc.stdout)["kinds"]) == list(EIGHT_KINDS)


def test_record_call_counts_one_logical_call() -> None:
    hubspot_api.record_call("email_thread")
    assert hubspot_api.snapshot_calls() == _zero(email_thread=1)


@pytest.mark.parametrize("kind", EIGHT_KINDS)
def test_record_call_accepts_each_of_the_eight_kinds(kind: str) -> None:
    hubspot_api.record_call(kind)
    hubspot_api.record_call(kind)
    assert hubspot_api.snapshot_calls() == _zero(**{kind: 2})


@pytest.mark.parametrize("kind", ["bogus", "", "SEARCH", None, 3, ["search"]])
def test_record_call_refuses_an_unknown_kind(kind) -> None:
    with pytest.raises(ValueError):
        hubspot_api.record_call(kind)
    assert hubspot_api.snapshot_calls() == _zero()


def test_reset_calls_goes_back_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _open_with(monkeypatch, {"results": []})
    hubspot_api.search_tickets({"limit": 1}, TOKEN)
    hubspot_api.record_call("number_backfill")
    assert hubspot_api.snapshot_calls() == _zero(search=1, number_backfill=1, raw_requests=1)
    hubspot_api.reset_calls()
    assert hubspot_api.snapshot_calls() == _zero()


def test_snapshot_is_a_copy_not_the_counter() -> None:
    hubspot_api.record_call("search")
    snapshot = hubspot_api.snapshot_calls()
    snapshot["kinds"]["search"] = 99
    snapshot["raw_requests"] = 99
    assert hubspot_api.snapshot_calls() == _zero(search=1)


def test_snapshot_is_plain_json() -> None:
    hubspot_api.record_call("rerun_read")
    assert json.loads(json.dumps(hubspot_api.snapshot_calls())) == _zero(rerun_read=1)


# ------------------------------------------- raw requests: one per HTTP attempt


def test_a_search_that_succeeds_first_try_is_one_search_and_one_raw_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _open_with(monkeypatch, {"total": 0, "results": []})
    assert hubspot_api.search_tickets({"limit": 1}, TOKEN) == {"total": 0, "results": []}
    assert opener.sent() == [("POST", BASE + SEARCH_PATH)]
    assert hubspot_api.snapshot_calls() == _zero(search=1, raw_requests=1)


def test_a_search_that_retries_after_a_429_is_one_search_and_two_raw_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _open_with(monkeypatch, _http_error(429), {"total": 0, "results": []})
    assert hubspot_api.search_tickets({"limit": 1}, TOKEN) == {"total": 0, "results": []}
    assert opener.sent() == [("POST", BASE + SEARCH_PATH)] * 2
    assert hubspot_api.snapshot_calls() == _zero(search=1, raw_requests=2)


def test_a_search_that_fails_twice_is_none_one_search_and_two_raw_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open_with(monkeypatch, _http_error(503), _http_error(500))
    assert hubspot_api.search_tickets({"limit": 1}, TOKEN) is None
    assert hubspot_api.snapshot_calls() == _zero(search=1, raw_requests=2)


def test_a_client_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _open_with(monkeypatch, _http_error(400))
    assert hubspot_api.search_tickets({"limit": 1}, TOKEN) is None
    assert len(opener.requests) == 1
    assert hubspot_api.snapshot_calls() == _zero(search=1, raw_requests=1)


def test_a_transport_error_is_retried_once_and_both_attempts_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open_with(monkeypatch, urllib.error.URLError("unreachable"), TimeoutError("slow"))
    assert hubspot_api.search_tickets({"limit": 1}, TOKEN) is None
    assert hubspot_api.snapshot_calls() == _zero(search=1, raw_requests=2)


def test_an_empty_answer_is_an_empty_object_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    _open_with(monkeypatch, b"")
    assert hubspot_api._request("GET", "/crm/v3/objects/tickets/1", TOKEN) == {}
    assert hubspot_api.snapshot_calls() == _zero(raw_requests=1)


def test_the_request_is_sent_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _open_with(monkeypatch, {"results": []})
    body = json.loads(PINNED_WIRE_JSON)
    hubspot_api.search_tickets(body, TOKEN)
    request = opener.requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == BASE + SEARCH_PATH
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert request.get_header("Content-type") == "application/json"
    assert request.data == PINNED_WIRE_JSON.encode("utf-8")
    assert opener.timeouts == [30]


def test_the_opener_can_be_handed_to_one_request(monkeypatch: pytest.MonkeyPatch) -> None:
    default = _open_with(monkeypatch)  # must not be used
    mine = FakeOpener({"ok": True})
    assert hubspot_api._request("GET", "/crm/v3/objects/tickets/1", TOKEN, opener=mine) == {"ok": True}
    assert default.requests == []
    assert hubspot_api.snapshot_calls() == _zero(raw_requests=1)


def test_the_request_function_keeps_its_positional_parameters() -> None:
    parameters = inspect.signature(hubspot_api._request).parameters
    positional = [
        p.name for p in parameters.values() if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    assert positional == ["method", "path", "token", "body"]
    assert parameters["opener"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["opener"].default is None


def test_a_failing_request_puts_no_token_in_any_output(
    monkeypatch: pytest.MonkeyPatch, caplog, capfd
) -> None:
    caplog.set_level(logging.DEBUG)
    _open_with(monkeypatch, _http_error(401))
    assert hubspot_api.search_tickets({"limit": 1}, TOKEN_MARKER) is None
    _open_with(monkeypatch, urllib.error.URLError("unreachable"), urllib.error.URLError("unreachable"))
    assert hubspot_api.search_tickets({"limit": 1}, TOKEN_MARKER) is None
    fakes.assert_marker_absent(TOKEN_MARKER, caplog, capfd)


# ------------------------------------------------ kinds: one per logical call


def test_a_thread_fetch_is_two_requests_and_one_email_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision Q7: the association list and the batch read are ONE email_thread."""
    opener = _open_with(
        monkeypatch,
        {"results": [{"toObjectId": 7}, {"toObjectId": 8}]},
        {"results": [{"id": "7"}, {"id": "8"}]},
    )
    hubspot_api.record_call("email_thread")
    email_ids = hubspot_api.get_associated_ids("42", "emails", TOKEN, limit=10)
    emails = hubspot_api.batch_read("emails", email_ids, hubspot_api.EMAIL_PROPERTIES, TOKEN)

    assert email_ids == ["7", "8"]
    assert [email["id"] for email in emails] == ["7", "8"]
    assert opener.sent() == [
        ("GET", BASE + "/crm/v4/objects/tickets/42/associations/emails?limit=10"),
        ("POST", BASE + "/crm/v3/objects/emails/batch/read"),
    ]
    assert hubspot_api.snapshot_calls() == _zero(email_thread=1, raw_requests=2)


def test_the_association_and_batch_helpers_record_no_kind_of_their_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open_with(monkeypatch, {"results": []}, {"results": []})
    hubspot_api.get_associated_ids("42", "contacts", TOKEN, limit=1)
    hubspot_api.batch_read("contacts", ["5"], hubspot_api.CONTACT_PROPERTIES, TOKEN)
    assert hubspot_api.snapshot_calls() == _zero(raw_requests=2)


def test_search_tickets_records_the_kind_it_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    _open_with(monkeypatch, {"results": []})
    hubspot_api.search_tickets({"limit": 3}, TOKEN, kind="related_lookup")
    assert hubspot_api.snapshot_calls() == _zero(related_lookup=1, raw_requests=1)


def test_search_tickets_with_an_unknown_kind_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _open_with(monkeypatch)
    with pytest.raises(ValueError):
        hubspot_api.search_tickets({"limit": 3}, TOKEN, kind="bogus")
    assert opener.requests == []
    assert hubspot_api.snapshot_calls() == _zero()


def test_search_tickets_stays_call_compatible() -> None:
    parameters = inspect.signature(hubspot_api.search_tickets).parameters
    assert list(parameters)[:3] == ["body", "token", "kind"]
    assert parameters["kind"].default == "search"
    assert parameters["transport"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(inspect.signature(hubspot_api.get_associated_ids).parameters) == [
        "ticket_id",
        "to_object",
        "token",
        "limit",
    ]
    assert list(inspect.signature(hubspot_api.batch_read).parameters) == [
        "object_type",
        "ids",
        "properties",
        "token",
    ]


def test_a_search_through_a_handed_transport_is_recorded_once() -> None:
    transport = fakes.FakeHubSpotTransport({SEARCH_PATH: {"results": []}})
    assert hubspot_api.search_tickets({"limit": 1}, TOKEN, transport=transport) == {"results": []}
    assert transport.calls == [("POST", SEARCH_PATH)]
    assert transport.bodies == [{"limit": 1}]
    # the transport replaces the request function, so no HTTP attempt was made
    assert hubspot_api.snapshot_calls() == _zero(search=1)


def test_the_patched_request_function_still_counts_the_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fakes replace `_request`; the logical kind is recorded above it."""
    transport = fakes.FakeHubSpotTransport({SEARCH_PATH: {"results": []}})
    monkeypatch.setattr(hubspot_api, "_request", transport)
    hubspot_api.search_tickets({"limit": 1}, TOKEN)
    assert hubspot_api.snapshot_calls() == _zero(search=1)


# ------------------------------------------------------ update_ticket_properties


def test_update_ticket_properties_is_one_patch_on_the_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _open_with(monkeypatch, {"id": "123", "properties": {"order_number": "A1"}})
    result = hubspot_api.update_ticket_properties("123", {"order_number": "A1"}, TOKEN)

    assert result == {"id": "123", "properties": {"order_number": "A1"}}
    assert opener.sent() == [("PATCH", BASE + "/crm/v3/objects/tickets/123")]
    assert json.loads(opener.requests[0].data) == {"properties": {"order_number": "A1"}}
    assert opener.requests[0].get_header("Authorization") == f"Bearer {TOKEN}"
    # the caller records the kind (number_backfill): one request, no kind yet
    assert hubspot_api.snapshot_calls() == _zero(raw_requests=1)


def test_update_ticket_properties_answers_none_when_the_patch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open_with(monkeypatch, _http_error(404))
    assert hubspot_api.update_ticket_properties("123", {"order_number": "A1"}, TOKEN) is None


@pytest.mark.parametrize("ticket_id", ["", "12/../34", "12?x=1", "abc", None, "0f8fad5b-d9cb-469f"])
def test_update_ticket_properties_refuses_an_id_that_is_not_a_hubspot_id(
    monkeypatch: pytest.MonkeyPatch, ticket_id
) -> None:
    opener = _open_with(monkeypatch)
    with pytest.raises(ValueError):
        hubspot_api.update_ticket_properties(ticket_id, {"order_number": "A1"}, TOKEN)
    assert opener.requests == []


@pytest.mark.parametrize("properties", [{}, None, ["order_number"], "order_number"])
def test_update_ticket_properties_refuses_no_properties(
    monkeypatch: pytest.MonkeyPatch, properties
) -> None:
    opener = _open_with(monkeypatch)
    with pytest.raises(ValueError):
        hubspot_api.update_ticket_properties("123", properties, TOKEN)
    assert opener.requests == []


@pytest.mark.parametrize(
    "name", ["hs_pipeline", "hs_pipeline_stage", "hubspot_owner_id", "hs_ticket_priority"]
)
def test_update_ticket_properties_refuses_stage_owner_and_priority(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """The cron's own invariant 5: code never changes a ticket's stage, owner or priority."""
    opener = _open_with(monkeypatch)
    with pytest.raises(ValueError):
        hubspot_api.update_ticket_properties("123", {"order_number": "A1", name: "x"}, TOKEN)
    assert opener.requests == []
    assert hubspot_api.snapshot_calls() == _zero()


# --------------------------------------------- the pre-gate goes through here


def test_the_pregate_has_no_hubspot_request_code_of_its_own() -> None:
    for name in ("_search_tickets", "_acquire_rate_token", "_read_token"):
        assert not hasattr(pg, name), name
    tree = ast.parse(Path(pg.__file__).read_text(encoding="utf-8"))
    imported = {
        (node.module or "") if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not {name for name in imported if name.split(".")[0] == "urllib"}
    assert "ticket_pipeline" in imported or "ticket_pipeline.hubspot_api" in imported
    defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert not defined & {"_search_tickets", "_acquire_rate_token", "_read_token"}
    text = Path(pg.__file__).read_text(encoding="utf-8")
    assert "api.hubapi.com" not in text
    assert "hubspot_token.txt" not in text


def test_the_pregates_search_through_a_handed_transport_moves_the_counter_by_one() -> None:
    transport = fakes.FakeHubSpotTransport({SEARCH_PATH: {"results": [NEW_HIT]}})
    result = pg.hubspot_candidates("hubspot", SETTINGS, STATE, TOKEN, transport=transport)
    assert result["candidates"] == ["10"]
    assert transport.calls == [("POST", SEARCH_PATH)]
    assert hubspot_api.snapshot_calls() == _zero(search=1)


def test_the_pregates_search_goes_through_search_tickets(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = fakes.FakeHubSpotTransport({SEARCH_PATH: {"results": [NEW_HIT]}})
    monkeypatch.setattr(hubspot_api, "_request", transport)
    result = pg.hubspot_candidates("hubspot", SETTINGS, STATE, TOKEN)
    assert (result["decision"], result["reason"]) == ("run_agent", "candidates")
    assert transport.calls == [("POST", SEARCH_PATH)]  # exactly one request
    assert hubspot_api.snapshot_calls() == _zero(search=1)


def test_the_pregates_search_is_todays_bytes_and_one_raw_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _open_with(monkeypatch, {"total": 0, "results": []})
    result = pg.hubspot_candidates("hubspot", SETTINGS, STATE, TOKEN)
    assert (result["decision"], result["reason"]) == ("no_work", "none")
    assert opener.sent() == [("POST", BASE + SEARCH_PATH)]
    assert opener.requests[0].data == PINNED_WIRE_JSON.encode("utf-8")
    assert hubspot_api.snapshot_calls() == _zero(search=1, raw_requests=1)


def test_the_pregate_keeps_one_retry_and_fails_open_when_the_search_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opener = _open_with(monkeypatch, _http_error(500), _http_error(502))
    result = pg.hubspot_candidates("hubspot", SETTINGS, STATE, TOKEN)
    assert (result["decision"], result["reason"]) == ("run_agent", "search_failed")
    assert len(opener.requests) == 2
    assert hubspot_api.snapshot_calls() == _zero(search=1, raw_requests=2)


def test_the_pregate_recovers_on_the_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    _open_with(monkeypatch, _http_error(429), {"results": [NEW_HIT]})
    result = pg.hubspot_candidates("hubspot", SETTINGS, STATE, TOKEN)
    assert (result["decision"], result["reason"]) == ("run_agent", "candidates")
    assert hubspot_api.snapshot_calls() == _zero(search=1, raw_requests=2)


def test_an_empty_answer_to_the_pregates_search_still_fails_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before the fold an empty body failed to parse and counted as a failed search.
    The shared request function answers {} for it; the pre-gate still fails open."""
    _open_with(monkeypatch, b"")
    result = pg.hubspot_candidates("hubspot", SETTINGS, STATE, TOKEN)
    assert (result["decision"], result["reason"]) == ("run_agent", "search_failed")


def test_os_mode_moves_no_counter_and_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _open_with(monkeypatch)
    result = pg.hubspot_candidates("os", SETTINGS, STATE, TOKEN)
    assert result["reason"] == "os_mode"
    assert opener.requests == []
    assert hubspot_api.snapshot_calls() == _zero()


@pytest.fixture
def pregate_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict]:
    """`pregate_tickets.main` in hubspot mode with the client, the loaders and the
    token replaced — the search itself is left to the real code."""
    records: list[dict] = []
    # RED only: while the pre-gate still has its own token reader, point it away
    # from the real token file. Removed with that reader in the GREEN commit.
    monkeypatch.setattr(pg, "TOKEN_PATH", tmp_path / "no-token-here", raising=False)
    monkeypatch.delenv("PACKN_SOURCE_MODE", raising=False)
    monkeypatch.setattr(client, "report_routine_schedule", lambda *_a, **_k: None)
    monkeypatch.setattr(client, "read_routine_enabled", lambda _name: True)
    monkeypatch.setattr(client, "read_pending_rerun_requests", lambda _name: [])
    monkeypatch.setattr(client, "write_run_record", lambda **kw: records.append(kw))
    monkeypatch.setattr(client, "read_ticket_source_mode", lambda: {"found": False, "value": None})
    monkeypatch.setattr(pg, "_load_settings", lambda: dict(SETTINGS))
    monkeypatch.setattr(pg, "_load_state", lambda: json.loads(json.dumps(STATE)))
    monkeypatch.setattr(hubspot_api, "read_token", lambda: TOKEN)
    return records


def test_main_end_to_end_with_a_candidate_runs_the_agent(
    pregate_main: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    opener = _open_with(monkeypatch, {"results": [NEW_HIT]})
    assert pg.main() == 0
    assert pregate_main == []
    assert opener.requests[0].data == PINNED_WIRE_JSON.encode("utf-8")
    assert opener.requests[0].get_header("Authorization") == f"Bearer {TOKEN}"
    assert hubspot_api.snapshot_calls() == _zero(search=1, raw_requests=1)


def test_main_end_to_end_with_nothing_new_skips_with_a_success_record(
    pregate_main: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_with(monkeypatch, {"results": []})
    assert pg.main() == 10
    assert [record["status"] for record in pregate_main] == ["success"]


def test_main_end_to_end_with_a_failed_search_fails_open(
    pregate_main: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_with(monkeypatch, _http_error(500), urllib.error.URLError("unreachable"))
    assert pg.main() == 0
    assert pregate_main == []
    assert hubspot_api.snapshot_calls() == _zero(search=1, raw_requests=2)


def test_main_reads_the_token_through_the_chokepoint_module(
    pregate_main: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    opener = _open_with(monkeypatch)
    monkeypatch.setattr(hubspot_api, "read_token", lambda: None)
    assert pg.main() == 0  # token missing: fail open, as before
    assert opener.requests == []


# ------------------------------------------------- the pipeline's search, too


def test_the_pipelines_search_is_counted_without_a_change_at_its_call_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(SETTINGS), encoding="utf-8")  # JSON is valid YAML
    monkeypatch.setattr(pipeline_run, "SETTINGS_PATH", settings_path)
    monkeypatch.setattr(pipeline_run, "STATE_PATH", fakes.temp_state_file(tmp_path, STATE))
    monkeypatch.setattr(pipeline_run, "SHADOW_DIR", tmp_path / "engine-shadow-out")
    monkeypatch.setattr(hubspot_api, "read_token", lambda: TOKEN)
    opener = _open_with(monkeypatch, {"results": []})

    assert pipeline_run.run_shadow(mode="hubspot") == 0

    assert opener.sent() == [("POST", BASE + SEARCH_PATH)]
    assert hubspot_api.snapshot_calls() == _zero(search=1, raw_requests=1)
