"""Pure-logic tests for scripts/heal_complaint_order_numbers.load_csv_rows
(2026-07-22 — /sla accuracy blind-spot part D). No DB, no HTTP."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import heal_complaint_order_numbers as heal  # noqa: E402

HEADER = (
    "ticket_id,ticket_link,first_seen_utc,customer_name,customer_email,"
    "company_name,order_number,tracking_number,sku_mentioned,issue_description,"
    "requested_credit_usd,reshipment_needed,classifier_confidence,priority,"
    "draft_note_id\n"
)


def _write_csv(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "mispack_log.csv"
    p.write_text(HEADER + body, encoding="utf-8")
    return p


def test_loader_keeps_rows_with_a_fillable_value(tmp_path: Path) -> None:
    p = _write_csv(
        tmp_path,
        # order only / tracking only / both / neither / no ticket id
        "t1,,2026-07-08T00:00:00Z,,,,#17086,,,,,,0.9,normal,\n"
        "t2,,2026-07-08T00:00:00Z,,,,,1Z999,,,,,0.9,normal,\n"
        "t3,,2026-07-08T00:00:00Z,,,,82600,1Z888,,,,,0.9,normal,\n"
        "t4,,2026-07-08T00:00:00Z,,,,,,,,,,0.9,normal,\n"
        ",,2026-07-08T00:00:00Z,,,,#555,,,,,,0.9,normal,\n",
    )
    rows = heal.load_csv_rows(p)
    assert rows == [
        {"ticket_id": "t1", "order_number": "#17086", "tracking_number": None},
        {"ticket_id": "t2", "order_number": None, "tracking_number": "1Z999"},
        {"ticket_id": "t3", "order_number": "82600", "tracking_number": "1Z888"},
    ]


def test_loader_normalizes_whitespace_only_values_to_none(tmp_path: Path) -> None:
    p = _write_csv(
        tmp_path,
        't5,,2026-07-08T00:00:00Z,,,,"   ",,,,,,0.9,normal,\n',
    )
    # whitespace-only order_number + empty tracking ⇒ nothing fillable ⇒ dropped.
    assert heal.load_csv_rows(p) == []
