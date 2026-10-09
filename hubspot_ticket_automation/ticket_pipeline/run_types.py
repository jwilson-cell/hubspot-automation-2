"""The types one ticket run is made of (Phase 93, plan 15).

Three shapes and the names that go with them:

    RunContext   what one started run is: its id, its mode, when it started,
                 where its scratch files are
    RunOutcome   what a mode runner hands back: the facts of the one run record
    Deps         every collaborator of the orchestrator, injected, so that a
                 test touches no database, no HubSpot, no agent and no real file

WHY THIS IS ITS OWN MODULE
--------------------------
`scripts/run_tickets.py` is started as a script, so inside that process it is
the module `__main__`. A mode runner that lives in another file and imported
the types from the script would get a SECOND copy of the script and a second,
different `RunOutcome` class. The types therefore live here, in a leaf every
runner imports directly: one class object whether the orchestrator is
`__main__` or an imported module.

This module imports the standard library only, and nothing of the project.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

ROUTINE = "tickets-process"

# The run record's status column.
STATUS_SUCCESS = "success"
STATUS_PARTIAL = "partial"
STATUS_FAILURE = "failure"
STATUS_SKIPPED = "skipped"
RUN_STATUSES = (STATUS_SUCCESS, STATUS_PARTIAL, STATUS_FAILURE, STATUS_SKIPPED)

# Failure reason codes the orchestrator itself gives a record. The two source
# codes (source_unreadable, source_unknown) are ticket_pipeline/source.py's.
AGENT_LAUNCH_FAILED = "agent_launch_failed"
AGENT_EXITED_ABNORMALLY = "agent_exited_abnormally"

# What the orchestrator exports to every child of a run.
RUN_ID_ENV = "PACKN_RUN_ID"
SOURCE_MODE_ENV = "PACKN_SOURCE_MODE"
RESULT_PATH_ENV = "PACKN_RESULT_PATH"
RUN_CONTEXT_ENV = "PACKN_RUN_CONTEXT"
RUN_ENV_NAMES = (RUN_ID_ENV, SOURCE_MODE_ENV, RESULT_PATH_ENV, RUN_CONTEXT_ENV)

# A run's scratch directory is <root>/outputs/runs/os/<run id>/ and holds:
SCRATCH_PARTS = ("outputs", "runs", "os")
RESULT_FILE_NAME = "result.json"
CONTEXT_FILE_NAME = "context.json"


@dataclass(frozen=True)
class RunContext:
    """One started run.

    `run_id` is one lower-case uuid4 string. It is the run record's id, the
    value of PACKN_RUN_ID and the name of the scratch directory.
    `mode` is the source mode the run started in and finishes in.
    """

    run_id: str
    mode: str
    started_at: datetime
    scratch_dir: Path
    result_path: Path
    context_path: Path
    manual_request_id: Optional[str] = None
    routine: str = ROUTINE


@dataclass
class RunOutcome:
    """What a mode runner returns: the facts of the run's one record.

    `error_summary` holds codes, counts and exception class names. It never
    holds a message body, a subject or an exception's message.
    `failure_reason_code` is one of the closed codes the Pack'N OS card knows,
    or None. `hubspot_calls` is `hubspot_api.snapshot_calls()` for a run whose
    HubSpot calls were made by code, else None (the card prints "not counted").
    """

    status: str
    tickets_processed: int = 0
    drafts_created: int = 0
    error_summary: Optional[str] = None
    failure_reason_code: Optional[str] = None
    hubspot_calls: Optional[dict] = None


@dataclass
class Deps:
    """Every collaborator of the orchestrator and of the mode runners.

    The real ones are built by `run_tickets.build_deps()`. A test builds its
    own, with fakes and paths under the pytest temp directory.

    No runner opens the HubSpot token file or config/state.json by a
    module-level path: the token comes from `read_hubspot_token()`, the state
    from `state_path`, the forwarder's seen-set from `seen_path`.
    """

    # where things are
    root: Path
    state_path: Path
    seen_path: Path
    # time and identity
    now: Callable[[], datetime]
    new_run_id: Callable[[], str]
    # the source mode: answers one of the three modes, or raises
    resolve_mode: Callable[[], str]
    # the pre-gate decision: decide(mode, deps) -> {"decision", "reason", ...}
    decide: Callable[[str, Any], dict]
    load_settings: Callable[[], dict]
    read_hubspot_token: Callable[[], Optional[str]]
    # children: engine_shadow_step(env) -> None ; launch_agent(command, env) -> exit code
    engine_shadow_step: Callable[[Mapping[str, str]], None]
    launch_agent: Callable[[Sequence[str], Mapping[str, str]], int]
    # the database helper (packn_os_hubspot_client.client)
    report_routine_schedule: Callable[[str, str], Any]
    read_routine_enabled: Callable[[str], bool]
    read_pending_rerun_requests: Callable[[str], list]
    rollback_cycle_entries: Callable[..., dict]
    write_run_record: Callable[..., None]
    mark_manual_run_completed: Callable[[str, Optional[str]], None]
    # optional: a replacement for the HubSpot request function (tests), and the
    # environment children start from (None = this process's own, read at launch)
    hubspot_transport: Optional[Callable[..., Any]] = None
    environ: Optional[Mapping[str, str]] = None
    # `os` mode (Plan 93-16). Each None means "the real one", resolved by the
    # runner when it needs it, so a `hubspot`-mode run imports none of them.
    #   os_api                        fetch_unprocessed(limit) and fetch_ticket(ref);
    #                                 None = packn_os_hubspot_client.tickets_api
    #   read_os_origin                the OS's scheme and host; None = that module's
    #   engine_shadow_contexts_step   the engine shadow over ready-built contexts;
    #                                 None = ticket_pipeline.os_pass's own
    os_api: Any = None
    read_os_origin: Optional[Callable[[], str]] = None
    engine_shadow_contexts_step: Optional[Callable[[list], Any]] = None
