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

PERSISTENCE (Plan 93-17)
------------------------
The agent persists nothing in this mode. `finalize_os_run` does, between
`collect` and the call-count snapshot, per bundle ticket and in this order:

    the draft        `client.replace_pending_draft`: the HubSpot id (or, with
                     none, the OS id) as the text key, the OS ticket id set at
                     insert, the cycle key in the row's prompt version
    the write-back   `completed` once the draft is in place, echoing the
                     consumed signal as the list supplied it; `failed` with a
                     code of the OS's closed list for a ticket that has no draft

A ticket is never reported `completed` without its draft. Every answer the
write-back route can give has one action (`post_ticket_writeback`). The only
answers after which a ticket is neither marked nor counted are those of an OS
that cannot be reached or refuses the signature: the draft stays, the run is
`partial` with the code, and the next run's list fetch fails closed or lists
the ticket again, where the draft write is a same-cycle replay.

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
import math
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import timezone
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

# --- persistence (Plan 93-17): the contract's constants -----------------------
# tests/test_contract_fixtures.py pins each of these to Pack'N OS's fixture
# docs/contracts/fixtures/tickets-api-classification.v1.json.
#
# The prompt version a write-back reports: the real one, never the
# cycle-suffixed version a draft row carries.
BASE_PROMPT_VERSION = "v3.3.0"
SOURCE_MODE_OS = "os"
DRAFT_WRITE_FAILED = "draft_write_failed"
# The OS's closed list of per-ticket failure codes (CRON_PASS_FAILURE_CODES).
PASS_FAILURE_CODES = (
    AGENT_NO_RESULT,
    AGENT_TICKET_ERROR,
    BUNDLE_BUILD_FAILED,
    THREAD_FETCH_FAILED,
    DRAFT_WRITE_FAILED,
    OTHER,
)
REASON_MAX_CHARS = 1000
FORM_TOPIC_MAX_CHARS = 200
PRIORITIES = ("urgent", "normal")
# The contract's fifteen category slugs. A copy, on purpose: the classifier's
# own set lives in a module that imports the model SDK, which a run that only
# persists must not need. A test compares this with the contract fixture AND
# with the classifier's set, so the copy cannot drift.
CATEGORY_SLUGS = (
    "wismo_tracking",
    "shipping_delay",
    "damaged_goods",
    "mispack_wrong_item",
    "inventory_discrepancy",
    "inbound_asn_receiving",
    "returns_rma",
    "integration_system",
    "billing_invoice",
    "sla_escalation",
    "account_onboarding",
    "kitting_project",
    "address_recipient",
    "retailer_compliance",
    "other_unclassified",
)
# One retry of a write-back the OS could not be asked, after this pause.
WRITEBACK_RETRY_PAUSE_S = 2.0

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
    deps: Deps,
    picks: list,
    settings: dict,
    origin: str,
    notes: _Notes,
    token_box: Optional[dict] = None,
    unbundled: Optional[list] = None,
) -> list[tuple[str, dict]]:
    """One slot per pick, in order: ("entry", the bundle entry) or ("failed",
    the ticket's record) when no entry could be built for it.

    `token_box` holds the run's one HubSpot token read, so the finalizer's
    number backfill does not read the token a second time. `unbundled` gains
    {"os_ticket_id", "signal"} for each ticket with no entry, so the finalizer
    can report it to the OS."""
    slots: list[tuple[str, dict]] = []
    token_box = token_box if token_box is not None else {}
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
            if unbundled is not None:
                unbundled.append({"os_ticket_id": os_id, "signal": _signal(payload)})
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
# PERSISTENCE (Plan 93-17)
# ===========================================================================

# The pause before the one retry. A module name so a test never waits.
_sleep = time.sleep

# How one ticket's write-back ended.
WB_PROCESSED = "processed"  # a completed pass the OS holds (200; or 409: it already held one)
WB_COUNTED = "counted"  # a failed pass the OS counted toward parking
WB_GONE = "gone"  # the OS will not offer the ticket: 404, or no client signal is left
WB_REJECTED = "rejected"  # a failed pass the OS refused: logged, nothing more
WB_UNRECORDED = "unrecorded"  # the OS could not be asked: the run is `partial`
WB_NO_SIGNAL = "no_signal"  # the entry has no client signal: there is nothing to echo

# Codes the finalizer adds to the run record's summary.
WRITEBACK_PREFIX = "writeback_"  # + the client's code, for a write-back not delivered
WRITEBACK_CONFLICT = "writeback_conflict"
WRITEBACK_REJECTED = "writeback_rejected"
WRITEBACK_TICKET_GONE = "writeback_ticket_gone"
WRITEBACK_SIGNAL_GONE = "writeback_signal_gone"
CLASSIFICATION_UNUSABLE = "classification_unusable"
DRAFT_BODY_MISSING = "draft_body_missing"
RERUN_MARK_FAILED = "rerun_mark_failed"
FINALIZE_TICKET_ERROR = "finalize_ticket_error"
FINALIZE_SETTINGS_UNREADABLE = "finalize_settings_unreadable"
FINALIZE_FAILED = "finalize_failed"
# A refused signature and a secret the OS does not hold are not retried: the
# same request would be refused again.
NO_RETRY_CODES = ("signature_rejected", "secret_missing")


@dataclass
class FinalizeSummary:
    """What the finalizer did, in ids, codes and counts. It holds no ticket text.

    `tickets_processed`  tickets whose draft is in place after this pass (a new
                         row or a same-cycle replay). In a dry run: how many
                         would have been drafted.
    `drafts_created`     new draft rows only; a replay is not one. 0 in a dry run.
    `codes`              code -> count, for the run record's summary
    `partial`            a write-back could not be delivered at all: the run is
                         `partial` whatever else happened
    `dry_run`, `would`   settings said dry run; `would` counts what was not done
    `tickets`            one record per bundle ticket, in bundle order:
                         {"ref", "os_ticket_id", "hubspot_ticket_id", "drafted",
                          "draft_id", "created", "writeback": one of the WB_* or None}
    """

    tickets_processed: int = 0
    drafts_created: int = 0
    codes: dict = field(default_factory=dict)
    partial: bool = False
    dry_run: bool = False
    would: dict = field(default_factory=dict)
    tickets: list = field(default_factory=list)

    def note(self, code: str, n: int = 1) -> None:
        self.codes[code] = self.codes.get(code, 0) + n

    def would_do(self, what: str, n: int = 1) -> None:
        self.would[what] = self.would.get(what, 0) + n


def summary_text(summary: FinalizeSummary) -> Optional[str]:
    """The finalizer's part of the run record's summary: codes and counts."""
    parts: list[str] = []
    if summary.dry_run:
        parts.append("dry_run")
        parts.extend(f"would_{what}={n}" for what, n in summary.would.items())
    parts.extend(f"{code}={n}" for code, n in summary.codes.items())
    return "; ".join(parts) or None


def _client() -> Any:
    """The database helper, imported on first use: a run of another mode, and a
    run that persists nothing, never loads it."""
    return importlib.import_module("packn_os_hubspot_client.client")


def _iso_ms(moment: Any) -> str:
    """An instant as `YYYY-MM-DDTHH:MM:SS.mmmZ` (UTC)."""
    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _storable(text: str) -> str:
    """Text the OS can store and a request can carry: no NUL (the database
    refuses one) and no lone surrogate (it has no UTF-8 form)."""
    return text.replace(chr(0), "").encode("utf-8", "replace").decode("utf-8")


def normalise_classification(raw: Any) -> Optional[dict]:
    """The agent's classification, made to fit the write-back contract, or None
    when it cannot be.

    The classifier's own schema bounds neither the reason nor the confidence,
    so the cron clamps to exactly what the route enforces: `reason` cut to
    REASON_MAX_CHARS (a missing one is ""), `form_topic` cut to
    FORM_TOPIC_MAX_CHARS or None, `confidence` clamped into 0 to 1, `priority`
    lowered to one of the two words. Lengths are code points, as the OS counts.

    None (the ticket is then a FAILED ticket, with no draft): `raw` is not an
    object, the category is not one of the contract's slugs, the confidence is
    not a finite number, the priority is not one of the two words, or the
    reason is something other than text.

    Returns a new dict with exactly the five keys; `raw` is not changed.
    """
    if not isinstance(raw, dict):
        return None
    category = raw.get("category")
    if not isinstance(category, str) or category not in CATEGORY_SLUGS:
        return None
    confidence = raw.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    if not math.isfinite(confidence):
        return None
    priority = raw.get("priority")
    if not isinstance(priority, str) or priority.strip().lower() not in PRIORITIES:
        return None
    reason = raw.get("reason")
    if reason is None:
        reason = ""
    if not isinstance(reason, str):
        return None
    form_topic = raw.get("form_topic")
    topic = _storable(form_topic).strip()[:FORM_TOPIC_MAX_CHARS] if isinstance(form_topic, str) else ""
    return {
        "category": category,
        "confidence": min(1, max(0, confidence)),
        "priority": priority.strip().lower(),
        "reason": _storable(reason)[:REASON_MAX_CHARS],
        "form_topic": topic or None,
    }


def map_failure_code(code: Any) -> str:
    """A per-ticket failure code as the OS accepts it: one of the six, or
    `other` for anything else the agent or the runner named."""
    return code if isinstance(code, str) and code in PASS_FAILURE_CODES else OTHER


def _echo_signal(entry: Any) -> Optional[dict]:
    """The consumed signal as the write-back echoes it: its id and its instant,
    character for character as the OS supplied them, and no `kind`. None when
    the entry carries no usable signal."""
    signal = entry.get("signal") if isinstance(entry, dict) else None
    if not isinstance(signal, dict):
        return None
    signal_id, at = signal.get("id"), signal.get("at")
    if not (_text(signal_id) and _text(at)):
        return None
    return {"id": signal_id, "at": at}


def build_classification_payload(
    entry: dict, ticket_result: dict, run_id: str, source_mode: str
) -> dict:
    """The body of one write-back (`POST /api/tickets/{id}/classification`).

    `entry`          a bundle entry; only its `signal` is read
    `ticket_result`  {"outcome": "completed", "classification": {...}} or
                     {"outcome": "failed", "failure_reason_code": <code>}
    `source_mode`    "os", or the source-shadow runner's mode word

    The prompt version and the model are not arguments: they are
    BASE_PROMPT_VERSION and AGENT_MODEL, so a caller cannot send the
    cycle-suffixed draft version or the small classify model. The
    classification is normalised here too and the code mapped, so the body is
    inside the contract whoever built `ticket_result`.

    ValueError: the entry has no signal to echo, or a completed result's
    classification cannot be made valid.
    """
    signal = _echo_signal(entry)
    if signal is None:
        raise ValueError("a write-back echoes a client signal and this entry has none")
    completed = ticket_result.get("outcome") == OUTCOME_COMPLETED
    payload: dict = {
        "contract_version": CONTRACT_VERSION,
        "run_id": run_id,
        "outcome": OUTCOME_COMPLETED if completed else OUTCOME_FAILED,
        "source_mode": source_mode,
        "signal": signal,
        "prompt_version": BASE_PROMPT_VERSION,
        "model": AGENT_MODEL,
    }
    if completed:
        classification = normalise_classification(ticket_result.get("classification"))
        if classification is None:
            raise ValueError("a completed write-back needs a classification the contract accepts")
        for key in ("category", "confidence", "priority", "reason", "form_topic"):
            payload[key] = classification[key]
    else:
        payload["failure_reason_code"] = map_failure_code(ticket_result.get("failure_reason_code"))
    return payload


def build_draft_snapshot(entry: dict, ticket_result: dict, now_iso: str) -> dict:
    """The ticket fields the draft was written against (the draft row's
    snapshot): the keys the `hubspot` skill's posting step builds, from the
    bundle entry's context. `category` and `captured_at` are the two the draft
    writer requires."""
    context = entry.get("ticket_context")
    if not isinstance(context, dict):
        raise TypeError("a bundle entry carries its ticket context")
    classification = ticket_result.get("classification")
    form_fields = context.get("form_fields")
    form_fields = form_fields if isinstance(form_fields, dict) else {}
    contact = context.get("contact")
    contact = contact if isinstance(contact, dict) else {}
    return {
        "subject": context.get("subject"),
        "body": context.get("latest_customer_message"),
        "category": classification["category"],
        "topic_of_ticket": context.get("topic_of_ticket"),
        "source_type": context.get("source_type"),
        "contact": {"name": contact.get("name"), "email": contact.get("email")},
        "custom_properties": {
            "order_number": form_fields.get("order_number"),
            "tracking_number": form_fields.get("tracking_number"),
        },
        "captured_at": now_iso,
    }


def _send_pass(
    api: Any, os_ticket_id: Any, payload: dict, pause: Any
) -> tuple[Optional[dict], Optional[str]]:
    """One write-back, with its one retry: (the OS's verdict, None), or
    (None, the code) when the OS could not be asked.

    A refused signature and a missing secret are not retried. Any other
    failure (a 5xx, a gateway 503, a timeout, a connection error, an exception
    that is not the client's) is retried ONCE after the pause."""
    for attempt in (1, 2):
        verdict: Any = None
        failure: Optional[str] = None
        cause = ""
        try:
            verdict = api.post_classification(os_ticket_id, payload)
        except Exception as e:  # noqa: BLE001 — whatever stopped it, it is a code
            raised = getattr(e, "code", None)
            failure = raised if raised in LIST_FAILURE_CODES else LIST_ERROR
            cause = type(e).__name__
        if failure is None:
            if isinstance(verdict, dict) and isinstance(verdict.get("status"), int):
                return verdict, None
            failure, cause = LIST_ERROR, "verdict_not_understood"
        if failure in NO_RETRY_CODES or attempt == 2:
            _log(f"write-back not delivered: ticket={os_ticket_id} code={failure} cause={cause}")
            return None, failure
        _log(f"write-back retry in {WRITEBACK_RETRY_PAUSE_S}s: ticket={os_ticket_id} code={failure}")
        pause(WRITEBACK_RETRY_PAUSE_S)
    return None, LIST_ERROR  # not reached


def _reread_signal(api: Any, os_ticket_id: Any) -> tuple[Optional[dict], Optional[str]]:
    """The ticket's current client signal, from the single-ticket route:
    (the signal or None, None), or (None, the code) when the OS could not be asked."""
    answer: Any = None
    failure: Optional[str] = None
    try:
        answer = api.fetch_ticket(os_ticket_id)
    except Exception as e:  # noqa: BLE001
        raised = getattr(e, "code", None)
        failure = raised if raised in LIST_FAILURE_CODES else LIST_ERROR
    if failure is not None:
        return None, failure
    payload = answer.get("ticket") if isinstance(answer, dict) else None
    return _echo_signal(payload), None


def post_ticket_writeback(
    api: Any, entry: dict, ticket_result: dict, run_id: str, source_mode: str, *, sleep: Any = None
) -> dict:
    """Tell the OS how one ticket's pass ended, and act on its answer. This
    function IS the write-back status table; it never raises for an answer.

    `api`            `post_classification(os_ticket_id, payload)` and
                     `fetch_ticket(ref)`: the signed client, or a fake
    `entry`          read: `os_ticket_id`, `signal`
    `ticket_result`  as for build_classification_payload
    `sleep`          the pause before the one retry (None = the module's)

    Returns {"end": one of the WB_* words, "codes": [codes to add to the run's
    summary]}.

    A `completed` write-back:
        200                  -> processed
        409                  -> processed, code writeback_conflict (the OS already
                                holds a completed pass for this signal and
                                prompt version)
        404                  -> gone, code writeback_ticket_gone
        422                  -> the ticket is read again; with a signal the
                                write-back is resent ONCE with it (200 or 409
                                ends it); with no ticket or no signal -> gone,
                                code writeback_signal_gone
        400, 413, or any other answer (and any answer but 200 / 409 to the
        resend)              -> code writeback_rejected, then ONE `failed`
                                write-back with `other`, echoing the signal
                                last sent -> counted
    A `failed` write-back:
        200                  -> counted
        404                  -> gone, code writeback_ticket_gone
        422                  -> one re-read and one resend, as above
        anything else        -> rejected, code writeback_rejected
    Either:
        401, a 503 naming the secret           -> unrecorded, no retry
        another 503, a 5xx, a transport error  -> ONE retry after the pause,
                                                  then unrecorded
      with the code `writeback_<the client's code>`.
    An entry with no signal -> no_signal: nothing is sent.
    """
    pause = sleep if sleep is not None else _sleep
    os_ticket_id = entry.get("os_ticket_id")
    codes: list[str] = []

    def ended(end: str, code: Optional[str] = None) -> dict:
        if code is not None:
            codes.append(code)
            _log(f"write-back: ticket={os_ticket_id} end={end} code={code}")
        return {"end": end, "codes": codes}

    if _echo_signal(entry) is None:
        return ended(WB_NO_SIGNAL)
    completed = ticket_result.get("outcome") == OUTCOME_COMPLETED
    done = WB_PROCESSED if completed else WB_COUNTED

    sent_as = entry
    verdict, failure = _send_pass(
        api, os_ticket_id, build_classification_payload(sent_as, ticket_result, run_id, source_mode), pause
    )
    if failure is not None:
        return ended(WB_UNRECORDED, WRITEBACK_PREFIX + failure)
    status = verdict["status"]
    resent = False
    if status == 422:
        signal, failure = _reread_signal(api, os_ticket_id)
        if failure is not None:
            return ended(WB_UNRECORDED, WRITEBACK_PREFIX + failure)
        if signal is None:
            return ended(WB_GONE, WRITEBACK_SIGNAL_GONE)
        sent_as = dict(entry, signal=signal)
        verdict, failure = _send_pass(
            api, os_ticket_id, build_classification_payload(sent_as, ticket_result, run_id, source_mode), pause
        )
        if failure is not None:
            return ended(WB_UNRECORDED, WRITEBACK_PREFIX + failure)
        status = verdict["status"]
        resent = True

    if 200 <= status < 300:
        return ended(done)
    if completed and status == 409:
        return ended(WB_PROCESSED, WRITEBACK_CONFLICT)
    if status == 404 and not resent:
        return ended(WB_GONE, WRITEBACK_TICKET_GONE)
    if not completed:
        return ended(WB_REJECTED, WRITEBACK_REJECTED)

    # A completed pass the OS refused. One failed pass, so the attempt counts
    # toward parking and the ticket cannot be re-drafted without limit.
    codes.append(WRITEBACK_REJECTED)
    _log(f"write-back: ticket={os_ticket_id} code={WRITEBACK_REJECTED} status={status}")
    fallback = build_classification_payload(
        sent_as, {"outcome": OUTCOME_FAILED, "failure_reason_code": OTHER}, run_id, source_mode
    )
    verdict, failure = _send_pass(api, os_ticket_id, fallback, pause)
    if failure is not None:
        return ended(WB_UNRECORDED, WRITEBACK_PREFIX + failure)
    return ended(WB_COUNTED if 200 <= verdict["status"] < 300 else WB_REJECTED)


def backfill_properties(entry: dict) -> dict:
    return {}


def post_ticket_action_items(entry: dict, items: Any, seen_path: Any, *, dry_run: bool = False) -> dict:
    return {"end": "none", "items": 0, "codes": []}


@dataclass
class _Pass:
    """What the per-ticket steps of one finalize share."""

    ctx: RunContext
    deps: Deps
    api: Any
    summary: FinalizeSummary
    settings: dict
    token_box: dict
    sleep: Any


def _write_back(run: _Pass, entry: dict, ticket: Optional[dict], ticket_result: dict) -> None:
    """One ticket's write-back, folded into the summary. In a dry run: counted."""
    summary = run.summary
    if summary.dry_run:
        if _echo_signal(entry) is not None:
            summary.would_do("writebacks")
        return
    ended = post_ticket_writeback(
        run.api, entry, ticket_result, run.ctx.run_id, SOURCE_MODE_OS, sleep=run.sleep
    )
    if ticket is not None:
        ticket["writeback"] = ended["end"]
    for code in ended["codes"]:
        summary.note(code)
    if ended["end"] == WB_UNRECORDED:
        summary.partial = True


def _failed_result(code: Any) -> dict:
    return {"outcome": OUTCOME_FAILED, "failure_reason_code": map_failure_code(code)}


def _mark_reruns(run: _Pass, entry: dict, draft_id: str) -> None:
    """Each operator rerun this entry answers is marked processed, with the
    draft it produced. Idempotent on the OS's side; a failure is a code and
    the request is answered again by the next run's same-cycle replay."""
    for request_id in entry.get("rerun_request_ids") or []:
        failure: Optional[str] = None
        try:
            _client().mark_rerun_processed(request_id, draft_id)
        except Exception as e:  # noqa: BLE001
            failure = type(e).__name__
        if failure is not None:
            run.summary.note(RERUN_MARK_FAILED)
            _log(f"rerun not marked: request={request_id} code={RERUN_MARK_FAILED} error={failure}")


def _finalize_ticket(run: _Pass, entry: dict, record: dict, ticket: dict) -> None:
    """One bundle ticket: the draft, THEN the write-back, then its reruns.

    A ticket is never reported `completed` without its draft: a classification
    that cannot be made valid, a missing draft text and a draft write that
    raises each make it a FAILED ticket, reported with a `failed` write-back."""
    summary = run.summary
    if record["outcome"] != OUTCOME_COMPLETED:
        _write_back(run, entry, ticket, _failed_result(record["failure_reason_code"]))
        return

    item = record["result"] if isinstance(record["result"], dict) else {}
    classification = normalise_classification(item.get("classification"))
    body = item.get("draft_body")
    unusable: Optional[str] = None
    if classification is None:
        unusable = CLASSIFICATION_UNUSABLE
    elif not (isinstance(body, str) and body.strip()):
        unusable = DRAFT_BODY_MISSING
    if unusable is not None:
        summary.note(unusable)
        _log(f"ticket not persisted: ticket={entry.get('os_ticket_id')} code={unusable}")
        _write_back(run, entry, ticket, _failed_result(AGENT_TICKET_ERROR))
        return

    ticket_result = {"outcome": OUTCOME_COMPLETED, "classification": classification}
    if summary.dry_run:
        summary.would_do("drafts")
        summary.tickets_processed += 1
        _write_back(run, entry, ticket, ticket_result)
        return

    draft_id: Optional[str] = None
    created = False
    failure: Optional[str] = None
    try:
        written = _client().replace_pending_draft(
            draft_key=entry["ref"],
            os_ticket_id=entry["os_ticket_id"],
            routine_name=ROUTINE,
            draft_body=body,
            model=AGENT_MODEL,
            base_prompt_version=BASE_PROMPT_VERSION,
            cycle_key=entry["cycle_key"],
            hubspot_ticket_snapshot=build_draft_snapshot(
                entry, ticket_result, _iso_ms(run.ctx.started_at)
            ),
        )
        draft_id = str(written["draft_id"])
        created = bool(written.get("created"))
    except Exception as e:  # noqa: BLE001 — no draft: the ticket is a failed one
        failure = type(e).__name__
    if failure is not None or draft_id is None:
        summary.note(DRAFT_WRITE_FAILED)
        _log(
            f"draft not written: ticket={entry.get('os_ticket_id')} "
            f"code={DRAFT_WRITE_FAILED} error={failure}"
        )
        _write_back(run, entry, ticket, _failed_result(DRAFT_WRITE_FAILED))
        return

    ticket.update(drafted=True, draft_id=draft_id, created=created)
    summary.tickets_processed += 1
    if created:
        summary.drafts_created += 1
    _write_back(run, entry, ticket, ticket_result)
    _mark_reruns(run, entry, draft_id)


def finalize_os_run(
    ctx: RunContext,
    deps: Deps,
    bundle_entries: list,
    result: Any,
    *,
    settings: Any = None,
    token_box: Optional[dict] = None,
    unbundled: Any = (),
    sleep: Any = None,
) -> FinalizeSummary:
    """Persist what the agent produced. `run_os` calls this after `collect`,
    with the outcome's bundle entries and the agent's parsed result.

    Per bundle ticket, in bundle order, each inside its own guard so that one
    ticket's error cannot stop the next:

        1. the draft, through the atomic replace (`client.replace_pending_draft`),
           keyed by the entry's ref, OS ticket id and cycle key;
        2. the write-back (`post_ticket_writeback`): `completed` once the draft
           is in place, else `failed` with a code of the OS's six;
        3. each operator rerun the entry answers is marked processed.

    `result` None (the agent did not launch, or left no complete result) is a
    RUN-level failure: no draft exists to write and no ticket is reported
    failed, so an agent outage does not count toward parking for every listed
    ticket (runbook tag RUN-FAILURE-NOT-COUNTED). The run record is already a
    `failure` with its code and the next run lists the same tickets.

    `unbundled`: the tickets no bundle entry could be built for, as
    {"os_ticket_id", "signal"}; each with a signal gets a `failed` write-back
    with `bundle_build_failed`, whatever the agent did.

    `settings` None = `deps.load_settings()`. With `dry_run` true in settings
    nothing is written or sent and the summary counts what would have been.

    Logs ids, codes, counts and exception class names only.
    """
    summary = FinalizeSummary()
    if settings is None:
        failure: Optional[str] = None
        try:
            settings = deps.load_settings()
        except Exception as e:  # noqa: BLE001
            failure = type(e).__name__
        if failure is not None:
            _log(f"{FINALIZE_SETTINGS_UNREADABLE}:{failure}")
    if not isinstance(settings, dict):
        # no settings, no dry-run answer: nothing is persisted
        summary.note(FINALIZE_SETTINGS_UNREADABLE)
        return summary
    summary.dry_run = bool(settings.get("dry_run"))

    api = deps.os_api if deps.os_api is not None else _real_api()
    run = _Pass(ctx, deps, api, summary, settings, token_box if token_box is not None else {}, sleep)

    for item in unbundled or ():
        failure = None
        try:
            if isinstance(item, dict) and _echo_signal(item) is not None:
                _write_back(run, item, None, _failed_result(BUNDLE_BUILD_FAILED))
        except Exception as e:  # noqa: BLE001
            failure = type(e).__name__
        if failure is not None:
            summary.note(FINALIZE_TICKET_ERROR)
            _log(f"{FINALIZE_TICKET_ERROR}:{failure}")

    if not isinstance(result, dict):
        if bundle_entries:
            _log(
                f"no complete result: tickets={len(bundle_entries)} are not persisted "
                "and not counted (a run-level failure)"
            )
        return summary

    records, _counts = reconcile(bundle_entries, result)
    for entry, record in zip(bundle_entries, records):
        ticket = {
            "ref": entry.get("ref"),
            "os_ticket_id": entry.get("os_ticket_id"),
            "hubspot_ticket_id": entry.get("hubspot_ticket_id"),
            "drafted": False,
            "draft_id": None,
            "created": False,
            "writeback": None,
        }
        summary.tickets.append(ticket)
        failure = None
        try:
            _finalize_ticket(run, entry, record, ticket)
        except Exception as e:  # noqa: BLE001 — one ticket never stops the others
            failure = type(e).__name__
        if failure is not None:
            summary.note(FINALIZE_TICKET_ERROR)
            _log(f"{FINALIZE_TICKET_ERROR}:{failure} ticket={entry.get('os_ticket_id')}")

    _log(
        f"finalized: tickets={len(summary.tickets)} processed={summary.tickets_processed} "
        f"drafts_created={summary.drafts_created} dry_run={summary.dry_run} "
        f"partial={summary.partial}"
    )
    return summary


def _persist(ctx: RunContext, deps: Deps, outcome: OsRunOutcome, carry: dict) -> None:
    """Run the finalizer and fold its summary into the run's outcome.

    The finalizer never stops a run from having its one record: if it raises,
    the outcome keeps what `collect` built, gains a code, and a `success`
    becomes `partial`."""
    summary: Optional[FinalizeSummary] = None
    failure: Optional[str] = None
    try:
        summary = finalize_os_run(
            ctx,
            deps,
            outcome.bundle_entries,
            outcome.result,
            settings=carry.get("settings"),
            token_box=carry.get("token_box"),
            unbundled=carry.get("unbundled") or (),
        )
    except Exception as e:  # noqa: BLE001
        failure = type(e).__name__
    if summary is None:
        _log(f"{FINALIZE_FAILED}:{failure}")
        outcome.error_summary = _join(outcome.error_summary, f"{FINALIZE_FAILED}:{failure}")
        if outcome.status == run_types.STATUS_SUCCESS:
            outcome.status = run_types.STATUS_PARTIAL
        return

    outcome.error_summary = _join(outcome.error_summary, summary_text(summary))
    if outcome.result is None:
        return  # a run-level failure stays the failure `collect` made it
    outcome.tickets_processed = summary.tickets_processed
    outcome.drafts_created = summary.drafts_created
    if summary.tickets_processed == 0:
        outcome.status = run_types.STATUS_FAILURE
    elif summary.tickets_processed == len(outcome.tickets) and not summary.partial:
        outcome.status = run_types.STATUS_SUCCESS
    else:
        outcome.status = run_types.STATUS_PARTIAL


# ===========================================================================
# ONE `os` RUN
# ===========================================================================


def collect(ctx: RunContext, deps: Deps, carry: Optional[dict] = None) -> OsRunOutcome:
    """Everything up to a parsed, reconciled result. Returns the outcome with
    the bundle entries, the agent's result and the per-ticket list on it.

    `carry`, when given, is filled with what the persistence step reuses rather
    than reads again: "settings" (the run's settings), "token_box" (the run's
    one HubSpot token read) and "unbundled" (the tickets no entry could be
    built for). None of it belongs on the outcome, which holds no secret.

    The HubSpot call counts are NOT taken here: `run_os` takes them after
    whatever follows this function has made its own requests."""
    notes = _Notes()
    carry = carry if carry is not None else {}
    carry.setdefault("token_box", {})
    carry.setdefault("unbundled", [])

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
    carry["settings"] = settings

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

    slots = _build_entries(
        deps, picks, settings, origin, notes, carry["token_box"], carry["unbundled"]
    )
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
        drafts_created=0,  # no draft row is written here; the finalizer sets it
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

    The call counter is zeroed first and read LAST, after the finalizer, so the
    counts on the record are every HubSpot request code made during the run,
    the finalizer's number backfill included.

    The finalizer runs whenever there is something it could report: a bundle
    entry, or a ticket no entry could be built for. A run that failed closed
    before the bundle (no list, paused, nothing listed) never reaches it."""
    hs.reset_calls()
    carry: dict = {}
    outcome = collect(ctx, deps, carry)
    if outcome.bundle_entries or carry.get("unbundled"):
        _persist(ctx, deps, outcome, carry)
    outcome.hubspot_calls = hs.snapshot_calls()
    return outcome
