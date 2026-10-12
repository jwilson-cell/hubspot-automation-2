#!/usr/bin/env python3
"""Deterministic pre-gate for the tickets-process cron (2026-07-06).

WHY THIS EXISTS
---------------
Every cron tick previously launched a full headless Claude Code run
(`claude -p /packn-tickets`) even when there was nothing to do. The agentic
run pays a large fixed token cost (system prompt + MCP schemas + SKILL.md +
CLAUDE.md) before it discovers "zero tickets". This script answers the single
question "is there possibly work this tick?" with plain API calls, so
run_tickets.sh can skip the LLM entirely on idle ticks. That makes a 30-minute
cadence cost the same as the old 12h cadence: LLM spend scales with ticket
volume, not with how often the cron fires.

WHAT IT CHECKS (mirrors SKILL.md Steps 0-1, read-only)
------------------------------------------------------
0. The ticket source mode (Phase 93) — `hubspot`, `shadow` or `os`, from
   ticket_pipeline/source.py: the value the orchestrator exported, else one
   read of the Pack'N OS setting. This script serves the HubSpot source only
   (`hubspot` and `shadow`). In `os` mode it makes no HubSpot call and skips.
   A mode that cannot be established (`source_unreadable`, `source_unknown`)
   stops the tick — see FAIL-OPEN, below, for why that is not a fail-open case.
1. report_routine_schedule — Step 0a's crontab report, done here so Pack'N OS
   sees the cron fired even on ticks where the agent never launches.
2. read_routine_enabled — Step 0b's gate, same fail-closed posture (missing
   row / DB unreachable => treated as paused => skip, write a 'skipped' run
   record). CONTEXT D-01.
3. read_pending_rerun_requests — any pending rerun means the agent MUST run
   (operator-initiated work, D-09), regardless of the HubSpot poll result.
4. HubSpot tickets/search with the SAME filters as SKILL.md Step 1
   (stage IN active_stages, hs_last_message_from_visitor when configured,
   hs_lastmodifieddate > state.last_run_at, limit per_run_cap), then the
   Step 2a-pre fingerprint dedupe approximated via hs_lastmodifieddate:
   a ticket whose fingerprint already covers its current lastmodified is
   not new work.

FAIL-OPEN BY DESIGN
-------------------
This is an optimization, not a gatekeeper. ANY ambiguity — settings unread,
token missing, HubSpot 5xx, unexpected exception — exits 0 ("launch the
agent") so a pre-gate bug can never silently stop ticket processing. The only
paths that skip the agent are the two affirmative ones: routine paused
(fail-closed, matching the skill's own gate) and a clean zero-candidate poll.
Both write an automation_runs record so the Pack'N OS status panel and the
"no run record" alert stay accurate.

That posture belongs to the HubSpot source. The source mode itself is NOT
fail-open (Phase 93, D-01): "launch the agent on any doubt" would mean "read
HubSpot on any doubt", which is choosing a mode. A mode read that fails, or a
stored value this code does not know, skips the agent and writes a 'failure'
record naming the code. `os` mode skips with a 'skipped' record.

ONE DECISION, TWO CALLERS (Phase 93, plan 15)
---------------------------------------------
The decision itself is `decide(mode, deps)`: it returns run_agent / no_work /
paused with a reason and writes nothing. The scheduled tick and Run now reach
it through scripts/run_tickets.py, the one entry point, which writes the run's
single record whatever happens next. `main` below is the hand-run shell over
the same function: it maps the decision to the exit codes this script always
had and writes its own record on the exits that skip the agent.

EXIT CODES (of a hand-run; the wrapper no longer runs this script)
------------------------------------------------------------------
0  = run the agentic pass (work exists, reruns pending, or pre-gate unsure)
10 = skip — no new work (a status='success' 0-ticket run record was written)
11 = skip — a gate failed closed: the routine is paused (status='skipped'
     record), the source mode is `os` (status='skipped' record), or the source
     mode could not be established (status='failure' record)

Stdlib + packn_os_hubspot_client only — no new dependencies (CLAUDE.md
invariant). PyYAML is already in scripts/requirements.txt.

State ownership: this script NEVER writes config/state.json. last_run_at and
ticket_fingerprints stay owned by the skill; the pre-gate only reads them.
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# The package root goes on the path so `ticket_pipeline` and the helper client
# import from a bare `python scripts/pregate_tickets.py` (ticket_pipeline/run.py
# does the mirror image to import this script).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ticket_pipeline import hubspot_api as hs  # noqa: E402 — the one HubSpot chokepoint
from ticket_pipeline import source  # noqa: E402
from ticket_pipeline.source import (  # noqa: E402,F401 — re-exported: one candidate rule
    build_search_body,
    iso_to_ms,
    parse_last_run_at,
    unprocessed_candidates,
)

ROUTINE = "tickets-process"
# Marker substring identifying our own crontab line (the crontab invokes
# `bash scripts/run_tickets.sh`). Keep in sync with the wrapper's filename.
CRON_MARKER = "run_tickets.sh"

SETTINGS_PATH = ROOT / "config" / "settings.yaml"
STATE_PATH = ROOT / "config" / "state.json"

EXIT_RUN_AGENT = 0
EXIT_SKIP_NO_WORK = 10
EXIT_SKIP_PAUSED = 11

# hubspot_candidates' decision and its reason.
DECISION_RUN_AGENT = "run_agent"
DECISION_NO_WORK = "no_work"
REASON_CANDIDATES = "candidates"  # run_agent: at least one hit survived the dedupe
REASON_PAGE_FULL = "page_full"  # run_agent: a full page, every hit deduped (fail-open)
REASON_SEARCH_FAILED = "search_failed"  # run_agent: no usable answer (fail-open)
REASON_NONE = "none"  # no_work: a clean poll with nothing new
REASON_OS_MODE = "os_mode"  # no_work: HubSpot is not the source; nothing was asked

# error_summary of the record written when this script is run in a mode it does not serve.
SUMMARY_OS_MODE_NOT_SERVED = "pregate: source_mode_os_not_served"


def _log(msg: str) -> None:
    print(f"[pregate] {msg}", file=sys.stderr)


# ===========================================================================
# PURE LAYER — lives in ticket_pipeline/source.py since Phase 93.
# iso_to_ms, parse_last_run_at, build_search_body and unprocessed_candidates are
# imported above and re-exported under their old names, so
# packn_os_hubspot_client/tests/test_pregate.py and ticket_pipeline/run.py keep
# their imports. Do not define a second copy here.
# ===========================================================================


# ===========================================================================
# SIDE-EFFECT LAYER
# ===========================================================================


def _load_settings() -> dict:
    import yaml  # lazy: keeps the pure layer importable without PyYAML

    with open(SETTINGS_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_state(path: Optional[Path] = None) -> dict:
    """The state file as a dict; {} when it is missing or not JSON. `path` is
    the file to read; None means the tracked config/state.json."""
    try:
        return json.loads((path or STATE_PATH).read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return {}


# The HubSpot request, the rate-limit floor and the token read are NOT here: they
# live once, in ticket_pipeline/hubspot_api.py, where every request code makes is
# counted (Phase 93). This script calls hs.read_token() and hs.search_tickets(),
# which keep what the pre-gate relies on — one retry on 429/5xx or a transport
# error, and None when the search fails.


def _write_run_record(status: str, error_summary: Optional[str], started_iso: str) -> None:
    """Best-effort automation_runs record so heartbeats/alerts stay green on
    skipped ticks. A write failure never changes the gate decision."""
    try:
        from packn_os_hubspot_client import client

        client.write_run_record(
            routine_name=ROUTINE,
            status=status,
            error_summary=error_summary,
            tickets_processed=0,
            drafts_created=0,
            started_at_iso=started_iso,
            finished_at_iso=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as e:
        _log(f"run-record write failed ({status}): {e!r}")


def _candidate_decision(
    decision: str,
    reason: str,
    matched: Optional[list[dict]] = None,
    candidates: Optional[list[str]] = None,
    page_full: bool = False,
) -> dict:
    return {
        "decision": decision,
        "reason": reason,
        "matched": matched or [],
        "candidates": candidates or [],
        "page_full": page_full,
    }


def hubspot_candidates(
    mode: str,
    settings: dict,
    state: dict,
    token: str,
    transport: Optional[Callable[..., Any]] = None,
) -> dict:
    """The HubSpot search-and-dedupe decision, for the one source mode.

    Returns
        {"decision":   "run_agent" | "no_work",
         "reason":     "candidates" | "page_full" | "search_failed" | "none" | "os_mode",
         "matched":    [{"id", "hs_lastmodifieddate", "deduped"}, ...]   every search hit
         "candidates": [ticket id, ...]                                  hits that are new work
         "page_full":  bool}                                             hits >= per_run_cap

    `hubspot` and `shadow` mode: one tickets/search with the SAME body as
    SKILL.md Step 1, then the fingerprint dedupe.
        candidates survive                -> run_agent / candidates
        a full page, every hit deduped    -> run_agent / page_full     (fail-open)
        no usable answer from the search  -> run_agent / search_failed (fail-open)
        anything else                     -> no_work  / none

    `os` mode: HubSpot is not the source. Nothing is asked of it — no search, no
    request of any kind — and the answer is no_work / os_mode with empty lists.
    The `os`-mode list comes from the OS, in another function.

    A mode outside the three raises SourceModeError(source_unknown) before any
    request. `settings` and `state` are read, never changed. A malformed
    `per_run_cap` raises (ValueError / TypeError): the caller owns that doubt.

    The search goes through ticket_pipeline.hubspot_api.search_tickets, so it is
    counted as one `search` call. `transport`, when given, replaces the request
    function for it: called as `transport(method, path, token, body)`, it answers
    the parsed response or None — the shape of hubspot_api's request function and
    of tests/helpers/fakes.FakeHubSpotTransport.
    """
    mode = source.validate_mode(mode)
    if mode == source.MODE_OS:
        return _candidate_decision(DECISION_NO_WORK, REASON_OS_MODE)

    active_stages = [str(s) for s in settings.get("active_stages") or []]
    per_run_cap = int(settings.get("per_run_cap", 25))
    require_visitor = bool(settings.get("require_last_message_from_visitor", False))
    last_run_ms = parse_last_run_at(state.get("last_run_at"))
    fingerprints = state.get("ticket_fingerprints") or {}

    body = build_search_body(last_run_ms, active_stages, require_visitor, per_run_cap)
    response = hs.search_tickets(body, token, transport=transport)
    # A search answer always carries `results`. One that does not (a failed call,
    # an empty or foreign body) is not a clean "nothing matched": fail open.
    if not isinstance(response, dict) or "results" not in response:
        _log("hubspot search failed — deferring to agent")
        return _candidate_decision(DECISION_RUN_AGENT, REASON_SEARCH_FAILED)

    results = response.get("results") or []
    candidates = unprocessed_candidates(results, fingerprints)
    wanted = set(candidates)
    matched = [
        {
            "id": str(hit.get("id")),
            "hs_lastmodifieddate": (hit.get("properties") or {}).get("hs_lastmodifieddate"),
            "deduped": str(hit.get("id")) not in wanted,
        }
        for hit in results
        if str(hit.get("id", ""))
    ]
    page_full = len(results) >= per_run_cap

    if candidates:
        _log(
            f"{len(candidates)} candidate ticket(s) after dedupe "
            f"({len(results)} matched) — launching agent"
        )
        return _candidate_decision(
            DECISION_RUN_AGENT, REASON_CANDIDATES, matched, candidates, page_full
        )

    if page_full:
        # Saturated-page blind spot (2026-07-23 audit): the single page is
        # full AND every row deduped out. Already-processed tickets whose
        # hs_lastmodifieddate keeps getting bumped can permanently occupy the
        # ascending-sorted page, hiding newer unprocessed tickets beyond it —
        # and since nothing advances last_run_at on a skip, the deadlock is
        # stable across ticks. Fail open: let the agent run and page deeper.
        _log(
            f"page saturated ({len(results)} >= per_run_cap {per_run_cap}) with all "
            "deduped — failing open to the agent so work beyond the page isn't hidden"
        )
        return _candidate_decision(DECISION_RUN_AGENT, REASON_PAGE_FULL, matched, [], True)

    _log(f"no new tickets ({len(results)} matched, all deduped) — skipping agentic run")
    return _candidate_decision(DECISION_NO_WORK, REASON_NONE, matched)


DECISION_PAUSED = "paused"
REASON_PAUSED = "routine_paused"  # paused: the routine gate said no (or could not be read)
REASON_RERUNS = "pending_reruns"  # run_agent: operator reruns are always agent work
REASON_SETTINGS_UNREADABLE = "settings_unreadable"  # run_agent (fail-open)
REASON_NO_ACTIVE_STAGES = "active_stages_empty"  # run_agent (fail-open)
REASON_TOKEN_MISSING = "token_missing"  # run_agent (fail-open)


def _decision(decision: str, reason: str, reruns: Optional[list] = None) -> dict:
    return {**_candidate_decision(decision, reason), "reruns": list(reruns or [])}


def _state_for(deps: Any) -> dict:
    loader = getattr(deps, "load_state", None)
    if callable(loader):
        return loader()
    return _load_state(getattr(deps, "state_path", None))


def decide(mode: str, deps: Any) -> dict:
    """The whole pre-gate decision, in one place. It writes NO run record.

    Returns hubspot_candidates' five keys plus `reruns`:

        {"decision": "run_agent" | "no_work" | "paused",
         "reason":   see the table,
         "matched": [...], "candidates": [...], "page_full": bool,
         "reruns":  the pending rerun rows, when they are why the agent runs}

    In this order, which is the order `main` always had:

        the routine gate says no (or cannot be read)     paused    / routine_paused
        a rerun request is pending                       run_agent / pending_reruns
        mode `os` (HubSpot is asked nothing)             no_work   / os_mode
        settings unreadable, or a cap that is no number  run_agent / settings_unreadable   fail-open
        active_stages empty                              run_agent / active_stages_empty   fail-open
        no HubSpot token                                 run_agent / token_missing         fail-open
        then hubspot_candidates: candidates, page_full and search_failed are
        run_agent (the last two fail-open); a clean empty poll is no_work / none

    `deps` is anything with these attributes (ticket_pipeline.run_types.Deps is
    one; `main` below builds another):

        read_routine_enabled(routine) -> bool
        read_pending_rerun_requests(routine) -> list
        load_settings() -> dict
        read_hubspot_token() -> str | None
        state_path                      the state file to read, or
        load_state() -> dict            (optional) a loader used instead of the path
        hubspot_transport               (optional) replaces the HubSpot request function

    The two callers are the orchestrator (scripts/run_tickets.py), which turns the
    decision into its one run record, and `main`, the hand-run shell. A mode
    outside the three raises SourceModeError(source_unknown) before anything is
    asked of anyone.
    """
    mode = source.validate_mode(mode)

    # Fail-closed routine gate (False on a database error too, CONTEXT D-01).
    if not deps.read_routine_enabled(ROUTINE):
        _log("routine paused (or gate unreachable) — skipping agentic run")
        return _decision(DECISION_PAUSED, REASON_PAUSED)

    # Operator rerun requests are always agent work (D-09).
    reruns = list(deps.read_pending_rerun_requests(ROUTINE) or [])
    if reruns:
        _log(f"{len(reruns)} pending rerun request(s) — launching agent")
        return _decision(DECISION_RUN_AGENT, REASON_RERUNS, reruns)

    if mode == source.MODE_OS:
        # HubSpot is not the source: no settings doubt, no token, no search.
        return _decision(DECISION_NO_WORK, REASON_OS_MODE)

    try:
        settings = deps.load_settings()
        active_stages = [str(s) for s in settings.get("active_stages") or []]
        int(settings.get("per_run_cap", 25))  # a cap that is not a number is a settings doubt
    except Exception as e:
        _log(f"settings unreadable — deferring to agent: {type(e).__name__}")
        return _decision(DECISION_RUN_AGENT, REASON_SETTINGS_UNREADABLE)
    if not active_stages:
        _log("active_stages empty in settings.yaml — deferring to agent")
        return _decision(DECISION_RUN_AGENT, REASON_NO_ACTIVE_STAGES)

    token = deps.read_hubspot_token()
    if token is None:
        _log("hubspot token missing — deferring to agent")
        return _decision(DECISION_RUN_AGENT, REASON_TOKEN_MISSING)

    state = _state_for(deps)

    # The search-and-dedupe decision lives once, in hubspot_candidates (which
    # also holds the two fail-open branches of the search itself).
    transport = getattr(deps, "hubspot_transport", None)
    if transport is None:
        outcome = hubspot_candidates(mode, settings, state, token)
    else:
        outcome = hubspot_candidates(mode, settings, state, token, transport=transport)
    return {**outcome, "reruns": []}


def _hand_run_deps() -> Any:
    """decide's collaborators for a hand-run of this script: the real ones, each
    looked up at the moment it is called."""
    import types

    from packn_os_hubspot_client import client

    return types.SimpleNamespace(
        read_routine_enabled=lambda routine: client.read_routine_enabled(routine),
        read_pending_rerun_requests=lambda routine: client.read_pending_rerun_requests(routine),
        load_settings=lambda: _load_settings(),
        load_state=lambda: _load_state(),
        read_hubspot_token=lambda: hs.read_token(),
        state_path=STATE_PATH,
        hubspot_transport=None,
    )


def main() -> int:
    """A hand-run of the pre-gate: a thin shell over `decide`.

    The scheduled tick and Run now do NOT come through here any more: they go
    through scripts/run_tickets.py, which calls `decide` itself and writes the
    run's one record. This shell keeps what a hand-run of the script always did:
    the same three exit codes, and its own record on the exits that skip the agent.
    """
    started_iso = datetime.now(timezone.utc).isoformat()

    from packn_os_hubspot_client import client

    # Step 0a equivalent — fire-and-forget crontab report (never raises).
    client.report_routine_schedule(ROUTINE, CRON_MARKER)

    # The one source mode (Phase 93): the exported value, else one read of the
    # setting. NOT fail-open — see the module docstring. A mode that cannot be
    # established is never treated as `hubspot`.
    try:
        mode = source.current_mode()
    except source.SourceModeError as e:
        _log(f"source mode not established ({e.code}) — skipping agentic run")
        _write_run_record("failure", f"pregate: {e.code}", started_iso)
        return EXIT_SKIP_PAUSED
    if mode == source.MODE_OS:
        # This script serves the HubSpot source. In `os` mode it asks HubSpot
        # nothing and launches nothing.
        _log("source mode is os — this pre-gate serves the HubSpot source only; skipping")
        _write_run_record("skipped", SUMMARY_OS_MODE_NOT_SERVED, started_iso)
        return EXIT_SKIP_PAUSED

    outcome = decide(mode, _hand_run_deps())
    if outcome["decision"] == DECISION_PAUSED:
        # Paused => the agent would exit at its own gate anyway; skip the launch
        # and write the same 'skipped' record.
        _write_run_record(
            "skipped",
            "pregate: routine paused via Pack'N OS (or gate unreachable — fail-closed)",
            started_iso,
        )
        return EXIT_SKIP_PAUSED
    if outcome["decision"] == DECISION_NO_WORK:
        _write_run_record("success", None, started_iso)
        return EXIT_SKIP_NO_WORK
    return EXIT_RUN_AGENT


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit:
        raise
    except Exception:
        # Fail-open: a pre-gate bug must never stop ticket processing.
        _log("unexpected error — deferring to agent:\n" + traceback.format_exc())
        code = EXIT_RUN_AGENT
    finally:
        try:
            from packn_os_hubspot_client.db import close_pool

            close_pool()
        except Exception:
            pass
    sys.exit(code)
