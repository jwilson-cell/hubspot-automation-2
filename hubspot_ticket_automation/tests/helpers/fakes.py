"""Test fakes for the cron (Phase 93, plan 12).

Four doubles and one assertion, shared by every cron test of the phase:

    FakeHubSpotTransport  stands in for ticket_pipeline.hubspot_api._request
    FakeOsApi             stands in for packn_os_hubspot_client.tickets_api
    FakeLauncher          stands in for the agent launcher, launch_agent(command, env)
    temp_state_file       a state file under the pytest temp directory
    assert_marker_absent  "this text reached no log record, stdout or stderr"

They record and replay. They hold no business rule: the OS fake does not cut a
list to a limit, the transport does not rate-limit, the launcher runs nothing.
Each is self-tested in tests/test_fakes.py, so a green result in a later test
cannot come from a fake that silently returns nothing.

Standard library only, plus the client's error type.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Optional

from packn_os_hubspot_client.tickets_api import TicketsApiError

# The environment variable the wrapper uses to tell the agent where to write its result.
RESULT_PATH_ENV = "PACKN_RESULT_PATH"

_OS_FUNCTIONS = (
    "fetch_unprocessed",
    "fetch_ticket",
    "post_classification",
    "post_source_shadow_observations",
)


class FakeHubSpotTransport:
    """A HubSpot transport with a call log.

    Callable like `hubspot_api._request(method, path, token, body=None)`, so it can
    be set over that function with monkeypatch and every helper above it
    (search_tickets, get_associated_ids, batch_read) goes through the fake.

        calls    every request, in order, as (method, path)
        bodies   the body of each request, in the same order (None for a GET)

    Responses are canned by path prefix; the longest matching prefix wins. A path
    with nothing canned answers None — the real client's value for a failed call.
    """

    def __init__(self, responses: Optional[dict[str, Any]] = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[Any] = []
        self._responses: dict[str, Any] = dict(responses or {})

    def respond(self, prefix: str, response: Any) -> None:
        """Can `response` for every path that starts with `prefix`."""
        self._responses[prefix] = response

    def calls_matching(self, prefix: str) -> list[tuple[str, str]]:
        """The recorded calls whose path starts with `prefix`, in order."""
        return [call for call in self.calls if call[1].startswith(prefix)]

    def __call__(self, method: str, path: str, token: Any = None, body: Any = None) -> Any:
        self.calls.append((method, path))
        self.bodies.append(copy.deepcopy(body))
        matching = [prefix for prefix in self._responses if path.startswith(prefix)]
        if not matching:
            return None
        return copy.deepcopy(self._responses[max(matching, key=len)])


class FakeOsApi:
    """The four tickets_api functions with canned results and a call log.

    Each method takes the same positional parameters as the real function and none
    of its keyword-only test seams (tests/test_fakes.py compares the signatures).

        unprocessed              what fetch_unprocessed answers (an empty list by default)
        tickets                  ref -> what fetch_ticket answers; an unknown ref answers
                                 None, as the real client does for a 404
        classification_result    what post_classification answers
        classification_results   os_ticket_id -> a result for that ticket only
        source_shadow_result     what post_source_shadow_observations answers
        calls                    every call, in order, as (function name, positional args)

    fail_next_list(code) makes the next list fetch raise TicketsApiError(code);
    fail_next(function_name, code) does the same for any one of the four.
    """

    def __init__(
        self,
        unprocessed: Optional[dict] = None,
        tickets: Optional[dict[str, dict]] = None,
    ) -> None:
        self.unprocessed: dict = (
            unprocessed
            if unprocessed is not None
            else {
                "contract_version": "1",
                "generated_at": "2026-01-01T00:00:00.000Z",
                "tickets": [],
            }
        )
        self.tickets: dict[str, dict] = dict(tickets or {})
        self.classification_result: dict = {"status": 200, "error": None, "replayed": False}
        self.classification_results: dict[str, dict] = {}
        self.source_shadow_result: dict = {"status": 200, "error": None, "replayed": False}
        self.calls: list[tuple[str, tuple]] = []
        self._pending_failures: dict[str, TicketsApiError] = {}

    def fail_next(self, function_name: str, code: str, http_status: Optional[int] = None) -> None:
        """Make the next call of one function raise TicketsApiError(code, http_status)."""
        if function_name not in _OS_FUNCTIONS:
            raise ValueError(f"not a tickets_api function: {function_name}")
        self._pending_failures[function_name] = TicketsApiError(code, http_status)

    def fail_next_list(self, code: str, http_status: Optional[int] = None) -> None:
        """Make the next fetch_unprocessed raise TicketsApiError(code, http_status)."""
        self.fail_next("fetch_unprocessed", code, http_status)

    def calls_to(self, function_name: str) -> list[tuple]:
        """The positional args of every recorded call of one function, in order."""
        return [args for name, args in self.calls if name == function_name]

    def _record(self, function_name: str, *args: Any) -> None:
        self.calls.append((function_name, copy.deepcopy(args)))
        failure = self._pending_failures.pop(function_name, None)
        if failure is not None:
            raise failure

    def fetch_unprocessed(self, limit):
        self._record("fetch_unprocessed", limit)
        return copy.deepcopy(self.unprocessed)

    def fetch_ticket(self, ticket_ref):
        self._record("fetch_ticket", ticket_ref)
        return copy.deepcopy(self.tickets.get(str(ticket_ref)))

    def post_classification(self, os_ticket_id, payload):
        self._record("post_classification", os_ticket_id, payload)
        return copy.deepcopy(
            self.classification_results.get(str(os_ticket_id), self.classification_result)
        )

    def post_source_shadow_observations(self, payload):
        self._record("post_source_shadow_observations", payload)
        return copy.deepcopy(self.source_shadow_result)


class FakeLauncher:
    """The agent launcher, without an agent: `launcher(command, env) -> exit code`.

    Records the command and the environment of every call. When it was given a
    `result` and the environment names a result path (PACKN_RESULT_PATH), it writes
    the result there as JSON — what a finished agent does — and then returns the
    exit code. With no result, or no path in the environment, it writes nothing.
    """

    def __init__(self, exit_code: int, result: Optional[Any] = None) -> None:
        self.exit_code = exit_code
        self.result = result
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, command, env):
        self.calls.append((list(command), dict(env)))
        target = env.get(RESULT_PATH_ENV)
        if self.result is not None and target:
            path = Path(target)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.result), encoding="utf-8")
        return self.exit_code


def temp_state_file(tmp_path: Path, state: dict) -> Path:
    """Write `state` as the cron's state file under the pytest temp directory.

    Returns the path to hand to the code under test. The tracked
    config/state.json is never opened here.
    """
    path = Path(tmp_path) / "state.json"
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


def assert_marker_absent(marker: str, caplog, capfd) -> None:
    """Assert `marker` reached no log record and no stdout or stderr text.

    This repo's scripts log with print to stderr, so a check of captured logging
    alone proves nothing about them. All three sinks are read: pytest's captured
    logging (the formatted text and every field of every record, so a value passed
    through `extra` counts), captured stdout and captured stderr.

    Two things the caller must do:
      - call `caplog.set_level(logging.DEBUG)` before the code under test, or a
        record below the root logger's level is never captured;
      - call this once, after the code under test: reading the captured streams
        empties them.
    """
    if not marker:
        raise ValueError("assert_marker_absent needs a non-empty marker")
    out, err = capfd.readouterr()
    logged = "\n".join([caplog.text, *(repr(vars(record)) for record in caplog.records)])
    for sink, text in (("logging", logged), ("stdout", out), ("stderr", err)):
        assert marker not in text, f"marker {marker!r} found in captured {sink}"
