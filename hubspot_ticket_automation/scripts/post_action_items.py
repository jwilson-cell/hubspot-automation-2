"""POST extracted action items (and ticket-closed signals) to the Pack'N OS ingestion route.

This is the cross-repo bridge for Phase 15.1: the hubspot-tickets skill extracts
action items (see prompts/extract_actions.md), and this helper forwards each one to
the Pack'N OS `POST /api/ingest/action-items` route, which materializes them as
first-class tasks in the Operator Task Hub (/tasks) — replacing the old hourly
digest email.

Security / transport contract (mirrors the OS verify, Plan 15.1-04):
    - HMAC-SHA256 over the EXACT raw request bytes. The OS route
      (verifyActionIngestSignature) reads `await req.text()` and verifies the HMAC
      over those exact bytes BEFORE JSON.parse — so this helper MUST sign the same
      buffer it sends. We build `data = json.dumps(body, separators=(",", ":"))
      .encode("utf-8")` ONCE and both sign AND send that single buffer (no
      re-serialization that could drift the byte representation).
    - Header: X-PackN-Signature = bare hex digest (no `algo=` prefix — the OS clone
      does NOT strip a prefix; Plan 04 documents stripping in lock-step IF a prefix
      is ever added).
    - The shared secret lives at config/.secrets/action_ingest_secret.txt (gitignored,
      same dir as the other tokens). The operator provisions the SAME value in the
      Coolify web env var ACTION_INGEST_SECRET (>= 16 chars). Until then the OS route
      fail-closes 503 and this helper just logs the would-be POST.

Invariants (sibling CLAUDE.md):
    - Zero new Python dependencies — stdlib only (urllib.request, json, hashlib, hmac,
      pathlib). NO requests / httpx.
    - dry_run default: when dry_run is true, this helper LOGS the payload + computed
      signature and does NOT send. The SKILL step passes dry_run=True during the D-07
      parallel run (independent of the global settings.yaml dry_run, which is already
      false for the rest of the automation); the operator flips this step to live only
      after inspecting a dry-run pass (Task 3 checkpoint).
    - Read-only on HubSpot tickets: POSTing to the Pack'N OS route is NOT a HubSpot
      ticket write, so it is compliant with the read-only invariant.
    - Never hydrate customer attachments: only the `evidence_summary` TEXT crosses the
      boundary (extract_actions.md enforces this upstream — this helper forwards the
      action-item objects verbatim and never fetches files).
    - Non-blocking: a failed POST never raises out. Every path returns a dict
      ({"ok": bool, ...}); a transport/secret failure returns {"ok": False, "error": ...}
      and is logged to stderr. The cron treats the POST as best-effort at-least-once
      (a later run re-POSTs; the OS dedups by ticket + action_type within the ET day).

CLI (manual testing only — `py` is the venv python on the droplet; use `py -3` on a Windows laptop):
    py scripts/post_action_items.py action-items <ticket_id> <items.json>   # dry-run
    py scripts/post_action_items.py action-items <ticket_id> <items.json> --send
    py scripts/post_action_items.py ticket-closed <hubspot_ticket_id>          # dry-run
    py scripts/post_action_items.py ticket-closed <hubspot_ticket_id> --send
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRET_PATH = ROOT / "config" / ".secrets" / "action_ingest_secret.txt"
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
SIGNATURE_HEADER = "X-PackN-Signature"

# Default ingestion endpoint path appended to the os_ingest_url base if the
# configured value is a bare origin. Kept here (not the full URL) so the domain
# is NEVER hardcoded in this file — the origin comes from settings.yaml.
INGEST_PATH = "/api/ingest/action-items"


def _log(msg: str) -> None:
    print(f"[post_action_items] {msg}", file=sys.stderr)


def _read_secret() -> str | None:
    """Return the shared HMAC secret, or None if not provisioned yet.

    Returning None (rather than raising) lets the caller log + skip the live POST
    without aborting the skill run. The OS route also fail-closes 503 when its env
    secret is unset, so a missing secret degrades gracefully on both sides.
    """
    if not SECRET_PATH.exists():
        _log(
            f"secret missing at {SECRET_PATH} -- the OS ingestion route fail-closes "
            f"503 until ACTION_INGEST_SECRET is provisioned in BOTH the droplet "
            f"({SECRET_PATH.name}) AND the Coolify web env. Skipping live POST."
        )
        return None
    secret = SECRET_PATH.read_text(encoding="utf-8").strip()
    if len(secret) < 16:
        _log(
            f"secret at {SECRET_PATH} is shorter than 16 chars — the OS route "
            f"length-guard rejects it (503). Skipping live POST."
        )
        return None
    return secret


def _read_os_ingest_url() -> str | None:
    """Read os_ingest_url from settings.yaml WITHOUT a yaml dependency.

    The repo ships no PyYAML in the cron venv, and this is the only key we need, so
    we do a minimal line scan for `os_ingest_url:` rather than add a dependency.
    Returns the full POST URL (origin + INGEST_PATH if the configured value is a
    bare origin), or None if the key is absent/blank/placeholder.
    """
    if not SETTINGS_PATH.exists():
        _log(f"settings missing at {SETTINGS_PATH}; cannot resolve os_ingest_url.")
        return None
    for raw_line in SETTINGS_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("#") or ":" not in line:
            continue
        key, _, rest = line.partition(":")
        if key.strip() != "os_ingest_url":
            continue
        # Strip an inline comment + surrounding quotes.
        value = rest.split("#", 1)[0].strip().strip('"').strip("'")
        if not value or "REPLACE" in value or "example.com" in value:
            _log(
                "os_ingest_url is unset/placeholder in settings.yaml -- set it to the "
                "Pack'N OS origin (e.g. https://os.gopackn.com). Skipping live POST."
            )
            return None
        # Allow either a bare origin or a full endpoint URL in settings.
        if value.rstrip("/").endswith(INGEST_PATH):
            return value
        if INGEST_PATH in value:
            return value
        return value.rstrip("/") + INGEST_PATH
    _log("os_ingest_url key not found in settings.yaml. Skipping live POST.")
    return None


def _sign(secret: str, data: bytes) -> str:
    """HMAC-SHA256 hex digest over the EXACT bytes that will be sent."""
    return hmac.new(secret.encode("utf-8"), data, hashlib.sha256).hexdigest()


def _post(envelope: dict, dry_run: bool) -> dict:
    """Sign + (conditionally) send one envelope. Never raises out.

    Returns a result dict:
        dry-run    -> {"ok": True, "mode": "dry-run", "signature": ..., "payload": ...}
        skipped    -> {"ok": False, "mode": "skipped", "reason": ...}
        live ok    -> {"ok": True, "mode": "sent", "status": 200, "response": {...}}
        live error -> {"ok": False, "mode": "error", "status": <int|None>, "error": ...}
    """
    # Build the EXACT bytes once — these are signed AND sent.
    data = json.dumps(envelope, separators=(",", ":")).encode("utf-8")

    secret = _read_secret()
    if secret is None:
        return {"ok": False, "mode": "skipped", "reason": "secret_unprovisioned"}

    signature = _sign(secret, data)

    if dry_run:
        _log(
            f"DRY-RUN — would POST type={envelope.get('type')} "
            f"({len(data)} bytes) sig={signature}"
        )
        _log(f"DRY-RUN payload: {data.decode('utf-8')}")
        return {
            "ok": True,
            "mode": "dry-run",
            "signature": signature,
            "payload": envelope,
        }

    url = _read_os_ingest_url()
    if url is None:
        return {"ok": False, "mode": "skipped", "reason": "os_ingest_url_unset"}

    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header(SIGNATURE_HEADER, signature)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            _log(f"sent type={envelope.get('type')} -> HTTP {resp.status}")
            return {
                "ok": True,
                "mode": "sent",
                "status": getattr(resp, "status", 200),
                "response": parsed,
            }
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        _log(
            f"HTTP {e.code} posting type={envelope.get('type')}: {err_body} "
            f"(401=signature mismatch, 503=secret unset on OS, 400=zod reject)"
        )
        return {"ok": False, "mode": "error", "status": e.code, "error": err_body}
    except urllib.error.URLError as e:
        _log(f"network error posting type={envelope.get('type')}: {e.reason}")
        return {"ok": False, "mode": "error", "status": None, "error": str(e.reason)}
    except Exception as e:  # noqa: BLE001 — best-effort: never let the skill crash.
        _log(f"unexpected error posting type={envelope.get('type')}: {e!r}")
        return {"ok": False, "mode": "error", "status": None, "error": repr(e)}


def post_action_items(ticket_id: str, items: list[dict], dry_run: bool) -> dict:
    """POST the action_items envelope for one ticket. Never raises out.

    envelope shape (Plan 04 ActionIngestSchema discriminated union):
        {"type": "action_items", "ticket_id": "<numeric>", "items": [<extract_actions objects>]}

    `items` is forwarded verbatim from the extractor output (each object carries
    action_type / description / owner_hint / severity / needs_hubspot_reply, plus the
    optional claim_packet for file_carrier_claim). Only evidence_summary TEXT crosses
    — never file bytes/URLs (sibling invariant; enforced upstream by extract_actions.md).
    """
    if not items:
        # Nothing to forward — an empty extraction is valid (no invented work).
        return {"ok": True, "mode": "noop", "reason": "no_items"}
    envelope = {
        "type": "action_items",
        "ticket_id": str(ticket_id),
        "items": items,
    }
    return _post(envelope, dry_run)


def post_ticket_closed(hubspot_ticket_id: str, dry_run: bool) -> dict:
    """POST the ticket_closed signal for one observed-closed ticket. Never raises out.

    envelope shape:
        {"type": "ticket_closed", "hubspot_ticket_id": "<numeric>"}

    The OS route auto-resolves that ticket's open hubspot_action_item tasks (D-15).
    """
    envelope = {
        "type": "ticket_closed",
        "hubspot_ticket_id": str(hubspot_ticket_id),
    }
    return _post(envelope, dry_run)


def _cli() -> int:
    argv = sys.argv[1:]
    send_flag = "--send" in argv
    pos = [a for a in argv if not a.startswith("--")]
    dry_run = not send_flag

    if not pos:
        _log("usage: post_action_items.py {action-items|ticket-closed} ... [--send]")
        return 2

    mode = pos[0]
    if mode == "action-items":
        if len(pos) < 2:
            _log("usage: post_action_items.py action-items <ticket_id> [items.json] [--send]")
            return 2
        ticket_id = pos[1]
        items: list[dict] = []
        if len(pos) >= 3:
            items = json.loads(Path(pos[2]).read_text(encoding="utf-8"))
        else:
            stdin = sys.stdin.read().strip()
            if stdin:
                items = json.loads(stdin)
        result = post_action_items(ticket_id, items, dry_run)
    elif mode == "ticket-closed":
        if len(pos) < 2:
            _log("usage: post_action_items.py ticket-closed <hubspot_ticket_id> [--send]")
            return 2
        result = post_ticket_closed(pos[1], dry_run)
    else:
        _log(f"unknown mode '{mode}' — expected action-items|ticket-closed")
        return 2

    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(_cli())
