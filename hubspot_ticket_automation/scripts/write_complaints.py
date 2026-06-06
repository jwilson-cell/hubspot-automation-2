#!/usr/bin/env python3
"""Deterministic complaint mirror — re-scan the skill's mispack CSV and INSERT
customer_complaints rows into Pack'N OS (Phase 20 D-02/D-04).

Runs deterministically AFTER the agentic ticket pass + the action-item forwarder
(via scripts/run_tickets.sh step 3), consuming the skill's accumulated
outputs/kpi/mispack_log.csv. ZERO new classification work, ZERO HubSpot calls —
it is a pure DB-side mirror of the mispack CSV the skill already writes.

D-04 (best-effort + REAL next-run retry): re-scan the WHOLE CSV every run. There
is NO local seen-state file — the DB partial-unique index is the only idempotency
arbiter. `client.write_complaints` uses
  ON CONFLICT (tenant_id, hubspot_ticket_id) WHERE deleted_at IS NULL DO NOTHING
so re-inserting an already-mirrored ticket is free, and a row that failed to land
last run (e.g. a torn CSV line) is naturally retried next run.

Never-fail posture (forward_action_items.py mold): any exception logs and exits 0
so this can NEVER break the cron pipeline. Pre-flights the `brand` column so a
deploy-order inversion (writer live before the OS migration) logs an explicit
reason and exits 0 rather than crashing or silently doing the wrong thing.

Stdlib + the existing packn_os_hubspot_client module only — no new dependencies
(sibling CLAUDE.md invariant).
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from packn_os_hubspot_client import client  # noqa: E402
from packn_os_hubspot_client.db import close_pool, get_pool  # noqa: E402

CSV_PATH = ROOT / "outputs" / "kpi" / "mispack_log.csv"
CLASSIFICATION = "mispack"


def _log(msg: str) -> None:
    print(f"[write_complaints] {msg}", file=sys.stderr)


def _zero_counts() -> dict:
    return {"rows": 0, "inserted": 0, "conflict_skipped": 0, "skipped_bad": 0}


def _brand_column_present() -> bool:
    """Pre-flight (Pitfall 2 deploy-order-inversion guard): is the `brand`
    column live on customer_complaints yet? Reading information_schema needs no
    extra GRANT. If absent, the writer logs an explicit reason and skips the run
    (it would otherwise INSERT against a column that doesn't exist)."""
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'customer_complaints' "
                "AND column_name = 'brand'"
            )
            return cur.fetchone() is not None


def run(csv_path: Path) -> dict:
    """Re-scan the whole mispack CSV and batch-INSERT customer_complaints rows.

    Returns {"rows": int, "inserted": int, "conflict_skipped": int,
             "skipped_bad": int}.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        _log(f"input missing: {csv_path} — nothing to mirror.")
        return _zero_counts()

    # Deploy-order-inversion guard: skip cleanly if the OS migration isn't live.
    if not _brand_column_present():
        _log(
            "brand column missing — OS migration (0073) not deployed yet; "
            "skipping run (will retry next run)."
        )
        return _zero_counts()

    batch: list[dict] = []
    rows = 0
    skipped_bad = 0

    try:
        with csv_path.open("r", newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)  # resolve BY NAME — on-disk order drifts
            for raw in reader:
                rows += 1
                try:
                    ticket = (raw.get("ticket_id") or "").strip()
                    if not ticket:
                        skipped_bad += 1
                        continue
                    complained = client.parse_complained_at(
                        raw.get("first_seen_utc")
                    )
                    if complained is None:
                        skipped_bad += 1
                        _log(
                            f"ticket {ticket}: unparseable first_seen_utc "
                            f"{raw.get('first_seen_utc')!r} — skipped (never "
                            f"defaulted)."
                        )
                        continue
                    batch.append(
                        {
                            "hubspot_ticket_id": ticket,
                            "classification": CLASSIFICATION,
                            "shipment_tracking_number": client.normalize_optional(
                                raw.get("tracking_number")
                            ),
                            "brand": client.normalize_optional(
                                raw.get("company_name")
                            ),
                            "complained_at": complained,
                        }
                    )
                except (csv.Error, KeyError, AttributeError) as exc:
                    # Torn / malformed row — skip+count; a concurrent CSV append
                    # heals on the next run (Pitfall 7).
                    skipped_bad += 1
                    _log(f"malformed row skipped: {exc!r}")
    except csv.Error as exc:
        _log(f"CSV read error: {exc!r} — partial batch will retry next run.")

    if batch:
        result = client.write_complaints(batch)
        inserted = result["inserted"]
        conflict_skipped = result["conflict_skipped"]
    else:
        inserted = 0
        conflict_skipped = 0

    summary = {
        "rows": rows,
        "inserted": inserted,
        "conflict_skipped": conflict_skipped,
        "skipped_bad": skipped_bad,
    }
    _log(
        f"done: inserted={inserted} conflict_skipped={conflict_skipped} "
        f"skipped_bad={skipped_bad} rows={rows}"
    )
    return summary


def main() -> int:
    csv_path = CSV_PATH
    argv = sys.argv[1:]
    if "--csv" in argv:
        i = argv.index("--csv")
        if i + 1 < len(argv):
            csv_path = Path(argv[i + 1])
    try:
        run(csv_path)
    finally:
        close_pool()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — never break the cron pipeline (D-04)
        _log(f"unexpected error: {exc!r}")
        sys.exit(0)
