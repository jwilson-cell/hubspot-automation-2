"""Shadow-run orchestrator.

Processes the SAME candidate set the agent is about to see (same search
filters, same fingerprint dedupe — imported from scripts/pregate_tickets.py
so the two can never drift), runs hydrate -> classify -> draft -> extract per
ticket, and writes one artifact per ticket plus a run summary under
outputs/shadow/<run-ts>/.

THE TICKET SOURCE MODE (Phase 93). This pipeline is one of the readers of the
one source setting (ticket_pipeline/source.py). In `hubspot` and `shadow` mode
it works as described above. In `os` mode HubSpot is not the source: it makes
no HubSpot search and never calls hydrate_ticket — it runs classify -> draft ->
extract over the ticket contexts it is handed (built from the OS's payload by
the caller), or does nothing when it is handed none. A mode that cannot be
established makes it do nothing; it is never read as `hubspot`.

The setting `pipeline.shadow_enabled`, the log tag and the output directory
are this pipeline's own in every mode. They are NOT the source setting: the
source's `shadow` mode compares two ticket sources and writes nothing here.

ZERO side effects: no state.json writes, no automation_drafts rows, no
pending_actions queue, no urgent emails, no HubSpot property writes, no
Sheets rows, no automation_runs records. The agent (which runs AFTER this in
run_tickets.sh) remains the system of record until cutover.

Always exits 0 — a shadow failure must never affect the cron.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import yaml  # noqa: E402

from pregate_tickets import (  # noqa: E402 — single source of candidate truth
    build_search_body,
    parse_last_run_at,
    unprocessed_candidates,
)
from ticket_pipeline import hubspot_api as hs  # noqa: E402
from ticket_pipeline import hydrate, kb, llm, source  # noqa: E402

SETTINGS_PATH = ROOT / "config" / "settings.yaml"
CATEGORIES_PATH = ROOT / "config" / "categories.yaml"
STATE_PATH = ROOT / "config" / "state.json"
SHADOW_DIR = ROOT / "outputs" / "shadow"

# What the engine shadow leaves on disk (SEC-OPEN-2, owner ruling 2026-10-14).
# Each artifact holds a ticket's full context and a draft, so the directory is
# private to the cron's user (0700, files 0600), git-ignored, and a run
# directory older than SHADOW_RETENTION_DAYS is removed at the start of each
# engine-shadow step. The source shadow reads only directories of its own run
# (source_shadow._engine_classification), so pruning older ones costs it nothing.
SHADOW_RETENTION_DAYS = 30
_RUN_DIR_FORMAT = "%Y-%m-%dT%H-%M-%SZ"
_RUN_DIR_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}Z")


def _log(msg: str) -> None:
    print(f"[pipeline.shadow] {msg}", file=sys.stderr)


# USD per 1M tokens (input, output) — for the run summary's cost estimate.
# claude-sonnet-5 is introductory pricing through 2026-08-31 ($3/$15 after).
_PRICING_USD_PER_MTOK = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-5": (3.00, 15.00),
}


def _estimate_cost_usd(usages: list[dict]) -> float:
    """Best-effort spend estimate from per-call usage records. Unknown
    models contribute 0 (the token counts still land in the summary)."""
    total = 0.0
    for u in usages:
        rate_in, rate_out = _PRICING_USD_PER_MTOK.get(u.get("model") or "", (0.0, 0.0))
        total += u.get("input_tokens", 0) * rate_in / 1_000_000
        total += u.get("output_tokens", 0) * rate_out / 1_000_000
    return round(total, 6)


def _load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _is_auto_send(ctx: dict, categories_cfg: dict) -> bool:
    """SKILL 2f routing rule (informational in shadow mode)."""
    return (ctx.get("source_type") or "").upper() == "FORM" and (
        ctx.get("topic_of_ticket") in (categories_cfg.get("auto_send_form_topics") or [])
    )


def process_ticket(
    ticket: dict, settings: dict, categories_cfg: dict, token: str
) -> dict:
    """hydrate -> classify -> draft -> extract for one HubSpot search hit.
    Raises on unrecoverable errors; the caller isolates per-ticket failures."""
    started = time.monotonic()
    ctx = hydrate.hydrate_ticket(ticket, settings, token)
    return process_context(ctx, settings, categories_cfg, started=started)


def process_context(
    ctx: dict, settings: dict, categories_cfg: dict, started: float | None = None
) -> dict:
    """classify -> draft -> extract for a ticket context that is already built.

    No HubSpot call and no hydration happen here: `os` mode hands in contexts
    built from the OS's payload, and process_ticket hands in the one it just
    hydrated. `started` lets process_ticket keep hydration in `elapsed_s`."""
    usages: list[dict] = []
    if started is None:
        started = time.monotonic()

    classification = llm.classify(ctx, settings, categories_cfg, usages)
    category = classification["category"]
    kb_context = kb.load_kb_context(category, settings, categories_cfg)
    draft_body = llm.draft(ctx, category, kb_context, usages)
    actions = llm.extract_actions(ctx, category, draft_body, usages)

    return {
        "ticket_id": ctx["ticket_id"],
        "ticket_link": ctx["ticket_link"],
        "subject": ctx.get("subject"),
        "topic_of_ticket": ctx.get("topic_of_ticket"),
        "source_type": ctx.get("source_type"),
        "would_route_as": "auto_send" if _is_auto_send(ctx, categories_cfg) else "draft",
        "classification": classification,
        "draft_body": draft_body,
        "action_items": actions,
        "ssk_found": (ctx.get("ssk_state") or {}).get("found"),
        "related_tickets_found": len(ctx.get("related_tickets") or []),
        "usage": usages,
        "elapsed_s": round(time.monotonic() - started, 1),
        "ticket_context": ctx,  # full context for the reviewer's comparison
    }


def _write_private(path: Path, text: str) -> None:
    """Write `text` to `path` readable by the cron's user only (0600), also
    when the file already exists with a wider mode."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        if hasattr(os, "fchmod"):
            os.fchmod(fh.fileno(), 0o600)
        fh.write(text)


def _make_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def prune_shadow_dir(now: datetime | None = None, base: Path | None = None) -> int:
    """Remove engine-shadow run directories older than SHADOW_RETENTION_DAYS
    and close the directory to other users. Only names in the run-directory
    format are touched; anything else is left alone. Returns how many run
    directories were removed. Never raises: a failure here must not stop the
    shadow step, and the shadow step must never fail the cron."""
    base = SHADOW_DIR if base is None else base
    removed = 0
    try:
        if not base.is_dir():
            return 0
        os.chmod(base, 0o700)
        now = datetime.now(timezone.utc) if now is None else now
        floor = (now - timedelta(days=SHADOW_RETENTION_DAYS)).strftime(_RUN_DIR_FORMAT)
        for entry in base.iterdir():
            # fixed-width UTC names: text order is time order
            if entry.is_dir() and not entry.is_symlink() and _RUN_DIR_RE.fullmatch(entry.name) and entry.name < floor:
                shutil.rmtree(entry)
                removed += 1
    except OSError as e:
        _log(f"pruning old shadow output failed ({type(e).__name__}); continuing")
    if removed:
        _log(f"removed {removed} shadow run director{'y' if removed == 1 else 'ies'} older than {SHADOW_RETENTION_DAYS} days")
    return removed


def _artifact_name(ticket_id: str) -> str:
    """A file name for one ticket's artifact. A HubSpot id (digits) and an OS id
    (a uuid) pass through unchanged; anything else in an id handed in from
    outside is flattened, so an artifact can only land inside the run directory."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", ticket_id) or "unknown"


def _hubspot_search_hits(
    settings: dict, ticket_id: str | None, token: str
) -> list[dict] | None:
    """The HubSpot candidates, as before Phase 93: one search, then the
    fingerprint dedupe. None means the search failed (already logged)."""
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8") or "{}")
    except (OSError, json.JSONDecodeError):
        state = {}

    if ticket_id:
        # single-ticket debug path: fetch it directly
        resp = hs.search_tickets(
            {
                "filterGroups": [
                    {"filters": [{"propertyName": "hs_object_id", "operator": "EQ", "value": ticket_id}]}
                ],
                "properties": hs.TICKET_BASE_PROPERTIES
                + list(settings.get("ticket_custom_properties") or []),
                "limit": 1,
            },
            token,
        )
        results = (resp or {}).get("results", [])
    else:
        body = build_search_body(
            parse_last_run_at(state.get("last_run_at")),
            [str(s) for s in settings.get("active_stages") or []],
            bool(settings.get("require_last_message_from_visitor", False)),
            int(settings.get("per_run_cap", 25)),
        )
        body["properties"] = hs.TICKET_BASE_PROPERTIES + list(
            settings.get("ticket_custom_properties") or []
        )
        resp = hs.search_tickets(body, token)
        if resp is None:
            _log("hubspot search failed — skipping shadow run")
            return None
        all_results = resp.get("results", [])
        wanted = set(
            unprocessed_candidates(all_results, state.get("ticket_fingerprints") or {})
        )
        results = [r for r in all_results if str(r.get("id")) in wanted]

    return results


def run_shadow(
    limit: int | None = None,
    ticket_id: str | None = None,
    mode: str | None = None,
    contexts: list[dict] | None = None,
) -> int:
    """One engine-shadow run. Always returns 0.

    `mode` is the ticket source mode; None means "take it from
    ticket_pipeline.source" (the value the orchestrator exported, else one read
    of the setting). `contexts` are ready-built ticket contexts, used in `os`
    mode only, where this function asks HubSpot nothing.
    """
    # Before the enabled check, so turning the shadow off still ages out what it left.
    prune_shadow_dir()
    settings = _load_yaml(SETTINGS_PATH)
    pipeline_cfg = settings.get("pipeline") or {}
    if not pipeline_cfg.get("shadow_enabled") and ticket_id is None:
        _log("pipeline.shadow_enabled is false — skipping")
        return 0

    try:
        mode = source.current_mode() if mode is None else source.validate_mode(mode)
    except source.SourceModeError as e:
        # Never read as `hubspot`: with no mode there is no source to shadow.
        _log(f"source mode not established ({e.code}) — skipping shadow run")
        return 0

    token: str | None = None
    if mode == source.MODE_OS:
        if ticket_id:
            _log("--ticket searches HubSpot, which is not the source in os mode — skipping")
            return 0
        # (kind, item): a context needs no hydration and no HubSpot call
        work = [("context", ctx) for ctx in (contexts or [])]
    else:
        if contexts:
            _log("contexts are used in os mode only — ignoring them")
        token = hs.read_token()
        if token is None:
            _log("hubspot token missing — skipping shadow run")
            return 0
        hits = _hubspot_search_hits(settings, ticket_id, token)
        if hits is None:
            return 0
        work = [("ticket", hit) for hit in hits]

    cap = limit if limit is not None else int(pipeline_cfg.get("shadow_limit", 25))
    work = work[:cap]
    if not work:
        _log("no candidate tickets — nothing to shadow")
        return 0

    run_ts = datetime.now(timezone.utc).strftime(_RUN_DIR_FORMAT)
    out_dir = SHADOW_DIR / run_ts
    _make_private_dir(SHADOW_DIR)
    _make_private_dir(out_dir)
    _log(f"shadowing {len(work)} ticket(s) -> {out_dir}")

    categories_cfg = _load_yaml(CATEGORIES_PATH)
    summary_rows = []
    for kind, item in work:
        id_key = "ticket_id" if kind == "context" else "id"
        tid = str(item.get(id_key)) if isinstance(item, dict) else "unknown"
        try:
            if kind == "context":
                artifact = process_context(item, settings, categories_cfg)
            else:
                artifact = process_ticket(item, settings, categories_cfg, token)
        except Exception:
            _log(f"ticket {tid} failed:\n{traceback.format_exc()}")
            summary_rows.append({"ticket_id": tid, "status": "error"})
            continue
        _write_private(
            out_dir / f"{_artifact_name(tid)}.json",
            json.dumps(artifact, indent=2, ensure_ascii=False),
        )
        tokens_in = sum(u["input_tokens"] for u in artifact["usage"])
        tokens_out = sum(u["output_tokens"] for u in artifact["usage"])
        summary_rows.append(
            {
                "ticket_id": tid,
                "status": "ok",
                "category": artifact["classification"]["category"],
                "confidence": artifact["classification"].get("confidence"),
                "priority": artifact["classification"].get("priority"),
                "would_route_as": artifact["would_route_as"],
                "actions": len(artifact["action_items"]),
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": _estimate_cost_usd(artifact["usage"]),
                "elapsed_s": artifact["elapsed_s"],
            }
        )
        _log(
            f"ticket {tid}: {artifact['classification']['category']} "
            f"({tokens_in} in / {tokens_out} out, {artifact['elapsed_s']}s)"
        )

    ok_rows = [r for r in summary_rows if r["status"] == "ok"]
    summary = {
        "run_ts": run_ts,
        "tickets": summary_rows,
        "totals": {
            "ok": len(ok_rows),
            "error": len(summary_rows) - len(ok_rows),
            "tokens_in": sum(r["tokens_in"] for r in ok_rows),
            "tokens_out": sum(r["tokens_out"] for r in ok_rows),
            "cost_usd": round(sum(r["cost_usd"] for r in ok_rows), 4),
        },
    }
    _write_private(out_dir / "_summary.json", json.dumps(summary, indent=2))
    _log(
        f"done: {summary['totals']['ok']} ok / {summary['totals']['error']} error, "
        f"{summary['totals']['tokens_in']} tokens in / {summary['totals']['tokens_out']} out, "
        f"~${summary['totals']['cost_usd']}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Shadow-mode ticket pipeline")
    parser.add_argument("--limit", type=int, default=None, help="max tickets this run")
    parser.add_argument("--ticket", default=None, help="shadow a single ticket id (ignores gate)")
    args = parser.parse_args()
    try:
        return run_shadow(limit=args.limit, ticket_id=args.ticket)
    except Exception:
        _log("unexpected shadow failure (cron unaffected):\n" + traceback.format_exc())
        return 0


if __name__ == "__main__":
    sys.exit(main())
