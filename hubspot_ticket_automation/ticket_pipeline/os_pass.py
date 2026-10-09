"""`os` mode, first half: the cron's work list is Pack'N OS's (Phase 93, plan 16).

When the owner's source switch says `os`, the orchestrator
(scripts/run_tickets.py) hands the run to `run_os` here. In order:

    1. the routine gate            paused -> one `skipped` record, nothing asked
    2. the list, from the OS       `limit` = the settings' per_run_cap, clamped
                                   into 1 to the client's maximum
    3. pending operator reruns     each fetched from the OS by the id it names
    4. one bundle entry per ticket the context, keys and backfill source all
                                   come from ticket_pipeline/os_bundle.py
    5. the engine shadow           handed the same contexts, when settings say so
    6. the agent                   launched with the HubSpot MCP server's tools
                                   removed; it reads the bundle, writes a result
    7. the result                  reconciled against the bundle's refs by CODE

FAIL CLOSED
-----------
A list that cannot be had (a timeout, an error answer, a refused signature, a
secret missing on either side, a body the contract does not describe) ends the
run at step 2 with ONE failure outcome that names its cause. No HubSpot request
is made, no agent is launched and nothing falls back to HubSpot as the source.
The orchestrator writes that outcome as the run's one record. The next run asks
the OS again, so a refused run followed by an accepted one needs no repair.

HUBSPOT IN THIS MODE
--------------------
Code makes every HubSpot request, through ticket_pipeline/hubspot_api.py, and
each is counted. The only read is `os_bundle.fetch_thread`: one `email_thread`
call (two requests) for a ticket that has a HubSpot id. A ticket with none
costs nothing. The token comes from `deps.read_hubspot_token()` and is asked
for only when a ticket needs it; with no token the linked tickets are built
from the OS thread (`thread_source: "os_fallback"`) and the run goes on.

The cron's local dedup file is not read here: selection is the OS's.

WHAT THE TOOL RESTRICTION DOES AND DOES NOT DO (runbook tag D-10-RESIDUAL)
--------------------------------------------------------------------------
`--disallowedTools "mcp__hubspot"` removes the HubSpot MCP server's tools from
the agent. It closes the TOOL path, not the SHELL path: the agent still has a
shell, and the HubSpot token file is on the droplet. So this module does not
claim HubSpot is unreachable for the agent. What it delivers is structural:
code does every HubSpot request and counts it, the `os`-mode skill holds no
HubSpot instruction, and the run record carries the counts. A request the agent
chose to make from its shell would not be counted. Closing that means removing
the token, which is Phase 94.

WHAT THIS PLAN STOPS AT
-----------------------
A parsed result and a run outcome. Writing the drafts, the write-back to the
OS, the action items and the number backfill is Plan 93-17's finalizer, which
plugs into `run_os` between `collect` and the call-count snapshot and reads
`OsRunOutcome.bundle_entries`, `.result` and `.tickets`. Until then
`drafts_created` is 0: no draft row is written by this module.

LOGGING
-------
Ids, codes, counts and exception class names. Never a subject, a body, a name,
an address or an exception's message.

The run's types come from ticket_pipeline/run_types.py. The orchestrator script
is never imported at module load and no name is ever taken from it: three of
its helper functions are looked up when a run needs them (`_orchestrator`).
"""
from __future__ import annotations

import copy
import importlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import hubspot_api as hs
from . import os_bundle, run_types
from .run_types import Deps, RunContext, RunOutcome

ROUTINE = run_types.ROUTINE

# The agent's model: the same pin as run_tickets.AGENT_MODEL and llm.DRAFT_MODEL
# (a test asserts the three are equal), so both sources draft with one model.
AGENT_MODEL = "claude-sonnet-5"
OS_AGENT_SKILL = "/packn-tickets-os"
# A bare MCP server name removes every tool that server provides.
DISALLOWED_TOOLS = "mcp__hubspot"
OS_AGENT_COMMAND = (
    "claude",
    "-p",
    OS_AGENT_SKILL,
    "--model",
    AGENT_MODEL,
    "--dangerously-skip-permissions",
    "--disallowedTools",
    DISALLOWED_TOOLS,
)

# Where the agent reads the bundle: <scratch dir>/bundle.json, beside its result.
BUNDLE_PATH_ENV = "PACKN_BUNDLE_PATH"
BUNDLE_FILE_NAME = "bundle.json"
DEFAULT_PER_RUN_CAP = 25
CONTRACT_VERSION = "1"

# Why the list could not be had: the run record's failure reason code.
LIST_FAILURE_CODES = ("list_timeout", "list_error", "signature_rejected", "secret_missing")
LIST_ERROR = "list_error"

# One ticket's pass.
OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
AGENT_NO_RESULT = "agent_no_result"
AGENT_TICKET_ERROR = "agent_ticket_error"
BUNDLE_BUILD_FAILED = "bundle_build_failed"
OTHER = "other"
# The codes the skill may write for a ticket it failed; anything else is `other`.
AGENT_TICKET_CODES = (AGENT_TICKET_ERROR, OTHER)

# Codes that reach the run record's summary.
SUMMARY_ROUTINE_PAUSED = "routine_paused"
SETTINGS_UNREADABLE = "settings_unreadable"
THREAD_FETCH_FAILED = "thread_fetch_failed"
HUBSPOT_TOKEN_MISSING = "hubspot_token_missing"
RERUN_REQUESTS_UNREADABLE = "rerun_requests_unreadable"
RERUN_ROWS_INVALID = "rerun_rows_invalid"
RERUN_REF_INVALID = "rerun_ref_invalid"
RERUN_NOT_FOUND = "rerun_not_found"
RERUN_FETCH_FAILED = "rerun_fetch_failed"
RESULT_UNKNOWN_REFS = "result_unknown_refs"
RESULT_DUPLICATE_REFS = "result_duplicate_refs"
BUNDLE_UNWRITABLE = "bundle_unwritable"

# The keys of one bundle entry, in the order they are written.
ENTRY_KEYS = (
    "ref",
    "os_ticket_id",
    "hubspot_ticket_id",
    "cycle_key",
    "is_rerun",
    "rerun_request_ids",
    "signal",
    "thread_source",
    "backfill_source",
    "ticket_context",
)

# A ticket ref the OS route accepts in its path: an OS uuid or a HubSpot id.
_REF_RE = re.compile(r"[A-Za-z0-9-]{1,64}")


@dataclass
class OsRunOutcome(RunOutcome):
    """The run's outcome, plus what the persistence step (Plan 93-17) needs.

    `tickets`         one record per selected ticket, in selection order:
                      {"ref", "os_ticket_id", "hubspot_ticket_id", "cycle_key",
                       "is_rerun", "outcome": "completed" | "failed",
                       "failure_reason_code": None or a code,
                       "result": the agent's entry for the ticket, or None}
    `bundle_entries`  the entries written to the bundle, in bundle order
    `result`          the agent's parsed result object when it was complete,
                      else None

    It holds ticket text (contexts, draft bodies). The orchestrator reads the
    six fields of RunOutcome only, and nothing here is ever logged.
    """

    tickets: list = field(default_factory=list)
    bundle_entries: list = field(default_factory=list)
    result: Optional[dict] = None


def _log(msg: str) -> None:
    print(f"[os_pass] {msg}", file=sys.stderr)


class _Notes:
    """Codes for the run record's summary: counted ones and plain flags."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._flags: list[str] = []

    def count(self, code: str, n: int = 1) -> None:
        self._counts[code] = self._counts.get(code, 0) + n

    def flag(self, code: str) -> None:
        if code not in self._flags:
            self._flags.append(code)

    def summary(self) -> Optional[str]:
        parts = [f"{code}={n}" for code, n in self._counts.items()] + self._flags
        return "; ".join(parts) or None


def _join(*parts: Optional[str]) -> Optional[str]:
    return "; ".join(part for part in parts if part) or None


def _text(value: Any) -> Optional[str]:
    """A string that is not blank, as given; anything else is None."""
    return value if isinstance(value, str) and value.strip() else None


def _id_text(value: Any) -> Optional[str]:
    """An id as text (a database uuid arrives as an object); None when absent."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _real_api() -> Any:
    """The signed client for the OS tickets routes, imported on first use."""
    return importlib.import_module("packn_os_hubspot_client.tickets_api")


def _orchestrator() -> Any:
    """scripts/run_tickets.py, for three helper FUNCTIONS: the child environment,
    the launch-and-map rule and (inside it) the result file's reader. Looked up
    when a run needs them, never at module load, and never for a type: the types
    are run_types', so it does not matter which copy of the script answers."""
    return importlib.import_module("run_tickets")


def agent_command() -> list[str]:
    """The agent launch of `os` mode: the `os` skill, the pinned model,
    permissions skipped so a headless run never blocks, and the HubSpot MCP
    server's tools removed."""
    return list(OS_AGENT_COMMAND)


def list_limit(settings: Any) -> int:
    """The `limit` the list is asked with: per_run_cap, clamped into 1 to the
    client's MAX_LIST_LIMIT, so a cap above the OS's maximum is never sent as
    given. Settings that are not a mapping, or a cap that is not a number, are
    an error: this mode does not guess a cap."""
    if not isinstance(settings, dict):
        raise TypeError("settings are a mapping")
    cap = settings.get("per_run_cap", DEFAULT_PER_RUN_CAP)
    if isinstance(cap, bool) or not isinstance(cap, (int, float)):
        raise ValueError("per_run_cap is a number")
    return max(1, min(int(_real_api().MAX_LIST_LIMIT), int(cap)))


def _failed(summary: Optional[str], code: Optional[str] = None, **extra: Any) -> OsRunOutcome:
    return OsRunOutcome(
        status=run_types.STATUS_FAILURE, error_summary=summary, failure_reason_code=code, **extra
    )


# ===========================================================================
# THE LIST (fail closed)
# ===========================================================================


def _list_payloads(answer: Any) -> tuple[list, Optional[str]]:
    """The ticket payloads of a list answer, or ([], why it is not one)."""
    if not isinstance(answer, dict):
        return [], "list_not_an_object"
    version = answer.get("contract_version")
    if version is not None and str(version) != CONTRACT_VERSION:
        return [], "contract_version"
    tickets = answer.get("tickets")
    if not isinstance(tickets, list):
        return [], "tickets_not_a_list"
    for item in tickets:
        if not (isinstance(item, dict) and _text(item.get("id"))):
            return [], "ticket_without_an_id"
    return tickets, None


def _fetch_list(api: Any, limit: int) -> tuple[list, Optional[OsRunOutcome]]:
    """(the picks, None), or ([], the failure outcome). Never a fallback."""
    code: Optional[str] = None
    detail: Optional[str] = None
    answer: Any = None
    try:
        answer = api.fetch_unprocessed(limit)
    except Exception as e:  # noqa: BLE001 — whatever stopped the list, the run stops
        raised = getattr(e, "code", None)
        if raised in LIST_FAILURE_CODES:
            code = raised
        else:
            code, detail = LIST_ERROR, type(e).__name__
    payloads: list = []
    if code is None:
        payloads, problem = _list_payloads(answer)
        if problem is not None:
            code, detail = LIST_ERROR, problem
    if code is not None:
        summary = f"{code}:{detail}" if detail else code
        _log(f"list not available ({summary}): no ticket is processed and HubSpot is not asked")
        return [], _failed(summary, code)

    picks: list[dict] = []
    seen: set[str] = set()
    for payload in payloads[:limit]:
        if payload["id"] in seen:
            continue
        seen.add(payload["id"])
        picks.append({"payload": payload, "requests": []})
    _log(f"list answered: tickets={len(picks)} limit={limit}")
    return picks, None


# ===========================================================================
# RERUNS (fetched from the OS, added on top of the cap)
# ===========================================================================


def _fetch_rerun(api: Any, ref: str, notes: _Notes) -> Optional[dict]:
    """The payload of a rerun's ticket, from the OS's single-ticket route, or
    None with a code. The ticket is never looked for in HubSpot."""
    if not _REF_RE.fullmatch(ref):
        notes.count(RERUN_REF_INVALID)
        _log(f"rerun skipped: code={RERUN_REF_INVALID}")
        return None
    failure: Optional[str] = None
    invalid = False
    answer: Any = None
    try:
        answer = api.fetch_ticket(ref)
    except ValueError:
        invalid = True
    except Exception as e:  # noqa: BLE001 — one rerun's fetch never stops the run
        raised = getattr(e, "code", None)
        failure = raised if raised in LIST_FAILURE_CODES else type(e).__name__
    if invalid:
        notes.count(RERUN_REF_INVALID)
        _log(f"rerun skipped: code={RERUN_REF_INVALID}")
        return None
    if failure is not None:
        notes.count(RERUN_FETCH_FAILED)
        _log(f"rerun skipped: ticket={ref} code={RERUN_FETCH_FAILED} cause={failure}")
        return None
    if answer is None:
        notes.count(RERUN_NOT_FOUND)
        _log(f"rerun skipped: ticket={ref} code={RERUN_NOT_FOUND}")
        return None
    payload = answer.get("ticket") if isinstance(answer, dict) else None
    if not (isinstance(payload, dict) and _text(payload.get("id"))):
        notes.count(RERUN_FETCH_FAILED)
        _log(f"rerun skipped: ticket={ref} code={RERUN_FETCH_FAILED} cause=no_ticket_in_answer")
        return None
    return payload


def _pick_named(picks: list, ref: str) -> Optional[dict]:
    for pick in picks:
        payload = pick["payload"]
        if payload["id"] == ref or _text(payload.get("hubspot_ticket_id")) == ref:
            return pick
    return None


def _fold_reruns(deps: Deps, api: Any, picks: list, notes: _Notes) -> None:
    """Add each pending rerun to `picks`: into the pick of its ticket when the
    list (or an earlier rerun) already holds that ticket, else as a new pick
    after the list's. One pick per distinct OS ticket, whatever id named it."""
    failure: Optional[str] = None
    rows: Any = None
    try:
        rows = deps.read_pending_rerun_requests(ROUTINE)
    except Exception as e:  # noqa: BLE001 — the list is still processed
        failure = type(e).__name__
    if failure is not None:
        notes.flag(RERUN_REQUESTS_UNREADABLE)
        _log(f"{RERUN_REQUESTS_UNREADABLE}:{failure}")
        return

    fetched: dict[str, Optional[dict]] = {}
    for row in rows if isinstance(rows, (list, tuple)) else []:
        request_id = _id_text(row.get("id")) if isinstance(row, dict) else None
        ref = _id_text(row.get("ticket_id")) if isinstance(row, dict) else None
        if request_id is None or ref is None:
            notes.count(RERUN_ROWS_INVALID)
            continue
        pick = _pick_named(picks, ref)
        if pick is None:
            if ref not in fetched:
                fetched[ref] = _fetch_rerun(api, ref, notes)
            payload = fetched[ref]
            if payload is None:
                continue
            pick = _pick_named(picks, payload["id"])
            if pick is None:
                pick = {"payload": payload, "requests": []}
                picks.append(pick)
        if request_id not in pick["requests"]:
            pick["requests"].append(request_id)


# ===========================================================================
# THE BUNDLE ENTRIES
# ===========================================================================


def _signal(payload: dict) -> Optional[dict]:
    signal = payload.get("signal")
    if not isinstance(signal, dict):
        return None
    return {"kind": signal.get("kind"), "id": signal.get("id"), "at": signal.get("at")}


def _ticket_record(entry: dict, outcome: str, code: Optional[str], result: Any) -> dict:
    return {
        "ref": entry.get("ref"),
        "os_ticket_id": entry.get("os_ticket_id"),
        "hubspot_ticket_id": entry.get("hubspot_ticket_id"),
        "cycle_key": entry.get("cycle_key"),
        "is_rerun": bool(entry.get("is_rerun")),
        "outcome": outcome,
        "failure_reason_code": code,
        "result": result,
    }


def _thread(deps: Deps, hubspot_id: str, settings: dict, token_box: dict, notes: _Notes) -> list:
    """The linked ticket's HubSpot emails: the one HubSpot read of this mode.

    The token is asked for once per run, through the injected reader, the first
    time a ticket needs it. No token, or a fetch that cannot be made, answers []
    and the caller builds the context from the OS thread."""
    if "token" not in token_box:
        token: Any = None
        failure: Optional[str] = None
        try:
            token = deps.read_hubspot_token()
        except Exception as e:  # noqa: BLE001 — an unreadable token is a missing one
            failure = type(e).__name__
        if failure is not None:
            _log(f"hubspot token not read: error={failure}")
        token_box["token"] = _text(token)
        if token_box["token"] is None:
            notes.flag(HUBSPOT_TOKEN_MISSING)
            _log(f"code={HUBSPOT_TOKEN_MISSING}: linked tickets are built from the OS thread")
    if token_box["token"] is None:
        return []
    failure = None
    emails: Any = []
    try:
        emails = os_bundle.fetch_thread(hubspot_id, settings, token_box["token"])
    except Exception as e:  # noqa: BLE001 — THREAD-FALLBACK: the OS thread is used
        failure = type(e).__name__
    if failure is not None:
        _log(f"thread fetch failed: ticket={hubspot_id} error={failure}")
        return []
    return emails if isinstance(emails, list) else []


def _build_entries(
    deps: Deps, picks: list, settings: dict, origin: str, notes: _Notes
) -> list[tuple[str, dict]]:
    """One slot per pick, in order: ("entry", the bundle entry) or ("failed",
    the ticket's record) when no entry could be built for it."""
    slots: list[tuple[str, dict]] = []
    token_box: dict = {}
    refs: set[str] = set()
    for pick in picks:
        payload, requests = pick["payload"], pick["requests"]
        os_id = payload["id"]
        hubspot_id = _text(payload.get("hubspot_ticket_id"))
        entry: Optional[dict] = None
        problem: Optional[str] = None
        try:
            ref = os_bundle.draft_key(payload)
            cycle = os_bundle.cycle_key(payload, requests[0] if requests else None)
            if ref in refs:
                raise ValueError("a ref names one bundle entry")
            emails = _thread(deps, hubspot_id, settings, token_box, notes) if hubspot_id else []
            entry = {
                "ref": ref,
                "os_ticket_id": os_id,
                "hubspot_ticket_id": hubspot_id,
                "cycle_key": cycle,
                "is_rerun": bool(requests),
                "rerun_request_ids": list(requests),
                "signal": _signal(payload),
                "thread_source": os_bundle.thread_source(payload, emails),
                "backfill_source": os_bundle.backfill_source(
                    payload, os_bundle.first_email_html(payload, emails)
                ),
                "ticket_context": os_bundle.build_context(payload, emails, settings, origin),
            }
        except Exception as e:  # noqa: BLE001 — one ticket never stops the others
            problem = type(e).__name__
        if entry is None:
            notes.count(BUNDLE_BUILD_FAILED)
            _log(f"ticket not bundled: ticket={os_id} code={BUNDLE_BUILD_FAILED} error={problem}")
            stub = {
                "ref": hubspot_id or os_id,
                "os_ticket_id": os_id,
                "hubspot_ticket_id": hubspot_id,
                "is_rerun": bool(requests),
            }
            slots.append(("failed", _ticket_record(stub, OUTCOME_FAILED, BUNDLE_BUILD_FAILED, None)))
            continue
        refs.add(entry["ref"])
        if hubspot_id and entry["thread_source"] == os_bundle.THREAD_OS_FALLBACK:
            notes.count(THREAD_FETCH_FAILED)
        slots.append(("entry", entry))
    return slots


def _write_run_context(ctx: RunContext, entries: list) -> None:
    """The run-context file (PACKN_RUN_CONTEXT): each bundle ticket's OS id and
    cycle key under its ref, in the shape the draft helper reads."""
    context = {}
    for entry in entries:
        signal = entry["signal"] or {}
        context[entry["ref"]] = {
            "os_ticket_id": entry["os_ticket_id"],
            "cycle_key": entry["cycle_key"],
            "signal_at": signal.get("at"),
            "signal_id": signal.get("id"),
        }
    ctx.context_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.context_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")


# ===========================================================================
# THE ENGINE SHADOW
# ===========================================================================


def run_engine_shadow_on_contexts(contexts: list) -> None:
    """The engine-shadow pipeline over ready-built contexts. In `os` mode it
    asks HubSpot nothing: it has no ticket to look for and no thread to fetch."""
    pipeline_run = importlib.import_module("ticket_pipeline.run")
    pipeline_run.run_shadow(mode="os", contexts=contexts)


def _engine_shadow(deps: Deps, settings: dict, entries: list) -> None:
    pipeline_cfg = settings.get("pipeline")
    if not (isinstance(pipeline_cfg, dict) and pipeline_cfg.get("shadow_enabled")):
        return
    step = deps.engine_shadow_contexts_step or run_engine_shadow_on_contexts
    failure: Optional[str] = None
    try:
        # copies: the engine shadow cannot change what the agent is handed
        step([copy.deepcopy(entry["ticket_context"]) for entry in entries])
    except Exception as e:  # noqa: BLE001 — the engine shadow never fails a run
        failure = type(e).__name__
    if failure is not None:
        _log(f"engine_shadow_step_failed:{failure}")


# ===========================================================================
# THE RESULT, AGAINST THE BUNDLE
# ===========================================================================


def _read_result(path: Any) -> Optional[dict]:
    """The agent's result object. Called only after the orchestrator's reader
    judged the file complete (a known status, whole-number counts, this run's
    id, under the size cap)."""
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return loaded if isinstance(loaded, dict) else None


def reconcile(entries: list, result: Any) -> tuple[list, dict]:
    """One record per bundle entry, in bundle order, and the counts to note.

    A bundle ticket the result does not name is `failed` with agent_no_result.
    One the result marks `completed` is completed. Anything else is `failed`,
    with the agent's code when it is one of AGENT_TICKET_CODES, else `other`.
    A result entry naming a ref the bundle does not hold is ignored (the agent
    cannot add a ticket), and a second entry for one ref is ignored; both are
    counted."""
    by_ref: dict[str, dict] = {}
    duplicates = 0
    items = result.get("tickets") if isinstance(result, dict) else None
    for item in items if isinstance(items, list) else []:
        ref = item.get("ref") if isinstance(item, dict) else None
        if not isinstance(ref, str):
            continue
        if ref in by_ref:
            duplicates += 1
            continue
        by_ref[ref] = item

    tickets: list[dict] = []
    missing = 0
    for entry in entries:
        item = by_ref.get(entry.get("ref"))
        if item is None:
            missing += 1
            tickets.append(_ticket_record(entry, OUTCOME_FAILED, AGENT_NO_RESULT, None))
        elif item.get("outcome") == OUTCOME_COMPLETED:
            tickets.append(_ticket_record(entry, OUTCOME_COMPLETED, None, item))
        else:
            code = item.get("failure_reason_code") if item.get("outcome") == OUTCOME_FAILED else None
            kept = code if isinstance(code, str) and code in AGENT_TICKET_CODES else OTHER
            tickets.append(_ticket_record(entry, OUTCOME_FAILED, kept, item))

    counts: dict[str, int] = {}
    if missing:
        counts[AGENT_NO_RESULT] = missing
    bundle_refs = {entry.get("ref") for entry in entries}
    unknown = sum(1 for ref in by_ref if ref not in bundle_refs)
    if unknown:
        counts[RESULT_UNKNOWN_REFS] = unknown
    if duplicates:
        counts[RESULT_DUPLICATE_REFS] = duplicates
    return tickets, counts


# ===========================================================================
# ONE `os` RUN
# ===========================================================================


def collect(ctx: RunContext, deps: Deps) -> OsRunOutcome:
    """Everything up to a parsed, reconciled result. Returns the outcome with
    the bundle entries, the agent's result and the per-ticket list on it.

    The HubSpot call counts are NOT taken here: `run_os` takes them after
    whatever follows this function has made its own requests."""
    notes = _Notes()

    # Fail-closed routine gate, as in every mode (False on a database error too).
    if not deps.read_routine_enabled(ROUTINE):
        _log("routine paused (or gate unreachable): nothing is asked of the OS")
        return OsRunOutcome(status=run_types.STATUS_SKIPPED, error_summary=SUMMARY_ROUTINE_PAUSED)

    settings: Any = None
    limit = 0
    failure: Optional[str] = None
    try:
        settings = deps.load_settings()
        limit = list_limit(settings)
    except Exception as e:  # noqa: BLE001 — no settings, no cap: this mode does not guess
        failure = type(e).__name__
    if failure is not None:
        _log(f"{SETTINGS_UNREADABLE}:{failure}")
        return _failed(f"{SETTINGS_UNREADABLE}:{failure}")

    api = deps.os_api if deps.os_api is not None else _real_api()
    picks, refused = _fetch_list(api, limit)
    if refused is not None:
        return refused

    _fold_reruns(deps, api, picks, notes)
    if not picks:
        return OsRunOutcome(status=run_types.STATUS_SUCCESS, error_summary=notes.summary())

    origin: Any = None
    code: Optional[str] = None
    try:
        origin = (deps.read_os_origin or _real_api().read_os_origin)()
    except Exception as e:  # noqa: BLE001 — no origin, no ticket link: stop before any request
        raised = getattr(e, "code", None)
        code = raised if raised in LIST_FAILURE_CODES else LIST_ERROR
    if code is not None:
        _log(f"os origin not readable: code={code}")
        return _failed(f"{code}:os_origin_unreadable", code)

    slots = _build_entries(deps, picks, settings, origin, notes)
    entries = [item for kind, item in slots if kind == "entry"]
    if not entries:
        return _failed(notes.summary(), tickets=[item for _kind, item in slots])

    bundle_path = ctx.scratch_dir / BUNDLE_FILE_NAME
    failure = None
    try:
        os_bundle.write_bundle(bundle_path, ctx.run_id, entries)
        _write_run_context(ctx, entries)
    except Exception as e:  # noqa: BLE001 — no bundle, no agent
        failure = type(e).__name__
    if failure is not None:
        _log(f"{BUNDLE_UNWRITABLE}:{failure}")
        return _failed(
            _join(f"{BUNDLE_UNWRITABLE}:{failure}", notes.summary()),
            bundle_entries=entries,
            tickets=[
                item
                if kind == "failed"
                else _ticket_record(item, OUTCOME_FAILED, BUNDLE_BUILD_FAILED, None)
                for kind, item in slots
            ],
        )
    _log(f"bundle written: tickets={len(entries)} run={ctx.run_id}")

    _engine_shadow(deps, settings, entries)

    orchestrator = _orchestrator()
    env = orchestrator.child_environment(ctx, deps)
    env[BUNDLE_PATH_ENV] = str(bundle_path)
    launched = orchestrator.launch_and_collect(ctx, deps, agent_command(), env)

    # A run whose agent did not leave a complete result has processed nothing:
    # the agent persists nothing in this mode, so there is nothing to keep.
    agent_failed = launched.failure_reason_code in (
        run_types.AGENT_LAUNCH_FAILED,
        run_types.AGENT_EXITED_ABNORMALLY,
    )
    result = None if agent_failed else _read_result(ctx.result_path)
    reconciled, counts = reconcile(entries, result)
    if not agent_failed:
        for counted, n in counts.items():
            notes.count(counted, n)

    by_ref = {record["ref"]: record for record in reconciled}
    tickets = [item if kind == "failed" else by_ref[item["ref"]] for kind, item in slots]
    completed = sum(1 for ticket in tickets if ticket["outcome"] == OUTCOME_COMPLETED)
    if agent_failed or completed == 0:
        status = run_types.STATUS_FAILURE
    elif completed == len(tickets):
        status = run_types.STATUS_SUCCESS
    else:
        status = run_types.STATUS_PARTIAL
    _log(f"result reconciled: tickets={len(tickets)} completed={completed} status={status}")
    return OsRunOutcome(
        status=status,
        tickets_processed=completed,
        drafts_created=0,  # no draft row is written here; Plan 93-17's finalizer sets it
        error_summary=_join(launched.error_summary, notes.summary()),
        failure_reason_code=launched.failure_reason_code if agent_failed else None,
        tickets=tickets,
        bundle_entries=entries,
        result=result,
    )


def run_os(ctx: RunContext, deps: Deps) -> RunOutcome:
    """The `os` runner: `runner(ctx, deps) -> RunOutcome`, registered in
    run_tickets.RUNNER_SPECS. The orchestrator writes the one run record from
    what this returns; this function writes none.

    The call counter is zeroed first and read last, so the counts on the record
    are every HubSpot request code made during the run. Plan 93-17's finalizer
    goes between `collect` and the snapshot, so its number backfill is counted."""
    hs.reset_calls()
    outcome = collect(ctx, deps)
    outcome.hubspot_calls = hs.snapshot_calls()
    return outcome
