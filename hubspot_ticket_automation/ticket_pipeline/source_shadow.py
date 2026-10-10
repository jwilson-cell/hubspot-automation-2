"""The SOURCE shadow: the cron's half of the ticket-source comparison (Phase 93, plan 18).

WHAT THIS IS
------------
The ticket source setting has three values. While it is `shadow`, the cron
keeps working from HubSpot exactly as it does in `hubspot` mode, and in the
same run it also asks Pack'N OS for the list `os` mode would work from,
classifies both inputs with the SAME classify step, and posts what it saw to
the OS (`POST /api/tickets/source-shadow`). The OS does the matching, assigns a
reason to every difference and computes the gate the owner signs off before
the setting may become `os`. The cron reports raw observations and judges
nothing.

WHAT THIS IS NOT
----------------
It is not the ENGINE shadow. That older comparison (the setting
`pipeline.shadow_enabled`, the directory `outputs/shadow/`, the log tag
`[pipeline.shadow]`, ticket_pipeline/run.py) runs the direct-API engine beside
the agent over one source. This module compares two SOURCES with one engine.
It does not change the engine shadow's setting, never writes under its
directory and logs under its own tag. Every name here says `source_shadow`,
`observation`, `comparison`, `cache` or `resolve`; the one place the engine
shadow is touched is `_engine_classification`, which READS an artifact that
step wrote during this same run so a ticket is not classified twice.

THE RUNNER
----------
`run_source_shadow(ctx, deps)` is the mode's runner, registered under the
setting's value in `run_tickets.RUNNER_SPECS`. Its docstring gives the order of
one run, exactly what the run writes to the OS, and what an OS failure does
(nothing, to the HubSpot half). In short: the OS list is fetched FIRST, the
HubSpot path then runs as it does in `hubspot` mode, the observation is posted
before the agent starts, and after the agent each pass it completed for a
ticket the OS knows is written back with the mode word `shadow`.

The runner writes no draft (every draft of the run is the agent's own) and
never touches the HubSpot path's own state file, which the agent keeps in this
mode exactly as it does in `hubspot` mode.

THE COMPARISON CORE
-------------------
    resolve_hubspot_candidates  asks the OS, once per HubSpot candidate, which
                                OS ticket it is and which client signal it is
                                awaiting us through. A 404 (the mirror has not
                                seen the ticket yet) is "nothing".
    compare_sources             classifies the HubSpot-built context and the
                                OS-built context of each selected ticket, each
                                ticket inside its own guard, and returns the two
                                sides ready for the builder.
    build_observation           assembles the strict body the OS route accepts.
    load_seen_cache /           which (OS ticket, signal) pairs the OS side has
    save_seen_cache             already classified, so a ticket that stays in
                                the OS list costs a model call once, not once
                                per run.

`compare_sources` is handed a classifier and three readers and NOTHING that can
write: no draft writer, no action-item poster, no write-back client, no sender.
The measuring half cannot change what it measures (threat T-93-56).

COST, PER TICKET
----------------
HubSpot side, per selected candidate:
    the engine shadow classified it in this run   0 model calls, 0 HubSpot requests
    otherwise                                     1 classify call (the small
                                                  model) and one hydration: a
                                                  single-ticket search plus the
                                                  thread, contact, company and
                                                  related-ticket reads
OS side, per listed ticket:
    already in the cache (same ticket, same signal)   nothing
    otherwise                                         1 classify call, plus one
                                                      thread fetch (2 HubSpot
                                                      requests) ONLY for a
                                                      HubSpot-linked ticket the
                                                      HubSpot side did not select
A ShipSidekick lookup may also run for an OS-built context, exactly as it does
in `os` mode, when settings enable it and the ticket carries a number.

THE OS-BUILT CONTEXT
--------------------
It comes from `os_bundle.build_context`, the builder `os` mode uses. One
difference, for a ticket BOTH sources selected: its thread is taken from the
HubSpot-built context of this run (no second thread fetch), where `os` mode
would hand the builder the fetched emails. The thread and the latest client
message are therefore the same as `os` mode's; the form block is parsed from
the OS payload's opening body rather than from the first email's HTML.

WHEN THE CLASSIFIER IS DOWN
---------------------------
A classify call that raises leaves that one category null. Three failures in a
row stop the step for the rest of the comparison (an expired key must not be
asked a hundred times). `ComparisonResult.classifier_unavailable` is then true,
as it is when every call the comparison made failed. The runner does not post
such an observation: the OS keeps the FIRST category it is told for a ticket's
selection, so a run of nulls would never be corrected by a later run.

NO TICKET TEXT
--------------
The observation holds ids, slugs, booleans, counts and instants. So does the
cache file (keys `<OS ticket id>:<signal id>`, values category slugs). Log
lines hold ids, codes, counts and exception class names; never a subject, a
body, a classifier's reason or an exception's message (threat T-93-57).

ABSENCE IS STATED ONE WAY (checker WIRE-W3)
-------------------------------------------
Every key of every object of the observation is always written. An absent
value is JSON null, and only six fields may be null: a matched hit's `stage`;
a selected HubSpot candidate's `os_ticket_id`, `signal` and `category`; a
listed OS ticket's `hubspot_ticket_id` and `category`. Anything else that is
absent is an error: `build_observation` raises rather than send a body the OS
would refuse. (The action-item envelope is the opposite and stays so.)
"""
from __future__ import annotations

import copy
import importlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ticket_pipeline import os_bundle, os_pass, run_types, source
from ticket_pipeline.run_types import Deps, RunContext, RunOutcome

_ROOT = Path(__file__).resolve().parent.parent

LOG_TAG = "[source_shadow]"

# The `limit` the comparison list is asked with. Not per_run_cap: the OS-side
# cap must not manufacture "HubSpot only" differences (RESEARCH item 10).
COMPARISON_LIMIT = 100
# This many classify failures in a row and the step is not asked again this run.
COMPARISON_CLASSIFY_FAILURE_LIMIT = 3

# Which (OS ticket, signal) pairs the OS side has classified. Ids and slugs only.
SEEN_CACHE_PATH = _ROOT / "outputs" / "runs" / "source_shadow_seen.json"
SEEN_CACHE_MAX_ENTRIES = 5000

# The limits of the OS route's schema (93-07-SUMMARY, "The observation body").
OBSERVATION_CONTRACT_VERSION = "1"
OBSERVATION_MAX_TICKETS_PER_LIST = 200
OBSERVATION_MAX_ACTIVE_STAGES = 50
OBSERVATION_MAX_PER_RUN_CAP = 1000

# Codes of the comparison, counted for the run record's summary.
SOURCE_SHADOW_CLASSIFY_FAILED = "source_shadow_classify_failed"
SOURCE_SHADOW_CATEGORY_INVALID = "source_shadow_category_invalid"
SOURCE_SHADOW_HYDRATE_FAILED = "source_shadow_hydrate_failed"
SOURCE_SHADOW_THREAD_FETCH_FAILED = "source_shadow_thread_fetch_failed"
SOURCE_SHADOW_OS_CONTEXT_FAILED = "source_shadow_os_context_failed"
SOURCE_SHADOW_OS_TICKET_UNUSABLE = "source_shadow_os_ticket_unusable"
SOURCE_SHADOW_RESOLVE_FAILED = "source_shadow_resolve_failed"
SOURCE_SHADOW_RESOLVE_INVALID = "source_shadow_resolve_invalid"

# The run's own id: one lower-case uuid in the 8-4-4-4-12 form (checker WIRE-W6).
_RUN_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# An OS id, by the OS's own test (which ignores case).
_OS_ID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_HUBSPOT_ID_RE = re.compile(r"[0-9]{1,20}")
# A raw pipeline stage: a short token, never a stage's display name.
_STAGE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_INSTANT_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_SIGNAL_KINDS = ("opening", "client_reply", "stage_reopen")
_SIGNAL_KEYS = ("kind", "id", "at")
_CACHE_KEY_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}:[A-Za-z0-9:_-]{1,64}"
)
# The name the engine shadow gives a run's directory: a UTC second.
_ENGINE_RUN_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}Z")
_ENGINE_RUN_FORMAT = "%Y-%m-%dT%H-%M-%SZ"


def _log(msg: str) -> None:
    print(f"{LOG_TAG} {msg}", file=sys.stderr)


def _count(codes: Optional[dict], code: str, n: int = 1) -> None:
    if codes is not None:
        codes[code] = codes.get(code, 0) + n


def _text(value: Any) -> Optional[str]:
    """A string that is not blank, as given; anything else is None."""
    return value if isinstance(value, str) and value.strip() else None


def _is_slug(value: Any) -> bool:
    return isinstance(value, str) and value in os_pass.CATEGORY_SLUGS


def _tickets_api() -> Any:
    """The signed client for the OS tickets routes, looked up on first use."""
    return importlib.import_module("packn_os_hubspot_client.tickets_api")


def _unique_ids(values: Any) -> list[str]:
    """The non-blank text ids of a list, each once, in order."""
    seen: set[str] = set()
    kept: list[str] = []
    for value in values if isinstance(values, (list, tuple)) else []:
        if isinstance(value, str) and value.strip() and value not in seen:
            seen.add(value)
            kept.append(value)
    return kept


def _signal_of(raw: Any) -> Optional[dict]:
    """A client signal as the OS supplied it: its three values, unchanged, in
    the contract's key order. None when any of the three is missing or blank."""
    if not isinstance(raw, dict):
        return None
    if not all(_text(raw.get(key)) for key in _SIGNAL_KEYS):
        return None
    return {key: raw[key] for key in _SIGNAL_KEYS}


# ===========================================================================
# THE SEEN CACHE (ids and slugs; a temp file and a rename)
# ===========================================================================


def seen_cache_key(os_ticket_id: Any, signal_id: Any) -> str:
    """The cache key of one OS-side classification: the ticket and the client
    signal it was listed for. A newer signal is a new key."""
    return f"{os_ticket_id}:{signal_id}"


def seen_cache_path(root: Any) -> Path:
    """The cache file under a package root (`deps.root` for a run)."""
    return Path(root) / "outputs" / "runs" / "source_shadow_seen.json"


def _cache_entries(cache: Any) -> dict:
    """What a cache may hold, in order: an id key and a contract slug. Anything
    else a file or a caller holds is dropped, so text cannot be kept here."""
    kept: dict[str, str] = {}
    for key, value in cache.items() if isinstance(cache, dict) else []:
        if isinstance(key, str) and _CACHE_KEY_RE.fullmatch(key) and _is_slug(value):
            kept[key] = value
    return kept


def load_seen_cache(path: Any) -> dict:
    """The cache as a dict, oldest entry first. A file that is missing,
    unreadable, not JSON or not an object is an empty cache: the cost of losing
    it is one classify call per listed ticket, once."""
    try:
        loaded = json.loads(Path(path).read_bytes().decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return _cache_entries(loaded)


def save_seen_cache(path: Any, cache: Any) -> None:
    """Replace the cache file with the newest SEEN_CACHE_MAX_ENTRIES valid
    entries of `cache`, through a temp file beside it and a rename, so a reader
    never sees half a file. Raises OSError when it cannot; the old file is then
    still whole and no temp file is left. `cache` itself is not changed."""
    entries = list(_cache_entries(cache).items())[-SEEN_CACHE_MAX_ENTRIES:]
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(dict(entries), indent=1) + "\n").encode("utf-8")
    handle, temp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".source_shadow_seen.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


# ===========================================================================
# THE ENGINE SHADOW'S ARTIFACT (read only)
# ===========================================================================


def _engine_classification(root: Any, hubspot_ticket_id: Any, since: Any) -> Optional[dict]:
    """What the engine shadow concluded for one HubSpot ticket DURING THIS RUN:
    {"category": <slug>, "ticket_context": <the context it classified, or None>},
    or None when it left no usable artifact.

    The engine shadow (ticket_pipeline/run.py) runs before the agent over the
    same candidates and writes one JSON file per ticket under a directory named
    for the UTC second it started. `since` is the run's start: a directory of
    an earlier second belongs to an earlier run and is never reused.

    This function only reads. It takes the category and the context from the
    artifact and nothing else (not the draft, not the classifier's reason)."""
    if not (isinstance(hubspot_ticket_id, str) and _HUBSPOT_ID_RE.fullmatch(hubspot_ticket_id)):
        return None
    base = Path(root) / "outputs" / "shadow"
    try:
        floor = since.astimezone(timezone.utc).strftime(_ENGINE_RUN_FORMAT)
        names = sorted((entry.name for entry in base.iterdir() if entry.is_dir()), reverse=True)
    except (OSError, AttributeError, ValueError, TypeError):
        return None
    for name in names:
        # fixed-width UTC names: text order is time order
        if not _ENGINE_RUN_RE.fullmatch(name) or name < floor:
            continue
        try:
            loaded = json.loads((base / name / f"{hubspot_ticket_id}.json").read_bytes().decode("utf-8"))
        except (OSError, ValueError, RecursionError):
            continue
        classification = loaded.get("classification") if isinstance(loaded, dict) else None
        category = classification.get("category") if isinstance(classification, dict) else None
        if _is_slug(category):
            context = loaded.get("ticket_context")
            return {"category": category, "ticket_context": context if isinstance(context, dict) else None}
    return None


# ===========================================================================
# WHICH OS TICKET IS A HUBSPOT CANDIDATE?
# ===========================================================================


def resolve_hubspot_candidates(candidates: Any, deps: Any, codes: Optional[dict] = None) -> dict:
    """Ask the OS's single-ticket route, once per HubSpot candidate id, which OS
    ticket it is: {<HubSpot id>: {"os_ticket_id": <uuid>, "signal": {kind, id,
    at} or None}}.

    A candidate the OS answers 404 for (the mirror has not seen it yet, at most
    one sync cycle behind) is ABSENT from the result: the HubSpot path then does
    exactly what it does in `hubspot` mode for that ticket.

    `deps.os_api` is the client (None = the real signed one). `codes`, when
    given, counts what went wrong:
        source_shadow_resolve_invalid          an answer that names no ticket,
                                               or names another one
        source_shadow_resolve_failed:<code>    the OS could not be asked. The
                                               lookups STOP there: an OS that is
                                               down costs one timeout, not one
                                               per candidate.
    An id that is not ASCII digits is never put into a request path."""
    api = getattr(deps, "os_api", None)
    if api is None:
        api = _tickets_api()
    resolved: dict[str, dict] = {}
    for hubspot_id in _unique_ids(candidates):
        if not _HUBSPOT_ID_RE.fullmatch(hubspot_id):
            continue
        answer: Any = None
        failure: Optional[str] = None
        cause = ""
        try:
            answer = api.fetch_ticket(hubspot_id)
        except Exception as e:  # noqa: BLE001 - whatever stopped it, it is a code
            raised = getattr(e, "code", None)
            failure = raised if raised in os_pass.LIST_FAILURE_CODES else os_pass.LIST_ERROR
            cause = type(e).__name__
        if failure is not None:
            _count(codes, f"{SOURCE_SHADOW_RESOLVE_FAILED}:{failure}")
            _log(f"candidate lookup stopped: code={failure} cause={cause} resolved={len(resolved)}")
            break
        if answer is None:
            continue  # 404: the mirror has not seen this ticket
        ticket = answer.get("ticket") if isinstance(answer, dict) else None
        os_ticket_id = ticket.get("id") if isinstance(ticket, dict) else None
        named = ticket.get("hubspot_ticket_id") if isinstance(ticket, dict) else None
        if not (isinstance(os_ticket_id, str) and _OS_ID_RE.fullmatch(os_ticket_id)) or named not in (None, hubspot_id):
            _count(codes, SOURCE_SHADOW_RESOLVE_INVALID)
            _log(f"candidate lookup answer unusable: ticket={hubspot_id}")
            continue
        resolved[hubspot_id] = {"os_ticket_id": os_ticket_id, "signal": _signal_of(ticket.get("signal"))}
    return resolved


# ===========================================================================
# THE COMPARISON
# ===========================================================================


@dataclass
class ComparisonDeps:
    """What `compare_sources` may use. A classifier, three readers, two values
    and the cache. Nothing here can write a draft, an action item, a write-back
    or an email.

    classify                 classify(context) -> a classification (a dict with
                             `category`) or a slug. ONE callable for both
                             sides (D-05): a disagreement can only come from
                             the input.
    hydrate_hubspot_context  hydrate(HubSpot ticket id) -> the HubSpot-built
                             ticket context
    fetch_hubspot_thread     fetch(HubSpot ticket id) -> the ticket's email
                             objects, as `os_bundle.fetch_thread` returns them
    engine_classification    lookup(HubSpot ticket id) -> {"category",
                             "ticket_context"} when the engine shadow already
                             classified the ticket in this run, else None
    settings, os_origin      handed to `os_bundle.build_context`
    cache                    the seen cache, read and added to in place
    """

    classify: Callable[[dict], Any]
    hydrate_hubspot_context: Callable[[str], Any]
    fetch_hubspot_thread: Callable[[str], Any]
    engine_classification: Callable[[str], Optional[dict]]
    settings: Any
    os_origin: str
    cache: dict


@dataclass
class ComparisonResult:
    """The two sides, in the shape `build_observation` takes, and what it cost.

    hubspot_side            {"page_full", "matched": [...], "selected": [...]}
    os_side                 {"selected": [...]}
    codes                   code -> count, for the run record's summary
    classify_calls          classify calls made (a reused or cached category is none)
    classify_failures       how many of them raised
    classifier_unavailable  the step was stopped, or every call it made failed
    engine_reused           HubSpot-side categories taken from the engine shadow
    """

    hubspot_side: dict = field(default_factory=dict)
    os_side: dict = field(default_factory=dict)
    codes: dict = field(default_factory=dict)
    classify_calls: int = 0
    classify_failures: int = 0
    classifier_unavailable: bool = False
    engine_reused: int = 0


class _Classifying:
    """The one classify callable, with its failures counted and its stop rule."""

    def __init__(self, classify: Callable[[dict], Any], codes: dict) -> None:
        self._classify = classify
        self._codes = codes
        self.calls = 0
        self.failures = 0
        self._in_a_row = 0
        self.stopped = False

    @property
    def unavailable(self) -> bool:
        return self.stopped or (self.calls > 0 and self.failures == self.calls)

    def category(self, context: dict) -> Optional[str]:
        """The contract slug the classifier gives this context, or None. Only
        the slug is kept of what it answers: never its reason."""
        if self.stopped:
            return None
        self.calls += 1
        answer: Any = None
        failure: Optional[str] = None
        try:
            answer = self._classify(context)
        except Exception as e:  # noqa: BLE001 - one ticket's failure is a null category
            failure = type(e).__name__
        if failure is not None:
            self.failures += 1
            self._in_a_row += 1
            _count(self._codes, SOURCE_SHADOW_CLASSIFY_FAILED)
            _log(f"classify failed: error={failure}")
            if self._in_a_row >= COMPARISON_CLASSIFY_FAILURE_LIMIT:
                self.stopped = True
                _log(f"classify stopped for this run after {self._in_a_row} failures in a row")
            return None
        self._in_a_row = 0
        category = answer.get("category") if isinstance(answer, dict) else answer
        if _is_slug(category):
            return category
        _count(self._codes, SOURCE_SHADOW_CATEGORY_INVALID)
        return None


def _stage_of(context: Any) -> Optional[str]:
    """A HubSpot-built context's raw stage as a token, or None."""
    stage = context.get("stage") if isinstance(context, dict) else None
    if isinstance(stage, bool) or not isinstance(stage, (str, int)):
        return None
    token = str(stage)
    return token if _STAGE_RE.fullmatch(token) else None


def _os_context(
    listed: dict, hubspot_id: Optional[str], hubspot_context: Any, deps: ComparisonDeps, codes: dict
) -> dict:
    """The OS-built context of one listed ticket, from the builder `os` mode uses.

    Its thread: the HubSpot-built context's when this run already has one for
    the ticket (no second fetch); one thread fetch for a linked ticket it does
    not; the OS thread for a ticket with no HubSpot id, and whenever HubSpot
    holds no email for the ticket (what `os` mode calls os_fallback)."""
    thread: Any = None
    if hubspot_id is not None and not isinstance(hubspot_context, dict):
        failure: Optional[str] = None
        try:
            thread = deps.fetch_hubspot_thread(hubspot_id)
        except Exception as e:  # noqa: BLE001 - the ticket is then built from the OS thread
            failure = type(e).__name__
        if failure is not None:
            thread = None
            _count(codes, SOURCE_SHADOW_THREAD_FETCH_FAILED)
            _log(f"thread fetch failed: ticket={hubspot_id} error={failure}")
    context = os_bundle.build_context(listed, thread, deps.settings, deps.os_origin)
    reused = hubspot_context.get("thread") if isinstance(hubspot_context, dict) else None
    if isinstance(reused, list) and reused:
        context["thread"] = copy.deepcopy(reused)
        latest = os_bundle.hydrate.latest_customer_message(context["thread"])
        if latest:
            context["latest_customer_message"] = latest
    return context


def compare_sources(hubspot_side: dict, os_list: list, deps: ComparisonDeps) -> ComparisonResult:
    """Classify what each source selected and return the two sides.

    `hubspot_side`  the pre-gate's decision (`matched`: every search hit as
                    {"id", "deduped", ...}; `candidates`: the selected ids;
                    `page_full`) plus `resolved`, the answer of
                    resolve_hubspot_candidates
    `os_list`       the ticket payloads of the OS's unprocessed list, as fetched
                    at the START of the run (RESEARCH Pitfall 7)

    Every ticket is classified inside its own guard: one that fails has a null
    category and the others are still classified. Nothing is written anywhere
    but `deps.cache` (in memory). See the module docstring for the cost."""
    result = ComparisonResult()
    codes = result.codes
    classifying = _Classifying(deps.classify, codes)
    side = hubspot_side if isinstance(hubspot_side, dict) else {}
    resolved = side.get("resolved") if isinstance(side.get("resolved"), dict) else {}
    cache = deps.cache if isinstance(deps.cache, dict) else {}

    # --- the HubSpot side: each selected candidate
    contexts: dict[str, dict] = {}
    selected: list[dict] = []
    for hubspot_id in _unique_ids(side.get("candidates")):
        category: Optional[str] = None
        context: Any = None
        reused: Any = None
        failure: Optional[str] = None
        try:
            reused = deps.engine_classification(hubspot_id)
        except Exception as e:  # noqa: BLE001 - no artifact, then: hydrate and classify
            failure = type(e).__name__
        if failure is not None:
            _log(f"engine artifact unreadable: ticket={hubspot_id} error={failure}")
        if isinstance(reused, dict) and _is_slug(reused.get("category")):
            category = reused["category"]
            context = reused.get("ticket_context")
            result.engine_reused += 1
        elif not classifying.stopped:
            failure = None
            try:
                context = deps.hydrate_hubspot_context(hubspot_id)
            except Exception as e:  # noqa: BLE001 - one ticket's failure is a null category
                failure = type(e).__name__
            if failure is None and not isinstance(context, dict):
                failure = "not_a_context"
            if failure is not None:
                context = None
                _count(codes, SOURCE_SHADOW_HYDRATE_FAILED)
                _log(f"hydration failed: ticket={hubspot_id} error={failure}")
            else:
                category = classifying.category(context)
        if isinstance(context, dict):
            contexts[hubspot_id] = context
        known = resolved.get(hubspot_id)
        known = known if isinstance(known, dict) else {}
        selected.append(
            {
                "hubspot_ticket_id": hubspot_id,
                "os_ticket_id": known.get("os_ticket_id"),
                "signal": known.get("signal"),
                "category": category,
            }
        )

    matched: list[dict] = []
    for hit in side.get("matched") if isinstance(side.get("matched"), list) else []:
        hit_id = hit.get("id") if isinstance(hit, dict) else None
        if not isinstance(hit_id, str):
            continue
        matched.append(
            {
                "hubspot_ticket_id": hit_id,
                "stage": _stage_of(contexts.get(hit_id)),
                "deduped": hit.get("deduped") is True,
            }
        )

    # --- the OS side: each listed ticket
    listed_rows: list[dict] = []
    seen: set[str] = set()
    cached = 0
    for listed in os_list if isinstance(os_list, list) else []:
        os_ticket_id = _text(listed.get("id")) if isinstance(listed, dict) else None
        signal = _signal_of(listed.get("signal")) if isinstance(listed, dict) else None
        if os_ticket_id is None or signal is None:
            # the contract has no null id and no null signal on this side
            _count(codes, SOURCE_SHADOW_OS_TICKET_UNUSABLE)
            continue
        if os_ticket_id in seen:
            continue
        seen.add(os_ticket_id)
        hubspot_id = _text(listed.get("hubspot_ticket_id"))
        key = seen_cache_key(os_ticket_id, signal["id"])
        category = cache.get(key) if _is_slug(cache.get(key)) else None
        if category is not None:
            cached += 1
        elif not classifying.stopped:
            failure = None
            context = None
            try:
                context = _os_context(listed, hubspot_id, contexts.get(hubspot_id or ""), deps, codes)
            except Exception as e:  # noqa: BLE001 - one ticket's failure is a null category
                failure = type(e).__name__
            if failure is not None:
                _count(codes, SOURCE_SHADOW_OS_CONTEXT_FAILED)
                _log(f"OS context not built: ticket={os_ticket_id} error={failure}")
            else:
                category = classifying.category(context)
                if category is not None:
                    cache[key] = category
        listed_rows.append(
            {
                "os_ticket_id": os_ticket_id,
                "hubspot_ticket_id": hubspot_id,
                "signal": signal,
                "category": category,
            }
        )

    result.hubspot_side = {"page_full": side.get("page_full") is True, "matched": matched, "selected": selected}
    result.os_side = {"selected": listed_rows}
    result.classify_calls = classifying.calls
    result.classify_failures = classifying.failures
    result.classifier_unavailable = classifying.unavailable
    _log(
        f"compared: hubspot_matched={len(matched)} hubspot_selected={len(selected)} "
        f"os_selected={len(listed_rows)} classify_calls={classifying.calls} "
        f"classify_failures={classifying.failures} engine_reused={result.engine_reused} cached={cached}"
    )
    return result


# ===========================================================================
# THE OBSERVATION (the strict body of POST /api/tickets/source-shadow)
# ===========================================================================


def _required(record: Any, key: str, where: str) -> Any:
    """A key the caller must have written, whatever its value. A missing key is
    never read as null: absence is stated by the caller, one way."""
    if not isinstance(record, dict):
        raise TypeError(f"{where} is an object")
    if key not in record:
        raise ValueError(f"{where} has no {key}")
    return record[key]


def _entries(value: Any, where: str) -> list:
    if not isinstance(value, list):
        raise TypeError(f"{where} is a list")
    if len(value) > OBSERVATION_MAX_TICKETS_PER_LIST:
        raise ValueError(f"{where} holds more than {OBSERVATION_MAX_TICKETS_PER_LIST} tickets")
    return value


def _flag(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{where} is true or false")
    return value


def _hubspot_id(value: Any, where: str) -> str:
    if not (isinstance(value, str) and _HUBSPOT_ID_RE.fullmatch(value)):
        raise ValueError(f"{where} is 1 to 20 digits")
    return value


def _os_id(value: Any, where: str) -> str:
    if not (isinstance(value, str) and _OS_ID_RE.fullmatch(value)):
        raise ValueError(f"{where} is a uuid")
    return value


def _stage(value: Any, where: str) -> str:
    if not (isinstance(value, str) and _STAGE_RE.fullmatch(value)):
        raise ValueError(f"{where} is a stage token")
    return value


def _slug(value: Any, where: str) -> str:
    if not _is_slug(value):
        raise ValueError(f"{where} is one of the contract's category slugs")
    return value


def _signal(value: Any, where: str) -> dict:
    """Exactly {kind, id, at}, each as the OS supplied it."""
    kind = _required(value, "kind", where)
    signal_id = _required(value, "id", where)
    at = _required(value, "at", where)
    if kind not in _SIGNAL_KINDS:
        raise ValueError(f"{where}.kind is not a client signal kind")
    _os_id(signal_id, f"{where}.id")
    if not (isinstance(at, str) and _INSTANT_RE.fullmatch(at)):
        raise ValueError(f"{where}.at is an instant with three fractional digits and a Z")
    return {"kind": kind, "id": signal_id, "at": at}


def _nullable(value: Any, check: Callable[[Any, str], Any], where: str) -> Any:
    return None if value is None else check(value, where)


def _active_stages(settings: dict) -> list[str]:
    raw = settings.get("active_stages")
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise TypeError("active_stages is a list")
    if len(raw) > OBSERVATION_MAX_ACTIVE_STAGES:
        raise ValueError(f"more than {OBSERVATION_MAX_ACTIVE_STAGES} active stages")
    stages: list[str] = []
    for stage in raw:
        if isinstance(stage, bool) or not isinstance(stage, (str, int)):
            raise TypeError("an active stage is a token")
        stages.append(_stage(str(stage), "an active stage"))
    return stages


def build_observation(run_id: Any, started_at: Any, settings: Any, hubspot_side: Any, os_side: Any) -> dict:
    """The body of one run's observation, as the OS route's strict schema takes it.

    `run_id`        the run's OWN id: the lower-case uuid the orchestrator
                    generated and writes as the run record's id, so the OS can
                    join the comparison to the run (checker WIRE-W6). Passed
                    through unchanged.
    `started_at`    the run's start as a timezone-aware datetime. It is written
                    by the client's `iso_millis_z` (UTC, exactly three
                    fractional digits, a trailing Z), never by isoformat().
    `settings`      read: `per_run_cap` (default 25) and `active_stages`
    `hubspot_side`  {"page_full", "matched": [{hubspot_ticket_id, stage,
                    deduped}], "selected": [{hubspot_ticket_id, os_ticket_id,
                    signal, category}]}
    `os_side`       {"selected": [{os_ticket_id, hubspot_ticket_id, signal,
                    category}]}

    Every key above must be present in what it is handed. A value may be None
    in exactly the six nullable fields; the builder writes it as null and never
    drops the key. Any other absent or malformed value RAISES (ValueError or
    TypeError): a list over 200 tickets, an id of the wrong form, a stage that
    is not a token, a category that is not a contract slug, a listed OS ticket
    with no id or no signal, a naive start instant, a cap outside 1 to 1000.

    Each object is assembled from its named keys only, so a key the caller
    added (a subject, a body) is not copied."""
    if not (isinstance(run_id, str) and _RUN_ID_RE.fullmatch(run_id)):
        raise ValueError("run_id is the run's lower-case hyphenated uuid")
    if not isinstance(started_at, datetime):
        raise TypeError("started_at is a timezone-aware datetime")
    started = _tickets_api().iso_millis_z(started_at)  # ValueError for a naive one
    if not isinstance(settings, dict):
        raise TypeError("settings are a mapping")
    cap = settings.get("per_run_cap", os_pass.DEFAULT_PER_RUN_CAP)
    if isinstance(cap, bool) or not isinstance(cap, int):
        raise TypeError("per_run_cap is a whole number")
    if not 1 <= cap <= OBSERVATION_MAX_PER_RUN_CAP:
        raise ValueError(f"per_run_cap is 1 to {OBSERVATION_MAX_PER_RUN_CAP}")
    stages = _active_stages(settings)

    matched = []
    for hit in _entries(_required(hubspot_side, "matched", "hubspot"), "hubspot.matched"):
        where = "hubspot.matched[]"
        matched.append(
            {
                "hubspot_ticket_id": _hubspot_id(_required(hit, "hubspot_ticket_id", where), f"{where}.hubspot_ticket_id"),
                "stage": _nullable(_required(hit, "stage", where), _stage, f"{where}.stage"),
                "deduped": _flag(_required(hit, "deduped", where), f"{where}.deduped"),
            }
        )
    selected = []
    for row in _entries(_required(hubspot_side, "selected", "hubspot"), "hubspot.selected"):
        where = "hubspot.selected[]"
        selected.append(
            {
                "hubspot_ticket_id": _hubspot_id(_required(row, "hubspot_ticket_id", where), f"{where}.hubspot_ticket_id"),
                "os_ticket_id": _nullable(_required(row, "os_ticket_id", where), _os_id, f"{where}.os_ticket_id"),
                "signal": _nullable(_required(row, "signal", where), _signal, f"{where}.signal"),
                "category": _nullable(_required(row, "category", where), _slug, f"{where}.category"),
            }
        )
    listed = []
    for row in _entries(_required(os_side, "selected", "os"), "os.selected"):
        where = "os.selected[]"
        listed.append(
            {
                "os_ticket_id": _os_id(_required(row, "os_ticket_id", where), f"{where}.os_ticket_id"),
                "hubspot_ticket_id": _nullable(
                    _required(row, "hubspot_ticket_id", where), _hubspot_id, f"{where}.hubspot_ticket_id"
                ),
                "signal": _signal(_required(row, "signal", where), f"{where}.signal"),
                "category": _nullable(_required(row, "category", where), _slug, f"{where}.category"),
            }
        )
    return {
        "contract_version": OBSERVATION_CONTRACT_VERSION,
        "run_id": run_id,
        "started_at": started,
        "per_run_cap": cap,
        "active_stages": stages,
        "hubspot": {
            "page_full": _flag(_required(hubspot_side, "page_full", "hubspot"), "hubspot.page_full"),
            "matched": matched,
            "selected": selected,
        },
        "os": {"selected": listed},
    }


# ===========================================================================
# THE RUNNER (`shadow` mode)
# ===========================================================================

# Codes of the runner, for the run record's summary. The first takes a cause:
#   source_shadow_skipped:<list code>[:<detail>]   the OS list could not be had
#   source_shadow_skipped:pregate_<reason>         the pre-gate failed open, so
#                                                  what HubSpot selects is unknown
#   source_shadow_skipped:settings_unreadable
#   source_shadow_skipped:classify_unavailable     see the module docstring
SOURCE_SHADOW_SKIPPED = "source_shadow_skipped"
SOURCE_SHADOW_OBSERVATION_REFUSED = "source_shadow_observation_refused"  # :<the route's error, or the status>
SOURCE_SHADOW_OBSERVATION_FAILED = "source_shadow_observation_failed"  # :<the client's code>
SOURCE_SHADOW_COMPARISON_FAILED = "source_shadow_comparison_failed"  # :<exception class>
SOURCE_SHADOW_CACHE_UNWRITABLE = "source_shadow_cache_unwritable"
SOURCE_SHADOW_WRITEBACKS_SKIPPED = "source_shadow_writebacks_skipped"  # :settings_unreadable
SOURCE_SHADOW_FINALISE_TICKET_ERROR = "source_shadow_finalise_ticket_error"
SOURCE_SHADOW_FINALISE_FAILED = "source_shadow_finalise_failed"  # :<exception class>

# The pre-gate reasons after which code knows what the HubSpot path selected.
_KNOWN_SELECTION_REASONS = ("candidates", "page_full", "none")
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_RERUN_PASS_PREFIX = f"{os_bundle.RERUN_CYCLE_PREFIX}:"


class _Notes:
    """Codes for the run record's summary, in the order they happened: a flag
    is written bare, a counted code as `code=n`."""

    def __init__(self) -> None:
        self._entries: dict[str, Optional[int]] = {}

    def flag(self, code: str) -> None:
        self._entries.setdefault(code, None)

    def count(self, code: str, n: int = 1) -> None:
        self._entries[code] = (self._entries.get(code) or 0) + n

    def merge(self, codes: Any) -> None:
        for code, n in codes.items() if isinstance(codes, dict) else []:
            self.count(code, n)

    def summary(self) -> Optional[str]:
        parts = [code if n is None else f"{code}={n}" for code, n in self._entries.items()]
        return "; ".join(parts) or None


def _join(*parts: Optional[str]) -> Optional[str]:
    return "; ".join(part for part in parts if part) or None


def _orchestrator() -> Any:
    """scripts/run_tickets.py, for the `hubspot` runner's own steps (the run
    context, the child environment, the launch-and-map rule). Looked up when a
    run needs them, never imported at load and never for a type: the types are
    run_types', so it does not matter which copy of the script answers."""
    return importlib.import_module("run_tickets")


def _hubspot_api() -> Any:
    return importlib.import_module("ticket_pipeline.hubspot_api")


def _build_classify(settings: dict) -> Callable[[dict], Any]:
    """The classify step both sides go through: `llm.classify`, the small
    model, with the real categories config (D-05). Imported here, on first
    use: a run that compares nothing never loads the model library."""
    import yaml

    from ticket_pipeline import llm

    with (_ROOT / "config" / "categories.yaml").open(encoding="utf-8") as handle:
        categories_cfg = yaml.safe_load(handle) or {}

    def classify(context: dict) -> Any:
        return llm.classify(context, settings, categories_cfg, [])

    return classify


def _build_hubspot_hydrate(settings: dict, read_token: Callable[[], Any]) -> Callable[[str], Any]:
    """Hydration of one HubSpot candidate the engine shadow did not classify in
    this run: one single-ticket search (the pre-gate's search asks for the
    modified time only), then hydration's own function. Raises LookupError when
    there is no token or HubSpot returns no such ticket."""

    def hydrate_one(hubspot_ticket_id: str) -> Any:
        if not (isinstance(hubspot_ticket_id, str) and _HUBSPOT_ID_RE.fullmatch(hubspot_ticket_id)):
            raise LookupError("not a HubSpot ticket id")
        token = read_token()
        if not token:
            raise LookupError("no HubSpot token")
        hs = _hubspot_api()
        answer = hs.search_tickets(
            {
                "filterGroups": [
                    {"filters": [{"propertyName": "hs_object_id", "operator": "EQ", "value": hubspot_ticket_id}]}
                ],
                "properties": hs.TICKET_BASE_PROPERTIES + list(settings.get("ticket_custom_properties") or []),
                "limit": 1,
            },
            token,
        )
        hits = answer.get("results") if isinstance(answer, dict) else None
        if not (isinstance(hits, list) and hits and isinstance(hits[0], dict)):
            raise LookupError("HubSpot returned no such ticket")
        return os_bundle.hydrate.hydrate_ticket(hits[0], settings, token)

    return hydrate_one


def _build_thread_fetch(settings: dict, read_token: Callable[[], Any]) -> Callable[[str], Any]:
    """`os` mode's own thread fetch (two requests, one counted email_thread),
    for a HubSpot-linked ticket only the OS selected."""

    def fetch(hubspot_ticket_id: str) -> Any:
        return os_bundle.fetch_thread(hubspot_ticket_id, settings, read_token())

    return fetch


def _token_reader(deps: Deps) -> Callable[[], Any]:
    """The HubSpot token, read at most once for the comparison and only when a
    ticket needs it."""
    box: dict = {}

    def read() -> Any:
        if "token" not in box:
            token: Any = None
            failure: Optional[str] = None
            try:
                token = deps.read_hubspot_token()
            except Exception as e:  # noqa: BLE001 - no token, then: no HubSpot request
                failure = type(e).__name__
            if failure is not None:
                _log(f"hubspot token unreadable: error={failure}")
            box["token"] = token if isinstance(token, str) and token.strip() else None
        return box["token"]

    return read


def _settings(deps: Deps) -> Optional[dict]:
    loaded: Any = None
    failure: Optional[str] = None
    try:
        loaded = deps.load_settings()
    except Exception as e:  # noqa: BLE001 - the pre-gate decides what that means for the HubSpot path
        failure = type(e).__name__
    if failure is not None:
        _log(f"settings unreadable: error={failure}")
    return loaded if isinstance(loaded, dict) else None


def _comparison_list(api: Any) -> tuple[Optional[list], Optional[str], Optional[str]]:
    """The OS's unprocessed list, asked with COMPARISON_LIMIT: (payloads, None,
    None), or (None, the cause for the summary, the list code). This is the
    snapshot the whole comparison is made against (RESEARCH Pitfall 7)."""
    code: Optional[str] = None
    detail: Optional[str] = None
    answer: Any = None
    try:
        answer = api.fetch_unprocessed(COMPARISON_LIMIT)
    except Exception as e:  # noqa: BLE001 - whatever stopped the list, the HubSpot path goes on
        raised = getattr(e, "code", None)
        if raised in os_pass.LIST_FAILURE_CODES:
            code = raised
        else:
            code, detail = os_pass.LIST_ERROR, type(e).__name__
    payloads: list = []
    if code is None:
        payloads, problem = os_pass._list_payloads(answer)
        if problem is not None:
            code, detail = os_pass.LIST_ERROR, problem
    if code is not None:
        cause = f"{code}:{detail}" if detail else code
        _log(f"OS list not available ({cause}): no comparison this run; the HubSpot path goes on")
        return None, cause, code
    _log(f"OS list answered: tickets={len(payloads)} limit={COMPARISON_LIMIT}")
    return payloads[:COMPARISON_LIMIT], None, None


def _rerun_refs(reruns: Any) -> set:
    """The tickets a pending rerun names, by the orchestrator's own rule for
    which rerun rows get a run-context entry."""
    refs: set = set()
    for row in reruns if isinstance(reruns, (list, tuple)) else []:
        if isinstance(row, dict) and row.get("ticket_id") not in (None, "") and row.get("id") not in (None, ""):
            refs.add(str(row["ticket_id"]))
    return refs


def _lay_signals(context: dict, resolved: dict, reruns: Any) -> None:
    """Lay each resolved candidate's client signal over the run context, as its
    draft's cycle key: the agent's own draft step then goes through the atomic
    replace, keyed as an `os` pass would key it.

    Precedence: a rerun's entry wins over the signal; the signal wins over a
    rollback entry (`hubspot:<run id>`). A candidate the OS did not resolve, or
    reported no signal for, keeps whatever the context already holds for it:
    a rollback entry, or nothing, which is today's plain draft write."""
    reran = _rerun_refs(reruns)
    for hubspot_id, known in resolved.items():
        signal = known.get("signal") if isinstance(known, dict) else None
        if hubspot_id in reran or not isinstance(signal, dict):
            continue
        context[hubspot_id] = {
            "os_ticket_id": known.get("os_ticket_id"),
            "cycle_key": signal["id"],
            "signal_at": signal["at"],
            "signal_id": signal["id"],
        }


def _compare_and_post(
    ctx: RunContext, deps: Deps, api: Any, settings: dict, decision: dict, resolved: dict, os_list: list, notes: _Notes
) -> None:
    """Compare, keep the cache, build the observation and post it. Raises
    whatever goes wrong outside the post itself; `_observe` turns that into a code."""
    read_origin = getattr(deps, "read_os_origin", None) or _tickets_api().read_os_origin
    origin = read_origin()
    read_token = _token_reader(deps)
    cache_file = seen_cache_path(deps.root)
    cache = load_seen_cache(cache_file)
    known = dict(cache)

    result = compare_sources(
        dict(decision, resolved=resolved),
        os_list,
        ComparisonDeps(
            classify=_build_classify(settings),
            hydrate_hubspot_context=_build_hubspot_hydrate(settings, read_token),
            fetch_hubspot_thread=_build_thread_fetch(settings, read_token),
            engine_classification=lambda hubspot_id: _engine_classification(deps.root, hubspot_id, ctx.started_at),
            settings=settings,
            os_origin=origin,
            cache=cache,
        ),
    )
    notes.merge(result.codes)

    if cache != known:
        failure: Optional[str] = None
        try:
            save_seen_cache(cache_file, cache)
        except Exception as e:  # noqa: BLE001 - the cost is one more classify call per ticket, next run
            failure = type(e).__name__
        if failure is not None:
            notes.flag(SOURCE_SHADOW_CACHE_UNWRITABLE)
            _log(f"{SOURCE_SHADOW_CACHE_UNWRITABLE}: error={failure}")

    if result.classifier_unavailable:
        notes.flag(f"{SOURCE_SHADOW_SKIPPED}:classify_unavailable")
        _log("classifier unavailable: no observation is posted for this run")
        return

    body = build_observation(ctx.run_id, ctx.started_at, settings, result.hubspot_side, result.os_side)
    verdict: Any = None
    failure = None
    cause = ""
    try:
        verdict = api.post_source_shadow_observations(body)
    except Exception as e:  # noqa: BLE001 - whatever stopped the post, it is a code
        raised = getattr(e, "code", None)
        failure = raised if raised in os_pass.LIST_FAILURE_CODES else os_pass.LIST_ERROR
        cause = type(e).__name__
    if failure is not None:
        notes.flag(f"{SOURCE_SHADOW_OBSERVATION_FAILED}:{failure}")
        _log(f"observation not posted: code={failure} cause={cause}")
        return
    status = verdict.get("status") if isinstance(verdict, dict) else None
    status = status if isinstance(status, int) and not isinstance(status, bool) else None
    if status is not None and 200 <= status < 300:
        _log(
            f"observation posted: run={ctx.run_id} replayed={verdict.get('replayed') is True} "
            f"hubspot_selected={len(body['hubspot']['selected'])} os_selected={len(body['os']['selected'])}"
        )
        return
    error = verdict.get("error") if isinstance(verdict, dict) else None
    if isinstance(error, str) and _CODE_RE.fullmatch(error):
        detail = error
    else:
        detail = str(status) if status is not None else "unknown"
    notes.flag(f"{SOURCE_SHADOW_OBSERVATION_REFUSED}:{detail}")
    _log(f"observation refused: status={status} detail={detail}")


def _observe(
    ctx: RunContext, deps: Deps, api: Any, settings: Any, decision: dict, resolved: dict, os_list: list, notes: _Notes
) -> None:
    """The comparison half of a run, inside one guard: whatever happens here is
    a code in the summary and never changes the HubSpot path (threat T-93-58)."""
    reason = decision.get("reason")
    if reason not in _KNOWN_SELECTION_REASONS:
        named = reason if isinstance(reason, str) and _CODE_RE.fullmatch(reason) else "unknown"
        notes.flag(f"{SOURCE_SHADOW_SKIPPED}:pregate_{named}")
        _log(f"the pre-gate failed open ({named}): what HubSpot selects is unknown, so nothing is compared")
        return
    if not isinstance(settings, dict):
        notes.flag(f"{SOURCE_SHADOW_SKIPPED}:settings_unreadable")
        return
    failure: Optional[str] = None
    try:
        _compare_and_post(ctx, deps, api, settings, decision, resolved, os_list, notes)
    except Exception as e:  # noqa: BLE001 - the measuring half never fails a run
        failure = type(e).__name__
    if failure is not None:
        notes.flag(f"{SOURCE_SHADOW_COMPARISON_FAILED}:{failure}")
        _log(f"{SOURCE_SHADOW_COMPARISON_FAILED}: error={failure}")


def _result_tickets(path: Any) -> list:
    """The per-ticket entries of the agent's result file. Read only after the
    orchestrator's own reader judged the file complete."""
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return []
    tickets = loaded.get("tickets") if isinstance(loaded, dict) else None
    return tickets if isinstance(tickets, list) else []


def _finalise_ticket(
    ctx: RunContext, deps: Deps, api: Any, item: dict, hubspot_id: str, known: dict, context: dict, notes: _Notes
) -> None:
    """Report one HubSpot-path pass to the OS, through the two helpers `os`
    mode's finaliser uses, with the mode word `shadow`:

        completed, with a draft id        a `completed` write-back; then its
                                          action items, named by the OS ticket
                                          id, with the draft's cycle key as the
                                          pass id
        completed, with NO draft id       a `failed` write-back with
                                          draft_write_failed: a ticket is never
                                          reported processed without its draft
        failed                            a `failed` write-back with the mapped code

    A completed entry whose classification the contract refuses gets no
    write-back (its draft is in place, so it is not a failed pass) and a code.
    The items are posted by this function only when the pass id is stable (the
    client signal's id, or a rerun's key); otherwise they are left to the
    forwarder, exactly as in `hubspot` mode."""
    held = context.get(hubspot_id)
    cycle_key = held.get("cycle_key") if isinstance(held, dict) else None
    signal = known.get("signal")
    entry = {
        "ref": hubspot_id,
        "hubspot_ticket_id": hubspot_id,
        "os_ticket_id": known.get("os_ticket_id"),
        "signal": signal,
        "cycle_key": cycle_key,
    }
    drafted = False
    ticket_result: Optional[dict] = None
    if item.get("outcome") == os_pass.OUTCOME_COMPLETED:
        drafted = _text(item.get("draft_id")) is not None
        if not drafted:
            ticket_result = {"outcome": os_pass.OUTCOME_FAILED, "failure_reason_code": os_pass.DRAFT_WRITE_FAILED}
        else:
            classification = os_pass.normalise_classification(item.get("classification"))
            if classification is None:
                notes.count(os_pass.CLASSIFICATION_UNUSABLE)
                _log(f"{os_pass.CLASSIFICATION_UNUSABLE}: ticket={entry['os_ticket_id']}")
            else:
                ticket_result = {"outcome": os_pass.OUTCOME_COMPLETED, "classification": classification}
    else:
        # the write-back builder maps the agent's own code into the OS's six (`other` for the rest)
        code = item.get("failure_reason_code") if item.get("outcome") == os_pass.OUTCOME_FAILED else None
        ticket_result = {"outcome": os_pass.OUTCOME_FAILED, "failure_reason_code": code}

    if ticket_result is not None:
        wrote = os_pass.post_ticket_writeback(api, entry, ticket_result, ctx.run_id, source.MODE_SHADOW)
        for code in wrote["codes"]:
            notes.count(code)

    signal_id = signal.get("id") if isinstance(signal, dict) else None
    stable = isinstance(cycle_key, str) and (cycle_key == signal_id or cycle_key.startswith(_RERUN_PASS_PREFIX))
    if drafted and stable:
        sent = os_pass.post_ticket_action_items(entry, item.get("action_items"), deps.seen_path)
        for code in sent["codes"]:
            notes.count(code)


def _finalise(
    ctx: RunContext, deps: Deps, api: Any, settings: Any, outcome: RunOutcome, resolved: dict, context: dict, notes: _Notes
) -> None:
    """After the agent: write back what the HubSpot path did with each ticket
    the OS resolved before the launch. Each ticket inside its own guard; nothing
    here changes the outcome's status or counts.

    Nothing is written back when the agent left no complete result (an agent
    outage must not count against every candidate), in a dry run, or for a
    ticket the result does not name (the agent applies its own dedupe: a
    candidate it skipped is not a failed pass)."""
    if not resolved or outcome.failure_reason_code is not None:
        return
    if not isinstance(settings, dict):
        notes.flag(f"{SOURCE_SHADOW_WRITEBACKS_SKIPPED}:settings_unreadable")
        return
    if settings.get("dry_run"):
        _log(f"dry run: no write-back and no action-item post (resolved={len(resolved)})")
        return
    named: set = set()
    for item in _result_tickets(ctx.result_path):
        hubspot_id = item.get("ref") if isinstance(item, dict) else None
        if not isinstance(hubspot_id, str) or hubspot_id in named:
            continue
        named.add(hubspot_id)
        known = resolved.get(hubspot_id)
        if not isinstance(known, dict):
            continue  # the mirror had not seen it: today's behaviour, nothing written back
        failure: Optional[str] = None
        try:
            _finalise_ticket(ctx, deps, api, item, hubspot_id, known, context, notes)
        except Exception as e:  # noqa: BLE001 - one ticket never stops the others
            failure = type(e).__name__
        if failure is not None:
            notes.count(SOURCE_SHADOW_FINALISE_TICKET_ERROR)
            _log(f"{SOURCE_SHADOW_FINALISE_TICKET_ERROR}: ticket={known.get('os_ticket_id')} error={failure}")


def run_source_shadow(ctx: RunContext, deps: Deps) -> RunOutcome:
    """`shadow` mode: the HubSpot path, authoritative and unchanged, plus the
    source comparison. Registered in `run_tickets.RUNNER_SPECS["shadow"]`.

    In order:

        0. the routine gate. Paused: one `skipped` outcome, nothing asked.
        1. THE OS LIST, first, with limit 100: the snapshot the comparison is
           made against. If it cannot be had the comparison is skipped for
           this run (a code) and the HubSpot path goes on.
        2. the HubSpot decision, by the one candidate rule (`deps.decide`),
           exactly as the `hubspot` runner asks it.
        3. each candidate is asked of the OS's single-ticket route (which OS
           ticket, which client signal). Skipped when the list was refused for
           its signature or its secret: the OS would refuse these too.
        4. when the agent will run: the run context, started from the rollback
           entries exactly as the `hubspot` runner starts it, then each resolved
           candidate's signal laid over it, then the reruns over that; and the
           engine-shadow step, as the `hubspot` runner runs it.
        5. THE COMPARISON and THE OBSERVATION, inside one guard. Posted before
           the agent, so before every write-back of this run: the OS computes
           its reasons at ingest.
        6. the agent, through the `hubspot` runner's own launch-and-map rule,
           with its command, its model and no tool restriction.
        7. the write-backs and the action items of what the agent completed.

    WHAT A `shadow` RUN WRITES TO THE OS
        - one observation (step 5);
        - per ticket the agent's result names AND the OS resolved in step 3: a
          write-back with source mode `shadow` (`completed`, or `failed`), and
          for a completed one with a stable pass id its action-item envelope
          with the OS ticket id and the pass id.
    Nothing is written for a ticket only the OS selected. This runner writes no
    draft: every draft of the run is the agent's own, so the OS half cannot add
    a second one. It never writes the HubSpot path's own state file either; in
    this mode the agent keeps that file, as in `hubspot` mode.

    WHAT AN OS FAILURE DOES
    Nothing to the HubSpot half. The outcome's status, counts and failure code
    are the agent's own in every case; a failure of the comparison, of a lookup
    or of a write-back is a code appended to `error_summary`. `hubspot_calls`
    stays None, as in `hubspot` mode: the agent's calls cannot be observed.
    """
    orchestrator = _orchestrator()
    if not deps.read_routine_enabled(run_types.ROUTINE):
        _log("routine paused (or gate unreachable): nothing is asked of anyone")
        return RunOutcome(status=run_types.STATUS_SKIPPED, error_summary=orchestrator.SUMMARY_ROUTINE_PAUSED)

    notes = _Notes()
    settings = _settings(deps)
    api = getattr(deps, "os_api", None)
    if api is None:
        api = _tickets_api()

    # 1. the OS list, FIRST
    os_list, cause, list_code = _comparison_list(api)
    if cause is not None:
        notes.flag(f"{SOURCE_SHADOW_SKIPPED}:{cause}")

    # 2. the HubSpot decision, as the hubspot runner asks it
    decision = deps.decide(ctx.mode, deps)
    kind = decision.get("decision") if isinstance(decision, dict) else None
    if kind == "paused":
        return RunOutcome(status=run_types.STATUS_SKIPPED, error_summary=orchestrator.SUMMARY_ROUTINE_PAUSED)
    decision = decision if isinstance(decision, dict) else {}

    # 3. which OS ticket is each candidate?
    resolved: dict = {}
    if decision.get("reason") in _KNOWN_SELECTION_REASONS and list_code not in os_pass.NO_RETRY_CODES:
        lookup_codes: dict = {}
        resolved = resolve_hubspot_candidates(decision.get("candidates"), deps, lookup_codes)
        notes.merge(lookup_codes)

    # 4. the run context and the engine-shadow step, when the agent will run.
    # `no_work` is the only answer that does not launch: this path is fail-open.
    launch = kind != "no_work"
    context: dict = {}
    env: Any = None
    context_note: Optional[str] = None
    if launch:
        reruns = decision.get("reruns")
        context, context_note = orchestrator.build_run_context(ctx, deps, reruns)
        _lay_signals(context, resolved, reruns)
        orchestrator.write_run_context(ctx, context)
        env = orchestrator.child_environment(ctx, deps)
        step_failure: Optional[str] = None
        try:
            deps.engine_shadow_step(env)
        except Exception as e:  # noqa: BLE001 - the engine shadow never fails a run
            step_failure = type(e).__name__
        if step_failure is not None:
            _log(f"engine_shadow_step_failed:{step_failure}")

    # 5. the comparison and the observation, before the agent and its write-backs
    if cause is None:
        _observe(ctx, deps, api, settings, decision, resolved, os_list or [], notes)

    if not launch:
        return RunOutcome(status=run_types.STATUS_SUCCESS, error_summary=notes.summary())

    # 6. the agent: the hubspot runner's command and its launch-and-map rule
    outcome = orchestrator.launch_and_collect(
        ctx, deps, orchestrator.agent_command(), env, [context_note] if context_note else []
    )

    # 7. what the HubSpot path did, reported to the OS
    failure: Optional[str] = None
    try:
        _finalise(ctx, deps, api, settings, outcome, resolved, context, notes)
    except Exception as e:  # noqa: BLE001 - reporting never changes the outcome
        failure = type(e).__name__
    if failure is not None:
        notes.flag(f"{SOURCE_SHADOW_FINALISE_FAILED}:{failure}")
        _log(f"{SOURCE_SHADOW_FINALISE_FAILED}: error={failure}")
    outcome.error_summary = _join(outcome.error_summary, notes.summary())
    return outcome
