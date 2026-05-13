"""Cheap pre-flight check that decides whether `claude -p /packn-tickets` needs to run.

Each Claude Code invocation costs ~$0.30-$1 in Sonnet API tokens before any
actual work happens (skill prompt + tool schemas + MCP registrations). On
most cron ticks there are zero new/updated tickets, so the entire run is
wasted spend. This script replicates the skill's candidate-detection step
using one HubSpot REST call (free) and exits with a code the cron wrapper
uses to decide whether to invoke Claude.

EXIT CODES
    0   work to do — cron wrapper should invoke Claude Code
    99  no work — cron wrapper should skip Claude Code
    *   any other code (errors, network failures) — cron wrapper should
        fail-open and invoke Claude anyway. The skill's own dedupe layer
        handles whatever the pre-filter couldn't see, and falling back to
        the status quo costs no more than today's spend.

CRON WRAPPER PATTERN
    .venv/bin/python scripts/pre_filter_tickets.py
    rc=$?
    if [ $rc -eq 99 ]; then
        echo "[pre-filter] no work; skipping claude invocation" >> outputs/runs/cron-tickets.log
        exit 0
    fi
    claude -p /packn-tickets --model claude-sonnet-4-6 --dangerously-skip-permissions >> outputs/runs/cron-tickets.log 2>&1

LOGIC
    1. Load config/state.json → last_run_at + ticket_fingerprints
    2. Load config/settings.yaml → pipeline_id, active_stages, last-message filter
    3. POST /crm/v3/objects/tickets/search with the same filter the skill uses,
       requesting properties ['num_notes', 'hs_lastmodifieddate']
    4. For each candidate, mark as "needs work" if any of:
         - ticket id not in fingerprints (new ticket)
         - num_notes > fingerprint.num_notes (customer/agent added content since last run)
    5. Exit 0 if any candidate needs work, 99 otherwise

NON-GOALS
    - Replacing the skill's own dedupe. The skill runs its own check after
      Claude Code starts; pre-filter is just there to avoid paying for the
      Claude Code overhead on quiet ticks.
    - Mutating state.json. This script is strictly read-only.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
TOKEN_PATH = ROOT / "config" / ".secrets" / "hubspot_token.txt"
STATE_PATH = ROOT / "config" / "state.json"
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
API_BASE = "https://api.hubapi.com"

EXIT_PROCEED = 0
EXIT_SKIP = 99
EXIT_ERROR_FAIL_OPEN = 0  # errors regress to status quo: invoke Claude.


def _log(msg: str) -> None:
    """Single-line stderr log so cron-tickets.log captures the decision."""
    print(f"[pre-filter] {msg}", file=sys.stderr)


def _iso_to_ms(iso: str) -> int:
    """HubSpot search API takes datetime filters as millisecond epoch ints."""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def _search_candidates(
    token: str, pipeline_id: str, active_stages: list[str], require_visitor: bool,
    last_run_at_ms: int | None,
) -> list[dict[str, Any]]:
    """Single HubSpot search call replicating the skill's filter."""
    filters: list[dict[str, Any]] = [
        {"propertyName": "hs_pipeline", "operator": "EQ", "value": pipeline_id},
        {"propertyName": "hs_pipeline_stage", "operator": "IN", "values": active_stages},
    ]
    if require_visitor:
        filters.append({
            "propertyName": "hs_last_message_from_visitor",
            "operator": "EQ",
            "value": "true",
        })
    if last_run_at_ms is not None:
        filters.append({
            "propertyName": "hs_lastmodifieddate",
            "operator": "GT",
            "value": str(last_run_at_ms),
        })

    body = {
        "filterGroups": [{"filters": filters}],
        "properties": ["num_notes", "hs_lastmodifieddate", "hs_pipeline_stage"],
        "limit": 100,
        "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}],
    }
    req = urllib.request.Request(
        f"{API_BASE}/crm/v3/objects/tickets/search",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode("utf-8") or "{}")
    return payload.get("results", [])


def _needs_work(
    candidates: list[dict[str, Any]], fingerprints: dict[str, dict[str, Any]],
) -> tuple[int, int]:
    """Return (count_needing_work, count_valid_candidates).

    Candidates without an `id` are dropped from both counts — they're a
    HubSpot anomaly the skill couldn't process either, so omitting them
    keeps the log message honest.
    """
    needs = 0
    total = 0
    for t in candidates:
        ticket_id = str(t.get("id", ""))
        if not ticket_id:
            continue
        total += 1
        props = t.get("properties") or {}
        try:
            current_notes = int(props.get("num_notes") or 0)
        except (TypeError, ValueError):
            current_notes = 0
        fp = fingerprints.get(ticket_id)
        if fp is None:
            needs += 1
            continue
        try:
            prior_notes = int(fp.get("num_notes") or 0)
        except (TypeError, ValueError):
            prior_notes = 0
        if current_notes > prior_notes:
            needs += 1
    return needs, total


def main() -> int:
    try:
        if not TOKEN_PATH.exists():
            _log(f"token missing at {TOKEN_PATH} — fail-open")
            return EXIT_ERROR_FAIL_OPEN
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()

        settings = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
        pipeline_id = str(settings.get("pipeline_id", "0"))
        active_stages = [str(s) for s in (settings.get("active_stages") or ["1", "3"])]
        require_visitor = bool(settings.get("require_last_message_from_visitor", True))

        state: dict[str, Any] = {}
        if STATE_PATH.exists():
            state = json.loads(STATE_PATH.read_text(encoding="utf-8") or "{}")
        fingerprints = state.get("ticket_fingerprints") or {}
        last_run_at = state.get("last_run_at")
        last_run_at_ms = _iso_to_ms(last_run_at) if last_run_at else None

        candidates = _search_candidates(
            token, pipeline_id, active_stages, require_visitor, last_run_at_ms,
        )
        needs, total = _needs_work(candidates, fingerprints)

        if needs == 0:
            _log(f"no work — {total} candidates, all fingerprinted; skipping claude")
            return EXIT_SKIP
        _log(f"work to do — {needs}/{total} candidates new or updated; invoking claude")
        return EXIT_PROCEED

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        _log(f"HubSpot HTTP {e.code}: {body} — fail-open")
        return EXIT_ERROR_FAIL_OPEN
    except urllib.error.URLError as e:
        _log(f"network error: {e.reason} — fail-open")
        return EXIT_ERROR_FAIL_OPEN
    except Exception as e:
        _log(f"unexpected error {type(e).__name__}: {e} — fail-open")
        return EXIT_ERROR_FAIL_OPEN


if __name__ == "__main__":
    sys.exit(main())
