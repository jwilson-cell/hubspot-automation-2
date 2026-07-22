#!/usr/bin/env python3
"""One-shot heal: backfill customer_complaints.order_number from the mispack CSV.

WHY THIS EXISTS (2026-07-22 — /sla accuracy blind-spot, part D)
---------------------------------------------------------------
The 2026-07-06 cron model pin emptied tracking_number extraction in
outputs/kpi/mispack_log.csv (14 of 15 rows 7/7–7/21), so the Pack'N OS /sla
accuracy surfaces went blind while mispacks arrived daily. Pack'N OS migration
0151 added `customer_complaints.order_number` + a collision-safe read-side
order-attribution tier, and the live mirror (scripts/write_complaints.py) now
writes order_number going forward — but every row inserted BEFORE that has
order_number NULL even though the CSV had the value all along. This script
heals those historical rows.

WHAT IT DOES
------------
Re-scans outputs/kpi/mispack_log.csv and, for each ticket, UPDATEs the matching
active customer_complaints row FILL-IF-EMPTY ONLY:

    order_number             = COALESCE(order_number, <csv order_number>)
    shipment_tracking_number = COALESCE(shipment_tracking_number, <csv tracking>)

A populated DB value is NEVER overwritten (COALESCE keeps it); a row is touched
only when it has a NULL the CSV can actually fill. Deterministic — the CSV IS
the ticket's own captured data; nothing is inferred or guessed. Attribution
stays READ-SIDE in Pack'N OS (the collision-safe tier), never here.

Idempotent: a second run finds nothing left to fill and updates 0 rows.

PREREQUISITES (runbook)
-----------------------
1. Pack'N OS migration 0151 deployed (the script preflights the column and
   exits loudly if absent).
2. One-time column-scoped UPDATE grant (the automation role has SELECT+INSERT
   only). Owner runs on the Coolify box (root@159.223.191.62):
     docker exec <pg-container> psql -U packn -d packn_os -c "GRANT UPDATE (order_number, shipment_tracking_number) ON customer_complaints TO packn_os_existing_automation;"
3. PACKN_OS_DATABASE_URL in the interactive env (same as backfill_complaints).

RUN (droplet packn@167.99.229.91, /opt/packn/hubspot_ticket_automation):
  1. Dry run (counts only, NO writes):
       .venv/bin/python scripts/heal_complaint_order_numbers.py --dry-run
  2. Real run:
       .venv/bin/python scripts/heal_complaint_order_numbers.py
  3. After the real run, the Pack'N OS nightly snapshot re-upserts the trailing
     ~40 days of sla_daily_history, so the 7/7+ accuracy trend heals on its own
     by the next morning (or immediately via the OS backfill script).

Interactive one-shot: MAY fail loudly (backfill_complaints mold), unlike the
never-fail cron writer. Stdlib + packn_os_hubspot_client only.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from packn_os_hubspot_client import client  # noqa: E402
from packn_os_hubspot_client.db import close_pool, get_pool  # noqa: E402
from packn_os_hubspot_client.tenant import TENANT_ID  # noqa: E402

CSV_PATH = ROOT / "outputs" / "kpi" / "mispack_log.csv"

EXIT_OK = 0
EXIT_COLUMN_MISSING = 3


def _log(msg: str) -> None:
    print(f"[heal_complaint_order_numbers] {msg}", file=sys.stderr)


class _DryRunRollback(Exception):
    """Raised through the transaction block to roll a dry run back — psycopg3
    forbids an explicit conn.rollback() inside `with conn.transaction()`."""


def _order_number_column_present() -> bool:
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'customer_complaints' "
                "AND column_name = 'order_number'"
            )
            return cur.fetchone() is not None


def load_csv_rows(csv_path: Path) -> list[dict]:
    """CSV → [{ticket_id, order_number, tracking_number}] for rows that carry a
    ticket id AND at least one fillable value. Malformed rows are skipped+counted
    by the caller via the returned list length vs file length — this loader
    never raises on a torn line."""
    out: list[dict] = []
    with csv_path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            try:
                ticket = (raw.get("ticket_id") or "").strip()
                order_number = client.normalize_optional(raw.get("order_number"))
                tracking = client.normalize_optional(raw.get("tracking_number"))
            except (csv.Error, KeyError, AttributeError):
                continue
            if not ticket:
                continue
            if order_number is None and tracking is None:
                continue
            out.append(
                {
                    "ticket_id": ticket,
                    "order_number": order_number,
                    "tracking_number": tracking,
                }
            )
    return out


def run(csv_path: Path, dry_run: bool) -> dict:
    if not csv_path.exists():
        _log(f"input missing: {csv_path} — nothing to heal.")
        return {"candidates": 0, "updated": 0}

    if not _order_number_column_present():
        _log(
            "order_number column missing — Pack'N OS migration 0151 not "
            "deployed yet; run this AFTER the OS deploy."
        )
        sys.exit(EXIT_COLUMN_MISSING)

    rows = load_csv_rows(csv_path)
    _log(f"csv rows with a fillable value: {len(rows)} (dry_run={dry_run})")

    updated = 0
    with get_pool().connection() as conn:
        try:
            with conn.transaction():
                with conn.cursor() as cur:
                    for row in rows:
                        # Fill-if-empty ONLY; the row qualifies only when it
                        # has a NULL this CSV row can actually fill (so
                        # rowcount == real changes and a re-run is a clean 0).
                        cur.execute(
                            """
                            UPDATE customer_complaints
                            SET order_number = COALESCE(order_number, %(order_number)s),
                                shipment_tracking_number =
                                  COALESCE(shipment_tracking_number, %(tracking)s)
                            WHERE tenant_id = %(tenant)s
                              AND hubspot_ticket_id = %(ticket)s
                              AND deleted_at IS NULL
                              AND (
                                (order_number IS NULL AND %(order_number)s::text IS NOT NULL)
                                OR (shipment_tracking_number IS NULL AND %(tracking)s::text IS NOT NULL)
                              )
                            """,
                            {
                                "tenant": TENANT_ID,
                                "ticket": row["ticket_id"],
                                "order_number": row["order_number"],
                                "tracking": row["tracking_number"],
                            },
                        )
                        updated += cur.rowcount
                if dry_run:
                    # Abort the whole transaction — the dry run exercises the
                    # REAL statements (grants, casts, predicates), zero writes.
                    raise _DryRunRollback()
        except _DryRunRollback:
            _log("dry-run: transaction rolled back, no rows persisted.")

    _log(f"done: candidates={len(rows)} updated={updated} dry_run={dry_run}")
    return {"candidates": len(rows), "updated": updated}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-shot fill-if-empty heal of customer_complaints.order_number from the mispack CSV."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the real UPDATEs inside a rolled-back transaction (counts, no writes).",
    )
    parser.add_argument(
        "--csv",
        default=str(CSV_PATH),
        help="override the mispack CSV path (default outputs/kpi/mispack_log.csv).",
    )
    args = parser.parse_args(argv)
    try:
        run(Path(args.csv), dry_run=args.dry_run)
    finally:
        close_pool()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
