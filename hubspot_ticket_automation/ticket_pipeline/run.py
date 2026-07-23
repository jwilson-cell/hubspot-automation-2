"""Shadow-run orchestrator.

Processes the SAME candidate set the agent is about to see (same search
filters, same fingerprint dedupe — imported from scripts/pregate_tickets.py
so the two can never drift), runs hydrate -> classify -> draft -> extract per
ticket, and writes one artifact per ticket plus a run summary under
outputs/shadow/<run-ts>/.

ZERO side effects: no state.json writes, no automation_drafts rows, no
pending_actions queue, no urgent emails, no HubSpot property writes, no
Sheets rows, no automation_runs records. The agent (which runs AFTER this in
run_tickets.sh) remains the system of record until cutover.

Always exits 0 — a shadow failure must never affect the cron.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime, timezone
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
from ticket_pipeline import hydrate, kb, llm  # noqa: E402

SETTINGS_PATH = ROOT / "config" / "settings.yaml"
CATEGORIES_PATH = ROOT / "config" / "categories.yaml"
STATE_PATH = ROOT / "config" / "state.json"
SHADOW_DIR = ROOT / "outputs" / "shadow"


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
    """hydrate -> classify -> draft -> extract for one ticket. Raises on
    unrecoverable errors; the caller isolates per-ticket failures."""
    usages: list[dict] = []
    started = time.monotonic()

    ctx = hydrate.hydrate_ticket(ticket, settings, token)
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


def run_shadow(limit: int | None = None, ticket_id: str | None = None) -> int:
    settings = _load_yaml(SETTINGS_PATH)
    pipeline_cfg = settings.get("pipeline") or {}
    if not pipeline_cfg.get("shadow_enabled") and ticket_id is None:
        _log("pipeline.shadow_enabled is false — skipping")
        return 0

    token = hs.read_token()
    if token is None:
        _log("hubspot token missing — skipping shadow run")
        return 0

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
            return 0
        all_results = resp.get("results", [])
        wanted = set(
            unprocessed_candidates(all_results, state.get("ticket_fingerprints") or {})
        )
        results = [r for r in all_results if str(r.get("id")) in wanted]

    cap = limit if limit is not None else int(pipeline_cfg.get("shadow_limit", 25))
    results = results[:cap]
    if not results:
        _log("no candidate tickets — nothing to shadow")
        return 0

    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    out_dir = SHADOW_DIR / run_ts
    out_dir.mkdir(parents=True, exist_ok=True)
    _log(f"shadowing {len(results)} ticket(s) -> {out_dir}")

    categories_cfg = _load_yaml(CATEGORIES_PATH)
    summary_rows = []
    for ticket in results:
        tid = str(ticket.get("id"))
        try:
            artifact = process_ticket(ticket, settings, categories_cfg, token)
        except Exception:
            _log(f"ticket {tid} failed:\n{traceback.format_exc()}")
            summary_rows.append({"ticket_id": tid, "status": "error"})
            continue
        (out_dir / f"{tid}.json").write_text(
            json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8"
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
    (out_dir / "_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
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
