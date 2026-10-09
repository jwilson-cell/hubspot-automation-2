"""Suite-level network refusal for the cron's tests (Phase 93, plan 14, task zero).

No cron test can open a connection to anything but this machine. Both test roots
install it from their conftest (`tests/conftest.py` and
`packn_os_hubspot_client/tests/conftest.py`), as an autouse fixture, so a test file
that drives the pre-gate, the hydrate step or a request function without a fake
fails instead of sending a request. Plan 93-13 found out why this has to be a
property of the suite: its first RED run sent eleven real searches to HubSpot.

Four entry points are replaced for the length of every test:

    urllib.request.urlopen              refused before the URL is opened
    urllib.request.OpenerDirector.open  the same, for an opener built by hand
                                        (`build_opener(...)`, as tickets_api does)
    socket.create_connection            refused before the name is resolved
    socket.socket.connect / connect_ex  the last line: every Python socket

Loopback stays allowed (`localhost`, 127.0.0.0/8, ::1): the helper suite's Redis
tests connect to the local docker Redis through Python sockets, and asyncio's
self-pipe on Windows is a loopback socket pair. The database tests do not pass
through here at all: psycopg's binary driver connects inside libpq, in C.

A refusal is an AssertionError, not an OSError. The request code catches OSError
and carries on (or retries), so an OSError would be mistaken for a failed call
and swallowed.

Some callers swallow every exception by their own fail-open rule (the rate
limiter's lookup does). So each refusal is also written to a ledger, and the
fixture fails the test at teardown when the ledger is not empty: a test cannot
pass having tried to leave the machine, whoever caught the error. A test that
provokes a refusal on purpose takes it back out with `drain()`.

Standard library only. This module opens nothing itself.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.parse
import urllib.request
from typing import Any, Optional

NETWORK_REFUSED_PREFIX = "a cron test tried to open a network connection"

LOOPBACK_NAMES = ("localhost",)

# Looked up once, at import, so a per-file guard that replaces the same names
# later (Plan 93-13's two files do) is never what this module calls through to.
_REAL_URLOPEN = urllib.request.urlopen
_REAL_OPENER_OPEN = urllib.request.OpenerDirector.open
_REAL_CREATE_CONNECTION = socket.create_connection
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex

# Every refusal since the last drain(), as its message. list.append is atomic, so
# a refusal on a worker thread is recorded too.
_REFUSALS: list[str] = []


def drain() -> list[str]:
    """The refusals recorded since the last call, removed from the ledger."""
    taken = list(_REFUSALS)
    del _REFUSALS[: len(taken)]
    return taken


def is_loopback_host(host: Any) -> bool:
    """True for `localhost` and for an address in 127.0.0.0/8 or ::1.

    Anything else is not loopback: a public name, a private address, an empty
    string, None, a name that merely starts with `localhost`.
    """
    if isinstance(host, (bytes, bytearray)):
        try:
            host = bytes(host).decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str):
        return False
    name = host.strip().lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    name = name.split("%", 1)[0]  # an IPv6 scope id
    if name in LOOPBACK_NAMES:
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        return mapped.is_loopback
    return address.is_loopback


def refusal_message(host: Any, port: Any = None, via: str = "") -> str:
    where = f"{host!r}" if port is None else f"{host!r} port {port!r}"
    how = f" through {via}" if via else ""
    return (
        f"{NETWORK_REFUSED_PREFIX} to {where}{how}. Cron tests reach this machine "
        "only: give the code a fake transport or opener."
    )


def _refuse(host: Any, port: Any, via: str) -> None:
    message = refusal_message(host, port, via)
    _REFUSALS.append(message)
    raise AssertionError(message)


def refuse_unless_loopback(host: Any, port: Any = None, via: str = "") -> None:
    if not is_loopback_host(host):
        _refuse(host, port, via)


def _url_target(target: Any) -> tuple[Optional[str], Optional[int]]:
    """Host and port of a URL string or a urllib Request; (None, None) with no host."""
    url = target.full_url if isinstance(target, urllib.request.Request) else target
    if not isinstance(url, str):
        return None, None
    try:
        parts = urllib.parse.urlsplit(url)
        return parts.hostname, parts.port
    except ValueError:
        return None, None


def _refuse_url(target: Any, via: str) -> None:
    host, port = _url_target(target)
    if host is None:
        # No host to judge (a file: or data: URL, or something unparseable). It
        # opens no socket; if it somehow tried, the socket layer below refuses.
        return
    refuse_unless_loopback(host, port, via)


def _refuse_address(address: Any, via: str) -> None:
    if isinstance(address, (str, bytes, bytearray)):
        return  # a filesystem socket path: local by construction
    if isinstance(address, tuple) and address:
        port = address[1] if len(address) > 1 else None
        refuse_unless_loopback(address[0], port, via)
        return
    _refuse(address, None, via)


def guarded_urlopen(url: Any, *args: Any, **kwargs: Any) -> Any:
    _refuse_url(url, "urllib.request.urlopen")
    return _REAL_URLOPEN(url, *args, **kwargs)


def guarded_opener_open(self: Any, fullurl: Any, *args: Any, **kwargs: Any) -> Any:
    _refuse_url(fullurl, "an opener's open()")
    return _REAL_OPENER_OPEN(self, fullurl, *args, **kwargs)


def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
    _refuse_address(address, "socket.create_connection")
    return _REAL_CREATE_CONNECTION(address, *args, **kwargs)


def guarded_connect(self: Any, address: Any) -> Any:
    _refuse_address(address, "socket.connect")
    return _REAL_CONNECT(self, address)


def guarded_connect_ex(self: Any, address: Any) -> Any:
    _refuse_address(address, "socket.connect_ex")
    return _REAL_CONNECT_EX(self, address)


def install(monkeypatch: Any) -> None:
    """Replace the four entry points until `monkeypatch` is undone."""
    drain()
    monkeypatch.setattr(urllib.request, "urlopen", guarded_urlopen)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", guarded_opener_open)
    monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


def unanswered_refusals() -> str:
    """Empty when the ledger is; else the text the fixture fails the test with."""
    refusals = drain()
    if not refusals:
        return ""
    return (
        f"{len(refusals)} network connection(s) were refused during this test and not "
        "taken back with drain(). If the test body passed, the code under test "
        "swallowed the refusal:\n  " + "\n  ".join(refusals)
    )
