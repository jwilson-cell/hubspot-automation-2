"""Unit tests for packn_os_hubspot_client.tickets_api (Phase 93, plan 12).

The module is the cron's signed client for the Pack'N OS tickets routes
(contract packn-tickets-v1). Nothing here touches the network, a secret file
or a database: every call goes through the keyword-only seams (base_url,
secret, now, opener).

The two signature literals are the cross-language vectors the OS pins in its
own verifier test. Neither repo imports the other, so a one-sided change to
the canonical string, the query rule or the body serialisation fails here.

The only key in this file is the documented fixture string. It is not a real
secret.
"""
from __future__ import annotations

import ast
import email.message
import hashlib
import hmac
import io
import json
import logging
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from packn_os_hubspot_client import tickets_api
from packn_os_hubspot_client.tickets_api import TicketsApiError

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # hubspot_ticket_automation/
GIT_ROOT = PACKAGE_ROOT.parent

FIXTURE_SECRET = "test-vector-secret-not-a-real-secret-0123456789"
FIXED_TS = 1790000000
ORIGIN = "https://os.packn.test"

# Vector A: GET, the query handed over as limit=25&contract_version=1.
VECTOR_A_PATH = "/api/tickets/unprocessed"
VECTOR_A_PAIRS = [("limit", "25"), ("contract_version", "1")]
VECTOR_A_CANONICAL = (
    "packn-tickets-v1\n"
    "GET\n"
    "/api/tickets/unprocessed\n"
    "contract_version=1&limit=25\n"
    "1790000000\n"
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)
VECTOR_A_SIGNATURE = "a9de186cb4a49b3bca4dcb8a09d8c664a983eb875cc01bc3afb9eefdfefa1856"

# Vector B: POST with a multi-byte body.
VECTOR_B_TICKET = "0f8fad5b-d9cb-469f-a165-70867728950e"
VECTOR_B_PATH = f"/api/tickets/{VECTOR_B_TICKET}/classification"
VECTOR_B_PAYLOAD = {
    "reason": "Clienté asked — “where is it?” 📦",
    "category": "wismo_tracking",
}
VECTOR_B_BODY_SHA256 = "4d14ab369e1203e4a092dd221baa7f1dbb5ea55fe653a809348da2d98ab38f99"
VECTOR_B_SIGNATURE = "69fd6584e3179e91004f29852d49453fb5589100db949590cf9936ce5a57e048"

MARKER = "ZZ-BODY-MARKER"
SECRET_ERROR_BODY = json.dumps({"ok": False, "error": "tickets_secret_not_configured"}).encode()


# ---------------------------------------------------------------- test doubles


class FakeResponse:
    """What an opener hands back for a 2xx: a context manager with read()."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False


class FakeOpener:
    """Stands in for urlopen: records (request, timeout), then answers or raises."""

    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls: list[tuple[urllib.request.Request, object]] = []

    def __call__(self, request, timeout=None):
        self.calls.append((request, timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    @property
    def request(self) -> urllib.request.Request:
        assert len(self.calls) == 1, f"expected exactly one request, saw {len(self.calls)}"
        return self.calls[0][0]

    @property
    def timeout(self):
        assert len(self.calls) == 1
        return self.calls[0][1]


def answers(obj, status: int = 200) -> FakeOpener:
    return FakeOpener(FakeResponse(status, json.dumps(obj).encode("utf-8")))


def answers_raw(body: bytes, status: int = 200) -> FakeOpener:
    return FakeOpener(FakeResponse(status, body))


def refuses(status: int, body: bytes = b"") -> FakeOpener:
    error = urllib.error.HTTPError(
        "https://os.packn.test/", status, "refused", email.message.Message(), io.BytesIO(body)
    )
    return FakeOpener(error)


def fails_with(exc: BaseException) -> FakeOpener:
    return FakeOpener(exc)


def seams(opener: FakeOpener) -> dict:
    return {
        "base_url": ORIGIN,
        "secret": FIXTURE_SECRET,
        "now": lambda: FIXED_TS,
        "opener": opener,
    }


def header(request: urllib.request.Request, name: str):
    for key, value in request.header_items():
        if key.lower() == name.lower():
            return value
    return None


def expected_signature(request: urllib.request.Request) -> str:
    """Recompute the signature from what was actually sent, independently of the module."""
    parts = urllib.parse.urlsplit(request.full_url)
    body = request.data or b""
    canonical = "\n".join(
        [
            "packn-tickets-v1",
            request.get_method(),
            parts.path,
            parts.query,  # the sent query must already be the canonical one
            header(request, "x-packn-timestamp"),
            hashlib.sha256(body).hexdigest(),
        ]
    )
    return hmac.new(
        FIXTURE_SECRET.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()


# ------------------------------------------------------------------ constants


def test_contract_constants() -> None:
    assert tickets_api.SIGNATURE_VERSION == "packn-tickets-v1"
    assert tickets_api.SIGNATURE_HEADER == "x-packn-signature"
    assert tickets_api.TIMESTAMP_HEADER == "x-packn-timestamp"
    assert tickets_api.CONTRACT_VERSION == "1"
    assert tickets_api.MIN_SECRET_LENGTH == 32
    assert tickets_api.TIMEOUT_S == 5.0
    assert tickets_api.MAX_LIST_LIMIT == 100
    assert tickets_api.SECRET_NOT_CONFIGURED_ERROR == "tickets_secret_not_configured"


def test_secret_path_is_the_tickets_file_not_the_action_ingest_one() -> None:
    assert tickets_api.SECRET_PATH == PACKAGE_ROOT / "config" / ".secrets" / "tickets_api_secret.txt"


def test_error_carries_a_code_and_an_optional_status() -> None:
    bare = TicketsApiError("list_timeout")
    assert (bare.code, bare.http_status) == ("list_timeout", None)
    with_status = TicketsApiError("list_error", 500)
    assert (with_status.code, with_status.http_status) == ("list_error", 500)
    assert str(with_status) == "list_error"


# ------------------------------------------------------------ canonical query


def test_canonical_query_sorts_the_two_parameter_query() -> None:
    assert tickets_api.canonical_query(VECTOR_A_PAIRS) == "contract_version=1&limit=25"


def test_canonical_query_of_no_pairs_is_the_empty_string() -> None:
    assert tickets_api.canonical_query([]) == ""


def test_canonical_query_percent_encodes_with_upper_case_hex() -> None:
    assert tickets_api.canonical_query([("q", "a b/é")]) == "q=a%20b%2F%C3%A9"


def test_canonical_query_leaves_only_the_unreserved_set_unencoded() -> None:
    unreserved = "AZaz09-._~"
    assert tickets_api.canonical_query([(unreserved, unreserved)]) == f"{unreserved}={unreserved}"
    assert (
        tickets_api.canonical_query([("k", "!*'();:@&=+$,?#[]")])
        == "k=%21%2A%27%28%29%3B%3A%40%26%3D%2B%24%2C%3F%23%5B%5D"
    )


def test_canonical_query_sorts_by_encoded_name_then_encoded_value() -> None:
    assert tickets_api.canonical_query([("b", "2"), ("a", "9"), ("c", "1")]) == "a=9&b=2&c=1"


def test_canonical_query_refuses_a_duplicated_name() -> None:
    with pytest.raises(ValueError):
        tickets_api.canonical_query([("limit", "1"), ("limit", "2")])


# ------------------------------------------------- canonical string + vectors


def test_canonical_string_is_six_lines_joined_by_a_line_feed() -> None:
    canonical = tickets_api.canonical_string(
        "get", VECTOR_A_PATH, VECTOR_A_PAIRS, str(FIXED_TS), b""
    )
    assert canonical == VECTOR_A_CANONICAL


def test_canonical_string_accepts_an_integer_timestamp() -> None:
    canonical = tickets_api.canonical_string("GET", VECTOR_A_PATH, VECTOR_A_PAIRS, FIXED_TS, b"")
    assert canonical == VECTOR_A_CANONICAL


def test_vector_a_signature_matches_the_pinned_literal() -> None:
    canonical = tickets_api.canonical_string(
        "GET", VECTOR_A_PATH, VECTOR_A_PAIRS, str(FIXED_TS), b""
    )
    assert tickets_api.sign(FIXTURE_SECRET, canonical) == VECTOR_A_SIGNATURE


def test_vector_b_signature_matches_the_pinned_literal() -> None:
    body = json.dumps(VECTOR_B_PAYLOAD, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert hashlib.sha256(body).hexdigest() == VECTOR_B_BODY_SHA256
    canonical = tickets_api.canonical_string("POST", VECTOR_B_PATH, [], str(FIXED_TS), body)
    assert tickets_api.sign(FIXTURE_SECRET, canonical) == VECTOR_B_SIGNATURE


def test_vector_a_end_to_end_through_fetch_unprocessed() -> None:
    opener = answers({"contract_version": "1", "tickets": []})
    tickets_api.fetch_unprocessed(25, **seams(opener))
    assert header(opener.request, "x-packn-signature") == VECTOR_A_SIGNATURE
    assert header(opener.request, "x-packn-timestamp") == "1790000000"
    assert opener.request.full_url == f"{ORIGIN}/api/tickets/unprocessed?contract_version=1&limit=25"


def test_vector_b_end_to_end_through_post_classification() -> None:
    opener = answers({"ok": True, "replayed": False})
    tickets_api.post_classification(VECTOR_B_TICKET, VECTOR_B_PAYLOAD, **seams(opener))
    assert header(opener.request, "x-packn-signature") == VECTOR_B_SIGNATURE
    assert hashlib.sha256(opener.request.data).hexdigest() == VECTOR_B_BODY_SHA256


# --------------------------------------------------------------------- secret


def test_read_secret_strips_a_trailing_newline(tmp_path: Path) -> None:
    path = tmp_path / "tickets_api_secret.txt"
    path.write_text(FIXTURE_SECRET + "\n", encoding="utf-8")
    assert tickets_api.read_secret(path) == FIXTURE_SECRET


def test_read_secret_strips_surrounding_spaces(tmp_path: Path) -> None:
    path = tmp_path / "tickets_api_secret.txt"
    path.write_text("  " + FIXTURE_SECRET + " \r\n", encoding="utf-8")
    assert tickets_api.read_secret(path) == FIXTURE_SECRET


def test_a_trailing_newline_secret_signs_identically_to_the_bare_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "tickets_api_secret.txt"
    path.write_text(FIXTURE_SECRET + "\n", encoding="utf-8")
    monkeypatch.setattr(tickets_api, "SECRET_PATH", path)
    opener = answers({"contract_version": "1", "tickets": []})
    tickets_api.fetch_unprocessed(25, base_url=ORIGIN, now=lambda: FIXED_TS, opener=opener)
    assert header(opener.request, "x-packn-signature") == VECTOR_A_SIGNATURE


def test_read_secret_refuses_31_characters(tmp_path: Path) -> None:
    path = tmp_path / "tickets_api_secret.txt"
    path.write_text("x" * 31 + "\n", encoding="utf-8")
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.read_secret(path)
    assert caught.value.code == "secret_missing"


def test_read_secret_accepts_exactly_32_characters(tmp_path: Path) -> None:
    path = tmp_path / "tickets_api_secret.txt"
    path.write_text("x" * 32, encoding="utf-8")
    assert tickets_api.read_secret(path) == "x" * 32


def test_read_secret_on_a_missing_file_is_secret_missing(tmp_path: Path) -> None:
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.read_secret(tmp_path / "absent.txt")
    assert caught.value.code == "secret_missing"


def test_a_missing_secret_file_raises_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tickets_api, "SECRET_PATH", tmp_path / "absent.txt")
    opener = answers({"contract_version": "1", "tickets": []})
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.fetch_unprocessed(25, base_url=ORIGIN, now=lambda: FIXED_TS, opener=opener)
    assert caught.value.code == "secret_missing"
    assert opener.calls == []


def test_a_short_secret_raises_before_any_request() -> None:
    opener = answers({"ok": True, "replayed": False})
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.post_classification(
            VECTOR_B_TICKET,
            VECTOR_B_PAYLOAD,
            base_url=ORIGIN,
            secret="x" * 31,
            now=lambda: FIXED_TS,
            opener=opener,
        )
    assert caught.value.code == "secret_missing"
    assert opener.calls == []


# --------------------------------------------------------------------- origin


def _settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    path = tmp_path / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(tickets_api, "SETTINGS_PATH", path)


def test_read_os_origin_reads_the_bare_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(tmp_path, monkeypatch, 'dry_run: false\nos_ingest_url: "https://os.packn.test"\n')
    assert tickets_api.read_os_origin() == "https://os.packn.test"


def test_read_os_origin_drops_a_path_and_an_inline_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(
        tmp_path,
        monkeypatch,
        "# os_ingest_url: https://commented.packn.test\n"
        "os_ingest_url: 'https://os.packn.test/api/ingest/action-items'  # the OS\n",
    )
    assert tickets_api.read_os_origin() == "https://os.packn.test"


@pytest.mark.parametrize(
    "text",
    [
        "dry_run: false\n",
        'os_ingest_url: ""\n',
        'os_ingest_url: "https://REPLACE-ME"\n',
        'os_ingest_url: "https://example.com"\n',
        'os_ingest_url: "ftp://os.packn.test"\n',
        'os_ingest_url: "os.packn.test"\n',
    ],
)
def test_read_os_origin_without_a_usable_value_is_list_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    _settings(tmp_path, monkeypatch, text)
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.read_os_origin()
    assert caught.value.code == "list_error"


def test_read_os_origin_with_no_settings_file_is_list_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tickets_api, "SETTINGS_PATH", tmp_path / "absent.yaml")
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.read_os_origin()
    assert caught.value.code == "list_error"


def test_the_origin_defaults_to_the_settings_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings(tmp_path, monkeypatch, 'os_ingest_url: "https://os.packn.test/"\n')
    opener = answers({"contract_version": "1", "tickets": []})
    tickets_api.fetch_unprocessed(
        25, secret=FIXTURE_SECRET, now=lambda: FIXED_TS, opener=opener
    )
    assert opener.request.full_url == f"{ORIGIN}/api/tickets/unprocessed?contract_version=1&limit=25"


# ---------------------------------------------------------- fetch_unprocessed


def test_fetch_unprocessed_sends_one_signed_get() -> None:
    opener = answers({"contract_version": "1", "tickets": []})
    tickets_api.fetch_unprocessed(25, **seams(opener))
    request = opener.request
    assert request.get_method() == "GET"
    assert request.full_url == f"{ORIGIN}/api/tickets/unprocessed?contract_version=1&limit=25"
    assert request.data is None
    assert opener.timeout == 5.0
    assert header(request, "x-packn-timestamp") == str(FIXED_TS)
    assert header(request, "x-packn-signature") == expected_signature(request)


def test_fetch_unprocessed_returns_the_parsed_body() -> None:
    body = {"contract_version": "1", "generated_at": "2026-10-09T14:07:03.120Z", "tickets": [{"id": "t1"}]}
    assert tickets_api.fetch_unprocessed(25, **seams(answers(body))) == body


def test_the_timestamp_defaults_to_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tickets_api.time, "time", lambda: 1790000123.9)
    opener = answers({"contract_version": "1", "tickets": []})
    tickets_api.fetch_unprocessed(25, base_url=ORIGIN, secret=FIXTURE_SECRET, opener=opener)
    assert header(opener.request, "x-packn-timestamp") == "1790000123"


@pytest.mark.parametrize(
    ("given", "sent"),
    [(250, "100"), (101, "100"), (100, "100"), (25, "25"), (1, "1"), (0, "1"), (-3, "1")],
)
def test_fetch_unprocessed_clamps_the_limit_into_1_to_100(given: int, sent: str) -> None:
    opener = answers({"contract_version": "1", "tickets": []})
    tickets_api.fetch_unprocessed(given, **seams(opener))
    request = opener.request
    assert request.full_url == f"{ORIGIN}/api/tickets/unprocessed?contract_version=1&limit={sent}"
    assert header(request, "x-packn-signature") == expected_signature(request)


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("timed out"),
        socket.timeout("timed out"),
        urllib.error.URLError("no route"),
        ConnectionResetError("reset"),
        ConnectionRefusedError("refused"),
    ],
)
def test_a_timeout_or_connection_error_on_the_list_is_list_timeout(exc: BaseException) -> None:
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.fetch_unprocessed(25, **seams(fails_with(exc)))
    assert caught.value.code == "list_timeout"
    assert caught.value.http_status is None


def test_a_401_on_the_list_is_signature_rejected() -> None:
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.fetch_unprocessed(25, **seams(refuses(401, b'{"ok":false,"error":"unauthorized"}')))
    assert (caught.value.code, caught.value.http_status) == ("signature_rejected", 401)


@pytest.mark.parametrize("status", [400, 403, 404, 500, 502])
def test_any_other_refusal_on_the_list_is_list_error(status: int) -> None:
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.fetch_unprocessed(25, **seams(refuses(status, b'{"ok":false,"error":"bad_request"}')))
    assert (caught.value.code, caught.value.http_status) == ("list_error", status)


def test_a_redirect_is_never_followed_and_is_list_error() -> None:
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.fetch_unprocessed(25, **seams(refuses(302)))
    assert (caught.value.code, caught.value.http_status) == ("list_error", 302)
    request = urllib.request.Request(f"{ORIGIN}/api/tickets/unprocessed")
    handler = tickets_api._NoRedirect()
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://elsewhere.test/") is None


@pytest.mark.parametrize("raw", [b"<html>ok</html>", b"", b"[1, 2]", b'"text"'])
def test_a_2xx_that_is_not_a_json_object_is_list_error(raw: bytes) -> None:
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.fetch_unprocessed(25, **seams(answers_raw(raw)))
    assert (caught.value.code, caught.value.http_status) == ("list_error", 200)


def test_a_503_naming_the_unconfigured_secret_is_secret_missing() -> None:
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.fetch_unprocessed(25, **seams(refuses(503, SECRET_ERROR_BODY)))
    assert (caught.value.code, caught.value.http_status) == ("secret_missing", 503)


@pytest.mark.parametrize(
    "body",
    [
        b"<html><body>502 Bad Gateway while redeploying</body></html>",
        b"",
        b'{"ok":false,"error":"server"}',
        b'{"ok":false}',
        b'["tickets_secret_not_configured"]',
        b'"tickets_secret_not_configured"',
    ],
)
def test_any_other_503_is_list_error(body: bytes) -> None:
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.fetch_unprocessed(25, **seams(refuses(503, body)))
    assert (caught.value.code, caught.value.http_status) == ("list_error", 503)


# --------------------------------------------------------------- fetch_ticket


def test_fetch_ticket_sends_exactly_the_path_and_the_one_query_parameter() -> None:
    opener = answers({"contract_version": "1", "ticket": {"id": "t1"}, "unprocessed": True})
    tickets_api.fetch_ticket("123456", **seams(opener))
    request = opener.request
    assert request.get_method() == "GET"
    assert request.full_url == f"{ORIGIN}/api/tickets/123456?contract_version=1"
    assert request.data is None
    assert opener.timeout == 5.0
    assert header(request, "x-packn-signature") == expected_signature(request)


def test_fetch_ticket_returns_the_parsed_object_on_200() -> None:
    body = {"contract_version": "1", "ticket": {"id": "t1"}, "unprocessed": False}
    assert tickets_api.fetch_ticket("123456", **seams(answers(body))) == body


def test_fetch_ticket_returns_none_on_404() -> None:
    opener = refuses(404, b'{"ok":false,"error":"not_found"}')
    assert tickets_api.fetch_ticket("123456", **seams(opener)) is None
    assert len(opener.calls) == 1  # None is the answer to a request, not the absence of one


def test_fetch_ticket_sends_a_uuid_ref_the_same_way() -> None:
    opener = answers({"contract_version": "1", "ticket": {"id": VECTOR_B_TICKET}, "unprocessed": True})
    tickets_api.fetch_ticket(VECTOR_B_TICKET, **seams(opener))
    request = opener.request
    assert request.full_url == f"{ORIGIN}/api/tickets/{VECTOR_B_TICKET}?contract_version=1"
    assert header(request, "x-packn-signature") == expected_signature(request)


@pytest.mark.parametrize("ref", ["", "a/b", "1?x=1", "..", "a b", "id#frag", "é"])
def test_a_ref_that_is_not_a_plain_id_is_refused_before_any_request(ref: str) -> None:
    opener = answers({"contract_version": "1", "ticket": {}, "unprocessed": True})
    with pytest.raises(ValueError):
        tickets_api.fetch_ticket(ref, **seams(opener))
    with pytest.raises(ValueError):
        tickets_api.post_classification(ref, {"category": "wismo_tracking"}, **seams(opener))
    assert opener.calls == []


@pytest.mark.parametrize(
    ("opener", "code", "status"),
    [
        (refuses(401), "signature_rejected", 401),
        (refuses(503, SECRET_ERROR_BODY), "secret_missing", 503),
        (refuses(503, b"<html>gateway</html>"), "list_error", 503),
        (refuses(400), "list_error", 400),
        (refuses(500), "list_error", 500),
        (fails_with(TimeoutError("timed out")), "list_timeout", None),
        (answers_raw(b"not json"), "list_error", 200),
    ],
)
def test_fetch_ticket_raises_a_code_for_every_other_outcome(opener, code, status) -> None:
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.fetch_ticket("123456", **seams(opener))
    assert (caught.value.code, caught.value.http_status) == (code, status)


# -------------------------------------------------------- post_classification


def test_post_classification_sends_one_signed_post_to_the_literal_path() -> None:
    opener = answers({"ok": True, "replayed": False})
    tickets_api.post_classification(VECTOR_B_TICKET, VECTOR_B_PAYLOAD, **seams(opener))
    request = opener.request
    assert request.get_method() == "POST"
    assert (
        request.full_url
        == "https://os.packn.test/api/tickets/0f8fad5b-d9cb-469f-a165-70867728950e/classification"
    )
    assert "?" not in request.full_url
    assert opener.timeout == 5.0
    assert header(request, "content-type") == "application/json"
    assert header(request, "x-packn-timestamp") == str(FIXED_TS)
    assert header(request, "x-packn-signature") == expected_signature(request)


def test_the_bytes_sent_are_the_bytes_signed() -> None:
    opener = answers({"ok": True, "replayed": False})
    tickets_api.post_classification(VECTOR_B_TICKET, VECTOR_B_PAYLOAD, **seams(opener))
    sent = opener.request.data
    assert isinstance(sent, bytes)
    # compact separators, UTF-8, non-ASCII left as it is
    assert sent == (
        '{"reason":"Clienté asked — “where is it?” 📦","category":"wismo_tracking"}'.encode("utf-8")
    )
    canonical = tickets_api.canonical_string("POST", VECTOR_B_PATH, [], FIXED_TS, sent)
    assert canonical.endswith("\n" + hashlib.sha256(sent).hexdigest())
    assert header(opener.request, "x-packn-signature") == tickets_api.sign(FIXTURE_SECRET, canonical)


@pytest.mark.parametrize("replayed", [False, True])
def test_post_classification_200_is_a_result_carrying_the_replayed_flag(replayed: bool) -> None:
    opener = answers({"ok": True, "replayed": replayed})
    result = tickets_api.post_classification(VECTOR_B_TICKET, VECTOR_B_PAYLOAD, **seams(opener))
    assert result == {"status": 200, "error": None, "replayed": replayed}


def test_post_classification_200_without_the_flag_reports_none() -> None:
    result = tickets_api.post_classification(
        VECTOR_B_TICKET, VECTOR_B_PAYLOAD, **seams(answers({"ok": True}))
    )
    assert result == {"status": 200, "error": None, "replayed": None}


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (400, "bad_payload"),
        (404, "not_found"),
        (409, "conflict"),
        (413, "payload_too_large"),
        (422, "signal_ahead"),
    ],
)
def test_post_classification_verdict_statuses_are_results_not_exceptions(
    status: int, error: str
) -> None:
    opener = refuses(status, json.dumps({"ok": False, "error": error}).encode())
    result = tickets_api.post_classification(VECTOR_B_TICKET, VECTOR_B_PAYLOAD, **seams(opener))
    assert result == {"status": status, "error": error, "replayed": None}


def test_the_verdict_name_is_keyed_on_the_status_not_on_the_body() -> None:
    opener = refuses(409, b"<html>" + MARKER.encode() + b"</html>")
    result = tickets_api.post_classification(VECTOR_B_TICKET, VECTOR_B_PAYLOAD, **seams(opener))
    assert result == {"status": 409, "error": "conflict", "replayed": None}


@pytest.mark.parametrize(
    ("opener", "code", "status"),
    [
        (refuses(401, b'{"ok":false,"error":"unauthorized"}'), "signature_rejected", 401),
        (refuses(503, SECRET_ERROR_BODY), "secret_missing", 503),
        (refuses(503, b"<html>gateway</html>"), "list_error", 503),
        (refuses(500, b'{"ok":false,"error":"server"}'), "list_error", 500),
        (refuses(502, b"<html>bad gateway</html>"), "list_error", 502),
        (fails_with(TimeoutError("timed out")), "list_timeout", None),
        (fails_with(socket.timeout("timed out")), "list_timeout", None),
        (fails_with(urllib.error.URLError("no route")), "list_timeout", None),
        (fails_with(ConnectionResetError("reset")), "list_timeout", None),
        (answers_raw(b"<html>ok</html>"), "list_error", 200),
        (answers_raw(b""), "list_error", 200),
    ],
)
def test_post_classification_raises_a_code_for_every_other_outcome(opener, code, status) -> None:
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.post_classification(VECTOR_B_TICKET, VECTOR_B_PAYLOAD, **seams(opener))
    assert (caught.value.code, caught.value.http_status) == (code, status)


# -------------------------------------------- post_source_shadow_observations


OBSERVATIONS = {"contract_version": "1", "run_id": "run-1", "observations": []}


def test_post_source_shadow_observations_sends_one_signed_post() -> None:
    opener = answers({"ok": True, "replayed": False})
    tickets_api.post_source_shadow_observations(OBSERVATIONS, **seams(opener))
    request = opener.request
    assert request.get_method() == "POST"
    assert request.full_url == f"{ORIGIN}/api/tickets/source-shadow"
    assert opener.timeout == 5.0
    assert request.data == json.dumps(OBSERVATIONS, separators=(",", ":")).encode("utf-8")
    assert header(request, "x-packn-signature") == expected_signature(request)


def test_post_source_shadow_observations_200_is_a_result() -> None:
    opener = answers({"ok": True, "replayed": True})
    result = tickets_api.post_source_shadow_observations(OBSERVATIONS, **seams(opener))
    assert result == {"status": 200, "error": None, "replayed": True}


@pytest.mark.parametrize(("status", "error"), [(400, "bad_payload"), (413, "payload_too_large")])
def test_post_source_shadow_observations_verdict_statuses_are_results(
    status: int, error: str
) -> None:
    result = tickets_api.post_source_shadow_observations(OBSERVATIONS, **seams(refuses(status)))
    assert result == {"status": status, "error": error, "replayed": None}


@pytest.mark.parametrize(
    ("opener", "code", "status"),
    [
        (refuses(401), "signature_rejected", 401),
        (refuses(503, SECRET_ERROR_BODY), "secret_missing", 503),
        (refuses(503, b""), "list_error", 503),
        (refuses(404), "list_error", 404),
        (refuses(409), "list_error", 409),
        (refuses(422), "list_error", 422),
        (refuses(500), "list_error", 500),
        (fails_with(TimeoutError("timed out")), "list_timeout", None),
        (fails_with(ConnectionRefusedError("refused")), "list_timeout", None),
        (answers_raw(b"not json"), "list_error", 200),
    ],
)
def test_post_source_shadow_observations_raises_for_everything_else(opener, code, status) -> None:
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.post_source_shadow_observations(OBSERVATIONS, **seams(opener))
    assert (caught.value.code, caught.value.http_status) == (code, status)


# --------------------------------------------------------------- iso_millis_z


def test_iso_millis_z_writes_three_fractional_digits_and_a_z() -> None:
    instant = datetime(2026, 10, 9, 14, 7, 3, 120456, tzinfo=timezone.utc)
    assert tickets_api.iso_millis_z(instant) == "2026-10-09T14:07:03.120Z"


def test_iso_millis_z_converts_an_offset_to_utc() -> None:
    eastern = timezone(timedelta(hours=-4))
    instant = datetime(2026, 10, 9, 10, 7, 3, 120456, tzinfo=eastern)
    assert tickets_api.iso_millis_z(instant) == "2026-10-09T14:07:03.120Z"


def test_iso_millis_z_pads_a_whole_second_instant() -> None:
    instant = datetime(2026, 10, 9, 14, 7, 3, tzinfo=timezone.utc)
    assert tickets_api.iso_millis_z(instant) == "2026-10-09T14:07:03.000Z"


def test_iso_millis_z_never_rounds_up_into_the_next_second() -> None:
    instant = datetime(2026, 12, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)
    assert tickets_api.iso_millis_z(instant) == "2026-12-31T23:59:59.999Z"


def test_iso_millis_z_refuses_a_naive_datetime() -> None:
    with pytest.raises(ValueError):
        tickets_api.iso_millis_z(datetime(2026, 10, 9, 14, 7, 3, 120456))


# ------------------------------------------------- no body text in any output


def _marker_body(kind: str) -> bytes:
    if kind == "secret":
        return json.dumps(
            {"ok": False, "error": "tickets_secret_not_configured", "detail": MARKER}
        ).encode()
    return ("<html><body>" + MARKER + "</body></html>").encode()


@pytest.mark.parametrize(
    ("status", "kind", "code"),
    [
        (401, "html", "signature_rejected"),
        (503, "secret", "secret_missing"),
        (503, "html", "list_error"),
        (500, "html", "list_error"),
    ],
)
@pytest.mark.parametrize("call", ["list", "ticket", "classification", "source_shadow"])
def test_a_failing_response_body_reaches_no_log_and_no_stream(
    status: int, kind: str, code: str, call: str, caplog, capfd
) -> None:
    caplog.set_level(logging.DEBUG)
    opener = refuses(status, _marker_body(kind))
    with pytest.raises(TicketsApiError) as caught:
        if call == "list":
            tickets_api.fetch_unprocessed(25, **seams(opener))
        elif call == "ticket":
            tickets_api.fetch_ticket("123456", **seams(opener))
        elif call == "classification":
            tickets_api.post_classification(VECTOR_B_TICKET, VECTOR_B_PAYLOAD, **seams(opener))
        else:
            tickets_api.post_source_shadow_observations(OBSERVATIONS, **seams(opener))
    error = caught.value
    assert error.code == code
    # nothing of the response rides the exception
    assert MARKER not in str(error)
    assert MARKER not in repr(error)
    assert MARKER not in repr(error.args)
    assert error.__cause__ is None
    assert error.__context__ is None
    out, err = capfd.readouterr()
    assert MARKER not in caplog.text
    assert MARKER not in out
    assert MARKER not in err


def test_a_transport_failure_text_reaches_no_log_and_no_stream(caplog, capfd) -> None:
    caplog.set_level(logging.DEBUG)
    opener = fails_with(urllib.error.URLError(MARKER))
    with pytest.raises(TicketsApiError) as caught:
        tickets_api.fetch_unprocessed(25, **seams(opener))
    error = caught.value
    assert MARKER not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    out, err = capfd.readouterr()
    assert MARKER not in caplog.text
    assert MARKER not in out
    assert MARKER not in err


def test_a_successful_response_body_reaches_no_log_and_no_stream(caplog, capfd) -> None:
    caplog.set_level(logging.DEBUG)
    body = {"contract_version": "1", "tickets": [{"id": "t1", "body_text": MARKER}]}
    assert tickets_api.fetch_unprocessed(25, **seams(answers(body))) == body
    tickets_api.fetch_ticket("123456", **seams(answers({"ticket": {"subject": MARKER}})))
    out, err = capfd.readouterr()
    assert MARKER not in caplog.text
    assert MARKER not in out
    assert MARKER not in err


def test_a_request_body_reaches_no_log_and_no_stream(caplog, capfd) -> None:
    caplog.set_level(logging.DEBUG)
    payload = {"category": "wismo_tracking", "reason": MARKER}
    tickets_api.post_classification(
        VECTOR_B_TICKET, payload, **seams(answers({"ok": True, "replayed": False}))
    )
    tickets_api.post_classification(VECTOR_B_TICKET, payload, **seams(refuses(409)))
    with pytest.raises(TicketsApiError):
        tickets_api.post_classification(VECTOR_B_TICKET, payload, **seams(refuses(500)))
    out, err = capfd.readouterr()
    assert MARKER not in caplog.text
    assert MARKER not in out
    assert MARKER not in err


def test_the_secret_reaches_no_log_and_no_stream(caplog, capfd) -> None:
    caplog.set_level(logging.DEBUG)
    tickets_api.fetch_unprocessed(25, **seams(answers({"contract_version": "1", "tickets": []})))
    with pytest.raises(TicketsApiError):
        tickets_api.fetch_unprocessed(25, **seams(refuses(401)))
    out, err = capfd.readouterr()
    assert FIXTURE_SECRET not in caplog.text
    assert FIXTURE_SECRET not in out
    assert FIXTURE_SECRET not in err


def test_a_log_line_names_the_route_the_status_and_a_count(capfd) -> None:
    body = {"contract_version": "1", "tickets": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
    tickets_api.fetch_unprocessed(25, **seams(answers(body)))
    _, err = capfd.readouterr()
    assert "[tickets_api]" in err
    assert "unprocessed" in err
    assert "200" in err
    assert "count=3" in err


# ----------------------------------------------------------------- static gates


def test_the_module_imports_only_the_standard_library() -> None:
    source = (PACKAGE_ROOT / "packn_os_hubspot_client" / "tickets_api.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # a relative import would tie the client to the database helper
            assert node.level == 0, f"relative import at line {node.lineno}"
            imported.add((node.module or "").split(".")[0])
    assert imported, "the module imports nothing — the parse found no import"
    outside = sorted(name for name in imported if name not in sys.stdlib_module_names)
    assert outside == []


def test_no_path_under_the_secrets_directory_is_tracked() -> None:
    listed = subprocess.run(
        ["git", "ls-files"], cwd=GIT_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert len(listed) > 50, "git ls-files returned too little to be the repository"
    assert [path for path in listed if "config/.secrets/" in path] == []


def test_the_per_run_bundle_directory_is_ignored() -> None:
    probe = "hubspot_ticket_automation/outputs/runs/os/run-1/bundle.json"
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", probe], cwd=GIT_ROOT, capture_output=True
    )
    assert ignored.returncode == 0, f"{probe} is not ignored"
