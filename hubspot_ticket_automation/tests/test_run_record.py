"""The orchestrator: one mode read, one runner, one run record (Phase 93, plan 15).

`scripts/run_tickets.py` is the one entry point of every tickets-process run.
These tests drive `run(deps)` and `main(argv)` with every collaborator injected
through one `Deps` object: the database helper, the mode reader, the pre-gate
decision, the engine-shadow step, the agent launcher, the clock, the id
generator and every path.

No test here reaches a database, HubSpot, the Pack'N OS, a secret file, the
tracked config/state.json or the real agent:

    - the suite's network guard and no-database guard (tests/conftest.py) apply;
    - an autouse fixture in this file refuses every child process, and keeps a
      ledger so that a refusal swallowed by the code under test still fails
      the test. The agent is only ever a fake launcher.
"""
from __future__ import annotations

import ast
import builtins
import inspect
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # hubspot_ticket_automation/
sys.path.insert(0, str(PACKAGE_ROOT / "scripts"))  # scripts/ is imported flat, as run.py does

import pregate_tickets as pg  # noqa: E402
import run_tickets as rt  # noqa: E402
from packn_os_hubspot_client import client  # noqa: E402
from tests.helpers import fakes  # noqa: E402
from ticket_pipeline import hubspot_api, llm, run_types, source  # noqa: E402

SEARCH_PATH = "/crm/v3/objects/tickets/search"
TEXT_MARKER = "ZZ-TEXT-MARKER"
EXC_MARKER = "ZZ-EXC-MARKER"

RUN_ID_FORM = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
MANUAL_REQUEST_ID = "3f0e7a52-8a1c-4f9e-9d57-0c2f6f3f8b11"
RERUN_REQUEST_ID = "9b2d4c1e-55aa-4f0b-8c3d-1e2f3a4b5c6d"
OS_TICKET_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"

SETTINGS = {
    "active_stages": ["1", "2"],
    "per_run_cap": 25,
    "require_last_message_from_visitor": False,
}
STATE = {
    "last_run_at": "2026-09-01T00:00:00Z",
    "ticket_fingerprints": {
        "900": {
            "processed_at": "2026-09-02T00:00:00Z",
            "hs_lastmodifieddate": "2026-09-02T00:00:00Z",
            "num_notes": 1,
        },
        "901": {
            "processed_at": "2026-09-02T00:00:00Z",
            "hs_lastmodifieddate": "2026-09-02T00:00:00Z",
            "num_notes": 4,
        },
    },
}
NEW_HIT = {"id": "501", "properties": {"hs_lastmodifieddate": "2026-09-03T10:00:00Z"}}
DEDUPED_HIT_A = {"id": "900", "properties": {"hs_lastmodifieddate": "2026-09-01T12:00:00Z"}}
DEDUPED_HIT_B = {"id": "901", "properties": {"hs_lastmodifieddate": "2026-09-01T13:00:00Z"}}

CHILD_REFUSED = "a test in test_run_record.py tried to start a child process"
_CHILD_LEDGER: list[str] = []


# ----------------------------------------------------------------- test safety


@pytest.fixture(autouse=True)
def _no_child_process(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No test in this file starts a process: not the agent, not the engine-shadow
    pipeline, not `crontab`. The refusal is recorded as well as raised, because
    the code under test turns a launcher's exception into a failure record."""
    _CHILD_LEDGER.clear()

    def refuse(*args: Any, **_kwargs: Any) -> Any:
        _CHILD_LEDGER.append(repr(args[:1]))
        raise AssertionError(CHILD_REFUSED)

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    yield
    leftover = list(_CHILD_LEDGER)
    _CHILD_LEDGER.clear()
    if leftover:
        pytest.fail(f"{CHILD_REFUSED}: {leftover}", pytrace=False)


def test_a_child_process_is_refused_in_this_file() -> None:
    with pytest.raises(AssertionError, match="child process"):
        subprocess.run(["definitely-not-the-agent"])
    with pytest.raises(AssertionError, match="child process"):
        subprocess.Popen(["definitely-not-the-agent"])
    assert len(_CHILD_LEDGER) == 2
    _CHILD_LEDGER.clear()  # provoked on purpose


# ----------------------------------------------------------------------- fakes


def _result(**over: Any) -> dict:
    """A complete result file, as the agent writes it: 3 tickets, 2 drafts."""
    result = {
        "status": "success",
        "tickets_processed": 3,
        "drafts_created": 2,
        "error_summary": None,
        "tickets": [
            {
                "ref": "501",
                "outcome": "completed",
                "classification": {
                    "category": "wismo_tracking",
                    "form_topic": None,
                    "priority": "normal",
                    "confidence": 0.91,
                    "reason": "asks where the order is",
                },
                "draft_id": "d-1",
                "draft_body": "Thanks for reaching out about your order.",
                "action_items": [],
            },
            {
                "ref": "502",
                "outcome": "completed",
                "classification": {
                    "category": "billing",
                    "form_topic": None,
                    "priority": "normal",
                    "confidence": 0.8,
                    "reason": "an invoice question",
                },
                "draft_id": "d-2",
                "draft_body": "Let me confirm that with the team.",
                "action_items": [],
            },
            {
                "ref": "503",
                "outcome": "completed",
                "classification": {
                    "category": "other_unclassified",
                    "form_topic": None,
                    "priority": "normal",
                    "confidence": 0.4,
                    "reason": "unclear",
                },
                "action_items": [],
            },
        ],
    }
    result.update(over)
    return result


class FakeClient:
    """The six database-helper functions the orchestrator uses, with the real
    signatures (compared in test_the_fake_client_has_the_real_signatures)."""

    def __init__(self) -> None:
        self.enabled = True
        self.reruns: list[dict] = []
        self.rollback_tickets: dict[str, Optional[str]] = {}
        self.rollback_error: Optional[Exception] = None
        self.rollback_answer: Any = None  # a non-dict answer, when set
        self.record_error: Optional[BaseException] = None
        self.records: list[dict] = []
        self.manual_links: list[tuple] = []
        self.schedule_reports: list[tuple] = []
        self.rollback_calls: list[tuple] = []
        self.calls: list[str] = []

    def report_routine_schedule(self, routine_name, cron_marker):
        self.calls.append("report_routine_schedule")
        self.schedule_reports.append((routine_name, cron_marker))
        return None

    def read_routine_enabled(self, routine_name):
        self.calls.append("read_routine_enabled")
        return self.enabled

    def read_pending_rerun_requests(self, routine_name):
        self.calls.append("read_pending_rerun_requests")
        return list(self.reruns)

    def rollback_cycle_entries(self, run_id, routine_name="tickets-process"):
        self.calls.append("rollback_cycle_entries")
        self.rollback_calls.append((run_id, routine_name))
        if self.rollback_error is not None:
            raise self.rollback_error
        if self.rollback_answer is not None:
            return self.rollback_answer
        return {
            key: {
                "os_ticket_id": os_id,
                "cycle_key": f"hubspot:{run_id}",
                "signal_at": None,
                "signal_id": None,
            }
            for key, os_id in self.rollback_tickets.items()
        }

    def write_run_record(
        self,
        routine_name,
        status,
        error_summary,
        tickets_processed,
        drafts_created,
        started_at_iso,
        finished_at_iso,
        *,
        run_id=None,
        source_mode=None,
        failure_reason_code=None,
        hubspot_calls=None,
    ):
        self.calls.append("write_run_record")
        if self.record_error is not None:
            raise self.record_error
        self.records.append(
            {
                "routine_name": routine_name,
                "status": status,
                "error_summary": error_summary,
                "tickets_processed": tickets_processed,
                "drafts_created": drafts_created,
                "started_at_iso": started_at_iso,
                "finished_at_iso": finished_at_iso,
                "run_id": run_id,
                "source_mode": source_mode,
                "failure_reason_code": failure_reason_code,
                "hubspot_calls": hubspot_calls,
            }
        )

    def mark_manual_run_completed(self, run_request_id, resulting_run_id):
        self.calls.append("mark_manual_run_completed")
        self.manual_links.append((run_request_id, resulting_run_id))


class Agent(fakes.FakeLauncher):
    """FakeLauncher that also looks at what the wrapper prepared for the agent,
    at launch time (the scratch directory is gone once the run is over)."""

    def __init__(self, exit_code: int, result: Optional[Any] = None, raw: Optional[str] = None):
        super().__init__(exit_code, result)
        self.raw = raw
        self.contexts: list[Any] = []
        self.scratch_existed: list[bool] = []

    def __call__(self, command, env):
        context_path = Path(env[run_types.RUN_CONTEXT_ENV])
        self.contexts.append(json.loads(context_path.read_text(encoding="utf-8")))
        self.scratch_existed.append(context_path.parent.is_dir())
        if self.raw is not None:
            Path(env[run_types.RESULT_PATH_ENV]).write_text(self.raw, encoding="utf-8")
        return super().__call__(command, env)


def _raiser(error: BaseException) -> Callable[..., Any]:
    def raise_it(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    return raise_it


class World:
    """One orchestrator with every collaborator replaced."""

    def __init__(self, tmp_path: Path, mode: Any = "hubspot") -> None:
        self.root = tmp_path / "root"
        self.root.mkdir()
        self.client = FakeClient()
        self.transport = fakes.FakeHubSpotTransport({SEARCH_PATH: {"results": [NEW_HIT]}})
        self.agent: Any = Agent(0, result=_result())
        self.launch_attempts = 0
        self.mode_reader: Callable[[], Any] = lambda: mode
        self.mode_reads = 0
        self.settings: Any = dict(SETTINGS)
        self.token: Optional[str] = "test-token"
        self.token_reads = 0
        self.decide: Callable[..., Any] = pg.decide
        self.decide_calls = 0
        self.engine_shadow_envs: list[dict] = []
        self.engine_shadow_error: Optional[Exception] = None
        self.run_ids: list[str] = []
        self.state_path = fakes.temp_state_file(tmp_path, STATE)
        self.seen_path = tmp_path / "seen.json"
        self.environ = {"PATH": "kept-path", "PACKN_KEEP_ME": "1"}
        self._tick = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)

    # --- collaborators
    def _now(self) -> datetime:
        self._tick += timedelta(seconds=5)
        return self._tick

    def _new_run_id(self) -> str:
        run_id = rt.new_run_id()
        self.run_ids.append(run_id)
        return run_id

    def _resolve_mode(self) -> Any:
        self.mode_reads += 1
        return self.mode_reader()

    def _decide(self, mode: str, deps: Any) -> Any:
        self.decide_calls += 1
        return self.decide(mode, deps)

    def _load_settings(self) -> dict:
        if isinstance(self.settings, Exception):
            raise self.settings
        return self.settings

    def _read_token(self) -> Optional[str]:
        self.token_reads += 1
        return self.token

    def _engine_shadow(self, env: Any) -> None:
        self.engine_shadow_envs.append(dict(env))
        if self.engine_shadow_error is not None:
            raise self.engine_shadow_error

    def _launch(self, command: Any, env: Any) -> Any:
        self.launch_attempts += 1
        return self.agent(command, env)

    def deps(self) -> Any:
        return run_types.Deps(
            root=self.root,
            state_path=self.state_path,
            seen_path=self.seen_path,
            now=self._now,
            new_run_id=self._new_run_id,
            resolve_mode=self._resolve_mode,
            decide=self._decide,
            load_settings=self._load_settings,
            read_hubspot_token=self._read_token,
            engine_shadow_step=self._engine_shadow,
            launch_agent=self._launch,
            report_routine_schedule=self.client.report_routine_schedule,
            read_routine_enabled=self.client.read_routine_enabled,
            read_pending_rerun_requests=self.client.read_pending_rerun_requests,
            rollback_cycle_entries=self.client.rollback_cycle_entries,
            write_run_record=self.client.write_run_record,
            mark_manual_run_completed=self.client.mark_manual_run_completed,
            hubspot_transport=self.transport,
            environ=self.environ,
        )

    # --- driving and reading
    def run(self, manual_request_id: Optional[str] = None) -> Any:
        return rt.run(self.deps(), manual_request_id=manual_request_id)

    @property
    def record(self) -> dict:
        assert len(self.client.records) == 1, self.client.records
        return self.client.records[0]

    @property
    def scratch_root(self) -> Path:
        return self.root / "outputs" / "runs" / "os"

    def scratch_left(self) -> list[str]:
        return sorted(p.name for p in self.scratch_root.iterdir()) if self.scratch_root.is_dir() else []

    def searches(self) -> int:
        return len(self.transport.calls_matching(SEARCH_PATH))


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    w = World(tmp_path)
    w.monkeypatch = monkeypatch  # for a case that has to narrow the runner registry
    return w


# --- one configuration per outcome; each is used by a named test and by the
# --- exactly-one-record table at the end of this section


def _complete_result(w: World) -> None:
    pass


def _exit_1_no_result(w: World) -> None:
    w.agent = Agent(1)


def _launcher_cannot_start(w: World) -> None:
    w.agent = _raiser(FileNotFoundError(f"no such command {EXC_MARKER}"))


def _exit_0_no_result(w: World) -> None:
    w.agent = Agent(0)


def _invalid_json(w: World) -> None:
    w.agent = Agent(0, raw='{"status": "success", "tickets_processed": 3')


def _missing_status(w: World) -> None:
    result = _result()
    del result["status"]
    w.agent = Agent(0, result=result)


def _no_work(w: World) -> None:
    w.transport.respond(SEARCH_PATH, {"results": []})


def _paused(w: World) -> None:
    w.client.enabled = False


def _rerun_only(w: World) -> None:
    w.transport.respond(SEARCH_PATH, {"results": []})
    w.client.reruns = [{"id": RERUN_REQUEST_ID, "ticket_id": "777", "requested_by": "op"}]


def _unknown_mode(w: World) -> None:
    w.mode_reader = lambda: "banana"


def _unreadable_mode(w: World) -> None:
    w.mode_reader = _raiser(RuntimeError(f"connection refused {EXC_MARKER}"))


def _runner_raises(w: World) -> None:
    w.decide = _raiser(RuntimeError(f"unexpected {EXC_MARKER}"))


def _text_in_result(w: World) -> None:
    result = _result(status="partial", error_summary="1 ticket hit an MCP error")
    result["tickets"][0]["draft_body"] = f"Hello, your parcel {TEXT_MARKER} is on its way."
    result["tickets"][0]["classification"]["reason"] = f"customer wrote {TEXT_MARKER} about tracking"
    result["tickets"][1]["outcome"] = "failed"
    result["tickets"][1]["failure_reason_code"] = "mcp_error"
    w.agent = Agent(0, result=result)


def _rollback_unreadable(w: World) -> None:
    w.client.rollback_error = RuntimeError(f"permission denied {EXC_MARKER}")


def _os_mode_without_a_runner(w: World) -> None:
    # Plan 93-16 registered the `os` runner, so a recognised mode with no runner
    # no longer exists by itself: this case narrows the registry to `hubspot`.
    w.monkeypatch.setattr(rt, "MODE_RUNNERS", {})
    w.monkeypatch.setattr(rt, "RUNNER_SPECS", {"hubspot": rt.RUNNER_SPECS["hubspot"]})
    w.mode_reader = lambda: "os"


CASES = [
    pytest.param(_complete_result, "success", None, 1, id="complete-result"),
    pytest.param(_exit_1_no_result, "failure", "agent_launch_failed", 1, id="exit-1-no-result"),
    pytest.param(_launcher_cannot_start, "failure", "agent_launch_failed", 1, id="cannot-start"),
    pytest.param(_exit_0_no_result, "failure", "agent_exited_abnormally", 1, id="exit-0-no-result"),
    pytest.param(_invalid_json, "failure", "agent_exited_abnormally", 1, id="invalid-json"),
    pytest.param(_missing_status, "failure", "agent_exited_abnormally", 1, id="missing-status"),
    pytest.param(_no_work, "success", None, 0, id="no-work"),
    pytest.param(_paused, "skipped", None, 0, id="paused"),
    pytest.param(_rerun_only, "success", None, 1, id="rerun-only"),
    pytest.param(_unknown_mode, "failure", "source_unknown", 0, id="unknown-mode"),
    pytest.param(_unreadable_mode, "failure", "source_unreadable", 0, id="unreadable-mode"),
    pytest.param(_runner_raises, "failure", None, 0, id="runner-raises"),
    pytest.param(_text_in_result, "partial", None, 1, id="text-in-result"),
    pytest.param(_rollback_unreadable, "success", None, 1, id="rollback-unreadable"),
    pytest.param(_os_mode_without_a_runner, "failure", "source_unknown", 0, id="mode-without-runner"),
]


# ------------------------------------------------------------- the one record


def test_a_complete_result_becomes_the_one_record(world: World) -> None:
    assert world.run() == 0
    record = world.record
    assert record["routine_name"] == "tickets-process"
    assert record["status"] == "success"
    assert record["tickets_processed"] == 3
    assert record["drafts_created"] == 2
    assert record["source_mode"] == "hubspot"
    assert record["hubspot_calls"] is None
    assert record["failure_reason_code"] is None
    assert record["error_summary"] is None
    assert record["run_id"] == world.run_ids[0]
    assert world.launch_attempts == 1


def test_the_record_carries_when_the_run_started_and_finished(world: World) -> None:
    world.run()
    started = datetime.fromisoformat(world.record["started_at_iso"])
    finished = datetime.fromisoformat(world.record["finished_at_iso"])
    assert started.tzinfo is not None and finished.tzinfo is not None
    assert started < finished


def test_a_launcher_that_exits_1_with_no_result_is_agent_launch_failed(world: World) -> None:
    _exit_1_no_result(world)
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["failure_reason_code"] == "agent_launch_failed"
    assert world.record["tickets_processed"] == 0
    assert "agent_exit_code=1" in world.record["error_summary"]


def test_a_launcher_that_cannot_be_started_is_agent_launch_failed(world: World) -> None:
    _launcher_cannot_start(world)
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["failure_reason_code"] == "agent_launch_failed"
    assert world.record["error_summary"] == "agent_launch_failed:FileNotFoundError"


@pytest.mark.parametrize(
    "configure, kept",
    [
        pytest.param(_exit_0_no_result, 0, id="no-result"),
        pytest.param(_invalid_json, 0, id="invalid-json"),
        pytest.param(_missing_status, 3, id="missing-status-keeps-the-readable-count"),
    ],
)
def test_an_agent_that_exits_without_a_complete_result_is_agent_exited_abnormally(
    world: World, configure, kept: int
) -> None:
    configure(world)
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["failure_reason_code"] == "agent_exited_abnormally"
    assert world.record["tickets_processed"] == kept


def test_a_result_of_another_run_is_not_this_runs_result(world: World) -> None:
    world.agent = Agent(0, result=_result(run_id="11111111-1111-4111-8111-111111111111"))
    world.run()
    assert world.record["failure_reason_code"] == "agent_exited_abnormally"
    assert world.record["tickets_processed"] == 0


def test_a_result_naming_this_run_is_accepted(world: World) -> None:
    class Naming(Agent):
        def __call__(self, command, env):
            self.result = _result(run_id=env[run_types.RUN_ID_ENV])
            return super().__call__(command, env)

    world.agent = Naming(0)
    world.run()
    assert world.record["status"] == "success"
    assert world.record["tickets_processed"] == 3


def test_a_complete_result_with_a_non_zero_exit_keeps_the_result_and_notes_the_exit(
    world: World,
) -> None:
    world.agent = Agent(2, result=_result())
    world.run()
    assert world.record["status"] == "success"
    assert world.record["tickets_processed"] == 3
    assert world.record["failure_reason_code"] is None
    assert world.record["error_summary"] == "agent_exit_code=2"


def test_no_work_writes_a_zero_ticket_success_and_never_launches(world: World) -> None:
    _no_work(world)
    assert world.run() == 0
    assert world.record["status"] == "success"
    assert world.record["tickets_processed"] == 0
    assert world.record["drafts_created"] == 0
    assert world.record["source_mode"] == "hubspot"
    assert world.launch_attempts == 0
    assert world.engine_shadow_envs == []


def test_a_paused_routine_writes_one_skipped_record_carrying_the_mode(world: World) -> None:
    _paused(world)
    assert world.run() == 0
    assert world.record["status"] == "skipped"
    assert world.record["source_mode"] == "hubspot"
    assert world.record["error_summary"] == "routine_paused"
    assert world.launch_attempts == 0
    assert world.searches() == 0


def test_a_pending_rerun_with_no_candidates_launches_the_agent_with_its_cycle_key(
    world: World,
) -> None:
    _rerun_only(world)
    assert world.run() == 0
    assert world.launch_attempts == 1
    assert world.searches() == 0  # reruns are always agent work: no search is needed to decide
    assert world.agent.contexts == [
        {
            "777": {
                "os_ticket_id": None,
                "cycle_key": f"rerun:{RERUN_REQUEST_ID}",
                "signal_at": None,
                "signal_id": None,
            }
        }
    ]


def test_an_unknown_mode_stops_the_run_with_source_unknown_and_touches_nothing(world: World) -> None:
    _unknown_mode(world)
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["failure_reason_code"] == "source_unknown"
    assert world.record["source_mode"] is None
    assert world.record["tickets_processed"] == 0
    assert world.decide_calls == 0
    assert world.transport.calls == []
    assert world.token_reads == 0
    assert world.launch_attempts == 0
    assert world.engine_shadow_envs == []


def test_an_unreadable_mode_stops_the_run_with_source_unreadable_and_touches_nothing(
    world: World,
) -> None:
    _unreadable_mode(world)
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["failure_reason_code"] == "source_unreadable"
    assert world.record["source_mode"] is None
    assert world.record["error_summary"] == "source_unreadable"
    assert world.decide_calls == 0
    assert world.transport.calls == []
    assert world.launch_attempts == 0


@pytest.mark.parametrize("code", ["source_unknown", "source_unreadable"])
def test_a_source_mode_error_from_the_reader_keeps_its_code(world: World, code: str) -> None:
    world.mode_reader = _raiser(source.SourceModeError(code))
    world.run()
    assert world.record["failure_reason_code"] == code
    assert world.record["source_mode"] is None
    assert world.launch_attempts == 0


def test_the_mode_is_read_once(world: World) -> None:
    world.run()
    assert world.mode_reads == 1


def test_a_record_that_cannot_be_written_is_exit_1(world: World) -> None:
    world.client.record_error = RuntimeError(f"insert refused {EXC_MARKER}")
    assert world.run() == 1
    assert world.client.records == []
    assert world.client.calls.count("write_run_record") == 1


def test_a_manual_run_whose_record_cannot_be_written_links_no_run(world: World) -> None:
    world.client.record_error = RuntimeError("insert refused")
    assert world.run(manual_request_id=MANUAL_REQUEST_ID) == 1
    assert world.client.manual_links == [(MANUAL_REQUEST_ID, None)]


def test_a_scheduled_run_and_a_manual_run_each_write_their_own_record(world: World) -> None:
    deps = world.deps()
    assert rt.run(deps) == 0
    assert rt.run(deps, manual_request_id=MANUAL_REQUEST_ID) == 0
    first, second = world.client.records
    assert first["run_id"] != second["run_id"]
    assert [first["run_id"], second["run_id"]] == world.run_ids
    assert world.client.manual_links == [(MANUAL_REQUEST_ID, second["run_id"])]
    # the link follows the record
    assert world.client.calls[-2:] == ["write_run_record", "mark_manual_run_completed"]


def test_a_manual_request_id_that_is_not_a_uuid_is_not_linked_and_the_run_still_records(
    world: World,
) -> None:
    assert world.run(manual_request_id="1; DROP TABLE") == 0
    assert world.record["status"] == "success"
    assert world.client.manual_links == []


def test_the_agent_is_launched_with_the_four_variables_and_todays_command(world: World) -> None:
    world.run()
    ((command, env),) = world.agent.calls
    run_id = world.run_ids[0]
    assert env["PACKN_RUN_ID"] == run_id
    assert env["PACKN_SOURCE_MODE"] == "hubspot"
    assert env["PACKN_RESULT_PATH"] == str(world.scratch_root / run_id / "result.json")
    assert env["PACKN_RUN_CONTEXT"] == str(world.scratch_root / run_id / "context.json")
    assert env["PATH"] == "kept-path" and env["PACKN_KEEP_ME"] == "1"
    assert command == [
        "claude",
        "-p",
        "/packn-tickets",
        "--model",
        llm.DRAFT_MODEL,
        "--dangerously-skip-permissions",
    ]
    # no tool restriction in this mode
    assert not any("ools" in part or "mcp" in part.lower() for part in command)


def test_the_four_variable_names_are_the_ones_the_helper_and_the_fakes_read() -> None:
    assert run_types.RUN_ID_ENV == "PACKN_RUN_ID"
    assert run_types.SOURCE_MODE_ENV == source.MODE_ENV == "PACKN_SOURCE_MODE"
    assert run_types.RESULT_PATH_ENV == fakes.RESULT_PATH_ENV == "PACKN_RESULT_PATH"
    assert run_types.RUN_CONTEXT_ENV == client.RUN_CONTEXT_ENV == "PACKN_RUN_CONTEXT"


def test_the_agent_model_is_the_one_pin(world: World) -> None:
    assert rt.AGENT_MODEL == llm.DRAFT_MODEL
    assert rt.agent_command().count(rt.AGENT_MODEL) == 1


def test_the_engine_shadow_step_runs_once_before_the_agent_with_the_same_environment(
    world: World,
) -> None:
    order: list[str] = []
    real_shadow, real_launch = world._engine_shadow, world.agent

    def shadow(env):
        order.append("engine_shadow")
        real_shadow(env)

    class Ordered(Agent):
        def __call__(self, command, env):
            order.append("agent")
            return real_launch(command, env)

    world._engine_shadow = shadow  # type: ignore[method-assign]
    world.agent = Ordered(0)
    world.run()
    assert order == ["engine_shadow", "agent"]
    ((_, agent_env),) = real_launch.calls
    assert world.engine_shadow_envs == [agent_env]
    assert world.engine_shadow_envs[0]["PACKN_SOURCE_MODE"] == "hubspot"


def test_an_engine_shadow_step_that_fails_does_not_stop_the_run(world: World) -> None:
    world.engine_shadow_error = RuntimeError(f"boom {EXC_MARKER}")
    world.run()
    assert world.record["status"] == "success"
    assert world.launch_attempts == 1


def test_the_schedule_is_reported_by_the_wrappers_filename(world: World) -> None:
    world.run()
    assert world.client.schedule_reports == [("tickets-process", "run_tickets.sh")]
    assert rt.CRON_MARKER == pg.CRON_MARKER == "run_tickets.sh"


def test_a_schedule_report_that_raises_does_not_stop_the_run(world: World) -> None:
    deps = world.deps()
    deps.report_routine_schedule = _raiser(RuntimeError("crontab missing"))
    assert rt.run(deps) == 0
    assert world.record["status"] == "success"


# ------------------------------------------------- no message text (T-93-47)


def _record_text(record: dict) -> str:
    return json.dumps(record, default=str, ensure_ascii=False)


def test_an_unexpected_exception_reaches_the_record_as_a_class_name_only(
    world: World, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _runner_raises(world)
    assert world.run() == 0
    record = world.record
    assert record["status"] == "failure"
    assert record["failure_reason_code"] is None
    assert record["error_summary"] == "runner_exception:RuntimeError"
    assert record["source_mode"] == "hubspot"
    assert EXC_MARKER not in _record_text(record)
    fakes.assert_marker_absent(EXC_MARKER, caplog, capfd)


def test_ticket_text_in_the_result_file_reaches_no_field_of_the_record(
    world: World, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    _text_in_result(world)
    assert world.run() == 0
    record = world.record
    assert record["status"] == "partial"
    assert record["tickets_processed"] == 3
    assert record["error_summary"] == "failed_tickets=1; codes=mcp_error:1; 1 ticket hit an MCP error"
    assert TEXT_MARKER not in _record_text(record)
    for value in record.values():
        assert TEXT_MARKER not in str(value)
    fakes.assert_marker_absent(TEXT_MARKER, caplog, capfd)


def test_a_result_summary_that_repeats_ticket_text_is_withheld(
    world: World, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    body = f"Hello, your parcel {TEXT_MARKER} is on its way and should arrive Friday."
    result = _result(error_summary=f"draft for 501 was: {body}")
    result["tickets"][0]["draft_body"] = body
    world.agent = Agent(0, result=result)
    world.run()
    assert world.record["error_summary"] == "result_summary_withheld"
    assert TEXT_MARKER not in _record_text(world.record)
    fakes.assert_marker_absent(TEXT_MARKER, caplog, capfd)


@pytest.mark.parametrize(
    "configure",
    [
        pytest.param(_launcher_cannot_start, id="launcher"),
        pytest.param(_unreadable_mode, id="mode-read"),
        pytest.param(_rollback_unreadable, id="rollback-read"),
    ],
)
def test_no_exception_message_reaches_the_record_or_any_output(
    world: World, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture, configure
) -> None:
    caplog.set_level(logging.DEBUG)
    configure(world)
    world.run()
    assert EXC_MARKER not in _record_text(world.record)
    fakes.assert_marker_absent(EXC_MARKER, caplog, capfd)


def test_a_record_write_failure_is_logged_as_a_code_only(
    world: World, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    world.client.record_error = RuntimeError(f"insert refused {EXC_MARKER}")
    assert world.run() == 1
    out, err = capfd.readouterr()
    assert "run_record_unwritable:RuntimeError" in err
    assert EXC_MARKER not in out + err + caplog.text


# ------------------------------------------------------ the scratch directory


@pytest.mark.parametrize(
    "configure",
    [
        pytest.param(_complete_result, id="success"),
        pytest.param(_exit_1_no_result, id="launch-failed"),
        pytest.param(_invalid_json, id="invalid-result"),
        pytest.param(_runner_raises, id="runner-raises"),
    ],
)
def test_the_scratch_directory_is_gone_after_the_run(world: World, configure) -> None:
    configure(world)
    world.run()
    assert world.scratch_left() == []
    if isinstance(world.agent, Agent) and world.agent.calls:
        assert world.agent.scratch_existed == [True]  # it was there while the agent ran


# The scratch directory holds ticket text: the bundle (full threads, names and
# addresses) and the agent's result (draft bodies). Review WR-50: it was made
# with default modes, its files were made readable and narrowed afterwards, it
# was removed only on an orderly exit, and nothing ever swept an older one.

RUN_STARTED = datetime(2026, 10, 9, 12, 0, 5, tzinfo=timezone.utc)  # the world's first clock reading
POSIX = os.name == "posix"


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _spy_mkdir(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, int]]:
    """Every directory that was really created, as (path, the mode asked for).
    pathlib's own `mkdir` goes through `os.mkdir`, so both ways of creating one
    are seen; an attempt on a directory that is already there creates nothing
    and is not recorded."""
    made: list[tuple[Path, int]] = []
    real = os.mkdir

    def spy(path: Any, mode: int = 0o777, *args: Any, **kwargs: Any) -> None:
        real(path, mode, *args, **kwargs)
        made.append((Path(path), mode))

    monkeypatch.setattr(os, "mkdir", spy)
    return made


def _spy_open(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int, int]]:
    """Every `os.open` that creates a file, as (file name, flags, mode)."""
    opened: list[tuple[str, int, int]] = []
    real = os.open

    def spy(path: Any, flags: int, mode: int = 0o777, *args: Any, **kwargs: Any) -> int:
        if flags & os.O_CREAT:
            opened.append((Path(path).name, flags, mode))
        return real(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    return opened


def test_the_scratch_directory_is_created_owner_only_in_one_step(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    made = _spy_mkdir(monkeypatch)
    modes: list[int] = []
    real_agent = world.agent

    def launch(command: Any, env: Any) -> int:
        modes.append(_mode(Path(env[run_types.RESULT_PATH_ENV]).parent))
        return real_agent(command, env)

    world.agent = launch
    world.run()

    scratch = world.scratch_root / world.run_ids[0]
    # asked for at creation: there is no moment at which the directory exists with a wider mode
    assert [mode for path, mode in made if path == scratch] == [0o700]
    assert run_types.SCRATCH_DIR_MODE == 0o700
    if POSIX:
        assert modes == [0o700]  # and that is the mode it had while the agent ran


def test_a_scratch_directory_that_already_exists_is_not_adopted(world: World) -> None:
    """A run id is new. A directory already at that path was not made by this
    run, so its mode and its contents are not this run's: the run fails closed
    with its one record, and it does not remove what it did not create."""
    planted = rt.new_run_id()
    deps = world.deps()
    deps.new_run_id = lambda: planted
    (world.scratch_root / planted).mkdir(parents=True)
    (world.scratch_root / planted / "not-ours.txt").write_text("kept", encoding="utf-8")

    assert rt.run(deps) == 0
    assert world.record["status"] == "failure"
    assert world.record["error_summary"] == "runner_exception:FileExistsError"
    assert world.launch_attempts == 0
    assert (world.scratch_root / planted / "not-ours.txt").read_text(encoding="utf-8") == "kept"


def test_the_run_context_file_is_created_owner_only_and_never_narrowed_afterwards(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = _spy_open(monkeypatch)
    chmods: list[str] = []
    monkeypatch.setattr(os, "chmod", lambda path, *_a, **_k: chmods.append(Path(path).name))
    modes: list[int] = []
    real_agent = world.agent

    def launch(command: Any, env: Any) -> int:
        modes.append(_mode(Path(env[run_types.RUN_CONTEXT_ENV])))
        return real_agent(command, env)

    world.agent = launch
    world.run()

    (created,) = [entry for entry in opened if entry[0].startswith("context.json")]
    assert created[1] & os.O_EXCL and created[1] & os.O_WRONLY  # a new file, never one that was already there
    assert created[2] == 0o600 == run_types.SCRATCH_FILE_MODE
    assert chmods == []  # owner-only at creation, not write-then-chmod
    if POSIX:
        assert modes == [0o600]


@pytest.mark.parametrize(
    "error",
    [RuntimeError("boom"), SystemExit(3), KeyboardInterrupt()],
    ids=["exception", "SystemExit", "KeyboardInterrupt"],
)
def test_the_scratch_directory_is_removed_whatever_ends_the_runner(world: World, error: BaseException) -> None:
    def runner(ctx: Any, deps: Any) -> Any:
        (ctx.scratch_dir / "bundle.json").write_text("ticket text", encoding="utf-8")
        raise error

    world.monkeypatch.setitem(rt.MODE_RUNNERS, "hubspot", runner)
    assert world.run() == 0  # the record is still written
    assert world.record["error_summary"] == f"runner_exception:{type(error).__name__}"
    assert world.scratch_left() == []


def test_the_scratch_directory_is_removed_when_the_crash_handler_itself_fails(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """stderr can be gone (a closed pipe). The line the crash handler logs then
    raises from INSIDE the handler, and the run leaves `run()` as an exception."""

    def runner(ctx: Any, deps: Any) -> Any:
        (ctx.scratch_dir / "bundle.json").write_text("ticket text", encoding="utf-8")
        raise RuntimeError("boom")

    real_log = rt._log

    def log(message: str) -> None:
        if message.startswith("runner_exception"):
            raise BrokenPipeError("stderr is closed")
        real_log(message)

    world.monkeypatch.setitem(rt.MODE_RUNNERS, "hubspot", runner)
    monkeypatch.setattr(rt, "_log", log)
    with pytest.raises(BrokenPipeError):
        world.run()
    assert world.scratch_left() == []


def test_a_removal_that_is_interrupted_is_tried_again_before_the_process_leaves(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_rmtree = shutil.rmtree
    attempts: list[str] = []

    def rmtree(path: Any, *args: Any, **kwargs: Any) -> None:
        attempts.append(Path(path).name)
        if len(attempts) == 1:
            raise KeyboardInterrupt()  # the first attempt is cut short
        real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    with pytest.raises(KeyboardInterrupt):
        world.run()
    assert attempts == [world.run_ids[0]] * 2
    assert world.scratch_left() == []


# --- the sweep at run start


def _old_run_dir(world: World, hours: float, name: Optional[str] = None) -> Path:
    """A leftover run directory whose last change was `hours` before this run started."""
    path = world.scratch_root / (name or rt.new_run_id())
    path.mkdir(parents=True)
    (path / "bundle.json").write_text("ticket text of a run that died", encoding="utf-8")
    moment = RUN_STARTED.timestamp() - hours * 3600
    os.utime(path, (moment, moment))
    return path


def test_a_run_removes_run_directories_older_than_a_day_and_nothing_younger(world: World) -> None:
    stale = _old_run_dir(world, 25)
    week_old = _old_run_dir(world, 24 * 7)
    young = _old_run_dir(world, 23)

    world.run()

    assert not stale.exists() and not week_old.exists()
    assert young.is_dir() and (young / "bundle.json").is_file()  # could be a run that is still going
    assert world.record["status"] == "success" and world.record["error_summary"] is None


def test_the_sweep_touches_only_run_directories_directly_under_the_scratch_root(world: World) -> None:
    stale = _old_run_dir(world, 48)
    not_a_run = _old_run_dir(world, 48, name="keep-me")  # not a run id: not ours to judge
    a_file = world.scratch_root / rt.new_run_id()  # a run id, but a file
    a_file.write_text("x", encoding="utf-8")
    sibling = world.root / "outputs" / "runs" / "cron-tickets.log"
    sibling.write_text("log", encoding="utf-8")
    elsewhere = world.root / "outputs" / "runs" / "other" / rt.new_run_id()
    elsewhere.mkdir(parents=True)
    for path in (a_file, sibling, elsewhere):
        moment = RUN_STARTED.timestamp() - 48 * 3600
        os.utime(path, (moment, moment))

    world.run()

    assert not stale.exists()
    assert not_a_run.is_dir() and a_file.is_file() and sibling.is_file() and elsewhere.is_dir()


def test_the_sweep_never_follows_a_symlink(world: World, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("not the cron's", encoding="utf-8")
    moment = RUN_STARTED.timestamp() - 48 * 3600
    os.utime(outside, (moment, moment))
    stale = _old_run_dir(world, 48)
    link = world.scratch_root / rt.new_run_id()  # a run-id-named LINK to a directory outside
    inner = stale / "inner-link"
    try:
        os.symlink(outside, link, target_is_directory=True)
        os.symlink(outside, inner, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create a symlink")
    os.utime(stale, (moment, moment))

    world.run()

    assert not stale.exists()  # the stale run went, the link inside it with it
    assert (outside / "precious.txt").read_text(encoding="utf-8") == "not the cron's"
    assert os.path.islink(link)  # a link is never removed and never entered


def test_a_scratch_root_that_is_a_symlink_is_not_swept(world: World, tmp_path: Path) -> None:
    real_root = tmp_path / "somewhere-else"
    real_root.mkdir()
    victim = real_root / rt.new_run_id()
    victim.mkdir()
    moment = RUN_STARTED.timestamp() - 48 * 3600
    os.utime(victim, (moment, moment))
    world.scratch_root.parent.mkdir(parents=True)
    try:
        os.symlink(real_root, world.scratch_root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create a symlink")

    assert rt.sweep_stale_scratch(world.root, RUN_STARTED) == 0
    assert victim.is_dir()


def test_the_sweep_is_bounded_per_run(world: World) -> None:
    for _ in range(rt.SCRATCH_SWEEP_LIMIT + 3):
        _old_run_dir(world, 48)

    assert rt.sweep_stale_scratch(world.root, RUN_STARTED) == rt.SCRATCH_SWEEP_LIMIT
    assert len(world.scratch_left()) == 3  # the next runs take the rest
    assert rt.sweep_stale_scratch(world.root, RUN_STARTED) == 3
    assert 1 <= rt.SCRATCH_SWEEP_LIMIT <= 100 and rt.SCRATCH_MAX_AGE_S == 24 * 60 * 60


def test_a_sweep_that_fails_never_fails_the_run(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    _old_run_dir(world, 48)

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError(f"cannot list {EXC_MARKER}")

    monkeypatch.setattr(os, "scandir", refuse)
    assert world.run() == 0
    assert world.record["status"] == "success"
    assert EXC_MARKER not in json.dumps(world.record, default=str)


def test_the_sweep_runs_even_when_the_run_fails_closed_before_any_runner(world: World) -> None:
    stale = _old_run_dir(world, 48)
    world.mode_reader = _raiser(RuntimeError("database down"))
    world.run()
    assert world.record["status"] == "failure" and world.launch_attempts == 0
    assert not stale.exists()


# --------------------------------------------- fail-open, one branch per test


def _assert_fails_open(world: World, reason: str) -> None:
    decision = pg.decide("hubspot", world.deps())
    assert decision["decision"] == "run_agent"
    assert decision["reason"] == reason
    assert world.client.records == []  # decide writes no record
    assert world.run() == 0
    assert world.launch_attempts == 1
    assert world.record["status"] == "success"


def test_fail_open_settings_unreadable_still_launches_the_agent(world: World) -> None:
    world.settings = OSError("settings.yaml is not readable")
    _assert_fails_open(world, "settings_unreadable")
    assert world.searches() == 0


def test_fail_open_no_active_stages_still_launches_the_agent(world: World) -> None:
    world.settings = dict(SETTINGS, active_stages=[])
    _assert_fails_open(world, "active_stages_empty")
    assert world.searches() == 0


def test_fail_open_missing_token_still_launches_the_agent(world: World) -> None:
    world.token = None
    _assert_fails_open(world, "token_missing")
    assert world.searches() == 0


def test_fail_open_failed_search_still_launches_the_agent(world: World) -> None:
    world.transport = fakes.FakeHubSpotTransport()  # nothing canned: the search answers None
    _assert_fails_open(world, "search_failed")
    assert world.searches() == 2  # one per decision: the direct call and the run


def test_fail_open_full_page_all_deduped_still_launches_the_agent(world: World) -> None:
    world.settings = dict(SETTINGS, per_run_cap=2)
    world.transport.respond(SEARCH_PATH, {"results": [DEDUPED_HIT_A, DEDUPED_HIT_B]})
    _assert_fails_open(world, "page_full")


def test_a_settings_cap_that_is_not_a_number_still_launches_the_agent(world: World) -> None:
    world.settings = dict(SETTINGS, per_run_cap="many")
    _assert_fails_open(world, "settings_unreadable")


def test_a_genuinely_empty_search_is_no_work(world: World) -> None:
    _no_work(world)
    decision = pg.decide("hubspot", world.deps())
    assert decision["decision"] == "no_work"
    assert decision["reason"] == "none"
    assert world.run() == 0
    assert world.launch_attempts == 0


# ------------------------------------------------------- decide, on its own


def test_decide_reports_candidates_with_the_search_outcome(world: World) -> None:
    decision = pg.decide("hubspot", world.deps())
    assert decision["decision"] == "run_agent"
    assert decision["reason"] == "candidates"
    assert decision["candidates"] == ["501"]
    assert decision["reruns"] == []


def test_decide_paused_asks_nothing_else(world: World) -> None:
    world.client.enabled = False
    decision = pg.decide("hubspot", world.deps())
    assert decision["decision"] == "paused"
    assert decision["reason"] == "routine_paused"
    assert world.client.calls == ["read_routine_enabled"]
    assert world.token_reads == 0 and world.searches() == 0


def test_decide_hands_back_the_pending_reruns(world: World) -> None:
    world.client.reruns = [{"id": RERUN_REQUEST_ID, "ticket_id": "777"}]
    decision = pg.decide("hubspot", world.deps())
    assert decision["decision"] == "run_agent"
    assert decision["reason"] == "pending_reruns"
    assert decision["reruns"] == [{"id": RERUN_REQUEST_ID, "ticket_id": "777"}]
    assert world.searches() == 0


def test_decide_in_os_mode_asks_hubspot_nothing(world: World) -> None:
    world.settings = OSError("must not be read")
    decision = pg.decide("os", world.deps())
    assert decision["decision"] == "no_work"
    assert decision["reason"] == "os_mode"
    assert world.token_reads == 0 and world.transport.calls == []


def test_decide_in_shadow_mode_decides_as_in_hubspot_mode(world: World) -> None:
    decision = pg.decide("shadow", world.deps())
    assert (decision["decision"], decision["reason"]) == ("run_agent", "candidates")


def test_decide_refuses_a_mode_outside_the_three(world: World) -> None:
    with pytest.raises(source.SourceModeError) as caught:
        pg.decide("banana", world.deps())
    assert caught.value.code == "source_unknown"
    assert world.client.calls == []


def test_decide_reads_the_state_at_the_path_it_is_given(world: World, tmp_path: Path) -> None:
    # 501 already processed after its last change: with THIS state there is no work
    state = json.loads(json.dumps(STATE))
    state["ticket_fingerprints"]["501"] = {
        "processed_at": "2026-09-04T00:00:00Z",
        "hs_lastmodifieddate": "2026-09-03T10:00:00Z",
        "num_notes": None,
    }
    world.state_path = fakes.temp_state_file(tmp_path / "root", state)
    assert pg.decide("hubspot", world.deps())["decision"] == "no_work"


# ----------------------------- one implementation: main is a shell over decide


@pytest.mark.parametrize(
    "decision, exit_code, statuses",
    [
        pytest.param("paused", 11, ["skipped"], id="paused"),
        pytest.param("no_work", 10, ["success"], id="no-work"),
        pytest.param("run_agent", 0, [], id="run-agent"),
    ],
)
def test_pregate_main_is_a_shell_over_decide(
    monkeypatch: pytest.MonkeyPatch, decision: str, exit_code: int, statuses: list
) -> None:
    records: list[dict] = []
    modes: list[str] = []

    def fake_decide(mode, deps):
        modes.append(mode)
        return {"decision": decision, "reason": "x", "matched": [], "candidates": [], "page_full": False, "reruns": []}

    monkeypatch.delenv("PACKN_SOURCE_MODE", raising=False)
    monkeypatch.setattr(client, "report_routine_schedule", lambda *_a, **_k: None)
    monkeypatch.setattr(client, "read_ticket_source_mode", lambda: {"found": False, "value": None})
    monkeypatch.setattr(client, "write_run_record", lambda **kw: records.append(kw))
    monkeypatch.setattr(pg, "decide", fake_decide)
    assert pg.main() == exit_code
    assert modes == ["hubspot"]
    assert [record["status"] for record in records] == statuses
    if decision == "paused":
        assert records[0]["error_summary"].startswith("pregate: routine paused")
    if decision == "no_work":
        assert records[0]["error_summary"] is None and records[0]["tickets_processed"] == 0


def _function(module: Any, name: str) -> ast.FunctionDef:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    (node,) = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]
    return node


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            target = inner.func
            names.add(target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", ""))
    return names


def test_pregate_main_holds_no_search_of_its_own() -> None:
    """Parsed, so a name in a comment or a docstring does not count either way."""
    called = _called_names(_function(pg, "main"))
    assert "decide" in called
    assert not called & {
        "build_search_body",
        "unprocessed_candidates",
        "search_tickets",
        "hubspot_candidates",
        "read_token",
        "_load_settings",
        "_load_state",
    }


def test_decide_is_the_one_caller_of_the_candidate_decision() -> None:
    assert "hubspot_candidates" in _called_names(_function(pg, "decide"))
    assert "write_run_record" not in _called_names(_function(pg, "decide"))
    assert "_write_run_record" not in _called_names(_function(pg, "decide"))


# ------------------------------------------------------ the run id (WIRE-W6)


def test_the_run_id_is_one_lower_case_uuid4_used_three_ways(world: World) -> None:
    generated = rt.new_run_id()
    assert RUN_ID_FORM.match(generated)
    assert generated != rt.new_run_id()

    world.run()
    (run_id,) = world.run_ids
    assert RUN_ID_FORM.match(run_id)
    ((_, env),) = world.agent.calls
    assert world.record["run_id"] == run_id
    assert env["PACKN_RUN_ID"] == run_id
    assert Path(env["PACKN_RESULT_PATH"]).parent.name == run_id
    assert Path(env["PACKN_RUN_CONTEXT"]).parent.name == run_id
    assert Path(env["PACKN_RESULT_PATH"]).parent.parent == world.scratch_root


def test_a_run_id_of_another_form_never_names_a_directory(world: World) -> None:
    deps = world.deps()
    deps.new_run_id = lambda: "../escape"
    assert rt.run(deps) == 0
    assert world.record["status"] == "failure"
    assert world.record["run_id"] is None
    assert world.launch_attempts == 0
    assert world.scratch_left() == []


# ------------------------------------------------- rollback entries (CRON-B2)


def test_a_rollback_entry_reaches_the_run_context_under_this_runs_cycle_key(world: World) -> None:
    world.client.rollback_tickets = {"H1": OS_TICKET_ID}
    world.run()
    run_id = world.run_ids[0]
    assert world.client.rollback_calls == [(run_id, "tickets-process")]
    assert world.agent.contexts == [
        {
            "H1": {
                "os_ticket_id": OS_TICKET_ID,
                "cycle_key": f"hubspot:{run_id}",
                "signal_at": None,
                "signal_id": None,
            }
        }
    ]


def test_a_pending_rerun_for_the_same_ticket_wins_over_its_rollback_entry(world: World) -> None:
    world.client.rollback_tickets = {"H1": OS_TICKET_ID, "H2": None}
    world.client.reruns = [{"id": RERUN_REQUEST_ID, "ticket_id": "H1"}]
    world.run()
    (context,) = world.agent.contexts
    assert context["H1"]["cycle_key"] == f"rerun:{RERUN_REQUEST_ID}"
    assert context["H1"]["os_ticket_id"] == OS_TICKET_ID
    assert context["H2"]["cycle_key"] == f"hubspot:{world.run_ids[0]}"


@pytest.mark.parametrize(
    "break_it",
    [
        pytest.param(lambda c: setattr(c, "rollback_error", RuntimeError("permission denied")), id="raises"),
        pytest.param(lambda c: setattr(c, "rollback_answer", ["not", "a", "map"]), id="not-a-map"),
    ],
)
def test_an_unreadable_rollback_read_is_a_code_and_the_run_goes_on(world: World, break_it) -> None:
    break_it(world.client)
    world.client.reruns = [{"id": RERUN_REQUEST_ID, "ticket_id": "H1"}]
    assert world.run() == 0
    assert world.launch_attempts == 1
    (context,) = world.agent.contexts
    assert context == {
        "H1": {
            "os_ticket_id": None,
            "cycle_key": f"rerun:{RERUN_REQUEST_ID}",
            "signal_at": None,
            "signal_id": None,
        }
    }
    assert world.record["status"] == "success"
    assert world.record["error_summary"] == "rollback_entries_unreadable"
    assert world.record["failure_reason_code"] is None


def test_a_rerun_row_without_a_ticket_does_not_stop_the_run(world: World) -> None:
    world.client.reruns = [{"id": "r1"}, {"id": RERUN_REQUEST_ID, "ticket_id": 4242}]
    world.run()
    (context,) = world.agent.contexts
    assert list(context) == ["4242"]
    assert world.record["status"] == "success"


# ------------------------------------------------ startup failure (CRON-W6)


class _RealClientRecorder:
    """Stands over the REAL client module's two functions for the startup path,
    which imports the database client itself."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, error: Optional[BaseException] = None):
        self.records: list[dict] = []
        self.links: list[tuple] = []
        self.error = error
        monkeypatch.setattr(client, "write_run_record", self._write)
        monkeypatch.setattr(client, "mark_manual_run_completed", self._link)

    def _write(self, *args: Any, **kwargs: Any) -> None:
        if self.error is not None:
            raise self.error
        bound = inspect.signature(FakeClient.write_run_record).bind(None, *args, **kwargs)
        bound.apply_defaults()
        record = dict(bound.arguments)
        record.pop("self")
        self.records.append(record)

    def _link(self, run_request_id: str, resulting_run_id: Optional[str]) -> None:
        self.links.append((run_request_id, resulting_run_id))


def test_a_startup_failure_still_leaves_one_failure_record(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    recorder = _RealClientRecorder(monkeypatch)
    monkeypatch.setattr(rt, "build_deps", _raiser(ModuleNotFoundError(f"No module named {EXC_MARKER}")))
    assert rt.main([]) == 0
    (record,) = recorder.records
    assert record["routine_name"] == "tickets-process"
    assert record["status"] == "failure" == run_types.STATUS_FAILURE
    assert record["failure_reason_code"] == "agent_launch_failed" == run_types.AGENT_LAUNCH_FAILED
    assert record["source_mode"] is None
    assert record["error_summary"] == "startup:ModuleNotFoundError"
    assert record["tickets_processed"] == 0 and record["drafts_created"] == 0
    assert RUN_ID_FORM.match(record["run_id"])
    assert record["hubspot_calls"] is None
    assert EXC_MARKER not in _record_text(record)
    fakes.assert_marker_absent(EXC_MARKER, caplog, capfd)


def test_a_startup_failure_of_a_manual_run_is_linked_to_its_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _RealClientRecorder(monkeypatch)
    monkeypatch.setattr(rt, "build_deps", _raiser(ImportError("broken")))
    assert rt.main(["--manual-request", MANUAL_REQUEST_ID]) == 0
    (record,) = recorder.records
    assert record["error_summary"] == "startup:ImportError"
    assert recorder.links == [(MANUAL_REQUEST_ID, record["run_id"])]


def test_a_startup_failure_with_no_database_client_is_exit_1_and_one_code_line(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    recorder = _RealClientRecorder(monkeypatch)
    monkeypatch.setattr(rt, "build_deps", _raiser(ModuleNotFoundError(f"No module named {EXC_MARKER}")))
    # the database client's import fails too
    monkeypatch.setitem(sys.modules, "packn_os_hubspot_client", None)
    assert rt.main([]) == 1
    assert recorder.records == []
    out, err = capfd.readouterr()
    code_lines = [line for line in err.splitlines() if "startup_record_unwritable" in line]
    assert code_lines == ["[run_tickets] startup_record_unwritable:ModuleNotFoundError:ModuleNotFoundError"]
    assert EXC_MARKER not in out + err + caplog.text


def test_a_startup_record_the_database_refuses_is_exit_1_and_names_both_classes(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture
) -> None:
    recorder = _RealClientRecorder(monkeypatch, error=ConnectionError(f"refused {EXC_MARKER}"))
    monkeypatch.setattr(rt, "build_deps", _raiser(ModuleNotFoundError(f"No module named {EXC_MARKER}")))
    assert rt.main([]) == 1
    assert recorder.records == []
    out, err = capfd.readouterr()
    code_lines = [line for line in err.splitlines() if "startup_record_unwritable" in line]
    assert code_lines == ["[run_tickets] startup_record_unwritable:ModuleNotFoundError:ConnectionError"]
    assert EXC_MARKER not in out + err


def test_main_runs_with_the_built_dependencies_and_closes_the_pool(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []
    monkeypatch.setattr(rt, "build_deps", world.deps)
    monkeypatch.setattr(rt, "_close_pool", lambda: closed.append(True))
    assert rt.main(["--manual-request", MANUAL_REQUEST_ID]) == 0
    assert world.record["status"] == "success"
    assert world.client.manual_links == [(MANUAL_REQUEST_ID, world.record["run_id"])]
    assert closed == [True]


def test_main_exits_1_when_the_record_cannot_be_written(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.client.record_error = RuntimeError("insert refused")
    monkeypatch.setattr(rt, "build_deps", world.deps)
    assert rt.main([]) == 1


def test_an_argument_main_does_not_know_never_stops_a_run(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rt, "build_deps", world.deps)
    assert rt.main(["--surprise", "value"]) == 0
    assert world.record["status"] == "success"


def test_the_real_dependencies_are_the_real_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Built, never called: building opens no connection and reads no file."""
    deps = rt.build_deps()
    assert isinstance(deps, run_types.Deps)
    assert deps.root == PACKAGE_ROOT
    assert deps.state_path == PACKAGE_ROOT / "config" / "state.json" == pg.STATE_PATH
    assert deps.seen_path == PACKAGE_ROOT / "config" / ".action_items_forwarded.json"
    assert deps.resolve_mode is source.resolve_mode
    assert deps.decide is pg.decide
    assert deps.read_hubspot_token is hubspot_api.read_token
    assert deps.launch_agent is rt.launch_agent
    assert deps.engine_shadow_step is rt.run_engine_shadow_step
    assert deps.new_run_id is rt.new_run_id
    assert deps.write_run_record is client.write_run_record
    assert deps.rollback_cycle_entries is client.rollback_cycle_entries
    assert deps.mark_manual_run_completed is client.mark_manual_run_completed
    assert deps.read_routine_enabled is client.read_routine_enabled
    assert deps.read_pending_rerun_requests is client.read_pending_rerun_requests
    assert deps.report_routine_schedule is client.report_routine_schedule
    assert deps.environ is None and deps.hubspot_transport is None
    assert deps.now().tzinfo is not None


def test_the_seen_set_path_is_the_forwarders() -> None:
    forwarder = (PACKAGE_ROOT / "scripts" / "forward_action_items.py").read_text(encoding="utf-8")
    assert 'SEEN_PATH = ROOT / "config" / ".action_items_forwarded.json"' in forwarder


# ------------------------------------------- the two real children, never run


def test_launch_agent_starts_the_command_it_is_given_and_returns_its_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple] = []

    class Done:
        returncode = 7

    def fake_run(command, **kwargs):
        seen.append((command, kwargs))
        return Done()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert rt.launch_agent(("claude", "-p", "/x"), {"A": "1"}) == 7
    ((command, kwargs),) = seen
    assert command == ["claude", "-p", "/x"]
    assert kwargs["env"] == {"A": "1"}
    assert kwargs["cwd"] == str(PACKAGE_ROOT)
    assert kwargs.get("check") is False


def test_a_command_that_cannot_be_started_raises_out_of_launch_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subprocess, "run", _raiser(FileNotFoundError("claude")))
    with pytest.raises(OSError):
        rt.launch_agent(["claude"], {})


def test_the_engine_shadow_step_is_the_pipeline_script_as_a_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple] = []
    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: seen.append((command, kwargs)))
    assert rt.run_engine_shadow_step({"PACKN_SOURCE_MODE": "hubspot"}) is None
    ((command, kwargs),) = seen
    assert command == [sys.executable, str(PACKAGE_ROOT / "scripts" / "run_pipeline.py")]
    assert kwargs["env"] == {"PACKN_SOURCE_MODE": "hubspot"}
    assert kwargs["cwd"] == str(PACKAGE_ROOT)
    assert kwargs.get("check") is False


def test_the_fake_launcher_has_the_real_launchers_positional_parameters() -> None:
    def positional(function: Any) -> list[str]:
        kinds = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        return [p.name for p in inspect.signature(function).parameters.values() if p.kind in kinds]

    assert positional(rt.launch_agent) == ["command", "env"]
    assert positional(fakes.FakeLauncher.__call__)[1:] == positional(rt.launch_agent)


@pytest.mark.parametrize(
    "name",
    [
        "report_routine_schedule",
        "read_routine_enabled",
        "read_pending_rerun_requests",
        "rollback_cycle_entries",
        "write_run_record",
        "mark_manual_run_completed",
    ],
)
def test_the_fake_client_has_the_real_signatures(name: str) -> None:
    def shape(function: Any) -> list[tuple]:
        return [
            (p.name, p.kind, p.default is inspect.Parameter.empty)
            for p in inspect.signature(function).parameters.values()
            if p.name != "self"
        ]

    assert shape(getattr(FakeClient, name)) == shape(getattr(client, name))


# ------------------------------------- shared types and the registry (N-CRON-1)


def _top_level_import_roots(path: Path) -> list[str]:
    """The root package of every import that runs when the module is loaded:
    module level, and inside a module-level `if` / `try` / `with` / loop, but not
    inside a function or a class body's methods."""
    roots: list[str] = []

    def visit(nodes: Any) -> None:
        for node in nodes:
            if isinstance(node, ast.Import):
                roots.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                roots.append("." if node.level else (node.module or "").split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            else:
                visit(ast.iter_child_nodes(node))

    visit(ast.parse(path.read_text(encoding="utf-8")).body)
    return roots


def test_every_top_level_import_of_the_orchestrator_is_standard_library() -> None:
    roots = _top_level_import_roots(Path(rt.__file__))
    assert roots
    assert [root for root in roots if root not in sys.stdlib_module_names] == []


def test_the_types_leaf_imports_the_standard_library_only() -> None:
    tree = ast.parse(Path(run_types.__file__).read_text(encoding="utf-8"))
    roots = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            roots.append("." if node.level else (node.module or "").split(".")[0])
    assert roots
    assert [root for root in roots if root not in sys.stdlib_module_names] == []


def test_the_orchestrator_makes_no_hubspot_request_of_its_own() -> None:
    text = Path(rt.__file__).read_text(encoding="utf-8")
    tree = ast.parse(text)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not [name for name in imported if name.split(".")[0] in ("urllib", "http", "socket")]
    assert "hubapi.com" not in text
    assert not _called_names(tree) & {"search_tickets", "_request", "urlopen", "batch_read"}


def test_the_three_types_are_one_class_object_whoever_imports_them() -> None:
    assert rt.RunOutcome is run_types.RunOutcome
    assert rt.RunContext is run_types.RunContext
    assert rt.Deps is run_types.Deps
    with pytest.raises(AttributeError):
        rt.NoSuchName  # noqa: B018


def test_load_runners_registers_hubspot_and_is_idempotent() -> None:
    first = rt.load_runners()
    assert first is rt.MODE_RUNNERS
    assert first["hubspot"] is rt.run_hubspot
    snapshot = dict(first)
    assert rt.load_runners() is first
    assert dict(rt.MODE_RUNNERS) == snapshot
    assert set(rt.MODE_RUNNERS) == set(rt.RUNNER_SPECS)


def test_load_runners_leaves_a_registered_mode_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    def already_there(ctx, deps):
        return None

    monkeypatch.setitem(rt.MODE_RUNNERS, "hubspot", already_there)
    rt.load_runners()
    rt.load_runners()
    assert rt.MODE_RUNNERS["hubspot"] is already_there


def test_run_and_the_entry_point_see_the_same_registry(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rt, "MODE_RUNNERS", {})
    world.run()  # run() loads the registry itself, before the lookup
    assert rt.MODE_RUNNERS["hubspot"] is rt.run_hubspot
    assert world.record["status"] == "success"


def test_a_recognised_mode_with_no_runner_is_source_unknown(world: World) -> None:
    _os_mode_without_a_runner(world)
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["failure_reason_code"] == "source_unknown"
    assert world.record["source_mode"] == "os"
    assert world.record["error_summary"] == "runner_not_registered:os"
    assert world.launch_attempts == 0 and world.decide_calls == 0


def test_a_runner_module_that_fails_to_import_ends_as_a_failure_record(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rt, "MODE_RUNNERS", {})
    monkeypatch.setattr(
        rt,
        "RUNNER_SPECS",
        dict(rt.RUNNER_SPECS, os=("ticket_pipeline.no_such_runner_module_93_15", "run_os")),
    )
    world.mode_reader = lambda: "os"
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["failure_reason_code"] == "source_unknown"
    assert world.record["error_summary"] == "runner_import_failed:os:ModuleNotFoundError"
    assert "hubspot" in rt.MODE_RUNNERS and "os" not in rt.MODE_RUNNERS


def test_a_registered_runner_is_called_with_the_context_and_the_dependencies(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple] = []

    def fake_os_runner(ctx, deps):
        seen.append((ctx, deps))
        return run_types.RunOutcome(
            status="success",
            tickets_processed=4,
            drafts_created=1,
            hubspot_calls={"kinds": {"email_thread": 4}, "raw_requests": 8},
        )

    monkeypatch.setitem(rt.MODE_RUNNERS, "os", fake_os_runner)
    world.mode_reader = lambda: "os"
    assert world.run(manual_request_id=MANUAL_REQUEST_ID) == 0
    ((ctx, deps),) = seen
    assert isinstance(ctx, run_types.RunContext)
    assert ctx.run_id == world.run_ids[0] and ctx.mode == "os"
    assert ctx.manual_request_id == MANUAL_REQUEST_ID
    assert ctx.scratch_dir == world.scratch_root / ctx.run_id
    assert ctx.result_path == ctx.scratch_dir / "result.json"
    assert ctx.context_path == ctx.scratch_dir / "context.json"
    assert isinstance(deps, run_types.Deps)
    assert world.record["source_mode"] == "os"
    assert world.record["tickets_processed"] == 4
    assert world.record["hubspot_calls"] == {"kinds": {"email_thread": 4}, "raw_requests": 8}
    assert world.scratch_left() == []


@pytest.mark.parametrize(
    "returned, summary",
    [
        pytest.param(None, "runner_returned_no_outcome", id="nothing"),
        pytest.param({"status": "success"}, "runner_returned_no_outcome", id="a-dict"),
        pytest.param(run_types.RunOutcome(status="great"), "runner_outcome_status_invalid", id="bad-status"),
    ],
)
def test_a_runner_that_returns_no_usable_outcome_is_a_failure_record(
    world: World, monkeypatch: pytest.MonkeyPatch, returned: Any, summary: str
) -> None:
    monkeypatch.setitem(rt.MODE_RUNNERS, "hubspot", lambda ctx, deps: returned)
    assert world.run() == 0
    assert world.record["status"] == "failure"
    assert world.record["error_summary"] == summary


# --------------------------------------------- real files untouched (CRON-W7)


def test_an_end_to_end_run_opens_neither_the_real_token_nor_the_tracked_state(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbidden = {hubspot_api.TOKEN_PATH.resolve(), pg.STATE_PATH.resolve()}
    attempts: list[str] = []
    real_open, real_read_text = builtins.open, Path.read_text

    def check(target: Any) -> None:
        try:
            resolved = Path(target).resolve()
        except (TypeError, ValueError, OSError):
            return
        if resolved in forbidden:
            attempts.append(resolved.name)
            raise AssertionError("a real file was opened by an orchestrator test")

    def guarded_open(file, *args, **kwargs):
        check(file)
        return real_open(file, *args, **kwargs)

    def guarded_read_text(self, *args, **kwargs):
        check(self)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    # the guard bites (on the tracked state file; the token file is never touched)
    with pytest.raises(AssertionError, match="real file"):
        pg._load_state()
    assert attempts == ["state.json"]
    attempts.clear()

    assert world.run() == 0
    assert world.record["status"] == "success"
    assert world.token_reads == 1  # the token came through deps.read_hubspot_token
    assert world.searches() == 1  # the search ran, against the temp state file
    assert attempts == []


# ------------------------------------------------- the result file (T-93-48)


@pytest.mark.parametrize(
    "raw, state",
    [
        pytest.param(None, "missing", id="no-file"),
        pytest.param("", "unreadable", id="empty"),
        pytest.param("not json", "unreadable", id="not-json"),
        pytest.param("[1, 2]", "unreadable", id="a-list"),
        pytest.param('"success"', "unreadable", id="a-string"),
        pytest.param("[" * 100_000, "unreadable", id="hostile-nesting"),
        pytest.param(json.dumps({"tickets_processed": 3, "drafts_created": 2}), "incomplete", id="no-status"),
        pytest.param(json.dumps(_result(status="great")), "incomplete", id="unknown-status"),
        pytest.param(json.dumps(_result(status=["success"])), "incomplete", id="status-not-text"),
        pytest.param(json.dumps(_result(tickets_processed="3")), "incomplete", id="count-as-text"),
        pytest.param(json.dumps(_result(tickets_processed=-1)), "incomplete", id="negative-count"),
        pytest.param(json.dumps(_result(drafts_created=True)), "incomplete", id="count-as-bool"),
        pytest.param(json.dumps(_result(drafts_created=2.5)), "incomplete", id="count-as-float"),
        pytest.param(json.dumps(_result()), "complete", id="complete"),
        pytest.param(json.dumps(_result(status="skipped", tickets=None)), "complete", id="no-tickets-list"),
    ],
)
def test_the_result_file_is_parsed_defensively(tmp_path: Path, raw: Optional[str], state: str) -> None:
    path = tmp_path / "result.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    result = rt.read_result_file(path)
    assert result["state"] == state
    assert set(result) == {"state", "status", "tickets_processed", "drafts_created", "summary"}
    assert isinstance(result["tickets_processed"], int) and isinstance(result["drafts_created"], int)
    assert (result["status"] is not None) == (state == "complete")


def test_a_result_file_that_is_too_large_is_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "result.json"
    path.write_text(json.dumps(_result()), encoding="utf-8")
    monkeypatch.setattr(rt, "RESULT_MAX_BYTES", 10)
    assert rt.read_result_file(path)["state"] == "unreadable"


def test_a_result_summary_that_is_not_text_is_dropped(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    path.write_text(json.dumps(_result(error_summary={"body": TEXT_MARKER})), encoding="utf-8")
    result = rt.read_result_file(path)
    assert result["state"] == "complete"
    assert result["summary"] == "result_summary_dropped"


def test_a_failure_code_that_is_not_a_code_is_counted_but_not_copied(tmp_path: Path) -> None:
    result = _result(status="partial")
    result["tickets"][0]["outcome"] = "failed"
    result["tickets"][0]["failure_reason_code"] = f"Customer said {TEXT_MARKER}"
    path = tmp_path / "result.json"
    path.write_text(json.dumps(result), encoding="utf-8")
    assert rt.read_result_file(path)["summary"] == "failed_tickets=1"


# ---------------------------------- exactly one record, whatever happens (R14)


@pytest.mark.parametrize("configure, status, code, launches", CASES)
def test_every_outcome_writes_exactly_one_record(
    world: World, configure, status: str, code: Optional[str], launches: int
) -> None:
    configure(world)
    assert world.run() == 0
    assert world.client.calls.count("write_run_record") == 1
    assert len(world.client.records) == 1
    assert world.record["status"] == status
    assert world.record["failure_reason_code"] == code
    assert world.record["run_id"] == world.run_ids[0]
    assert world.launch_attempts == launches
    assert world.scratch_left() == []
