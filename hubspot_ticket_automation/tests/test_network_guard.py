"""The suite-level network refusal bites (Phase 93, plan 14, task zero).

These tests have no network guard of their own: what refuses is the autouse
fixture in tests/conftest.py. Every non-loopback target is under the reserved
`.invalid` top-level domain, which no resolver answers. The cases that go through
Python's resolver also put a spy on it first, so with a broken guard they would
still look nothing up; the bare `socket.connect` case resolves inside the C
socket, where no spy reaches, and relies on the reserved name alone.

A test that provokes a refusal takes it back out of the ledger with `drain()`;
otherwise the fixture would fail it at teardown, which is the guard's second
half and has its own case below.
"""
from __future__ import annotations

import socket
import urllib.request

import pytest

from packn_os_hubspot_client import tickets_api
from tests.helpers import network_guard
from ticket_pipeline import hubspot_api

FAR_HOST = "far.example.invalid"
FAR_URL = f"https://{FAR_HOST}/crm/v3/objects/tickets/search"


@pytest.fixture
def no_lookup(monkeypatch: pytest.MonkeyPatch) -> list:
    """A spy on the resolver the socket module uses: records, then raises."""
    lookups: list = []

    def spy(host, *args, **kwargs):
        lookups.append(host)
        raise RuntimeError(f"a name was resolved: {host!r}")

    monkeypatch.setattr(socket, "getaddrinfo", spy)
    return lookups


def _refused(call) -> str:
    """Run `call`, require the guard's AssertionError, hand back its message."""
    with pytest.raises(AssertionError) as caught:
        call()
    message = str(caught.value)
    assert message.startswith(network_guard.NETWORK_REFUSED_PREFIX)
    assert network_guard.drain() == [message]
    return message


# ------------------------------------------------------------------ the rule


@pytest.mark.parametrize(
    "host",
    ["localhost", "LOCALHOST", "127.0.0.1", "127.8.9.10", "::1", "[::1]", "::ffff:127.0.0.1", b"127.0.0.1"],
)
def test_loopback_hosts_are_loopback(host) -> None:
    assert network_guard.is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "api.hubapi.com",
        "far.example.invalid",
        "localhost.example.invalid",
        "10.0.0.5",
        "192.168.1.20",
        "0.0.0.0",
        "::",
        "::ffff:10.0.0.5",
        "",
        None,
        443,
    ],
)
def test_everything_else_is_not_loopback(host) -> None:
    assert network_guard.is_loopback_host(host) is False


def test_the_message_names_the_host_and_the_port() -> None:
    message = network_guard.refusal_message(FAR_HOST, 443, "socket.connect")
    assert message.startswith(network_guard.NETWORK_REFUSED_PREFIX)
    assert FAR_HOST in message and "443" in message and "socket.connect" in message
    assert "network connection" in message  # the phrase Plan 93-13's per-file tests match


def test_a_refusal_is_not_an_oserror() -> None:
    """The request code catches OSError and carries on; this must not be one."""
    assert not issubclass(AssertionError, OSError)
    with pytest.raises(AssertionError):
        network_guard.refuse_unless_loopback(FAR_HOST, 443)
    assert len(network_guard.drain()) == 1


# ------------------------------------------------------------------ the four entry points


def test_urlopen_is_refused_before_any_lookup(no_lookup: list) -> None:
    message = _refused(lambda: urllib.request.urlopen(FAR_URL, timeout=1))
    assert FAR_HOST in message
    assert no_lookup == []


def test_an_opener_built_by_hand_is_refused_before_any_lookup(no_lookup: list) -> None:
    opener = urllib.request.build_opener()
    message = _refused(lambda: opener.open(urllib.request.Request(FAR_URL), timeout=1))
    assert FAR_HOST in message
    assert no_lookup == []


def test_create_connection_is_refused_before_any_lookup(no_lookup: list) -> None:
    message = _refused(lambda: socket.create_connection((FAR_HOST, 443), timeout=1))
    assert FAR_HOST in message and "443" in message
    assert no_lookup == []


def test_a_bare_socket_connect_is_refused() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        message = _refused(lambda: sock.connect((FAR_HOST, 443)))
        assert FAR_HOST in message
        _refused(lambda: sock.connect_ex((FAR_HOST, 443)))


# ------------------------------------------------------------------ the cron's own request code


def test_the_hubspot_transport_is_refused_before_any_lookup(no_lookup: list) -> None:
    """`_default_opener` is what `_request` sends through when no seam is used."""
    request = urllib.request.Request(FAR_URL, data=b"{}", method="POST")
    message = _refused(lambda: hubspot_api._default_opener(request, timeout=1))
    assert FAR_HOST in message
    assert no_lookup == []


def test_the_request_function_does_not_swallow_the_refusal(
    monkeypatch: pytest.MonkeyPatch, no_lookup: list
) -> None:
    """The incident of Plan 93-13: a search with no fake. It must fail, not retry."""
    monkeypatch.setattr(hubspot_api, "HUBSPOT_BASE", f"https://{FAR_HOST}")
    monkeypatch.setattr(hubspot_api, "_acquire_rate_token", lambda: None)
    monkeypatch.setattr(hubspot_api, "RETRY_SLEEP_S", 0)
    hubspot_api.reset_calls()
    try:
        message = _refused(lambda: hubspot_api.search_tickets({"limit": 1}, "test-token"))
        assert hubspot_api.snapshot_calls()["raw_requests"] == 1  # one attempt, no retry
    finally:
        hubspot_api.reset_calls()
    assert FAR_HOST in message
    assert no_lookup == []


def test_the_os_tickets_opener_is_refused_before_any_lookup(no_lookup: list) -> None:
    """tickets_api builds its own opener, so `urlopen` alone would not cover it."""
    request = urllib.request.Request(f"https://{FAR_HOST}/api/tickets/unprocessed")
    message = _refused(lambda: tickets_api._OPENER.open(request, timeout=1))
    assert FAR_HOST in message
    assert no_lookup == []


# ------------------------------------------------------------------ loopback stays open


def test_a_loopback_connection_is_allowed() -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as via_helper:
            assert via_helper.getpeername()[1] == port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as bare:
            bare.settimeout(5)
            bare.connect(("localhost", port))
            assert bare.getpeername()[1] == port
    assert network_guard.drain() == []


# ------------------------------------------------------------------ the ledger


def test_a_swallowed_refusal_is_still_on_the_ledger() -> None:
    """A caller with `except Exception` hides the error; the ledger keeps it."""
    try:
        socket.create_connection((FAR_HOST, 443), timeout=1)
    except Exception:  # noqa: BLE001 - this is the fail-open shape under test
        pass
    report = network_guard.unanswered_refusals()
    assert "1 network connection(s) were refused" in report
    assert FAR_HOST in report
    assert network_guard.unanswered_refusals() == ""  # reading it emptied it


def test_install_starts_from_an_empty_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AssertionError):
        network_guard.refuse_unless_loopback(FAR_HOST)
    network_guard.install(monkeypatch)
    assert network_guard.drain() == []
