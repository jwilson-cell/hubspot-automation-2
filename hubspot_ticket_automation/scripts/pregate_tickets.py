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

EXIT CODES (consumed by scripts/run_tickets.sh)
-----------------------------------------------
0  = run the agentic pass (work exists, reruns pending, or pre-gate unsure)
10 = skip — no new work (a status='success' 0-ticket run record was written)
11 = skip — routine paused / gate fail-closed (status='skipped' record written)

Stdlib + packn_os_hubspot_client only — no new dependencies (CLAUDE.md
invariant). PyYAML is already in scripts/requirements.txt.

State ownership: this script NEVER writes config/state.json. last_run_at and
ticket_fingerprints stay owned by the skill; the pre-gate only reads them.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ROUTINE = "tickets-process"
# Marker substring identifying our own crontab line (the crontab invokes
# `bash scripts/run_tickets.sh`). Keep in sync with the wrapper's filename.
CRON_MARKER = "run_tickets.sh"

HUBSPOT_BASE = "https://api.hubapi.com"
TOKEN_PATH = ROOT / "config" / ".secrets" / "hubspot_token.txt"
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
STATE_PATH = ROOT / "config" / "state.json"

RATE_FLOOR_S = 0.34  # same hard per-call floor as backfill_complaints.py
RETRY_SLEEP_S = 2.0  # single retry backoff on 429/5xx

EXIT_RUN_AGENT = 0
EXIT_SKIP_NO_WORK = 10
EXIT_SKIP_PAUSED = 11


def _log(msg: str) -> None:
    print(f"[pregate] {msg}", file=sys.stderr)


# ===========================================================================
# PURE LAYER (unit-tested in packn_os_hubspot_client/tests/test_pregate.py).
# No HTTP, no DB, no filesystem. Do not add side effects here.
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


# ===========================================================================
# SIDE-EFFECT LAYER
# ===========================================================================


def _load_settings() -> dict:
    import yaml  # lazy: keeps the pure layer importable without PyYAML

    with open(SETTINGS_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        return {}


def _read_token() -> Optional[str]:
    try:
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        return token or None
    except OSError:
        return None


def _acquire_rate_token() -> None:
    """Shared-bucket token + hard sleep floor (T-20-05-01 posture: the token
    degrades to passthrough without REDIS_URL, so the sleep is the real guard)."""
    try:
        from packn_os_hubspot_client.rate_limit import acquire_hubspot_token

        acquire_hubspot_token()
    except Exception:
        pass
    time.sleep(RATE_FLOOR_S)


def _search_tickets(body: dict, token: str) -> Optional[dict]:
    """POST /crm/v3/objects/tickets/search; one retry on 429/5xx; None on failure."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/tickets/search"
    data = json.dumps(body).encode("utf-8")
    for attempt in (1, 2):
        _acquire_rate_token()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            _log(f"search HTTP {e.code} (attempt {attempt})")
            if e.code in (429,) or e.code >= 500:
                time.sleep(RETRY_SLEEP_S)
                continue
            return None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            _log(f"search transport error (attempt {attempt}): {e!r}")
            time.sleep(RETRY_SLEEP_S)
    return None


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


def main() -> int:
    started_iso = datetime.now(timezone.utc).isoformat()

    from packn_os_hubspot_client import client

    # Step 0a equivalent — fire-and-forget crontab report (never raises).
    client.report_routine_schedule(ROUTINE, CRON_MARKER)

    # Step 0b equivalent — fail-closed routine gate (returns False on DB
    # error too, per CONTEXT D-01). Paused => the agent would exit at its own
    # gate anyway; skip the launch and write the same 'skipped' record.
    if not client.read_routine_enabled(ROUTINE):
        _log("routine paused (or gate unreachable) — skipping agentic run")
        _write_run_record(
            "skipped",
            "pregate: routine paused via Pack'N OS (or gate unreachable — fail-closed)",
            started_iso,
        )
        return EXIT_SKIP_PAUSED

    # Operator rerun requests are always agent work (D-09).
    reruns = client.read_pending_rerun_requests(ROUTINE)
    if reruns:
        _log(f"{len(reruns)} pending rerun request(s) — launching agent")
        return EXIT_RUN_AGENT

    try:
        settings = _load_settings()
        active_stages = [str(s) for s in settings.get("active_stages") or []]
        per_run_cap = int(settings.get("per_run_cap", 25))
        require_visitor = bool(settings.get("require_last_message_from_visitor", False))
        if not active_stages:
            _log("active_stages empty in settings.yaml — deferring to agent")
            return EXIT_RUN_AGENT
    except Exception as e:
        _log(f"settings unreadable — deferring to agent: {e!r}")
        return EXIT_RUN_AGENT

    token = _read_token()
    if token is None:
        _log("hubspot token missing — deferring to agent")
        return EXIT_RUN_AGENT

    state = _load_state()
    last_run_ms = parse_last_run_at(state.get("last_run_at"))
    fingerprints = state.get("ticket_fingerprints") or {}

    body = build_search_body(last_run_ms, active_stages, require_visitor, per_run_cap)
    response = _search_tickets(body, token)
    if response is None:
        _log("hubspot search failed — deferring to agent")
        return EXIT_RUN_AGENT

    results = response.get("results") or []
    candidates = unprocessed_candidates(results, fingerprints)
    if candidates:
        _log(
            f"{len(candidates)} candidate ticket(s) after dedupe "
            f"({len(results)} matched) — launching agent"
        )
        return EXIT_RUN_AGENT

    _log(f"no new tickets ({len(results)} matched, all deduped) — skipping agentic run")
    _write_run_record("success", None, started_iso)
    return EXIT_SKIP_NO_WORK


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
