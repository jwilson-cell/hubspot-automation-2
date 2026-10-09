"""`os` mode, first half: the list comes from Pack'N OS (Phase 93, plan 16).

`ticket_pipeline/os_pass.py` is the runner the orchestrator dispatches to when
the source switch says `os`. These tests drive `run_tickets.run(deps)` end to
end, so the one-record rule of plan 93-15 is exercised for this mode too:

    the list        asked of the OS, with the cap clamped into 1 to 100
    fail closed     a list that cannot be had is ONE failure record with its own
                    code: no HubSpot request, no agent, no draft, no fallback
    the bundle      one entry per distinct ticket; `ref` is its draft key
    HubSpot         one counted thread fetch per linked ticket, nothing else;
                    a ticket with no HubSpot id costs nothing
    the agent       launched with the HubSpot MCP server's tools removed
    the result      parsed and reconciled against the bundle's refs

No test here reaches a database, HubSpot, the Pack'N OS, a real secret file,
the tracked state file, the ShipSidekick lookup or the real agent:

    - the suite's network guard and no-database guard (tests/conftest.py) apply;
    - an autouse fixture refuses every child process and the ShipSidekick
      lookup (which is a child process), with a ledger, because the code under
      test turns a refusal into a code and would otherwise hide it;
    - an autouse fixture points the token path, the secret path and the
      settings path of the two clients at files that do not exist.
"""
from __future__ import annotations

import ast
import builtins
import copy
import io
import json
import logging
import re
import subprocess
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # hubspot_ticket_automation/
sys.path.insert(0, str(PACKAGE_ROOT / "scripts"))  # scripts/ is imported flat

import pregate_tickets as pg  # noqa: E402
import run_tickets as rt  # noqa: E402
from packn_os_hubspot_client import client, tickets_api  # noqa: E402
from tests.helpers import fakes  # noqa: E402
from tests.test_run_record import FakeClient  # noqa: E402 - the signature-checked fake
from ticket_pipeline import hubspot_api, hydrate, llm, os_bundle, os_pass, run_types  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
OS_ORIGIN = "https://os.example.test"
PORTAL_ID = 50830922
MARKER = "ZZ-MARKER-93-16"
EXC_MARKER = "ZZ-EXC-93-16"
FIXTURE_SECRET = "test-vector-secret-not-a-real-secret-0123456789"

T1_OS_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
T2_OS_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
T3_OS_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
T1_HS_ID = "9300000000001"
T3_HS_ID = "9300000000003"
T1_SIGNAL_ID = "1c7d2e3f-4a5b-4c6d-9e8f-0a1b2c3d4e5f"
T2_SIGNAL_ID = "2d8e3f4a-5b6c-4d7e-8f9a-1b2c3d4e5f6a"
T3_SIGNAL_ID = "4f0a5b6c-7d8e-4f9a-8b1c-3d4e5f6a7b8c"
RERUN_A = "9b2d4c1e-55aa-4f0b-8c3d-1e2f3a4b5c6d"
RERUN_B = "5e6f7a8b-1c2d-4e3f-9a4b-5c6d7e8f9a0b"

# The paths the real code would read. Taken at import, before any test moves them.
REAL_TOKEN_PATH = hubspot_api.TOKEN_PATH
REAL_SECRET_PATH = tickets_api.SECRET_PATH
REAL_STATE_PATH = pg.STATE_PATH

SETTINGS = {
    "per_run_cap": 25,
    "hubspot_portal_id": PORTAL_ID,
    "conversation_email_limit": 10,
    "pipeline": {"shadow_enabled": False},
}
STATE = {
    "last_run_at": "2026-09-01T00:00:00Z",
    "ticket_fingerprints": {
        T1_HS_ID: {
            "processed_at": "2026-10-09T00:00:00Z",
            "hs_lastmodifieddate": "2026-10-08T10:00:00.500Z",
            "num_notes": 2,
        },
    },
}

ZERO_KINDS = {
    "search": 0,
    "ticket_read": 0,
    "contact_read": 0,
    "company_read": 0,
    "related_lookup": 0,
    "rerun_read": 0,
    "email_thread": 0,
    "number_backfill": 0,
}
NO_CALLS = {"kinds": dict(ZERO_KINDS), "raw_requests": 0}

CHILD_REFUSED = "a test in test_os_mode_run.py tried to start a child process"
LOOKUP_REFUSED = "a test in test_os_mode_run.py reached the ShipSidekick lookup"
_CHILD_LEDGER: list[str] = []


def _list() -> dict:
    return json.loads((FIXTURES / "tickets-api-list.v1.json").read_text(encoding="utf-8"))


def _single() -> dict:
    return json.loads((FIXTURES / "tickets-api-ticket.v1.json").read_text(encoding="utf-8"))


def _payload(index: int) -> dict:
    return _list()["tickets"][index]


# ----------------------------------------------------------------- test safety


@pytest.fixture(autouse=True)
def _no_child_process(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test in this file starts a process: not the agent, not the engine
    shadow, not the ShipSidekick lookup (a child process the suite's network
    guard cannot see into). The same refusal-plus-ledger shape as the
    orchestrator's tests: the runner turns an exception into a code, so a
    refusal is recorded as well as raised and fails the test at teardown."""
    _CHILD_LEDGER.clear()

    def refuse(*args: Any, **_kwargs: Any) -> Any:
        _CHILD_LEDGER.append(repr(args[:1]))
        raise AssertionError(CHILD_REFUSED)

    def refuse_lookup(*_args: Any, **_kwargs: Any) -> Any:
        _CHILD_LEDGER.append("ssk_lookup")
        raise AssertionError(LOOKUP_REFUSED)

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(hydrate, "_ssk_lookup", refuse_lookup)
    yield
    leftover = list(_CHILD_LEDGER)
    _CHILD_LEDGER.clear()
    if leftover:
        pytest.fail(f"{CHILD_REFUSED}: {leftover}", pytrace=False)


@pytest.fixture(autouse=True)
def _no_real_file_and_a_clean_counter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[None]:
    """The two clients' file paths point at nothing, HubSpot's pacing is off,
    and the per-process call counter starts and ends at zero."""
    nowhere = tmp_path / "nowhere"
    monkeypatch.setattr(hubspot_api, "TOKEN_PATH", nowhere / "hubspot_token.txt")
    monkeypatch.setattr(tickets_api, "SECRET_PATH", nowhere / "tickets_api_secret.txt")
    monkeypatch.setattr(tickets_api, "SETTINGS_PATH", nowhere / "settings.yaml")
    monkeypatch.setattr(hubspot_api, "_acquire_rate_token", lambda: None)
    monkeypatch.setattr(hubspot_api, "RETRY_SLEEP_S", 0)
    hubspot_api.reset_calls()
    yield
    hubspot_api.reset_calls()


def test_a_child_process_and_the_lookup_are_refused_in_this_file() -> None:
    with pytest.raises(AssertionError, match="child process"):
        subprocess.run(["definitely-not-the-agent"])
    with pytest.raises(AssertionError, match="child process"):
        subprocess.Popen(["definitely-not-the-agent"])
    with pytest.raises(AssertionError, match="ShipSidekick lookup"):
        hydrate._ssk_lookup("FX-ORDER-1001", None, merchant="")
    assert len(_CHILD_LEDGER) == 3
    _CHILD_LEDGER.clear()  # provoked on purpose


# ----------------------------------------------------------------------- fakes


def _email(email_id: str, text: str, html: Any, direction: str, sender: str, created: str) -> dict:
    return {
        "id": email_id,
        "properties": {
            "hs_email_subject": "Where is my parcel",
            "hs_email_text": text,
            "hs_email_html": html,
            "hs_email_direction": direction,
            "hs_email_from_email": sender,
            "hs_email_to_email": "care@example.test",
            "hs_createdate": created,
        },
    }


T1_EMAILS = [
    _email(
        "501",
        "",
        "<blockquote><p>Topic of Ticket: Shipping question</p>"
        "<p>Order Number: FX-ORDER-0000</p>"
        "<p>Inquiry Description: It was due on Tuesday.</p></blockquote>",
        "INCOMING_EMAIL",
        "pat@example.test",
        "2026-10-01T09:00:00.000Z",
    ),
    _email(
        "502",
        "We are checking with the carrier.",
        None,
        "EMAIL",
        "care@example.test",
        "2026-10-02T14:30:00.250Z",
    ),
    _email("503", "Any news?", None, "INCOMING_EMAIL", "pat@example.test", "2026-10-08T10:00:00.123Z"),
]
T3_EMAILS = [
    _email(
        "601",
        "",
        "<blockquote><p>Topic of Ticket: Shipping question</p>"
        "<p>Order Number: FX-ORDER-3003</p>"
        "<p>Tracking Number: FX-TRACK-3003</p>"
        "<p>Inquiry Description: The label was created a week ago.</p></blockquote>",
        "INCOMING_EMAIL",
        "sam@example.test",
        "2026-09-30T11:05:00.000Z",
    ),
    _email(
        "602",
        "We have asked the carrier to trace it.",
        None,
        "EMAIL",
        "care@example.test",
        "2026-10-01T09:10:00.000Z",
    ),
]

_ASSOCIATION_RE = re.compile(r"^/crm/v4/objects/tickets/(\d+)/associations/emails\?limit=\d+$")
BATCH_PATH = "/crm/v3/objects/emails/batch/read"


class _Answer:
    """What an opener hands back: a context manager with a status and a body."""

    def __init__(self, body: Any, status: int = 200) -> None:
        self.status = status
        self._raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")

    def __enter__(self) -> "_Answer":
        return self

    def __exit__(self, *_exc: Any) -> bool:
        return False

    def read(self, *_args: Any) -> bytes:
        return self._raw


class HubSpotOpener:
    """Stands in for `hubspot_api._default_opener`, below the real request
    function, so the raw request total is counted as it is in production.

    It knows the two requests of a thread fetch and nothing else. Any other
    request is kept in `unexpected` (the code under test may swallow the
    AssertionError, so tests assert that list is empty)."""

    def __init__(self, threads: dict[str, list[dict]]) -> None:
        self.threads = threads
        self.requests: list[tuple[str, str]] = []
        self.unexpected: list[tuple[str, str]] = []
        self.refused: set[str] = set()  # HubSpot ids whose association request answers 404

    def __call__(self, request: Any, timeout: Any = None) -> _Answer:
        method = request.get_method()
        path = request.full_url[len(hubspot_api.HUBSPOT_BASE) :]
        self.requests.append((method, path))
        association = _ASSOCIATION_RE.match(path)
        if method == "GET" and association:
            ticket_id = association.group(1)
            if ticket_id in self.refused:
                raise urllib.error.HTTPError(request.full_url, 404, "not found", None, io.BytesIO(b""))
            emails = self.threads.get(ticket_id, [])
            return _Answer({"results": [{"toObjectId": email["id"]} for email in emails]})
        if method == "POST" and path == BATCH_PATH:
            wanted = [item["id"] for item in json.loads(request.data.decode("utf-8"))["inputs"]]
            held = {email["id"]: email for emails in self.threads.values() for email in emails}
            return _Answer({"results": [held[i] for i in wanted if i in held]})
        self.unexpected.append((method, path))
        raise AssertionError(f"a HubSpot request os mode must never make: {method} {path}")


class OsOpener:
    """Stands in for `tickets_api._OPENER`, below the real signed client."""

    def __init__(self, answer: Callable[[Any], Any]) -> None:
        self._answer = answer
        self.requests: list[Any] = []

    def open(self, request: Any, timeout: Any = None) -> Any:
        self.requests.append(request)
        return self._answer(request)


def _http_error(status: int, body: bytes = b"") -> Callable[[Any], Any]:
    def answer(request: Any) -> Any:
        raise urllib.error.HTTPError(request.full_url, status, "refused", None, io.BytesIO(body))

    return answer


CLASSIFICATION = {
    "category": "wismo_tracking",
    "form_topic": None,
    "priority": "normal",
    "confidence": 0.9,
    "reason": "asks where the order is",
}
DRAFT_BODY = "Thanks for reaching out. Let me confirm that with the team."


def _result_for(bundle: dict, drop: tuple = ()) -> dict:
    """A complete result file: one completed entry per bundle ticket, by ref."""
    tickets = [
        {
            "ref": entry["ref"],
            "outcome": "completed",
            "classification": dict(CLASSIFICATION),
            "draft_body": DRAFT_BODY,
            "action_items": [],
        }
        for entry in bundle["tickets"]
        if entry["ref"] not in drop
    ]
    return {
        "run_id": bundle["run_id"],
        "status": "success",
        "tickets_processed": len(tickets),
        "drafts_created": len(tickets),
        "error_summary": None,
        "tickets": tickets,
    }


class OsAgent:
    """The agent, without an agent: `launcher(command, env) -> exit code`.

    It reads the bundle the runner prepared, at launch time (the scratch
    directory is gone once the run is over), and writes a result for it."""

    def __init__(
        self,
        exit_code: int = 0,
        write: bool = True,
        drop: tuple = (),
        edit: Optional[Callable[[dict], None]] = None,
        raw: Optional[str] = None,
        error: Optional[BaseException] = None,
    ) -> None:
        self.exit_code = exit_code
        self.write = write
        self.drop = drop
        self.edit = edit
        self.raw = raw
        self.error = error
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.bundles: list[dict] = []
        self.bundle_paths: list[Path] = []
        self.run_contexts: list[Any] = []
        self.results: list[dict] = []

    def __call__(self, command: Any, env: Any) -> int:
        self.calls.append((list(command), dict(env)))
        if self.error is not None:
            raise self.error
        bundle_path = Path(env[os_pass.BUNDLE_PATH_ENV])
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        self.bundle_paths.append(bundle_path)
        self.bundles.append(bundle)
        context_path = Path(env[run_types.RUN_CONTEXT_ENV])
        self.run_contexts.append(
            json.loads(context_path.read_text(encoding="utf-8")) if context_path.is_file() else None
        )
        result_path = Path(env[run_types.RESULT_PATH_ENV])
        if self.raw is not None:
            result_path.write_text(self.raw, encoding="utf-8")
        elif self.write:
            result = _result_for(bundle, self.drop)
            if self.edit is not None:
                self.edit(result)
            self.results.append(result)
            result_path.write_text(json.dumps(result), encoding="utf-8")
        return self.exit_code


class OsWorld:
    """One orchestrator in `os` mode with every collaborator replaced."""

    def __init__(self, base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        base.mkdir(parents=True, exist_ok=True)
        self.base = base
        self.monkeypatch = monkeypatch
        self.root = base / "root"
        self.root.mkdir()
        self.client = FakeClient()
        self.os_api: Any = fakes.FakeOsApi(unprocessed=_list())
        self.read_os_origin: Any = self._read_os_origin
        self.origin_reads = 0
        self.hubspot = HubSpotOpener({T1_HS_ID: list(T1_EMAILS), T3_HS_ID: list(T3_EMAILS)})
        monkeypatch.setattr(hubspot_api, "_default_opener", self.hubspot)
        self.agent: Any = OsAgent()
        self.launch_attempts = 0
        self.settings: Any = copy.deepcopy(SETTINGS)
        self.token: Any = "test-token"
        self.token_reads = 0
        self.decide_calls = 0
        self.engine_child_calls = 0
        self.engine_contexts: list[list] = []
        self.engine_error: Optional[Exception] = None
        self.hubspot_requests_seen_by_engine: list[int] = []
        self.order: list[str] = []
        self.run_ids: list[str] = []
        self.state_path = fakes.temp_state_file(base, STATE)
        self.environ = {"PATH": "kept-path", "PACKN_KEEP_ME": "1"}
        self._tick = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
        self.outcomes: list[Any] = []
        self.draft_writes: list[str] = []
        for name in ("write_draft", "write_cron_draft", "replace_pending_draft"):
            monkeypatch.setattr(client, name, self._draft_writer(name))

        def spy(ctx: Any, deps: Any) -> Any:
            outcome = os_pass.run_os(ctx, deps)
            self.outcomes.append(outcome)
            return outcome

        rt.load_runners()
        monkeypatch.setitem(rt.MODE_RUNNERS, "os", spy)

    # --- collaborators
    def _draft_writer(self, name: str) -> Callable[..., Any]:
        def record(*_args: Any, **_kwargs: Any) -> Any:
            self.draft_writes.append(name)
            raise AssertionError(f"plan 93-16 writes no draft, yet {name} was called")

        return record

    def _now(self) -> datetime:
        self._tick += timedelta(seconds=5)
        return self._tick

    def _new_run_id(self) -> str:
        run_id = rt.new_run_id()
        self.run_ids.append(run_id)
        return run_id

    def _decide(self, mode: str, deps: Any) -> Any:
        self.decide_calls += 1
        return pg.decide(mode, deps)

    def _load_settings(self) -> dict:
        if isinstance(self.settings, Exception):
            raise self.settings
        return self.settings

    def _read_token(self) -> Any:
        self.token_reads += 1
        if isinstance(self.token, Exception):
            raise self.token
        return self.token

    def _read_os_origin(self) -> str:
        self.origin_reads += 1
        return OS_ORIGIN

    def _engine_child(self, env: Any) -> None:
        self.engine_child_calls += 1

    def _engine(self, contexts: list) -> None:
        self.order.append("engine")
        self.engine_contexts.append(contexts)
        self.hubspot_requests_seen_by_engine.append(len(self.hubspot.requests))
        if self.engine_error is not None:
            raise self.engine_error

    def _launch(self, command: Any, env: Any) -> Any:
        self.launch_attempts += 1
        self.order.append("agent")
        return self.agent(command, env)

    def deps(self) -> Any:
        return run_types.Deps(
            root=self.root,
            state_path=self.state_path,
            seen_path=self.base / "seen.json",
            now=self._now,
            new_run_id=self._new_run_id,
            resolve_mode=lambda: "os",
            decide=self._decide,
            load_settings=self._load_settings,
            read_hubspot_token=self._read_token,
            engine_shadow_step=self._engine_child,
            launch_agent=self._launch,
            report_routine_schedule=self.client.report_routine_schedule,
            read_routine_enabled=self.client.read_routine_enabled,
            read_pending_rerun_requests=self.client.read_pending_rerun_requests,
            rollback_cycle_entries=self.client.rollback_cycle_entries,
            write_run_record=self.client.write_run_record,
            mark_manual_run_completed=self.client.mark_manual_run_completed,
            hubspot_transport=None,
            environ=self.environ,
            os_api=self.os_api,
            read_os_origin=self.read_os_origin,
            engine_shadow_contexts_step=self._engine,
        )

    # --- driving and reading
    def run(self) -> Any:
        return rt.run(self.deps())

    @property
    def record(self) -> dict:
        assert len(self.client.records) == 1, self.client.records
        return self.client.records[0]

    @property
    def outcome(self) -> Any:
        assert self.outcomes, "the os runner was never called"
        return self.outcomes[-1]

    @property
    def bundle(self) -> dict:
        assert self.agent.bundles, "the agent was never launched"
        return self.agent.bundles[-1]

    @property
    def refs(self) -> list[str]:
        return [entry["ref"] for entry in self.bundle["tickets"]]

    def entry(self, ref: str) -> dict:
        (entry,) = [e for e in self.bundle["tickets"] if e["ref"] == ref]
        return entry

    @property
    def scratch_root(self) -> Path:
        return self.root / "outputs" / "runs" / "os"

    def scratch_left(self) -> list[str]:
        return sorted(p.name for p in self.scratch_root.iterdir()) if self.scratch_root.is_dir() else []

    def asked_nothing_of_anyone(self) -> None:
        """The fail-closed assertions, in one place: no HubSpot request, no
        agent, no draft, no engine shadow, no rerun read, no token read."""
        assert self.hubspot.requests == []
        assert self.launch_attempts == 0
        assert self.draft_writes == []
        assert self.engine_contexts == [] and self.engine_child_calls == 0
        assert self.token_reads == 0
        assert self.decide_calls == 0
        assert self.scratch_left() == []


@pytest.fixture
def make_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., OsWorld]:
    def make(name: str = "w") -> OsWorld:
        return OsWorld(tmp_path / name, monkeypatch)

    return make


@pytest.fixture
def world(make_world: Callable[..., OsWorld]) -> OsWorld:
    return make_world()


def _rerun(request_id: str, ticket_ref: str) -> dict:
    return {"id": request_id, "ticket_id": ticket_ref, "requested_by": "op"}


def _single_answer(payload: Optional[dict] = None, unprocessed: bool = False) -> dict:
    return {"contract_version": "1", "ticket": payload or _single()["ticket"], "unprocessed": unprocessed}


# -------------------------------------------------------------- the registry


def test_the_os_runner_is_registered_by_a_lazy_spec() -> None:
    assert rt.RUNNER_SPECS["os"] == ("ticket_pipeline.os_pass", "run_os")
    assert rt.load_runners()["os"] is os_pass.run_os


def test_the_orchestrator_still_imports_no_runner_module_at_load() -> None:
    tree = ast.parse(Path(rt.__file__).read_text(encoding="utf-8"))
    top_level = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            top_level.append(node.module or "")
    assert not [name for name in top_level if "os_pass" in name or "ticket_pipeline" in name]


def test_the_runner_takes_its_types_from_the_leaf_and_never_from_the_script() -> None:
    assert os_pass.RunOutcome is run_types.RunOutcome
    assert os_pass.RunContext is run_types.RunContext
    assert os_pass.Deps is run_types.Deps
    assert issubclass(os_pass.OsRunOutcome, run_types.RunOutcome)
    tree = ast.parse(Path(os_pass.__file__).read_text(encoding="utf-8"))
    script = "run_" + "tickets"
    for node in tree.body:  # the script is never imported at module load
        if isinstance(node, ast.Import):
            assert script not in [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "") != script
    for node in ast.walk(tree):  # and no name is ever taken FROM it
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "") != script


def test_the_agent_model_is_the_one_pin() -> None:
    assert os_pass.AGENT_MODEL == rt.AGENT_MODEL == llm.DRAFT_MODEL


# ------------------------------------------------- the list becomes a bundle


def test_three_fixture_tickets_become_a_bundle_and_one_success_record(world: OsWorld) -> None:
    assert world.run() == 0
    record = world.record
    assert record["status"] == "success"
    assert record["tickets_processed"] == 3
    assert record["source_mode"] == "os"
    assert record["failure_reason_code"] is None
    assert record["error_summary"] is None
    assert record["run_id"] == world.run_ids[0]
    # the whole object, as a literal: two thread fetches (two requests each) and nothing else
    assert record["hubspot_calls"] == {
        "kinds": {
            "search": 0,
            "ticket_read": 0,
            "contact_read": 0,
            "company_read": 0,
            "related_lookup": 0,
            "rerun_read": 0,
            "email_thread": 2,
            "number_backfill": 0,
        },
        "raw_requests": 4,
    }
    assert [entry["os_ticket_id"] for entry in world.bundle["tickets"]] == [T1_OS_ID, T2_OS_ID, T3_OS_ID]
    assert world.launch_attempts == 1
    assert world.os_api.calls_to("fetch_unprocessed") == [(25,)]
    assert world.os_api.calls_to("fetch_ticket") == []
    assert world.decide_calls == 0  # the HubSpot pre-gate decision is not this mode's


def test_the_transport_log_holds_only_the_two_requests_of_each_thread_fetch(world: OsWorld) -> None:
    world.run()
    assert world.hubspot.unexpected == []
    assert world.hubspot.requests == [
        ("GET", f"/crm/v4/objects/tickets/{T1_HS_ID}/associations/emails?limit=10"),
        ("POST", BATCH_PATH),
        ("GET", f"/crm/v4/objects/tickets/{T3_HS_ID}/associations/emails?limit=10"),
        ("POST", BATCH_PATH),
    ]
    for _method, path in world.hubspot.requests:
        assert "/search" not in path
        assert "/contacts" not in path and "/companies" not in path
        assert not path.startswith("/crm/v3/objects/tickets")


def test_a_ticket_with_no_hubspot_id_costs_no_hubspot_request_and_no_token_read(
    world: OsWorld,
) -> None:
    world.os_api.unprocessed["tickets"] = [_payload(1)]
    world.run()
    assert world.record["status"] == "success" and world.record["tickets_processed"] == 1
    assert world.hubspot.requests == []
    assert world.token_reads == 0
    assert world.record["hubspot_calls"] == NO_CALLS
    (entry,) = world.bundle["tickets"]
    assert entry["thread_source"] == "os"
    assert entry["ticket_context"]["ticket_id"] == T2_OS_ID
    assert entry["ticket_context"]["ticket_link"] == f"{OS_ORIGIN}/tickets/{T2_OS_ID}"


def test_each_entrys_ref_is_its_draft_key_and_the_refs_are_unique(world: OsWorld) -> None:
    world.run()
    assert world.refs == [T1_HS_ID, T2_OS_ID, T3_HS_ID]
    assert len(set(world.refs)) == 3
    for index, entry in enumerate(world.bundle["tickets"]):
        assert entry["ref"] == os_bundle.draft_key(_payload(index))
        assert entry["ref"] == entry["ticket_context"]["ticket_id"]


def test_the_bundle_has_the_documented_shape(world: OsWorld) -> None:
    world.run()
    bundle = world.bundle
    assert list(bundle) == ["run_id", "mode", "generated_at", "tickets"]
    assert bundle["run_id"] == world.run_ids[0] and bundle["mode"] == "os"
    for entry in bundle["tickets"]:
        assert tuple(entry) == os_pass.ENTRY_KEYS
    first, second, third = bundle["tickets"]
    assert first == {
        "ref": T1_HS_ID,
        "os_ticket_id": T1_OS_ID,
        "hubspot_ticket_id": T1_HS_ID,
        "cycle_key": T1_SIGNAL_ID,
        "is_rerun": False,
        "rerun_request_ids": [],
        "signal": {"kind": "client_reply", "id": T1_SIGNAL_ID, "at": "2026-10-08T10:00:00.123Z"},
        "thread_source": "hubspot",
        "backfill_source": {
            "payload": {"order_number": "FX-ORDER-1001", "tracking_number": "FX-TRACK-1001"},
            "form_block": {"order_number": "FX-ORDER-0000", "tracking_number": None},
        },
        "ticket_context": os_bundle.build_context(_payload(0), T1_EMAILS, SETTINGS, OS_ORIGIN),
    }
    assert second["hubspot_ticket_id"] is None and second["cycle_key"] == T2_SIGNAL_ID
    assert second["signal"] == {"kind": "opening", "id": T2_SIGNAL_ID, "at": "2026-10-08T15:45:10.000Z"}
    assert third["thread_source"] == "hubspot" and third["cycle_key"] == T3_SIGNAL_ID
    assert third["backfill_source"] == {
        "payload": {"order_number": None, "tracking_number": None},
        "form_block": {"order_number": "FX-ORDER-3003", "tracking_number": "FX-TRACK-3003"},
    }


def test_the_run_context_names_each_bundle_ticket_with_its_cycle(world: OsWorld) -> None:
    world.run()
    (context,) = world.agent.run_contexts
    assert context == {
        T1_HS_ID: {
            "os_ticket_id": T1_OS_ID,
            "cycle_key": T1_SIGNAL_ID,
            "signal_at": "2026-10-08T10:00:00.123Z",
            "signal_id": T1_SIGNAL_ID,
        },
        T2_OS_ID: {
            "os_ticket_id": T2_OS_ID,
            "cycle_key": T2_SIGNAL_ID,
            "signal_at": "2026-10-08T15:45:10.000Z",
            "signal_id": T2_SIGNAL_ID,
        },
        T3_HS_ID: {
            "os_ticket_id": T3_OS_ID,
            "cycle_key": T3_SIGNAL_ID,
            "signal_at": "2026-10-09T07:20:30.456Z",
            "signal_id": T3_SIGNAL_ID,
        },
    }


@pytest.mark.parametrize(
    "cap, sent",
    [(25, 25), (250, 100), (0, 1), (-3, 1), (100, 100), (1, 1)],
)
def test_the_list_limit_is_the_cap_clamped_into_1_to_100(world: OsWorld, cap: int, sent: int) -> None:
    world.settings["per_run_cap"] = cap
    world.run()
    assert world.os_api.calls_to("fetch_unprocessed") == [(sent,)]


def test_the_clamp_is_the_clients_own_maximum() -> None:
    assert os_pass.list_limit({"per_run_cap": 10_000}) == tickets_api.MAX_LIST_LIMIT == 100
    assert os_pass.list_limit({}) == os_pass.DEFAULT_PER_RUN_CAP == 25


def test_a_list_longer_than_the_limit_is_cut_to_it(world: OsWorld) -> None:
    world.settings["per_run_cap"] = 2  # the fake does not cut; the runner must not trust the count
    world.run()
    assert world.refs == [T1_HS_ID, T2_OS_ID]


def test_the_call_counter_is_reset_at_the_start_of_the_run(world: OsWorld) -> None:
    hubspot_api.record_call("search")
    hubspot_api.record_call("contact_read")
    world.run()
    assert world.record["hubspot_calls"]["kinds"]["search"] == 0
    assert world.record["hubspot_calls"]["kinds"]["contact_read"] == 0


# -------------------------------------------------------- fail closed (R14)


@pytest.mark.parametrize(
    "code, http_status",
    [
        pytest.param("signature_rejected", 401, id="401"),
        pytest.param("list_timeout", None, id="timeout"),
        pytest.param("list_error", 500, id="5xx"),
        pytest.param("secret_missing", None, id="no-local-secret"),
        pytest.param("secret_missing", 503, id="503-unset-secret"),
        pytest.param("list_error", 503, id="503-gateway"),
    ],
)
def test_a_list_that_cannot_be_had_is_one_failure_record_and_nothing_else(
    world: OsWorld, code: str, http_status: Optional[int]
) -> None:
    world.client.reruns = [_rerun(RERUN_A, T1_HS_ID)]  # even a pending rerun is not read
    world.os_api.fail_next_list(code, http_status)
    assert world.run() == 0
    assert world.client.calls.count("write_run_record") == 1
    record = world.record
    assert record["status"] == "failure"
    assert record["failure_reason_code"] == code
    assert record["tickets_processed"] == 0 and record["drafts_created"] == 0
    assert record["source_mode"] == "os"
    assert record["hubspot_calls"] == NO_CALLS
    assert world.os_api.calls_to("fetch_unprocessed") == [(25,)]
    assert world.os_api.calls_to("fetch_ticket") == []
    assert "read_pending_rerun_requests" not in world.client.calls
    world.asked_nothing_of_anyone()


def test_an_unexpected_exception_from_the_list_fetch_fails_closed_as_list_error(
    world: OsWorld,
) -> None:
    def explode(limit: int) -> dict:
        raise RuntimeError(f"connection reset {EXC_MARKER}")

    world.os_api.fetch_unprocessed = explode
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["failure_reason_code"] == "list_error"
    assert EXC_MARKER not in json.dumps(world.record)
    assert "RuntimeError" in world.record["error_summary"]
    world.asked_nothing_of_anyone()


@pytest.mark.parametrize(
    "answer",
    [
        pytest.param({"contract_version": "1", "tickets": "none"}, id="tickets-not-a-list"),
        pytest.param({"contract_version": "1"}, id="no-tickets-key"),
        pytest.param({"contract_version": "1", "tickets": ["x"]}, id="an-item-not-an-object"),
        pytest.param({"contract_version": "1", "tickets": [{"subject": "no id"}]}, id="an-item-with-no-id"),
        pytest.param({"contract_version": "2", "tickets": []}, id="another-contract-version"),
        pytest.param(["not", "an", "object"], id="not-an-object"),
    ],
)
def test_a_list_the_contract_does_not_describe_fails_closed(world: OsWorld, answer: Any) -> None:
    world.os_api.unprocessed = answer
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["failure_reason_code"] == "list_error"
    assert world.record["tickets_processed"] == 0
    world.asked_nothing_of_anyone()


def _use_the_real_client(world: OsWorld, answer: Callable[[Any], Any], secret: Optional[str]) -> OsOpener:
    """No injected OS API: the runner resolves the real signed client, whose
    secret file, settings file and opener are replaced under the temp dir."""
    secret_path = world.base / "fake-secrets" / "tickets_api_secret.txt"
    settings_path = world.base / "fake-config" / "settings.yaml"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        f'os_ingest_url: "{OS_ORIGIN}/api/ingest/action-items"\n', encoding="utf-8"
    )
    if secret is not None:
        secret_path.parent.mkdir(parents=True)
        secret_path.write_text(secret, encoding="utf-8")
    opener = OsOpener(answer)
    world.monkeypatch.setattr(tickets_api, "SECRET_PATH", secret_path)
    world.monkeypatch.setattr(tickets_api, "SETTINGS_PATH", settings_path)
    world.monkeypatch.setattr(tickets_api, "_OPENER", opener)
    world.os_api = None
    world.read_os_origin = None
    return opener


def test_with_no_injected_api_the_runner_uses_the_real_signed_client(world: OsWorld) -> None:
    opener = _use_the_real_client(world, lambda request: _Answer(_list()), FIXTURE_SECRET + "\n")
    assert world.run() == 0
    assert world.record["status"] == "success" and world.record["tickets_processed"] == 3
    (request,) = opener.requests
    assert request.get_method() == "GET"
    assert request.full_url == f"{OS_ORIGIN}/api/tickets/unprocessed?contract_version=1&limit=25"
    assert request.get_header(tickets_api.SIGNATURE_HEADER.capitalize())
    assert world.entry(T2_OS_ID)["ticket_context"]["ticket_link"] == f"{OS_ORIGIN}/tickets/{T2_OS_ID}"


@pytest.mark.parametrize(
    "secret",
    [pytest.param(None, id="no-file"), pytest.param("too-short", id="under-32-characters")],
)
def test_a_secret_missing_locally_is_secret_missing_before_any_request(
    world: OsWorld, secret: Optional[str]
) -> None:
    opener = _use_the_real_client(world, lambda request: _Answer(_list()), secret)
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["failure_reason_code"] == "secret_missing"
    assert opener.requests == []  # refused before anything was sent
    world.asked_nothing_of_anyone()


def test_the_os_answering_503_with_its_unset_secret_error_is_secret_missing(world: OsWorld) -> None:
    body = json.dumps({"error": tickets_api.SECRET_NOT_CONFIGURED_ERROR}).encode("utf-8")
    opener = _use_the_real_client(world, _http_error(503, body), FIXTURE_SECRET)
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["failure_reason_code"] == "secret_missing"
    assert len(opener.requests) == 1
    world.asked_nothing_of_anyone()


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"<html><body>502 Bad Gateway</body></html>", id="html"),
        pytest.param(b"", id="empty"),
        pytest.param(b'{"error": "redeploying"}', id="another-error"),
        pytest.param(b'["tickets_secret_not_configured"]', id="json-not-an-object"),
    ],
)
def test_a_gateway_503_is_list_error_and_not_secret_missing(world: OsWorld, body: bytes) -> None:
    opener = _use_the_real_client(world, _http_error(503, body), FIXTURE_SECRET)
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["failure_reason_code"] == "list_error"
    assert world.record["failure_reason_code"] != "secret_missing"
    assert len(opener.requests) == 1
    world.asked_nothing_of_anyone()


def test_a_401_from_the_real_client_is_signature_rejected(world: OsWorld) -> None:
    _use_the_real_client(world, _http_error(401), FIXTURE_SECRET)
    assert world.run() == 0
    assert world.record["failure_reason_code"] == "signature_rejected"
    world.asked_nothing_of_anyone()


def test_a_timeout_from_the_real_client_is_list_timeout(world: OsWorld) -> None:
    def time_out(request: Any) -> Any:
        raise TimeoutError("timed out")

    _use_the_real_client(world, time_out, FIXTURE_SECRET)
    assert world.run() == 0
    assert world.record["failure_reason_code"] == "list_timeout"
    world.asked_nothing_of_anyone()


def test_a_refused_run_then_an_accepted_run_needs_no_repair(world: OsWorld) -> None:
    # the rotation gap: the first run is refused, the second is accepted, and
    # nothing but the OS's answer changed in between
    world.os_api.fail_next_list("signature_rejected", 401)
    assert world.run() == 0
    assert world.run() == 0
    refused, accepted = world.client.records
    assert (refused["status"], refused["failure_reason_code"]) == ("failure", "signature_rejected")
    assert refused["tickets_processed"] == 0
    assert (accepted["status"], accepted["failure_reason_code"]) == ("success", None)
    assert accepted["tickets_processed"] == 3
    assert refused["run_id"] != accepted["run_id"]
    assert world.refs == [T1_HS_ID, T2_OS_ID, T3_HS_ID]
    assert world.scratch_left() == []


@pytest.mark.parametrize(
    "settings",
    [
        pytest.param(RuntimeError(f"yaml broke {EXC_MARKER}"), id="unreadable"),
        pytest.param(["not", "a", "mapping"], id="not-a-mapping"),
        pytest.param({"per_run_cap": "many"}, id="cap-not-a-number"),
        pytest.param({"per_run_cap": True}, id="cap-a-boolean"),
    ],
)
def test_settings_the_runner_cannot_use_fail_closed_before_any_request(
    world: OsWorld, settings: Any
) -> None:
    world.settings = settings
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["failure_reason_code"] is None
    assert world.record["error_summary"].startswith("settings_unreadable")
    assert EXC_MARKER not in world.record["error_summary"]
    assert world.os_api.calls == []
    world.asked_nothing_of_anyone()


# ------------------------------------------------- nothing to do, and paused


def test_an_empty_list_and_no_rerun_is_a_zero_ticket_success_and_no_launch(world: OsWorld) -> None:
    world.os_api.unprocessed["tickets"] = []
    assert world.run() == 0
    assert world.record["status"] == "success"
    assert world.record["tickets_processed"] == 0
    assert world.record["failure_reason_code"] is None
    assert world.record["hubspot_calls"] == NO_CALLS
    assert world.launch_attempts == 0
    assert world.hubspot.requests == []
    assert world.scratch_left() == []


def test_a_paused_routine_is_one_skipped_record_and_asks_nothing(world: OsWorld) -> None:
    world.client.enabled = False
    assert world.run() == 0
    assert world.record["status"] == "skipped"
    assert world.record["error_summary"] == "routine_paused"
    assert world.record["source_mode"] == "os"
    assert world.os_api.calls == []
    world.asked_nothing_of_anyone()


# ------------------------------------------------------------------- reruns


def test_a_pending_rerun_is_fetched_from_the_os_by_the_id_the_request_names(world: OsWorld) -> None:
    world.os_api.unprocessed["tickets"] = []
    world.os_api.tickets[T1_HS_ID] = _single_answer()
    world.client.reruns = [_rerun(RERUN_A, T1_HS_ID)]
    assert world.run() == 0
    assert world.record["status"] == "success" and world.record["tickets_processed"] == 1
    assert world.os_api.calls_to("fetch_ticket") == [(T1_HS_ID,)]
    (entry,) = world.bundle["tickets"]
    assert entry["ref"] == T1_HS_ID and entry["os_ticket_id"] == T1_OS_ID
    assert entry["is_rerun"] is True
    assert entry["rerun_request_ids"] == [RERUN_A]
    assert entry["cycle_key"] == f"rerun:{RERUN_A}"
    # no HubSpot ticket read: the only requests are the one thread fetch
    assert world.hubspot.unexpected == []
    assert world.hubspot.requests == [
        ("GET", f"/crm/v4/objects/tickets/{T1_HS_ID}/associations/emails?limit=10"),
        ("POST", BATCH_PATH),
    ]
    kinds = world.record["hubspot_calls"]["kinds"]
    assert kinds["rerun_read"] == 0 and kinds["ticket_read"] == 0 and kinds["search"] == 0
    assert kinds["email_thread"] == 1


def test_reruns_are_added_on_top_of_the_cap(world: OsWorld) -> None:
    world.settings["per_run_cap"] = 1
    world.os_api.unprocessed["tickets"] = [_payload(1)]
    world.os_api.tickets[T1_HS_ID] = _single_answer()
    world.client.reruns = [_rerun(RERUN_A, T1_HS_ID)]
    world.run()
    assert world.os_api.calls_to("fetch_unprocessed") == [(1,)]
    assert world.refs == [T2_OS_ID, T1_HS_ID]  # the list's entries first, then the rerun
    assert world.record["tickets_processed"] == 2


@pytest.mark.parametrize(
    "named_by",
    [pytest.param(T1_HS_ID, id="by-hubspot-id"), pytest.param(T1_OS_ID, id="by-os-id")],
)
def test_a_rerun_whose_ticket_is_in_the_list_is_one_entry(world: OsWorld, named_by: str) -> None:
    world.os_api.tickets[named_by] = _single_answer()
    world.client.reruns = [_rerun(RERUN_A, named_by)]
    world.run()
    assert len(world.bundle["tickets"]) == 3  # the number of distinct tickets
    assert world.refs == [T1_HS_ID, T2_OS_ID, T3_HS_ID]
    entry = world.entry(T1_HS_ID)
    assert entry["is_rerun"] is True
    assert entry["rerun_request_ids"] == [RERUN_A]
    assert entry["cycle_key"] == f"rerun:{RERUN_A}"
    # the list's payload and signal, not a second fetch's
    assert entry["signal"] == {"kind": "client_reply", "id": T1_SIGNAL_ID, "at": "2026-10-08T10:00:00.123Z"}
    assert entry["os_ticket_id"] == T1_OS_ID
    assert world.os_api.calls_to("fetch_ticket") == []
    assert world.record["tickets_processed"] == 3
    assert world.record["hubspot_calls"]["kinds"]["email_thread"] == 2  # still one fetch per ticket
    for other in (T2_OS_ID, T3_HS_ID):
        assert world.entry(other)["is_rerun"] is False
        assert world.entry(other)["rerun_request_ids"] == []


def test_two_reruns_for_one_ticket_are_one_entry_with_both_request_ids(world: OsWorld) -> None:
    world.client.reruns = [_rerun(RERUN_A, T1_HS_ID), _rerun(RERUN_B, T1_HS_ID)]
    world.run()
    assert len(world.bundle["tickets"]) == 3
    entry = world.entry(T1_HS_ID)
    assert entry["rerun_request_ids"] == [RERUN_A, RERUN_B]
    assert entry["cycle_key"] == f"rerun:{RERUN_A}"  # built from the first


def test_two_reruns_naming_one_unlisted_ticket_by_its_two_ids_are_one_entry(world: OsWorld) -> None:
    world.os_api.unprocessed["tickets"] = [_payload(1)]
    world.os_api.tickets[T1_HS_ID] = _single_answer()
    world.os_api.tickets[T1_OS_ID] = _single_answer()
    world.client.reruns = [_rerun(RERUN_A, T1_HS_ID), _rerun(RERUN_B, T1_OS_ID)]
    world.run()
    assert world.refs == [T2_OS_ID, T1_HS_ID]
    assert world.entry(T1_HS_ID)["rerun_request_ids"] == [RERUN_A, RERUN_B]
    assert world.record["hubspot_calls"]["kinds"]["email_thread"] == 1


def test_a_rerun_request_id_that_is_a_uuid_object_is_written_as_text(world: OsWorld) -> None:
    import uuid

    world.os_api.unprocessed["tickets"] = []
    world.os_api.tickets[T1_HS_ID] = _single_answer()
    world.client.reruns = [{"id": uuid.UUID(RERUN_A), "ticket_id": T1_HS_ID}]
    world.run()
    (entry,) = world.bundle["tickets"]
    assert entry["rerun_request_ids"] == [RERUN_A]
    assert entry["cycle_key"] == f"rerun:{RERUN_A}"


def test_a_rerun_the_os_does_not_hold_is_skipped_with_a_code_and_the_run_goes_on(
    world: OsWorld,
) -> None:
    world.client.reruns = [_rerun(RERUN_A, "9300000000777")]  # the fake answers None: a 404
    assert world.run() == 0
    assert world.os_api.calls_to("fetch_ticket") == [("9300000000777",)]
    assert world.refs == [T1_HS_ID, T2_OS_ID, T3_HS_ID]
    assert world.record["status"] == "success" and world.record["tickets_processed"] == 3
    assert "rerun_not_found=1" in world.record["error_summary"]
    assert world.hubspot.unexpected == []  # the ticket is never looked for in HubSpot


@pytest.mark.parametrize(
    "ref, failure, note",
    [
        pytest.param("9300000000777", "list_timeout", "rerun_fetch_failed=1", id="the-fetch-fails"),
        pytest.param("not a ref!", None, "rerun_ref_invalid=1", id="a-ref-that-cannot-be-a-path"),
        pytest.param(None, None, "rerun_rows_invalid=1", id="a-row-with-no-ticket"),
    ],
)
def test_a_rerun_that_cannot_be_fetched_is_skipped_and_never_read_from_hubspot(
    world: OsWorld, ref: Any, failure: Optional[str], note: str
) -> None:
    if failure is not None:
        world.os_api.fail_next("fetch_ticket", failure)
    world.client.reruns = [_rerun(RERUN_A, ref)]
    assert world.run() == 0
    assert world.refs == [T1_HS_ID, T2_OS_ID, T3_HS_ID]
    assert world.record["status"] == "success"
    assert note in world.record["error_summary"]
    assert world.hubspot.unexpected == []
    assert len(world.hubspot.requests) == 4


def test_a_rerun_read_that_fails_is_a_code_and_the_list_is_still_processed(world: OsWorld) -> None:
    def explode(routine: str) -> list:
        raise RuntimeError(f"permission denied {EXC_MARKER}")

    world.client.read_pending_rerun_requests = explode
    assert world.run() == 0
    assert world.record["status"] == "success" and world.record["tickets_processed"] == 3
    assert "rerun_requests_unreadable" in world.record["error_summary"]
    assert EXC_MARKER not in world.record["error_summary"]


# ------------------------------------------- the state file is not consulted


def test_the_state_file_does_not_change_which_tickets_are_selected(
    make_world: Callable[..., OsWorld],
) -> None:
    valid = make_world("valid")
    valid.run()
    refs_with_a_valid_state = valid.refs

    deleted = make_world("deleted")
    deleted.state_path.unlink()
    deleted.run()

    garbage = make_world("garbage")
    garbage.state_path.write_text("{ this is not json", encoding="utf-8")
    garbage.run()

    # ticket 1 is fingerprinted as done in the valid state file: HubSpot mode
    # would dedupe it, `os` mode selects what the OS lists
    assert T1_HS_ID in STATE["ticket_fingerprints"]
    assert refs_with_a_valid_state == [T1_HS_ID, T2_OS_ID, T3_HS_ID]
    assert deleted.refs == refs_with_a_valid_state
    assert garbage.refs == refs_with_a_valid_state
    assert garbage.state_path.read_text(encoding="utf-8") == "{ this is not json"  # and not rewritten


def test_an_end_to_end_run_opens_no_real_token_secret_or_state_file(
    world: OsWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbidden = {
        REAL_TOKEN_PATH.resolve(),
        REAL_SECRET_PATH.resolve(),
        REAL_STATE_PATH.resolve(),
        world.state_path.resolve(),  # this mode has no use for any state file
    }
    attempts: list[str] = []
    real_open, real_read_text, real_read_bytes = builtins.open, Path.read_text, Path.read_bytes

    def check(target: Any) -> None:
        try:
            resolved = Path(target).resolve()
        except (TypeError, ValueError, OSError):
            return
        if resolved in forbidden:
            attempts.append(resolved.name)
            raise AssertionError("a real file was opened by an os-mode test")

    def guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        check(file)
        return real_open(file, *args, **kwargs)

    def guarded_read_text(self: Path, *args: Any, **kwargs: Any) -> Any:
        check(self)
        return real_read_text(self, *args, **kwargs)

    def guarded_read_bytes(self: Path, *args: Any, **kwargs: Any) -> Any:
        check(self)
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    # the guard bites (on the tracked state file; no secret file is ever touched)
    with pytest.raises(AssertionError, match="real file"):
        pg._load_state()
    assert attempts == ["state.json"]
    attempts.clear()

    assert world.run() == 0
    assert world.record["status"] == "success" and world.record["tickets_processed"] == 3
    assert world.token_reads == 1  # the token came through deps.read_hubspot_token, once
    assert attempts == []


def test_the_runner_never_names_the_state_file_or_a_hubspot_search() -> None:
    text = Path(os_pass.__file__).read_text(encoding="utf-8")
    for halves in (("state", ".json"), ("state", "_path"), ("search", "_tickets"), ("hubapi", ".com")):
        assert "".join(halves) not in text
    tree = ast.parse(text)
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    forbidden = {"_request", "urlopen", "batch_read", "get_associated_ids", "hydrate_ticket", "read_token"}
    assert not called & forbidden
    assert "fetch_thread" in called and "build_context" in called  # the one builder, not a second


# ---------------------------------------------------- the token and the thread


def test_a_missing_token_falls_back_to_the_os_thread_and_does_not_fail_the_run(world: OsWorld) -> None:
    world.token = None
    assert world.run() == 0
    assert world.record["status"] == "success" and world.record["tickets_processed"] == 3
    assert world.hubspot.requests == []
    assert world.record["hubspot_calls"] == NO_CALLS
    assert world.token_reads == 1
    assert world.entry(T1_HS_ID)["thread_source"] == "os_fallback"
    assert world.entry(T3_HS_ID)["thread_source"] == "os_fallback"
    assert world.entry(T2_OS_ID)["thread_source"] == "os"
    assert "thread_fetch_failed=2" in world.record["error_summary"]
    assert "hubspot_token_missing" in world.record["error_summary"]
    # still a complete context, from the OS thread
    assert [m["from"] for m in world.entry(T1_HS_ID)["ticket_context"]["thread"]] == [
        "customer",
        "agent",
        "customer",
    ]


def test_a_token_reader_that_raises_is_treated_as_a_missing_token(world: OsWorld) -> None:
    world.token = OSError(f"unreadable {EXC_MARKER}")
    assert world.run() == 0
    assert world.record["status"] == "success"
    assert world.hubspot.requests == []
    assert "hubspot_token_missing" in world.record["error_summary"]
    assert EXC_MARKER not in world.record["error_summary"]


def test_a_thread_fetch_hubspot_refuses_is_processed_from_the_os_thread_with_a_code(
    world: OsWorld,
) -> None:
    world.hubspot.refused.add(T1_HS_ID)
    assert world.run() == 0
    assert world.record["status"] == "success" and world.record["tickets_processed"] == 3
    assert world.entry(T1_HS_ID)["thread_source"] == "os_fallback"
    assert world.entry(T3_HS_ID)["thread_source"] == "hubspot"
    assert world.record["error_summary"] == "thread_fetch_failed=1"
    calls = world.record["hubspot_calls"]
    assert calls["kinds"]["email_thread"] == 2  # one logical call per linked ticket, refused or not
    assert calls["raw_requests"] == 3  # the refused one stopped after its first request


def test_ssk_state_reaches_the_context_through_the_lookup_and_no_child_process_starts(
    world: OsWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    lookups: list[tuple] = []

    def fake_lookup(order_number: Any, tracking_number: Any, merchant: str = "") -> dict:
        lookups.append((order_number, tracking_number))
        return {"order_status": "shipped"}

    monkeypatch.setattr(hydrate, "_ssk_lookup", fake_lookup)
    world.settings["shipsidekick"] = {"enabled": True}
    world.run()
    assert world.entry(T1_HS_ID)["ticket_context"]["ssk_state"] == {"order_status": "shipped"}
    assert "ssk_state" not in world.entry(T2_OS_ID)["ticket_context"]  # no order or tracking number
    assert ("FX-ORDER-1001", "FX-TRACK-1001") in lookups
    assert _CHILD_LEDGER == []


# ------------------------------------------------------------ the agent launch


def test_the_agent_is_launched_with_the_os_command_and_the_hubspot_tools_removed(
    world: OsWorld,
) -> None:
    world.run()
    ((command, env),) = world.agent.calls
    assert command == [
        "claude",
        "-p",
        "/packn-tickets-os",
        "--model",
        llm.DRAFT_MODEL,
        "--dangerously-skip-permissions",
        "--disallowedTools",
        "mcp__hubspot",
    ]
    assert command == list(os_pass.OS_AGENT_COMMAND) == os_pass.agent_command()
    assert os_pass.DISALLOWED_TOOLS == "mcp__hubspot"
    run_id = world.run_ids[0]
    scratch = world.scratch_root / run_id
    assert env["PACKN_SOURCE_MODE"] == "os"
    assert env["PACKN_RUN_ID"] == run_id
    assert env["PACKN_BUNDLE_PATH"] == str(scratch / "bundle.json")
    assert env["PACKN_RESULT_PATH"] == str(scratch / "result.json")
    assert env["PACKN_RUN_CONTEXT"] == str(scratch / "context.json")
    assert env["PATH"] == "kept-path" and env["PACKN_KEEP_ME"] == "1"
    assert os_pass.BUNDLE_PATH_ENV == "PACKN_BUNDLE_PATH"


def test_the_hubspot_mode_command_is_left_as_it_was() -> None:
    assert rt.agent_command() == [
        "claude",
        "-p",
        "/packn-tickets",
        "--model",
        llm.DRAFT_MODEL,
        "--dangerously-skip-permissions",
    ]


def test_exit_1_with_no_result_is_agent_launch_failed(world: OsWorld) -> None:
    world.agent = OsAgent(exit_code=1, write=False)
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["failure_reason_code"] == "agent_launch_failed"
    assert world.record["tickets_processed"] == 0
    assert "agent_exit_code=1" in world.record["error_summary"]
    assert [t["failure_reason_code"] for t in world.outcome.tickets] == ["agent_no_result"] * 3


def test_a_launcher_that_cannot_start_is_agent_launch_failed(world: OsWorld) -> None:
    world.agent = OsAgent(error=FileNotFoundError(f"no such command {EXC_MARKER}"))
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["failure_reason_code"] == "agent_launch_failed"
    assert EXC_MARKER not in world.record["error_summary"]
    assert world.scratch_left() == []


def test_exit_0_with_no_result_is_agent_exited_abnormally(world: OsWorld) -> None:
    world.agent = OsAgent(write=False)
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["failure_reason_code"] == "agent_exited_abnormally"
    assert world.record["tickets_processed"] == 0
    assert [t["outcome"] for t in world.outcome.tickets] == ["failed"] * 3


def test_a_result_that_is_not_complete_processes_nothing(world: OsWorld) -> None:
    world.agent = OsAgent(raw='{"status": "success", "tickets_processed": 3')
    assert world.run() == 0
    assert world.record["failure_reason_code"] == "agent_exited_abnormally"
    assert world.record["tickets_processed"] == 0
    assert world.outcome.result is None


# -------------------------------------------- the result against the bundle


def test_a_result_missing_one_ticket_counts_two_and_carries_the_third_as_agent_no_result(
    world: OsWorld,
) -> None:
    world.agent = OsAgent(drop=(T3_HS_ID,))
    assert world.run() == 0
    assert world.record["status"] == "partial"
    assert world.record["tickets_processed"] == 2
    assert world.record["failure_reason_code"] is None
    assert "agent_no_result=1" in world.record["error_summary"]
    by_ref = {ticket["ref"]: ticket for ticket in world.outcome.tickets}
    assert [t["ref"] for t in world.outcome.tickets] == [T1_HS_ID, T2_OS_ID, T3_HS_ID]
    assert by_ref[T1_HS_ID]["outcome"] == "completed" and by_ref[T2_OS_ID]["outcome"] == "completed"
    assert by_ref[T3_HS_ID]["outcome"] == "failed"
    assert by_ref[T3_HS_ID]["failure_reason_code"] == "agent_no_result"
    assert by_ref[T3_HS_ID]["result"] is None
    assert by_ref[T3_HS_ID]["os_ticket_id"] == T3_OS_ID


def test_the_count_is_codes_not_the_agents_own(world: OsWorld) -> None:
    def overclaim(result: dict) -> None:
        result["tickets_processed"] = 40
        result["drafts_created"] = 40

    world.agent = OsAgent(drop=(T1_HS_ID,), edit=overclaim)
    world.run()
    assert world.record["tickets_processed"] == 2
    assert world.record["drafts_created"] == 0  # no draft row is written by this plan
    assert world.record["status"] == "partial"


@pytest.mark.parametrize(
    "written, kept",
    [
        ("agent_ticket_error", "agent_ticket_error"),
        ("other", "other"),
        ("mcp_error", "other"),
        (None, "other"),
        (f"the model said {MARKER}", "other"),
    ],
)
def test_a_ticket_the_agent_marks_failed_keeps_a_code_of_its_closed_list(
    world: OsWorld, written: Any, kept: str
) -> None:
    def fail_the_second(result: dict) -> None:
        result["tickets"][1]["outcome"] = "failed"
        result["tickets"][1]["failure_reason_code"] = written
        result["status"] = "partial"

    world.agent = OsAgent(edit=fail_the_second)
    world.run()
    assert world.record["status"] == "partial" and world.record["tickets_processed"] == 2
    failed = world.outcome.tickets[1]
    assert (failed["ref"], failed["outcome"], failed["failure_reason_code"]) == (T2_OS_ID, "failed", kept)
    assert MARKER not in json.dumps(world.record)


def test_an_outcome_the_contract_does_not_name_is_a_failed_ticket(world: OsWorld) -> None:
    def invent(result: dict) -> None:
        result["tickets"][0]["outcome"] = "sort of done"

    world.agent = OsAgent(edit=invent)
    world.run()
    first = world.outcome.tickets[0]
    assert (first["outcome"], first["failure_reason_code"]) == ("failed", "other")
    assert world.record["tickets_processed"] == 2


def test_a_result_entry_for_a_ref_the_bundle_does_not_hold_is_counted_and_ignored(
    world: OsWorld,
) -> None:
    def add_a_stranger(result: dict) -> None:
        result["tickets"].append({"ref": "9300000000999", "outcome": "completed", "draft_body": "x"})
        result["tickets"].append(dict(result["tickets"][0]))  # and a second entry for a known ref

    world.agent = OsAgent(edit=add_a_stranger)
    world.run()
    assert world.record["tickets_processed"] == 3
    assert [t["ref"] for t in world.outcome.tickets] == [T1_HS_ID, T2_OS_ID, T3_HS_ID]
    assert "result_unknown_refs=1" in world.record["error_summary"]
    assert "result_duplicate_refs=1" in world.record["error_summary"]


def test_no_ticket_completed_is_a_failure_record(world: OsWorld) -> None:
    world.agent = OsAgent(drop=(T1_HS_ID, T2_OS_ID, T3_HS_ID))
    world.run()
    assert world.record["status"] == "failure"
    assert world.record["failure_reason_code"] is None
    assert world.record["tickets_processed"] == 0
    assert "agent_no_result=3" in world.record["error_summary"]


def test_the_outcome_carries_the_bundle_entries_the_result_and_the_per_ticket_list(
    world: OsWorld,
) -> None:
    world.run()
    outcome = world.outcome
    assert isinstance(outcome, os_pass.OsRunOutcome) and isinstance(outcome, run_types.RunOutcome)
    assert outcome.bundle_entries == world.bundle["tickets"]
    assert outcome.result == world.agent.results[0]
    assert [sorted(ticket) for ticket in outcome.tickets] == [
        sorted(
            [
                "ref",
                "os_ticket_id",
                "hubspot_ticket_id",
                "cycle_key",
                "is_rerun",
                "outcome",
                "failure_reason_code",
                "result",
            ]
        )
    ] * 3
    first = outcome.tickets[0]
    assert first["outcome"] == "completed" and first["failure_reason_code"] is None
    assert first["result"]["draft_body"] == DRAFT_BODY
    assert first["result"]["classification"] == CLASSIFICATION
    assert outcome.hubspot_calls == world.record["hubspot_calls"]


def test_reconcile_is_a_pure_function_of_the_entries_and_the_result() -> None:
    entries = [
        {"ref": "a", "os_ticket_id": "A", "hubspot_ticket_id": None, "cycle_key": "k1", "is_rerun": False},
        {"ref": "b", "os_ticket_id": "B", "hubspot_ticket_id": "7", "cycle_key": "k2", "is_rerun": True},
    ]
    tickets, counts = os_pass.reconcile(entries, {"tickets": [{"ref": "b", "outcome": "completed"}]})
    assert [(t["ref"], t["outcome"], t["failure_reason_code"]) for t in tickets] == [
        ("a", "failed", "agent_no_result"),
        ("b", "completed", None),
    ]
    assert counts == {"agent_no_result": 1}
    tickets, counts = os_pass.reconcile(entries, None)
    assert [t["failure_reason_code"] for t in tickets] == ["agent_no_result", "agent_no_result"]
    assert counts == {"agent_no_result": 2}


# ------------------------------------------- a ticket the bundle cannot hold


def test_a_listed_ticket_with_no_signal_is_a_failed_ticket_and_the_rest_go_on(world: OsWorld) -> None:
    world.os_api.unprocessed["tickets"][1]["signal"] = None  # no cycle key can be made
    assert world.run() == 0
    assert world.refs == [T1_HS_ID, T3_HS_ID]
    assert world.record["status"] == "partial" and world.record["tickets_processed"] == 2
    assert "bundle_build_failed=1" in world.record["error_summary"]
    failed = [t for t in world.outcome.tickets if t["outcome"] == "failed"]
    assert [(t["os_ticket_id"], t["failure_reason_code"]) for t in failed] == [
        (T2_OS_ID, "bundle_build_failed")
    ]


def test_when_no_ticket_can_be_bundled_the_agent_is_not_launched(world: OsWorld) -> None:
    payload = _payload(1)
    payload["signal"] = None
    world.os_api.unprocessed["tickets"] = [payload]
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["tickets_processed"] == 0
    assert world.launch_attempts == 0
    assert "bundle_build_failed=1" in world.record["error_summary"]


def test_an_os_origin_that_cannot_be_read_fails_closed(world: OsWorld) -> None:
    def no_origin() -> str:
        raise tickets_api.TicketsApiError("list_error")

    world.read_os_origin = no_origin
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["failure_reason_code"] == "list_error"
    assert world.launch_attempts == 0 and world.hubspot.requests == []


# ------------------------------------------------------- the scratch directory


def test_after_the_run_the_scratch_directory_and_the_bundle_do_not_exist(world: OsWorld) -> None:
    seen: dict[str, Any] = {}
    real = world.agent

    def launch(command: Any, env: Any) -> int:
        bundle_path = Path(env[os_pass.BUNDLE_PATH_ENV])
        seen["path"] = bundle_path
        seen["file_existed"] = bundle_path.is_file()
        seen["only_files"] = sorted(p.name for p in bundle_path.parent.iterdir())
        return real(command, env)

    world.agent = launch
    world.run()
    assert seen["file_existed"] is True  # it was really there while the agent ran
    assert seen["only_files"] == ["bundle.json", "context.json"]  # no partial file left beside it
    assert seen["path"].parent == world.scratch_root / world.run_ids[0]
    assert not seen["path"].exists()
    assert not seen["path"].parent.exists()
    assert world.scratch_left() == []


# ------------------------------------------------------------ the engine shadow


def test_the_engine_shadow_is_handed_the_bundles_contexts_before_the_agent(world: OsWorld) -> None:
    world.settings["pipeline"] = {"shadow_enabled": True}
    world.run()
    assert world.order == ["engine", "agent"]
    (contexts,) = world.engine_contexts
    assert contexts == [entry["ticket_context"] for entry in world.bundle["tickets"]]
    # it asked HubSpot nothing: the request log did not grow after the thread fetches
    assert world.hubspot_requests_seen_by_engine == [4]
    assert len(world.hubspot.requests) == 4
    assert world.engine_child_calls == 0  # the HubSpot-mode child step is not this mode's
    assert world.record["status"] == "success"


def test_the_engine_shadow_cannot_change_what_the_agent_reads(world: OsWorld) -> None:
    world.settings["pipeline"] = {"shadow_enabled": True}

    def scribble(contexts: list) -> None:
        for context in contexts:
            context["subject"] = "changed by the engine shadow"

    deps = world.deps()
    deps.engine_shadow_contexts_step = scribble
    assert rt.run(deps) == 0
    assert world.bundle["tickets"][0]["ticket_context"]["subject"] == "Where is my parcel"
    assert world.outcome.bundle_entries[0]["ticket_context"]["subject"] == "Where is my parcel"


def test_the_engine_shadow_is_not_run_when_the_settings_do_not_enable_it(world: OsWorld) -> None:
    world.run()
    assert world.engine_contexts == [] and world.engine_child_calls == 0
    assert world.order == ["agent"]


def test_an_engine_shadow_that_fails_does_not_stop_the_run(world: OsWorld) -> None:
    world.settings["pipeline"] = {"shadow_enabled": True}
    world.engine_error = RuntimeError(f"boom {EXC_MARKER}")
    assert world.run() == 0
    assert world.record["status"] == "success" and world.record["tickets_processed"] == 3
    assert EXC_MARKER not in json.dumps(world.record)


def test_the_real_engine_shadow_step_calls_the_pipeline_in_os_mode_with_the_contexts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    seen: list[dict] = []
    stand_in = types.ModuleType("ticket_pipeline.run")
    stand_in.run_shadow = lambda **kwargs: seen.append(kwargs) or 0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ticket_pipeline.run", stand_in)
    monkeypatch.setattr(sys.modules["ticket_pipeline"], "run", stand_in, raising=False)
    os_pass.run_engine_shadow_on_contexts([{"ticket_id": "1"}])
    assert seen == [{"mode": "os", "contexts": [{"ticket_id": "1"}]}]


# ----------------------------------------------------------- no text in output


def test_no_ticket_text_reaches_a_log_record_stdout_stderr_or_the_run_record(
    world: OsWorld, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    ticket = world.os_api.unprocessed["tickets"][0]
    ticket["subject"] = f"Where is {MARKER} my parcel"
    ticket["body"] = f"It was due {MARKER} on Tuesday"
    ticket["requester"]["name"] = f"Pat {MARKER}"
    for item in ticket["thread"]:
        item["body"] = f"{item['body']} {MARKER}"
    world.hubspot.threads[T1_HS_ID] = [
        _email("501", f"Any news {MARKER}?", f"<p>Any news {MARKER}?</p>", "INCOMING_EMAIL",
               "pat@example.test", "2026-10-01T09:00:00.000Z"),
    ]
    world.settings["pipeline"] = {"shadow_enabled": True}
    world.engine_error = RuntimeError(f"could not parse {MARKER}")
    world.client.reruns = [_rerun(RERUN_A, "9300000000777")]  # a skipped rerun logs a code too

    def put_text_in_the_result(result: dict) -> None:
        body = f"Hello, your parcel {MARKER} is on its way to you today."
        result["tickets"][0]["draft_body"] = body
        result["tickets"][0]["classification"]["reason"] = f"customer wrote {MARKER} about tracking"
        result["tickets"][1]["outcome"] = "failed"
        result["tickets"][1]["failure_reason_code"] = f"it said {MARKER}"
        result["error_summary"] = f"one ticket failed: {body}"

    world.agent = OsAgent(edit=put_text_in_the_result)
    assert world.run() == 0
    # the marker really was in what the agent read and wrote
    assert MARKER in json.dumps(world.bundle)
    assert MARKER in json.dumps(world.agent.results[0])
    assert world.record["status"] == "partial"
    assert MARKER not in json.dumps(world.client.records)
    fakes.assert_marker_absent(MARKER, caplog, capfd)
