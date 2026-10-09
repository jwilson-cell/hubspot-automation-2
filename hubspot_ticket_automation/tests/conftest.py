"""Pure-suite database guard (Phase 93, plan 12).

No test under tests/ can reach a database. For the length of every test in this
directory the helper's pool getter is replaced with one that raises, on the
helper's `db` module and on every loaded module that bound the name with
`from ...db import get_pool` (today `client`, and the scripts that import it).
"No database" is then a property of the suite, not of each test's good
behaviour: a URL left exported in the shell is never used, because nothing asks
for a pool.

The helper package's own suite under packn_os_hubspot_client/tests/ does use the
pool. It is not affected when both directories run in one session: monkeypatch
undoes the replacement after each test, and a module that was first imported
while a test ran (and so bound the guard by name) is handed the real getter back.

What a caller sees: `RuntimeError` with NO_DATABASE_MESSAGE. A function that
swallows every exception by its own fail-closed rule (`client.read_routine_enabled`
returns False) reports its fail-closed value instead; no connection is attempted
either way.

The fixture reads no environment variable and prints nothing.
"""
from __future__ import annotations

import sys
from typing import Any, Callable, Iterable, Iterator, Optional

import pytest

NO_DATABASE_MESSAGE = (
    "the pure suite has no database: a test under tests/ asked for the helper's "
    "connection pool. Use a fake, or put a database test under "
    "packn_os_hubspot_client/tests/."
)


def _no_database_pool(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError(NO_DATABASE_MESSAGE)


def _holders(getter: Callable[..., Any], modules: Optional[Iterable[Any]] = None) -> list[Any]:
    """The modules whose own `get_pool` name is bound to `getter`."""
    candidates = list(sys.modules.values()) if modules is None else list(modules)
    return [
        module
        for module in candidates
        if getattr(module, "__dict__", {}).get("get_pool") is getter
    ]


def _hand_back(real: Callable[..., Any], modules: Optional[Iterable[Any]] = None) -> None:
    """Give the real getter to every module still holding the guard."""
    for module in _holders(_no_database_pool, modules):
        setattr(module, "get_pool", real)


@pytest.fixture(autouse=True)
def _pure_suite_has_no_database(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    try:
        from packn_os_hubspot_client import db
    except ImportError:
        # No database driver on this machine: it cannot reach a database either.
        yield
        return
    real = db.get_pool
    for module in _holders(real):
        monkeypatch.setattr(module, "get_pool", _no_database_pool)
    yield
    _hand_back(real)
