"""Where the cron gets its tickets, and which of them are new work (Phase 93).

ONE SETTING
-----------
The owner picks the ticket source in Pack'N OS: `hubspot` (the cron searches
HubSpot, as it always has), `shadow` (the HubSpot path stays authoritative and
the OS list is compared with it) or `os` (the cron takes its list from the OS
and makes no HubSpot search). The choice is one row of the OS's `app_settings`
table, key `ticket_source_mode`, value `{"mode": "<one of the three>"}`, read
over the cron's existing direct database connection.

The read has three outcomes and no fourth:

    a mode               the row is absent (-> `hubspot`), or it holds an object
                         whose `mode` is exactly one of the three values; other
                         keys in the object are ignored
    source_unreadable    the setting could not be read (database down, grant
                         missing, a reader that did not answer the question)
    source_unknown       a row is there and its value is anything else — a stored
                         JSON null, an empty object, another spelling, a bare
                         string, a list

The last two are errors (`SourceModeError`). Neither is ever folded into a
default: a cron that cannot tell which source it was told to trust stops, it
does not guess `hubspot`. Only an ABSENT row means `hubspot`. The OS's own
reader applies the same absent / valid / unrecognised rule, value for value.

A RUN FINISHES IN THE MODE IT STARTED IN
----------------------------------------
The orchestrator resolves the mode once and exports it as `PACKN_SOURCE_MODE`.
Every child takes it from there (`current_mode`); only a process started
outside the orchestrator — a hand-run — reads the setting itself.

ONE CANDIDATE RULE
------------------
The search body and the fingerprint dedupe live here and nowhere else. The
pre-gate (`scripts/pregate_tickets.py`, which re-exports them) and the
engine-shadow pipeline (`ticket_pipeline/run.py`) import them, so the two can
never see different queues.

This module imports the standard library only. The database client is imported
inside `resolve_mode`, so the pure functions stay usable without a driver.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Optional

MODE_HUBSPOT = "hubspot"
MODE_SHADOW = "shadow"
MODE_OS = "os"
MODES = (MODE_HUBSPOT, MODE_SHADOW, MODE_OS)

# What an ABSENT settings row means. Nothing else resolves to it.
DEFAULT_MODE = MODE_HUBSPOT

# The `app_settings.key` of the setting (the client's query names the same key).
SETTING_KEY = "ticket_source_mode"

# The variable the orchestrator sets so that children do not read the setting again.
MODE_ENV = "PACKN_SOURCE_MODE"

SOURCE_UNREADABLE = "source_unreadable"
SOURCE_UNKNOWN = "source_unknown"
ERROR_CODES = (SOURCE_UNREADABLE, SOURCE_UNKNOWN)


class SourceModeError(Exception):
    """The source mode could not be established. `code` is one of ERROR_CODES.

    The message is the code and nothing else: no stored value and no driver text
    travels on it.
    """

    def __init__(self, code: str) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"not a source mode error code: {code!r}")
        super().__init__(code)
        self.code = code


def _log(msg: str) -> None:
    print(f"[source] {msg}", file=sys.stderr)


# ===========================================================================
# THE MODE
# ===========================================================================


def validate_mode(mode: Any) -> str:
    """`mode` when it is exactly one of the three values, else `source_unknown`."""
    if isinstance(mode, str) and mode in MODES:
        return mode
    raise SourceModeError(SOURCE_UNKNOWN)


def parse_mode(found: bool, value: Any) -> str:
    """The mode a settings read means.

    `found` is whether a row exists; `value` is its stored JSON value, which can
    itself be None (a stored JSON null) — which is why the two travel separately.

        not found                                   -> `hubspot` (the default)
        found, an object whose `mode` is one of
        the three lower-case strings                -> that mode (other keys ignored)
        found, anything else                        -> SourceModeError(source_unknown)
    """
    if not found:
        return DEFAULT_MODE
    if isinstance(value, dict):
        return validate_mode(value.get("mode"))
    raise SourceModeError(SOURCE_UNKNOWN)


def resolve_mode(reader: Optional[Callable[[], Any]] = None) -> str:
    """Read the setting and return the mode, or raise SourceModeError.

    `reader` returns `{"found": bool, "value": <stored JSON value>}` and raises
    when the setting cannot be read. The default is
    `packn_os_hubspot_client.client.read_ticket_source_mode`.

    Any exception from the reader — and a reader result that does not say
    whether a row was found — is `source_unreadable`. There is no default here.
    """
    failure: Optional[str] = None
    result: Any = None
    try:
        if reader is None:
            from packn_os_hubspot_client import client  # lazy: needs the database driver

            reader = client.read_ticket_source_mode
        result = reader()
    except Exception as e:  # noqa: BLE001 — every failure of the read is the same outcome
        failure = type(e).__name__
    # Raised outside the handler: an exception raised inside it would carry the
    # driver's error (and whatever its message holds) as its context.
    if failure is not None:
        _log(f"mode read failed ({failure}) -> {SOURCE_UNREADABLE}")
        raise SourceModeError(SOURCE_UNREADABLE)
    if not isinstance(result, dict) or not isinstance(result.get("found"), bool):
        _log(f"mode read answered no found/value pair -> {SOURCE_UNREADABLE}")
        raise SourceModeError(SOURCE_UNREADABLE)
    return parse_mode(result["found"], result.get("value"))


def mode_from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The mode the orchestrator exported, or None when it exported none.

    Unset and empty both mean "not exported". A value that is not exactly one of
    the three is `source_unknown` — it is never replaced by a read of the setting.
    """
    raw = (os.environ if environ is None else environ).get(MODE_ENV)
    if raw is None or raw == "":
        return None
    return validate_mode(raw)


def current_mode(
    reader: Optional[Callable[[], Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """The mode of this process: the exported value when there is one, else one
    read of the setting. Raises SourceModeError; never returns a default for an
    error."""
    exported = mode_from_env(environ)
    if exported is not None:
        return exported
    return resolve_mode(reader)


# ===========================================================================
# THE SKILL'S GUARD (Phase 93, plan 15) — SKELETON: names only, no behaviour
# ===========================================================================

GUARD_EXIT_SERVED = 0
GUARD_EXIT_USAGE = 2
GUARD_EXIT_NOT_SERVED = 3
GUARD_EXIT_NOT_ESTABLISHED = 4
TOKEN_NOT_SERVED = "SOURCE_MODE_NOT_SERVED"
TOKEN_UNKNOWN = "SOURCE_MODE_UNKNOWN"
TOKEN_UNREADABLE = "SOURCE_MODE_UNREADABLE"
RUN_ID_ENV = "PACKN_RUN_ID"
GUARD_ROUTINE = "tickets-process"


def skill_guard(
    served: Any,
    env: Optional[Mapping[str, str]] = None,
    reader: Optional[Callable[[], Any]] = None,
    write_record: Optional[Callable[..., Any]] = None,
) -> tuple[Any, Any]:
    return None, None


def main(argv: Optional[list] = None) -> int:
    return None  # type: ignore[return-value]


# ===========================================================================
# THE CANDIDATE RULE (pure: no HTTP, no database, no filesystem)
# Unit-tested in packn_os_hubspot_client/tests/test_pregate.py through the
# pre-gate's re-exports, and in tests/test_source_modes.py.
# ===========================================================================


def iso_to_ms(value: Optional[str]) -> Optional[int]:
    """ISO-8601 (Z or offset) -> epoch ms, or None on any parse problem."""
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def parse_last_run_at(value: Optional[str], now_ms: Optional[int] = None) -> int:
    """state.json last_run_at -> epoch ms; empty/missing/bad -> 24h ago.

    Mirrors SKILL.md Preconditions #3: "If empty, use the timestamp from
    24h ago." A stale-but-parseable value is used as-is — a wide window only
    means more candidates, which the agent's own dedupe handles.
    """
    parsed = iso_to_ms(value)
    if parsed is not None:
        return parsed
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    return now_ms - int(timedelta(hours=24).total_seconds() * 1000)


def build_search_body(
    last_run_ms: int,
    active_stages: list[str],
    require_last_message_from_visitor: bool,
    per_run_cap: int,
) -> dict:
    """The tickets/search body — SAME filters as SKILL.md Step 1.

    Only hs_lastmodifieddate is hydrated: the pre-gate decides launch/skip,
    it never builds ticket context (that stays the agent's job).
    """
    filters: list[dict] = [
        {
            "propertyName": "hs_lastmodifieddate",
            "operator": "GT",
            "value": last_run_ms,
        },
        {
            "propertyName": "hs_pipeline_stage",
            "operator": "IN",
            "values": [str(s) for s in active_stages],
        },
    ]
    if require_last_message_from_visitor:
        filters.append(
            {
                "propertyName": "hs_last_message_from_visitor",
                "operator": "EQ",
                "value": "true",
            }
        )
    return {
        "filterGroups": [{"filters": filters}],
        "properties": ["hs_lastmodifieddate"],
        "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
        "limit": per_run_cap,
    }


def unprocessed_candidates(results: list[dict], fingerprints: dict) -> list[str]:
    """Filter search hits through the state.json fingerprint dedupe.

    Approximation of SKILL.md Step 2a-pre (which compares num_notes): a
    ticket is NOT new work when either
      (a) its fingerprint recorded an hs_lastmodifieddate >= the ticket's
          current one — nothing changed since the skill last processed it; or
      (b) its fingerprint's processed_at >= the ticket's current
          hs_lastmodifieddate — every HubSpot-side change we know of predates
          the moment the skill FINISHED that ticket. This closes the
          echo-launch hole: the skill's own step-2a.6 property backfill bumps
          hs_lastmodifieddate DURING processing (i.e. before processed_at is
          stamped), so without (b) every productive tick re-flags its tickets
          once and wastes one agent launch. A genuinely new customer message
          arrives AFTER processed_at, bumps lastmodified past it, and still
          fails open. Relies only on sane NTP clocks (sub-second skew vs the
          minutes-long gap between backfill and fingerprint write).

    A fingerprint written by code (Phase 93). In `os` and `shadow` mode the
    fingerprint of a completed ticket is written by code, not by the agent:
    `{processed_at: <when the pass finished>, hs_lastmodifieddate: <the OS's
    mirrored value>, num_notes: null}`. The OS holds no note count, so there is
    no `num_notes` to compare. Such a fingerprint is judged by `processed_at`
    — branch (b) above: the ticket is not new work until HubSpot reports a
    change later than the moment the pass finished. That is what makes a
    rollback to `hubspot` safe: a ticket completed in `os` mode is not a
    HubSpot candidate afterwards, and it becomes one again as soon as it
    really changes. No sentinel `num_notes` is ever written — a large one
    would silence the ticket for good. `num_notes` is not read here at all.

    Every ambiguous case (no fingerprint, no comparable timestamps) counts as
    a candidate: a false positive costs one agent launch whose own dedupe
    skips the ticket; a false negative would silently drop work.
    """
    candidates: list[str] = []
    for result in results:
        ticket_id = str(result.get("id", ""))
        if not ticket_id:
            continue
        current_ms = iso_to_ms((result.get("properties") or {}).get("hs_lastmodifieddate"))
        fp = fingerprints.get(ticket_id)
        if not isinstance(fp, dict):
            candidates.append(ticket_id)
            continue
        fp_ms = iso_to_ms(fp.get("hs_lastmodifieddate"))
        processed_ms = iso_to_ms(fp.get("processed_at"))
        if current_ms is not None and (
            (fp_ms is not None and current_ms <= fp_ms)
            or (processed_ms is not None and current_ms <= processed_ms)
        ):
            continue
        candidates.append(ticket_id)
    return candidates
