"""The helper suite carries the suite-level network refusal (Phase 93, plan 14).

The guard and its full self-tests live with the pure suite
(tests/helpers/network_guard.py, tests/test_network_guard.py). These two cases
prove only that THIS test root installs it too: the fixture is in this
directory's conftest, and a conftest that lost it would turn them red.

The target is under the reserved `.invalid` top-level domain and the resolver is
replaced by a spy, so nothing is looked up even if the guard were missing.
"""

import socket
import urllib.request

import pytest

from tests.helpers import network_guard

FAR_HOST = "far.example.invalid"


def test_the_helper_suite_refuses_a_non_loopback_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookups: list = []

    def spy(host, *args, **kwargs):
        lookups.append(host)
        raise RuntimeError(f"a name was resolved: {host!r}")

    monkeypatch.setattr(socket, "getaddrinfo", spy)

    with pytest.raises(AssertionError, match="network connection") as by_url:
        urllib.request.urlopen(f"https://{FAR_HOST}/", timeout=1)
    with pytest.raises(AssertionError, match="network connection") as by_socket:
        socket.create_connection((FAR_HOST, 443), timeout=1)

    assert FAR_HOST in str(by_url.value) and FAR_HOST in str(by_socket.value)
    assert lookups == []
    assert len(network_guard.drain()) == 2


def test_the_helper_suite_still_reaches_loopback() -> None:
    """Redis and the local docker Postgres are on this machine."""
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
            assert conn.getpeername()[1] == port
    assert network_guard.drain() == []
