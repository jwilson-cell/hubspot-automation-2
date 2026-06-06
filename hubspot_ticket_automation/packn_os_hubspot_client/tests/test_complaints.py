"""Phase 20 D-01/D-04 — client.write_complaints + parse helpers + writer e2e.

Activated by Plan 20-04. Tests run against the live dev Postgres DB
(localhost:5433, migration 0073 applied by Plan 20-01 — `brand` column +
the PARTIAL unique index customer_complaints_tenant_hubspot_uniq … WHERE
deleted_at IS NULL). Test tickets use the 'pytest-' prefix so the cleanup
in conftest._cleanup is targeted.

THE load-bearing regression guard: test_write_complaints_idempotent_no_42p10
FAILS LOUDLY with psycopg.errors.InvalidColumnReference (42P10) if the
ON CONFLICT target omits `WHERE deleted_at IS NULL` — because the unique
index is PARTIAL (memory project_packn_os_partial_index_onconflict_targetwhere).
"""

import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

from packn_os_hubspot_client import client
from packn_os_hubspot_client.db import close_pool

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# client.write_complaints — DB-backed (the 42P10 regression guard)
# ---------------------------------------------------------------------------


def test_write_complaints_idempotent_no_42p10(
    db_conn: psycopg.Connection, cleanup_test_data: None
) -> None:
    """Same batch twice ⇒ first inserts 2, second inserts 0 conflict_skipped 2,
    and raises NOTHING. If the conflict target omits WHERE deleted_at IS NULL
    this raises psycopg.errors.InvalidColumnReference (42P10) — the partial
    index trap. This is the load-bearing regression guard."""
    nonce = uuid4().hex[:8]
    rows = [
        {
            "hubspot_ticket_id": f"pytest-c-{nonce}-1",
            "classification": "mispack",
            "shipment_tracking_number": "1Zpytest0000000001",
            "brand": "Pytest Brand LLC",
            "complained_at": "2026-06-01T12:00:00+00:00",
        },
        {
            "hubspot_ticket_id": f"pytest-c-{nonce}-2",
            "classification": "mispack",
            "shipment_tracking_number": "1Zpytest0000000002",
            "brand": "Pytest Brand LLC",
            "complained_at": "2026-06-01T12:00:00+00:00",
        },
    ]

    close_pool()
    first = client.write_complaints(rows)
    second = client.write_complaints(rows)
    close_pool()

    assert first == {"inserted": 2, "conflict_skipped": 0}
    assert second == {"inserted": 0, "conflict_skipped": 2}

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*)::int AS n FROM customer_complaints "
            "WHERE hubspot_ticket_id LIKE %s",
            (f"pytest-c-{nonce}-%",),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["n"] == 2


def test_empty_strings_coerced_to_null(
    db_conn: psycopg.Connection, cleanup_test_data: None
) -> None:
    """tracking='' and brand='' land as SQL NULL, never ''."""
    nonce = uuid4().hex[:8]
    rows = [
        {
            "hubspot_ticket_id": f"pytest-c-{nonce}-empty",
            "classification": "mispack",
            "shipment_tracking_number": "",
            "brand": "",
            "complained_at": "2026-06-01T12:00:00+00:00",
        }
    ]

    close_pool()
    result = client.write_complaints(rows)
    close_pool()

    assert result == {"inserted": 1, "conflict_skipped": 0}

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT shipment_tracking_number, brand FROM customer_complaints "
            "WHERE hubspot_ticket_id = %s",
            (f"pytest-c-{nonce}-empty",),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["shipment_tracking_number"] is None
    assert row["brand"] is None


# ---------------------------------------------------------------------------
# parse helpers — pure, no DB
# ---------------------------------------------------------------------------


def test_parse_complained_at() -> None:
    """Strict dual-format parse → ISO string with tz, or None. Never defaults."""
    # ISO-8601 with 'Z' (the live on-disk format per probe-3)
    z = client.parse_complained_at("2026-06-01T12:00:00Z")
    assert z is not None
    # round-trips to a tz-aware instant at UTC
    assert "+00:00" in z or z.endswith("Z")
    from datetime import datetime, timezone

    assert datetime.fromisoformat(z.replace("Z", "+00:00")) == datetime(
        2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc
    )

    # ISO-8601 with explicit offset
    off = client.parse_complained_at("2026-06-01T12:00:00+00:00")
    assert off is not None
    assert datetime.fromisoformat(off.replace("Z", "+00:00")) == datetime(
        2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc
    )

    # 13-digit epoch-ms → correct UTC instant
    ms = client.parse_complained_at("1780000000000")
    assert ms is not None
    assert datetime.fromisoformat(ms.replace("Z", "+00:00")) == datetime.fromtimestamp(
        1780000000000 / 1000, tz=timezone.utc
    )

    # garbage / empty / None → None (never defaulted)
    assert client.parse_complained_at("garbage") is None
    assert client.parse_complained_at("") is None
    assert client.parse_complained_at(None) is None


def test_normalize_optional() -> None:
    """'' / whitespace-only / None → None; else stripped string."""
    assert client.normalize_optional("  x ") == "x"
    assert client.normalize_optional("") is None
    assert client.normalize_optional("   ") is None
    assert client.normalize_optional(None) is None


# ---------------------------------------------------------------------------
# scripts/write_complaints.py — deterministic post-run writer (Task 2)
# ---------------------------------------------------------------------------

sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _write_fixture_csv(path: Path, nonce: str) -> None:
    """Header + 4 data rows: good / bad-timestamp / empty-ticket / good-empties."""
    import csv as _csv

    header = [
        "ticket_id",
        "ticket_link",
        "first_seen_utc",
        "customer_name",
        "customer_email",
        "company_name",
        "order_number",
        "tracking_number",
        "sku_mentioned",
        "issue_description",
        "requested_credit_usd",
        "reshipment_needed",
        "classifier_confidence",
        "priority",
        "draft_note_id",
    ]
    rows = [
        # (1) good row — valid ISO ts, tracking + company present
        {
            "ticket_id": f"pytest-w-{nonce}-1",
            "first_seen_utc": "2026-06-01T12:00:00Z",
            "company_name": "WKR",
            "tracking_number": "1Zpytestw000000001",
        },
        # (2) bad-timestamp row → skipped_bad
        {
            "ticket_id": f"pytest-w-{nonce}-2",
            "first_seen_utc": "garbage",
            "company_name": "OFUURE",
            "tracking_number": "1Zpytestw000000002",
        },
        # (3) empty ticket_id row → skipped_bad
        {
            "ticket_id": "",
            "first_seen_utc": "2026-06-01T13:00:00Z",
            "company_name": "Vague Studios",
            "tracking_number": "1Zpytestw000000003",
        },
        # (4) good row with tracking="" and company_name="" → NULL coercion
        {
            "ticket_id": f"pytest-w-{nonce}-3",
            "first_seen_utc": "2026-06-01T14:00:00Z",
            "company_name": "",
            "tracking_number": "",
        },
    ]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = _csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in header})


def test_writer_run_over_fixture_csv(
    db_conn: psycopg.Connection, cleanup_test_data: None, tmp_path: Path
) -> None:
    """run() over a 4-row fixture: 2 inserted, 2 skipped_bad; second run all
    conflict_skipped (D-04 whole-file re-scan, conflicts free)."""
    import write_complaints

    nonce = uuid4().hex[:8]
    csv_path = tmp_path / "mispack_log.csv"
    _write_fixture_csv(csv_path, nonce)

    close_pool()
    first = write_complaints.run(csv_path)
    second = write_complaints.run(csv_path)
    close_pool()

    assert first == {
        "rows": 4,
        "inserted": 2,
        "conflict_skipped": 0,
        "skipped_bad": 2,
    }
    assert second == {
        "rows": 4,
        "inserted": 0,
        "conflict_skipped": 2,
        "skipped_bad": 2,
    }

    # row 4: empty tracking + company → NULL/NULL
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT shipment_tracking_number, brand FROM customer_complaints "
            "WHERE hubspot_ticket_id = %s",
            (f"pytest-w-{nonce}-3",),
        )
        row = cur.fetchone()
    assert row is not None
    assert row["shipment_tracking_number"] is None
    assert row["brand"] is None


def test_writer_missing_csv_is_tolerated(
    cleanup_test_data: None, tmp_path: Path
) -> None:
    """run() with a nonexistent csv_path → zero-counts, no raise."""
    import write_complaints

    missing = tmp_path / "does-not-exist.csv"

    close_pool()
    result = write_complaints.run(missing)
    close_pool()

    assert result["rows"] == 0
    assert result["inserted"] == 0
