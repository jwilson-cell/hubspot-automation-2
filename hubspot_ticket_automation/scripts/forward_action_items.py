#!/usr/bin/env python3
"""Deterministic forwarder — push extracted action items to Pack'N OS /tasks.

Phase 15.1 cutover fix (2026-06-04). The agentic `claude -p /packn-tickets`
run was EXTRACTING action items and narrating "posted to /tasks" WITHOUT ever
running the forwarding command in SKILL step 2g.5 — so 0 tasks landed despite
items being found (diagnosed 2026-06-04: 0 `post_action_items` lines in the
cron log, 0 hubspot_action_item rows in the OS DB, yet run summaries claimed
"all posted"). This script removes the LLM from the forwarding loop.

It runs deterministically AFTER the ticket pass (via scripts/run_tickets.sh),
reads the queue file the skill reliably writes in step 2g
(config/pending_actions.json), and POSTs each ticket's non-empty `action_items`
to the OS ingestion route via the existing post_action_items helper.

Forward-once semantics: a local seen-state file
(config/.action_items_forwarded.json, gitignored) records the
(ticket_id, action_type) pairs already delivered, so:
  - an accumulating pending_actions.json is never re-POSTed, and
  - already-delivered items are never re-fired across ET-day boundaries
    (which would otherwise resurrect resolved tasks via the OS fresh-emit path).
The OS route is ALSO idempotent (dedups by ticket+action_type+ET-day) as a
backstop if the seen-state file is ever lost.

Non-blocking: never raises out; logs a one-line summary and exits 0 so it can
never break the cron pipeline. Stdlib + the existing post_action_items module
only — no new dependencies (sibling CLAUDE.md invariant).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# scripts/ is sys.path[0] when run as `.venv/bin/python scripts/forward_action_items.py`,
# so the sibling helper imports directly. post_action_items has no import-time
# side effects (only constants + an `if __name__ == "__main__"` guard).
import post_action_items as poster

ROOT = Path(__file__).resolve().parent.parent
QUEUE_PATH = ROOT / "config" / "pending_actions.json"
SEEN_PATH = ROOT / "config" / ".action_items_forwarded.json"


def _log(msg: str) -> None:
    print(f"[forward_action_items] {msg}", file=sys.stderr)


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8") or "null") or default
    except (json.JSONDecodeError, OSError) as exc:
        _log(f"could not parse {path.name}: {exc!r} — using {default!r}")
        return default


def _key(ticket_id: str, action_type: str) -> str:
    return f"{ticket_id}:{action_type}"


def main() -> int:
    records = _load_json(QUEUE_PATH, [])
    if not isinstance(records, list):
        _log(f"{QUEUE_PATH.name} is not a JSON array — nothing to forward.")
        return 0

    seen_raw = _load_json(SEEN_PATH, [])
    seen = set(seen_raw) if isinstance(seen_raw, list) else set()

    forwarded = already = errors = 0
    fresh_keys: set[str] = set()

    for rec in records:
        if not isinstance(rec, dict):
            continue
        ticket_id = str(rec.get("ticket_id") or "").strip()
        items = rec.get("action_items") or []
        if not ticket_id or not isinstance(items, list) or not items:
            continue

        # Forward only items not already delivered (by ticket + action_type).
        fresh = [
            it
            for it in items
            if isinstance(it, dict)
            and it.get("action_type")
            and _key(ticket_id, str(it["action_type"])) not in seen
        ]
        already += len(items) - len(fresh)
        if not fresh:
            continue

        result = poster.post_action_items(ticket_id, fresh, dry_run=False)
        if result.get("ok"):
            forwarded += len(fresh)
            for it in fresh:
                fresh_keys.add(_key(ticket_id, str(it["action_type"])))
            status = result.get("status", "")
            _log(
                f"ticket {ticket_id}: forwarded {len(fresh)} item(s) "
                f"({result.get('mode')} {status})".rstrip()
            )
        else:
            # Not marked seen → retried on the next run (transient secret/url/net).
            errors += len(fresh)
            reason = result.get("reason") or result.get("error") or "unknown"
            _log(
                f"ticket {ticket_id}: forward FAILED "
                f"({result.get('mode')}: {reason}) — will retry next run"
            )

    if fresh_keys:
        seen |= fresh_keys
        try:
            SEEN_PATH.write_text(json.dumps(sorted(seen)), encoding="utf-8")
        except OSError as exc:
            _log(f"could not persist seen-state ({exc!r}); OS idempotency still dedups")

    _log(
        f"done: forwarded={forwarded} already_delivered={already} "
        f"errors={errors} records={len(records)}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — never break the cron pipeline.
        _log(f"unexpected error: {exc!r}")
        sys.exit(0)
