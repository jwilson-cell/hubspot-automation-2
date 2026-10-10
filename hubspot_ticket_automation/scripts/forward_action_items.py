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
(ticket_id, action_type) pairs whose delivery the OS CONFIRMED (response body
errors == 0 — HTTP 200 alone is NOT trusted; see the false-seen note in the
loop below), so:
  - an accumulating pending_actions.json is never re-POSTed, and
  - already-delivered items are never re-fired across ET-day boundaries
    (which would otherwise resurrect resolved tasks via the OS fresh-emit path).
The OS route is ALSO idempotent (dedups by ticket+action_type+ET-day) as a
backstop if the seen-state file is ever lost.

THE RETRY FILE (Phase 93, plan 17 — runbook tag RETRY-IDS)
---------------------------------------------------------
When the ticket source is the OS, code posts a ticket's action items itself
(ticket_pipeline/os_pass.py), with the OS ticket id and the pass id, and this
script then finds only leftovers: items whose post was not confirmed. The
queue's records do not hold those two ids, so before it posts, that code
writes them to a second local file beside the seen-set:

    config/.action_items_retry.json   (gitignored; ids only, never ticket text)
    { "<ticket key>:<action type>": { "os_ticket_id": "<uuid>", "pass_id": "<pass id>" } }

For an item whose key is in that file this script sends the entry's
`os_ticket_id` and `pass_id` with the envelope, and drops the entry once the
post is confirmed. A key held in the retry file is sent even when the seen-set
already holds it: a later pass re-created the item and its post is still owed.
A record with no retry entry is sent exactly as before; a record whose key is
uuid-shaped (a ticket with no HubSpot id) is sent as `os_ticket_id`.

WHICH RECORD A PASS'S RETRY IS RESENT FROM (review WR-46)
The queue keeps one record per pass of a ticket, oldest first, and no record
names its pass. An entry that names a pass is owed ONE post: that pass's. So
it is resent from the NEWEST record of its ticket that holds the action type,
the record the owed pass's run appended, and never from an older one: an
older record's item under the new pass id would deliver a stale item as the
current one and leave the current one unsent. Older records' items of such a
key are not sent at all (`superseded` in the summary line). The pass is part
of the entry's identity in the other direction too: a confirmation drops the
entry only while it still names the pass that was confirmed, so an entry a
newer pass wrote meanwhile is kept. An entry that names no pass (its cycle key
was not one the OS accepts) is matched as before: the first record holding the
action type.

The limit, stated: the file holds ids, so what is resent is the queue's copy
of the item, found by position. If the owed pass's run queued no record for
the ticket, the newest record is an earlier pass's.

The retry file's path is derived from the seen-set's, so one injected path
moves both. Every write here goes through a temp file and a rename.

Non-blocking: never raises out; logs a one-line summary and exits 0 so it can
never break the cron pipeline. Stdlib + the existing post_action_items module
only — no new dependencies (sibling CLAUDE.md invariant). It logs ids, codes
and counts; never an envelope or an item's text.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# scripts/ is sys.path[0] when run as `.venv/bin/python scripts/forward_action_items.py`,
# so the sibling helper imports directly. post_action_items has no import-time
# side effects (only constants + an `if __name__ == "__main__"` guard).
import post_action_items as poster

ROOT = Path(__file__).resolve().parent.parent
QUEUE_PATH = ROOT / "config" / "pending_actions.json"
SEEN_PATH = ROOT / "config" / ".action_items_forwarded.json"
RETRY_FILE_NAME = ".action_items_retry.json"

# The OS's two rules for the ids a retry entry holds (93-06-SUMMARY). Both are
# matched WHOLE (`fullmatch`): Python's `$` accepts a trailing line feed and the
# OS does not.
PASS_ID_RE = re.compile(r"[A-Za-z0-9:_-]{1,64}")
OS_TICKET_ID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


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


def valid_pass_id(value) -> bool:
    """True for a pass id the OS accepts: 1 to 64 letters, digits, `:`, `_`, `-`."""
    return isinstance(value, str) and PASS_ID_RE.fullmatch(value) is not None


def is_os_ticket_id(value) -> bool:
    """True for a uuid-shaped id: an OS ticket id, never a HubSpot (digit) id."""
    return isinstance(value, str) and OS_TICKET_ID_RE.fullmatch(value) is not None


def confirmed(result) -> bool:
    """True only when the post succeeded AND the OS's body reports zero errors.

    HTTP 200 is NOT proof of persistence. The OS route returns 200 with a
    per-item {"emitted","errors"} body even when emitTask throws for some
    items; trusting the status alone caused the false-seen bug (2026-06-08)
    that marked ~104 items delivered while zero task rows were written, then
    permanently suppressed them. With a pass id, a 200 with errors above zero
    is also what the loser of two simultaneous passes is answered: sending the
    same envelope again with the same pass id then creates nothing."""
    if not isinstance(result, dict):
        return False
    resp = result.get("response") or {}
    try:
        body_errors = int(resp.get("errors", 0) or 0) if isinstance(resp, dict) else 0
    except (TypeError, ValueError):
        body_errors = 0
    return bool(result.get("ok")) and body_errors == 0


def _seen_path(seen_path=None) -> Path:
    return Path(seen_path) if seen_path is not None else SEEN_PATH


def retry_path(seen_path=None) -> Path:
    """The retry file: beside the seen-set, whichever seen-set path is in use."""
    return _seen_path(seen_path).with_name(RETRY_FILE_NAME)


def _write_json(path: Path, value) -> bool:
    """Write through a temp file and a rename, so a reader never sees half a
    file. False (and one log line) when the file could not be written."""
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temp, path)
        return True
    except OSError as exc:
        _log(f"could not write {path.name}: {type(exc).__name__}")
        return False


def load_seen(seen_path=None) -> set:
    """The keys whose delivery the OS confirmed. A missing or unusable file is empty."""
    raw = _load_json(_seen_path(seen_path), [])
    return {key for key in raw if isinstance(key, str)} if isinstance(raw, list) else set()


def mark_forwarded(keys, seen_path=None) -> bool:
    """Add confirmed keys to the seen-set, merged with what is already there."""
    path = _seen_path(seen_path)
    merged = load_seen(path) | {key for key in keys if isinstance(key, str)}
    return _write_json(path, sorted(merged))


def load_retry(seen_path=None) -> dict:
    """The retry file: key -> {"os_ticket_id", "pass_id"}. A missing or
    unparseable file, and any entry that is not an object, is nothing."""
    raw = _load_json(retry_path(seen_path), {})
    if not isinstance(raw, dict):
        return {}
    return {key: entry for key, entry in raw.items() if isinstance(key, str) and isinstance(entry, dict)}


def remember_retry(entries, seen_path=None) -> bool:
    """Record the ids a retry of these keys must carry, merged with what is
    already there. Called BEFORE a post is attempted. Ids only."""
    held = load_retry(seen_path)
    for key, entry in (entries or {}).items():
        if isinstance(key, str) and isinstance(entry, dict):
            held[key] = {name: entry[name] for name in ("os_ticket_id", "pass_id") if entry.get(name)}
    return _write_json(retry_path(seen_path), held)


def forget_retry(keys, seen_path=None, passes=None) -> bool:
    """Drop the entries of confirmed keys. Nothing is written when none is held.

    `passes`, when given, maps a key to the `pass_id` its entry held when the
    confirmed post was made (None for an entry with none, or for no entry).
    The pass is part of an entry's identity: an entry is dropped only while it
    still names that pass. One that another run has since replaced with a newer
    pass is a different post, still owed, and is kept. With no `passes` the
    keys are dropped whatever they name."""
    held = load_retry(seen_path)
    dropped = [
        key
        for key in keys
        if key in held and (passes is None or held[key].get("pass_id") == passes.get(key))
    ]
    if not dropped:
        return True
    for key in dropped:
        del held[key]
    return _write_json(retry_path(seen_path), held)


def _retry_ids(ticket_id: str, entry) -> tuple:
    """(os_ticket_id, pass_id) for one item: the retry entry's, each only when
    the OS would accept it; a uuid-shaped record key is its own OS ticket id."""
    entry = entry if isinstance(entry, dict) else {}
    os_ticket_id = entry.get("os_ticket_id")
    if not is_os_ticket_id(os_ticket_id):
        os_ticket_id = ticket_id if is_os_ticket_id(ticket_id) else None
    pass_id = entry.get("pass_id")
    return os_ticket_id, (pass_id if valid_pass_id(pass_id) else None)


def _record_items(rec) -> tuple:
    """(ticket key, action items) of one queue record, or ("", []) when it is
    not a record this script forwards."""
    if not isinstance(rec, dict):
        return "", []
    ticket_id = str(rec.get("ticket_id") or "").strip()
    items = rec.get("action_items") or []
    if not ticket_id or not isinstance(items, list):
        return "", []
    return ticket_id, items


def newest_record_of_pass_held_keys(records, retry) -> dict:
    """key -> the index of the NEWEST queue record that holds an item of that
    key, for every key whose retry entry names a pass the OS accepts.

    That record is the one a pass's retry is resent from (see the module
    docstring). A key whose entry names no pass is not in the answer: it is
    matched by the earlier rule."""
    newest: dict[str, int] = {}
    for index, rec in enumerate(records):
        ticket_id, items = _record_items(rec)
        for it in items:
            if not (isinstance(it, dict) and it.get("action_type")):
                continue
            key = _key(ticket_id, str(it["action_type"]))
            entry = retry.get(key)
            if isinstance(entry, dict) and valid_pass_id(entry.get("pass_id")):
                newest[key] = index
    return newest


def main(queue_path=None, seen_path=None) -> int:
    queue = Path(queue_path) if queue_path is not None else QUEUE_PATH
    seen_file = _seen_path(seen_path)
    records = _load_json(queue, [])
    if not isinstance(records, list):
        _log(f"{queue.name} is not a JSON array — nothing to forward.")
        return 0

    seen = load_seen(seen_file)
    retry = load_retry(seen_file)
    newest = newest_record_of_pass_held_keys(records, retry)

    forwarded = already = errors = superseded = 0
    fresh_keys: set[str] = set()
    sent_under: dict[str, object] = {}  # key -> the pass id its entry held when it was sent

    for index, rec in enumerate(records):
        ticket_id, items = _record_items(rec)
        if not ticket_id or not items:
            continue

        # Forward only items not already delivered (by ticket + action_type) —
        # and any item the retry file still holds: its post is owed even when an
        # earlier pass marked the key delivered. An item whose entry names a
        # pass is owed from ONE record, the newest that holds it (WR-46); the
        # same key in an older record is another pass's item and is not sent.
        fresh = []
        stale = 0
        for it in items:
            if not (isinstance(it, dict) and it.get("action_type")):
                continue
            key = _key(ticket_id, str(it["action_type"]))
            if key in newest:
                if newest[key] != index:
                    stale += 1
                elif key in retry:
                    fresh.append(it)
            elif key not in seen or key in retry:
                fresh.append(it)
        superseded += stale
        already += len(items) - len(fresh) - stale
        if not fresh:
            continue

        # One envelope per (os_ticket_id, pass_id) the items are held under. With
        # no retry entry that is one envelope with neither: today's, byte for byte.
        hubspot_id = None if is_os_ticket_id(ticket_id) else ticket_id
        groups: dict[tuple, list] = {}
        for it in fresh:
            ids = _retry_ids(ticket_id, retry.get(_key(ticket_id, str(it["action_type"]))))
            groups.setdefault(ids, []).append(it)

        for (os_ticket_id, pass_id), group in groups.items():
            result = poster.post_action_items(
                hubspot_id, group, dry_run=False, os_ticket_id=os_ticket_id, pass_id=pass_id
            )
            result = result if isinstance(result, dict) else {}
            # Confirm the body reports errors == 0 before marking an item seen;
            # otherwise leave it un-seen so the next run retries (emitTask is
            # idempotent, and with a pass id a repeat of the same pass creates
            # nothing, so a retry is safe).
            if confirmed(result):
                forwarded += len(group)
                for it in group:
                    key = _key(ticket_id, str(it["action_type"]))
                    fresh_keys.add(key)
                    entry = retry.pop(key, None)
                    if key not in sent_under:
                        sent_under[key] = entry.get("pass_id") if isinstance(entry, dict) else None
                status = result.get("status", "")
                _log(
                    f"ticket {ticket_id}: forwarded {len(group)} item(s) "
                    f"({result.get('mode')} {status})".rstrip()
                )
            else:
                # Not marked seen → retried next run (transient secret/url/net, an
                # HTTP-error status, OR a 200-with-errors partial persistence).
                errors += len(group)
                resp = result.get("response")
                body_errors = resp.get("errors") if isinstance(resp, dict) else None
                reason = (
                    result.get("reason")
                    or result.get("error")
                    or (f"{body_errors} item(s) errored server-side" if body_errors else "unknown")
                )
                _log(
                    f"ticket {ticket_id}: forward NOT confirmed "
                    f"({result.get('mode')}: {reason}) — will retry next run"
                )

    if fresh_keys:
        if not mark_forwarded(fresh_keys, seen_file):
            _log("could not persist seen-state; OS idempotency still dedups")
        # only an entry that still names the pass that was confirmed
        forget_retry(fresh_keys, seen_file, passes=sent_under)

    _log(
        f"done: forwarded={forwarded} already_delivered={already} "
        f"errors={errors} records={len(records)}" + (f" superseded={superseded}" if superseded else "")
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — never break the cron pipeline.
        _log(f"unexpected error: {exc!r}")
        sys.exit(0)
