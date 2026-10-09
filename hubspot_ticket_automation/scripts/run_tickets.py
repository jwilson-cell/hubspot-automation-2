#!/usr/bin/env python3
"""One entry point for every tickets-process run (Phase 93, plan 15).

    python scripts/run_tickets.py [--manual-request <uuid>]

WHY THIS EXISTS
---------------
Until this script, a run's record was written by whoever happened to finish
it: the pre-gate on its two skip exits, the agent otherwise. A run that passed
the pre-gate and then died at agent launch wrote nothing, and Pack'N OS showed
only an absence (the silent outage seen since 2026-09-21). And "Run now"
launched the agent directly, around the pre-gate, the mode and the record.

This script is the one door. A scheduled tick (`scripts/run_tickets.sh`) and a
pressed Run now (`scripts/poll_manual_runs.sh`, with --manual-request) both
come through it, and it does four things in a fixed order:

    1. reads the ticket source mode ONCE (hubspot / shadow / os). A setting
       that cannot be read, or holds a value this code does not know, stops
       the run with a failure record. It is never read as `hubspot`.
    2. hands the run to that mode's runner (MODE_RUNNERS).
    3. removes the run's scratch directory.
    4. writes EXACTLY ONE run record, whatever happened to the agent: it never
       started, it died, it found nothing, it finished.

The agent no longer writes the record. It writes a result file at the path in
PACKN_RESULT_PATH and this script turns that file into the record.

EXIT CODES (the shell wrapper exits with this status)
-----------------------------------------------------
0  a run record was written (any status, a startup failure's record included)
1  the record could not be written

WHAT EVERY CHILD OF A RUN IS HANDED
-----------------------------------
PACKN_RUN_ID        the run's id: one lower-case uuid4, also the record's id
PACKN_SOURCE_MODE   the mode the run started in, so no child reads it again
PACKN_RESULT_PATH   where the agent writes its result file
PACKN_RUN_CONTEXT   the run-context file: which tickets have a draft cycle key

NOTHING CAN FAIL BEFORE A GUARD EXISTS
--------------------------------------
Every import at the top of this file is standard library. The project's own
modules are imported inside build_deps(), which main() calls inside a guard. If
an import or the building of the dependencies fails, main() still writes one
`failure` record (reason code agent_launch_failed, summary `startup:<exception
class>`); if even the database client cannot be loaded it prints one code line
and exits 1. It never ends as a traceback with no record.

NO MESSAGE TEXT
---------------
The record's error_summary, and every line this script logs, holds codes,
counts and exception class names. Never a draft body, a subject, a classifier
reason or an exception's message.

A mode runner lives in its own module and imports the shared types from
ticket_pipeline/run_types.py, never from this script (see that module for why).
This script keeps no per-run state at module level, so a runner module may
`import run_tickets` to reuse run_hubspot or the helpers below.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

# Not an import: the package root and scripts/ go on the path so that the lazy
# imports below resolve from a bare `python scripts/run_tickets.py`.
ROOT = Path(__file__).resolve().parent.parent
for _entry in (str(ROOT), str(ROOT / "scripts")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

ROUTINE = "tickets-process"
# The crontab line is found by this marker. The wrapper's filename must not change.
CRON_MARKER = "run_tickets.sh"

# The agent's model. One constant: it must equal DRAFT_MODEL in
# ticket_pipeline/llm.py (a test asserts it), so a manual run and a scheduled
# run draft with the same model and the CLI default can never silently apply.
AGENT_MODEL = "claude-sonnet-5"
AGENT_SKILL = "/packn-tickets"

EXIT_RECORDED = 0
EXIT_RECORD_UNWRITABLE = 1

# One runner per source mode: runner(ctx, deps) -> RunOutcome.
# load_runners() fills MODE_RUNNERS from RUNNER_SPECS. A spec is
# (module name, attribute); a module name of None means this module.
#   hubspot   here
#   os        Plans 93-16 / 93-17 add their line
#   shadow    Plan 93-18 adds its line
MODE_RUNNERS: dict[str, Callable[..., Any]] = {}
RUNNER_SPECS: dict[str, tuple[Optional[str], str]] = {
    "hubspot": (None, "run_hubspot"),
}
_RUNNER_LOAD_FAILURES: dict[str, str] = {}

# The agent's result file, as read_result_file reports it.
RESULT_MISSING = "missing"
RESULT_UNREADABLE = "unreadable"
RESULT_INCOMPLETE = "incomplete"
RESULT_COMPLETE = "complete"
RESULT_MAX_BYTES = 5 * 1024 * 1024
MAX_COUNT = 100_000
SUMMARY_MAX_CHARS = 500
QUOTE_WINDOW = 24

ROLLBACK_ENTRIES_UNREADABLE = "rollback_entries_unreadable"
SUMMARY_ROUTINE_PAUSED = "routine_paused"

_RE_EXPORTED = ("RunContext", "RunOutcome", "Deps")
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RUN_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Keys of a result-file ticket entry whose values are ids and codes, not text.
_NOT_TEXT_KEYS = frozenset(
    {"ref", "outcome", "failure_reason_code", "draft_id", "category", "form_topic", "priority"}
)


def _types() -> Any:
    """ticket_pipeline.run_types, imported on first use and never at load."""
    from ticket_pipeline import run_types

    return run_types


def __getattr__(name: str) -> Any:
    # RunContext, RunOutcome and Deps are re-exported from the leaf, lazily, so
    # that `run_tickets.RunOutcome is run_types.RunOutcome` without a top-level
    # project import.
    if name in _RE_EXPORTED:
        return getattr(_types(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _log(msg: str) -> None:
    print(f"[run_tickets] {msg}", file=sys.stderr)


def _where(exc: BaseException) -> str:
    """Where an exception was raised: file, line and function of the last few
    frames. Never the exception's message and never a line of source."""
    frames = traceback.extract_tb(exc.__traceback__)[-4:]
    return " < ".join(f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in reversed(frames))


def _join(*parts: Optional[str]) -> Optional[str]:
    return "; ".join(part for part in parts if part) or None


def new_run_id() -> str:
    """One lower-case uuid4 in the 8-4-4-4-12 form. Generated once per run: it
    is the run record's id, PACKN_RUN_ID, and the name of the scratch directory."""
    return str(uuid.uuid4())


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: Any) -> str:
    if not isinstance(moment, datetime):
        moment = _utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat()


# ===========================================================================
# CHILDREN
# ===========================================================================


def agent_command() -> list[str]:
    """The agent launch of `hubspot` mode: today's command, the pinned model,
    permissions skipped so a headless run never blocks, and no tool restriction."""
    return ["claude", "-p", AGENT_SKILL, "--model", AGENT_MODEL, "--dangerously-skip-permissions"]


def launch_agent(command: Sequence[str], env: Mapping[str, str]) -> int:
    """Start the agent, wait for it, return its exit code.

    Raises OSError when the command cannot be started at all (not found, not
    executable). The caller turns that into agent_launch_failed.
    """
    return subprocess.run(list(command), env=dict(env), cwd=str(ROOT), check=False).returncode


def run_engine_shadow_step(env: Mapping[str, str]) -> None:
    """The engine-shadow pipeline, as the shell wrapper ran it: one child, before
    the agent, over the same candidates. It is gated by its own setting
    (pipeline.shadow_enabled), writes comparison artifacts only and always exits 0."""
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_pipeline.py")],
        env=dict(env),
        cwd=str(ROOT),
        check=False,
    )


def child_environment(ctx: Any, deps: Any) -> dict[str, str]:
    """The environment of every child of this run: the base environment plus the
    four variables that carry the run's id, mode, result path and context path."""
    types = _types()
    base = os.environ if deps.environ is None else deps.environ
    env = {str(key): str(value) for key, value in base.items()}
    env[types.RUN_ID_ENV] = ctx.run_id
    env[types.SOURCE_MODE_ENV] = ctx.mode
    env[types.RESULT_PATH_ENV] = str(ctx.result_path)
    env[types.RUN_CONTEXT_ENV] = str(ctx.context_path)
    return env


# ===========================================================================
# THE RESULT FILE (written by the agent, parsed here — never trusted)
# ===========================================================================


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > MAX_COUNT:
        return None
    return value


def _ticket_texts(value: Any, out: list[str]) -> None:
    """Collect the free text of a result-file ticket entry: every string that is
    not an id or a code (draft body, classifier reason, action-item wording)."""
    if isinstance(value, str):
        text = " ".join(value.split())
        if len(text) >= 12:
            out.append(text)
    elif isinstance(value, dict):
        for key, inner in value.items():
            if key not in _NOT_TEXT_KEYS:
                _ticket_texts(inner, out)
    elif isinstance(value, list):
        for inner in value:
            _ticket_texts(inner, out)


def _quotes(summary: str, texts: list[str]) -> bool:
    """True when `summary` repeats a run of one of `texts`."""
    for text in texts:
        if len(text) <= QUOTE_WINDOW:
            if text in summary:
                return True
            continue
        for start in range(0, len(text) - QUOTE_WINDOW + 1):
            if text[start : start + QUOTE_WINDOW] in summary:
                return True
    return False


def _result_summary(loaded: dict) -> Optional[str]:
    """The summary a complete result contributes to the record: a count of failed
    tickets, their reason codes, and the agent's own error_summary — which is
    withheld when it repeats ticket text the same file holds."""
    parts: list[str] = []
    tickets = loaded.get("tickets")
    failed = 0
    codes: dict[str, int] = {}
    texts: list[str] = []
    for ticket in tickets if isinstance(tickets, list) else []:
        if not isinstance(ticket, dict):
            continue
        if ticket.get("outcome") == "failed":
            failed += 1
            code = ticket.get("failure_reason_code")
            if isinstance(code, str) and _CODE_RE.match(code):
                codes[code] = codes.get(code, 0) + 1
        _ticket_texts(ticket, texts)
    if failed:
        parts.append(f"failed_tickets={failed}")
    if codes:
        parts.append("codes=" + ",".join(f"{code}:{n}" for code, n in sorted(codes.items())))
    raw = loaded.get("error_summary")
    if raw is None or raw == "":
        pass
    elif not isinstance(raw, str):
        parts.append("result_summary_dropped")
    else:
        cleaned = " ".join(raw.split())[:SUMMARY_MAX_CHARS]
        parts.append("result_summary_withheld" if _quotes(cleaned, texts) else cleaned)
    return _join(*parts)


def read_result_file(path: Any, expected_run_id: Optional[str] = None) -> dict:
    """Read the agent's result file. Never raises.

    Returns
        {"state":   "missing" | "unreadable" | "incomplete" | "complete",
         "status":  one of the four run statuses, or None unless complete,
         "tickets_processed": int, "drafts_created": int,   0 when not readable
         "summary": codes / the agent's own summary, or None}

    complete    a JSON object with a known `status` and two whole-number counts
    incomplete  a JSON object missing one of those, or naming another run's id;
                its counts are kept when they are whole numbers
    unreadable  not JSON, not an object, too large, or not readable
    missing     no file

    The per-ticket entries are never copied anywhere: only their count of
    failures and their reason codes reach the summary.
    """
    blank = {
        "state": RESULT_MISSING,
        "status": None,
        "tickets_processed": 0,
        "drafts_created": 0,
        "summary": None,
    }
    try:
        target = Path(path)
        if not target.is_file():
            return blank
        if target.stat().st_size > RESULT_MAX_BYTES:
            return {**blank, "state": RESULT_UNREADABLE, "summary": "result_too_large"}
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, RecursionError):
        return {**blank, "state": RESULT_UNREADABLE, "summary": "result_unreadable"}
    if not isinstance(loaded, dict):
        return {**blank, "state": RESULT_UNREADABLE, "summary": "result_not_an_object"}

    types = _types()
    tickets = _count(loaded.get("tickets_processed"))
    drafts = _count(loaded.get("drafts_created"))
    kept = {"tickets_processed": tickets or 0, "drafts_created": drafts or 0}
    named = loaded.get("run_id")
    if named is not None and expected_run_id is not None and named != expected_run_id:
        return {**blank, "state": RESULT_INCOMPLETE, "summary": "result_run_id_mismatch"}
    problems: list[str] = []
    status = loaded.get("status")
    if not isinstance(status, str) or status not in types.RUN_STATUSES:
        problems.append("result_status_missing")
    if tickets is None or drafts is None:
        problems.append("result_counts_missing")
    if problems:
        return {"state": RESULT_INCOMPLETE, "status": None, **kept, "summary": ",".join(problems)}
    return {"state": RESULT_COMPLETE, "status": status, **kept, "summary": _result_summary(loaded)}


# ===========================================================================
# THE RUN CONTEXT (read by client.write_cron_draft through PACKN_RUN_CONTEXT)
# ===========================================================================

_CONTEXT_KEYS = ("os_ticket_id", "cycle_key", "signal_at", "signal_id")


def _context_entry(source_entry: Any = None) -> dict:
    entry = dict.fromkeys(_CONTEXT_KEYS)
    if isinstance(source_entry, dict):
        for key in _CONTEXT_KEYS:
            entry[key] = source_entry.get(key)
    return entry


def build_run_context(ctx: Any, deps: Any, reruns: Any) -> tuple[dict, Optional[str]]:
    """The run-context map of a HubSpot-source run, and a note code or None.

    First the rollback entries: every ticket that still holds a live pending
    draft written under a cycle key (by an `os` or `shadow` pass, or a rerun).
    This pass replaces that draft under `hubspot:<run id>` instead of adding a
    second pending one. Then one entry per pending rerun under
    `rerun:<request id>`; a rerun replaces a rollback entry for the same ticket
    and keeps its OS ticket id.

    The rollback read failing is not fatal: the note is
    `rollback_entries_unreadable`, and the run goes on with the rerun entries
    only — today's plain writes for every other ticket.
    """
    context: dict[str, dict] = {}
    note: Optional[str] = None
    failure: Optional[str] = None
    entries: Any = None
    try:
        entries = deps.rollback_cycle_entries(ctx.run_id)
    except Exception as e:  # noqa: BLE001 — one read; any failure is the same outcome
        failure = type(e).__name__
    if failure is None and not isinstance(entries, dict):
        failure = "not_an_object"
    if failure is not None:
        _log(f"{ROLLBACK_ENTRIES_UNREADABLE}:{failure}")
        note = ROLLBACK_ENTRIES_UNREADABLE
    else:
        for key, entry in entries.items():
            if isinstance(entry, dict):
                context[str(key)] = _context_entry(entry)

    skipped = 0
    for row in reruns if isinstance(reruns, (list, tuple)) else []:
        ticket_id = row.get("ticket_id") if isinstance(row, dict) else None
        request_id = row.get("id") if isinstance(row, dict) else None
        if ticket_id in (None, "") or request_id in (None, ""):
            skipped += 1
            continue
        entry = _context_entry(context.get(str(ticket_id)))
        entry["cycle_key"] = f"rerun:{request_id}"
        entry["signal_at"] = None
        entry["signal_id"] = None
        context[str(ticket_id)] = entry
    if skipped:
        _log(f"rerun_rows_without_a_ticket_or_an_id={skipped}")
    return context, note


def write_run_context(ctx: Any, context: dict) -> None:
    ctx.context_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.context_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")


# ===========================================================================
# LAUNCH THE AGENT AND TURN ITS RESULT INTO AN OUTCOME
# ===========================================================================


def launch_and_collect(
    ctx: Any,
    deps: Any,
    command: Sequence[str],
    env: Mapping[str, str],
    notes: Sequence[str] = (),
) -> Any:
    """Launch the agent, then read its result file into a RunOutcome.

        the launcher raises                         failure / agent_launch_failed
        non-zero exit, no result file               failure / agent_launch_failed
        exit 0, no result file                      failure / agent_exited_abnormally
        a result that is not complete (any exit)    failure / agent_exited_abnormally
                                                    (its tickets_processed kept when readable)
        a complete result                           the result's own status and counts

    `notes` are codes the caller wants on the record's summary.
    """
    types = _types()
    launch_failure: Optional[str] = None
    exit_code: Any = None
    try:
        exit_code = deps.launch_agent(list(command), env)
    except Exception as e:  # noqa: BLE001 — whatever stopped the launch, the code is the same
        launch_failure = type(e).__name__
    if launch_failure is not None:
        _log(f"{types.AGENT_LAUNCH_FAILED}:{launch_failure}")
        return types.RunOutcome(
            status=types.STATUS_FAILURE,
            error_summary=_join(f"{types.AGENT_LAUNCH_FAILED}:{launch_failure}", *notes),
            failure_reason_code=types.AGENT_LAUNCH_FAILED,
        )

    clean_exit = exit_code == 0 and not isinstance(exit_code, bool)
    exit_note = None if clean_exit else f"agent_exit_code={exit_code if isinstance(exit_code, int) else 'unknown'}"
    result = read_result_file(ctx.result_path, ctx.run_id)
    state = result["state"]
    if state == RESULT_MISSING and not clean_exit:
        _log(f"{types.AGENT_LAUNCH_FAILED}:{exit_note}")
        return types.RunOutcome(
            status=types.STATUS_FAILURE,
            error_summary=_join(types.AGENT_LAUNCH_FAILED, exit_note, *notes),
            failure_reason_code=types.AGENT_LAUNCH_FAILED,
        )
    if state != RESULT_COMPLETE:
        detail = "result_missing" if state == RESULT_MISSING else result["summary"]
        _log(f"{types.AGENT_EXITED_ABNORMALLY}:{detail}")
        return types.RunOutcome(
            status=types.STATUS_FAILURE,
            tickets_processed=result["tickets_processed"],
            drafts_created=result["drafts_created"],
            error_summary=_join(types.AGENT_EXITED_ABNORMALLY, detail, exit_note, *notes),
            failure_reason_code=types.AGENT_EXITED_ABNORMALLY,
        )
    return types.RunOutcome(
        status=result["status"],
        tickets_processed=result["tickets_processed"],
        drafts_created=result["drafts_created"],
        error_summary=_join(result["summary"], exit_note, *notes),
    )


# ===========================================================================
# THE `hubspot` RUNNER
# ===========================================================================


def run_hubspot(ctx: Any, deps: Any) -> Any:
    """`hubspot` mode: today's processing, with the record moved to the wrapper.

    The same pre-gate decision (`decide`, fail-open as it always was: unreadable
    settings, a missing token or a failed search still launch the agent), the
    same engine-shadow step before the agent, the same agent command and model.
    HubSpot calls are not counted in this mode: the agent makes them through
    MCP, which code cannot observe.
    """
    types = _types()
    decision = deps.decide(ctx.mode, deps)
    kind = decision.get("decision") if isinstance(decision, dict) else None
    if kind == "paused":
        return types.RunOutcome(status=types.STATUS_SKIPPED, error_summary=SUMMARY_ROUTINE_PAUSED)
    if kind == "no_work":
        return types.RunOutcome(status=types.STATUS_SUCCESS)
    # run_agent — and any answer this code does not know: this mode is fail-open.
    reruns = decision.get("reruns") if isinstance(decision, dict) else None
    context, note = build_run_context(ctx, deps, reruns)
    write_run_context(ctx, context)
    env = child_environment(ctx, deps)
    step_failure: Optional[str] = None
    try:
        deps.engine_shadow_step(env)
    except Exception as e:  # noqa: BLE001 — the engine shadow never fails a run
        step_failure = type(e).__name__
    if step_failure is not None:
        _log(f"engine_shadow_step_failed:{step_failure}")
    return launch_and_collect(ctx, deps, agent_command(), env, [note] if note else [])


# ===========================================================================
# THE REGISTRY
# ===========================================================================


def load_runners() -> dict[str, Callable[..., Any]]:
    """Fill MODE_RUNNERS from RUNNER_SPECS, importing each runner module lazily.

    Idempotent: a mode that is registered is left alone. A runner module that
    fails to import leaves its mode unregistered, and the class of the failure
    is kept for the record of the run that needed it — an import error never
    escapes from here. A mode that failed is tried again on the next call.
    """
    _RUNNER_LOAD_FAILURES.clear()
    for mode, (module_name, attribute) in list(RUNNER_SPECS.items()):
        if mode in MODE_RUNNERS:
            continue
        failure: Optional[str] = None
        runner: Any = None
        try:
            if module_name is None:
                runner = globals()[attribute]
            else:
                runner = getattr(importlib.import_module(module_name), attribute)
            if not callable(runner):
                raise TypeError("runner is not callable")
        except BaseException as e:  # noqa: BLE001 — an import can raise anything
            failure = type(e).__name__
        if failure is not None:
            _RUNNER_LOAD_FAILURES[mode] = failure
            _log(f"runner_import_failed:{mode}:{failure}")
            continue
        MODE_RUNNERS[mode] = runner
        _RUNNER_LOAD_FAILURES.pop(mode, None)
    return MODE_RUNNERS


# ===========================================================================
# ONE RUN
# ===========================================================================


def _failure(summary: str, code: Optional[str] = None) -> Any:
    types = _types()
    return types.RunOutcome(status=types.STATUS_FAILURE, error_summary=summary, failure_reason_code=code)


def _establish_mode(deps: Any) -> tuple[Optional[str], Any]:
    """(mode, None), or (None, a failure outcome). Never a default for an error."""
    from ticket_pipeline import source

    failure: Optional[str] = None
    raw: Any = None
    try:
        raw = deps.resolve_mode()
    except Exception as e:  # noqa: BLE001 — every failure of the read is one of two codes
        code = getattr(e, "code", None)
        failure = code if code in source.ERROR_CODES else source.SOURCE_UNREADABLE
        _log(f"source mode not established ({failure}; {type(e).__name__})")
    if failure is None and not (isinstance(raw, str) and raw in source.MODES):
        failure = source.SOURCE_UNKNOWN
        _log(f"source mode not established ({failure})")
    if failure is not None:
        return None, _failure(failure, failure)
    return raw, None


def _runner_for(mode: str) -> tuple[Any, Any]:
    """(runner, None), or (None, a failure outcome) for a mode with no runner."""
    from ticket_pipeline import source

    load_runners()
    runner = MODE_RUNNERS.get(mode)
    if runner is not None:
        return runner, None
    why = _RUNNER_LOAD_FAILURES.get(mode)
    summary = f"runner_import_failed:{mode}:{why}" if why else f"runner_not_registered:{mode}"
    return None, _failure(summary, source.SOURCE_UNKNOWN)


def _run_context_for(deps: Any, run_id: str, mode: str, started_at: Any, manual_request_id: Any) -> Any:
    types = _types()
    scratch = Path(deps.root).joinpath(*types.SCRATCH_PARTS, run_id)
    return types.RunContext(
        run_id=run_id,
        mode=mode,
        started_at=started_at,
        scratch_dir=scratch,
        result_path=scratch / types.RESULT_FILE_NAME,
        context_path=scratch / types.CONTEXT_FILE_NAME,
        manual_request_id=manual_request_id if isinstance(manual_request_id, str) else None,
    )


def _remove_scratch(scratch: Optional[Path]) -> Optional[str]:
    """Remove the run's scratch directory. Returns a note code when it is still there."""
    if scratch is None:
        return None
    failure: Optional[str] = None
    try:
        if scratch.exists():
            shutil.rmtree(scratch)
    except Exception as e:  # noqa: BLE001
        failure = type(e).__name__
    if failure is None:
        return None
    _log(f"scratch_not_removed:{failure}")
    return "scratch_not_removed"


def _link_manual_run(deps: Any, manual_request_id: Any, run_id: Optional[str]) -> None:
    if manual_request_id is None:
        return
    if not isinstance(manual_request_id, str) or not _UUID_RE.match(manual_request_id):
        _log("manual_request_id_invalid — the request is not linked to this run")
        return
    failure: Optional[str] = None
    try:
        deps.mark_manual_run_completed(manual_request_id, run_id)
    except Exception as e:  # noqa: BLE001 — the link is best effort; the record is written
        failure = type(e).__name__
    if failure is not None:
        _log(f"manual_run_link_failed:{failure}")


def run(deps: Any, manual_request_id: Optional[str] = None) -> int:
    """One run, from the mode read to the one record. Returns the exit code.

    Whatever is raised between the first line and the record — by the mode
    read, by the runner, by anything a runner calls — becomes a `failure`
    outcome that carries the exception's class name and nothing else of it.
    """
    types = _types()
    started_at: Any = None
    run_id: Optional[str] = None
    mode: Optional[str] = None
    outcome: Any = None
    scratch: Optional[Path] = None
    crash: Optional[str] = None
    try:
        started_at = deps.now()
        candidate_id = deps.new_run_id()
        if not isinstance(candidate_id, str) or not _RUN_ID_RE.match(candidate_id):
            raise ValueError("the run id is not a lower-case uuid4")
        run_id = candidate_id
        try:
            deps.report_routine_schedule(ROUTINE, CRON_MARKER)
        except Exception as e:  # noqa: BLE001 — fire and forget
            _log(f"schedule_report_failed:{type(e).__name__}")
        mode, outcome = _establish_mode(deps)
        runner: Any = None
        if outcome is None:
            runner, outcome = _runner_for(mode)
        if outcome is None:
            ctx = _run_context_for(deps, run_id, mode, started_at, manual_request_id)
            scratch = ctx.scratch_dir
            scratch.mkdir(parents=True, exist_ok=True)
            outcome = runner(ctx, deps)
    except BaseException as e:  # noqa: BLE001 — a run always reaches its record
        crash = type(e).__name__
        _log(f"runner_exception:{crash} at {_where(e)}")
    if crash is not None:
        outcome = _failure(f"runner_exception:{crash}")
    elif not isinstance(outcome, types.RunOutcome):
        outcome = _failure("runner_returned_no_outcome")
    elif outcome.status not in types.RUN_STATUSES:
        outcome = _failure("runner_outcome_status_invalid")

    scratch_note = _remove_scratch(scratch)
    if scratch_note:
        outcome.error_summary = _join(outcome.error_summary, scratch_note)

    record_failure: Optional[str] = None
    try:
        deps.write_run_record(
            ROUTINE,
            outcome.status,
            outcome.error_summary,
            _count(outcome.tickets_processed) or 0,
            _count(outcome.drafts_created) or 0,
            _iso(started_at),
            _iso(_safe_now(deps)),
            run_id=run_id,
            source_mode=mode,
            failure_reason_code=outcome.failure_reason_code,
            hubspot_calls=outcome.hubspot_calls,
        )
    except BaseException as e:  # noqa: BLE001 — the exit code says the record is missing
        record_failure = type(e).__name__
    if record_failure is not None:
        _log(f"run_record_unwritable:{record_failure}")
        _link_manual_run(deps, manual_request_id, None)
        return EXIT_RECORD_UNWRITABLE
    _log(f"run {run_id} recorded: status={outcome.status} mode={mode} tickets={outcome.tickets_processed}")
    _link_manual_run(deps, manual_request_id, run_id)
    return EXIT_RECORDED


def _safe_now(deps: Any) -> Any:
    try:
        return deps.now()
    except Exception:  # noqa: BLE001
        return _utc_now()


# ===========================================================================
# STARTUP
# ===========================================================================


def build_deps() -> Any:
    """The real dependencies. The ONE place the project's modules are imported
    for a real run; main() calls it inside a guard."""
    import pregate_tickets
    from packn_os_hubspot_client import client
    from ticket_pipeline import hubspot_api, run_types, source

    return run_types.Deps(
        root=ROOT,
        state_path=pregate_tickets.STATE_PATH,
        seen_path=ROOT / "config" / ".action_items_forwarded.json",
        now=_utc_now,
        new_run_id=new_run_id,
        resolve_mode=source.resolve_mode,
        decide=pregate_tickets.decide,
        load_settings=pregate_tickets._load_settings,
        read_hubspot_token=hubspot_api.read_token,
        engine_shadow_step=run_engine_shadow_step,
        launch_agent=launch_agent,
        report_routine_schedule=client.report_routine_schedule,
        read_routine_enabled=client.read_routine_enabled,
        read_pending_rerun_requests=client.read_pending_rerun_requests,
        rollback_cycle_entries=client.rollback_cycle_entries,
        write_run_record=client.write_run_record,
        mark_manual_run_completed=client.mark_manual_run_completed,
    )


def record_startup_failure(failure_class: str, manual_request_id: Optional[str] = None) -> int:
    """The orchestrator could not start. Write one `failure` record for a fresh
    run id, importing only the database client; return 0 when it was written.

    The reason code is agent_launch_failed: nothing was launched and nothing was
    processed. The summary is `startup:<exception class>`, which tells it from a
    launch that failed later. No source mode: it was never read.

    When even this cannot be done, one code line goes to stderr and the return
    is 1, so the cron line itself exits non-zero.
    """
    run_id = new_run_id()
    moment = _iso(_utc_now())
    second: Optional[str] = None
    client: Any = None
    try:
        from packn_os_hubspot_client import client  # the one import of this path

        client.write_run_record(
            ROUTINE,
            "failure",
            f"startup:{failure_class}",
            0,
            0,
            moment,
            moment,
            run_id=run_id,
            source_mode=None,
            failure_reason_code="agent_launch_failed",
        )
    except BaseException as e:  # noqa: BLE001
        second = type(e).__name__
    if second is not None:
        print(
            f"[run_tickets] startup_record_unwritable:{failure_class}:{second}",
            file=sys.stderr,
        )
        return EXIT_RECORD_UNWRITABLE
    if isinstance(manual_request_id, str) and _UUID_RE.match(manual_request_id):
        try:
            client.mark_manual_run_completed(manual_request_id, run_id)
        except Exception:  # noqa: BLE001 — best effort
            pass
    return EXIT_RECORDED


def _close_pool() -> None:
    try:
        from packn_os_hubspot_client.db import close_pool

        close_pool()
    except Exception:  # noqa: BLE001
        pass


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One tickets-process run: read the source mode, run it, write one run record."
    )
    parser.add_argument(
        "--manual-request",
        default=None,
        metavar="UUID",
        help="the automation_run_requests id of a pressed Run now; the run is linked to it",
    )
    # An argument this script does not know never stops a run.
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        _log(f"unknown_arguments_ignored={len(unknown)}")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    deps: Any = None
    startup_failure: Optional[str] = None
    try:
        deps = build_deps()
    except BaseException as e:  # noqa: BLE001 — nothing may fail before the record
        startup_failure = type(e).__name__
        _log(f"startup_failed:{startup_failure} at {_where(e)}")
    try:
        if startup_failure is not None:
            return record_startup_failure(startup_failure, args.manual_request)
        crash: Optional[str] = None
        try:
            return run(deps, manual_request_id=args.manual_request)
        except BaseException as e:  # noqa: BLE001 — run() is written not to raise
            crash = type(e).__name__
        _log(f"orchestrator_crashed:{crash}")
        return EXIT_RECORD_UNWRITABLE
    finally:
        _close_pool()


if __name__ == "__main__":
    sys.exit(main())
