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

THE COMPARISON CORE (this half of the module)
---------------------------------------------
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

from ticket_pipeline import os_bundle, os_pass

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


def run_source_shadow(ctx: Any, deps: Any) -> Any:
    """The `shadow` mode runner. Built by this plan's second task."""
    return None
