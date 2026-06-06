"""Phase 20 Plan 20-05 — pure-logic pytest for the historical complaint backfill.

NO HTTP, NO DB. Pins the testable core of scripts/backfill_complaints.py:
  - build_search_body — the exact HubSpot /crm/v3/objects/tickets/search body
    (probe-1 CONFIRMED: topic_of_ticket EQ "Mispack (Provide Order Number)" is
    searchable, total=35 — the search source this body queries IS valid)
  - extract_tracking_from_blockquote — the form-vs-property fallback: old tickets
    carry NULL tracking_number properties; the real value lives in the first
    email body's HTML blockquote (memory reference_hubspot_form_vs_property)
  - chunk — input-id chunking ≤100 for the v4 association / v3 company batch reads
  - load_watermark / save_watermark — resumable createdate watermark round-trip;
    a lost/corrupt watermark degrades to 0 (a free full idempotent re-sweep)
  - brand_from_batches — ticket→company association → company name (ER-3 brand)

The script module is imported via sys.path.insert (scripts/ is not a package),
the same trick the writer tests use (test_complaints.py).
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import backfill_complaints as bf  # noqa: E402


# ---------------------------------------------------------------------------
# build_search_body — exact HubSpot search request shape
# ---------------------------------------------------------------------------


def test_build_search_body_first_page_omits_after() -> None:
    """First page (after=None) → no 'after' key in the body."""
    body = bf.build_search_body(watermark_ms=0, after=None)
    assert "after" not in body

    fg = body["filterGroups"][0]["filters"]
    # topic_of_ticket EQ the exact mispack topic value (probe-1 source)
    topic = next(f for f in fg if f["propertyName"] == "topic_of_ticket")
    assert topic["operator"] == "EQ"
    assert topic["value"] == "Mispack (Provide Order Number)"

    # createdate GT the watermark
    created = next(f for f in fg if f["propertyName"] == "createdate")
    assert created["operator"] == "GT"
    assert created["value"] == 0

    # properties requested for hydration
    assert "tracking_number" in body["properties"]
    assert "order_number" in body["properties"]
    assert "createdate" in body["properties"]

    # createdate ASCENDING sort + 200/page
    assert body["sorts"] == [
        {"propertyName": "createdate", "direction": "ASCENDING"}
    ]
    assert body["limit"] == 200


def test_build_search_body_with_after_includes_after() -> None:
    """after='tok' → 'after' present with the token; watermark passed through."""
    body = bf.build_search_body(watermark_ms=1780000000000, after="tok-123")
    assert body["after"] == "tok-123"

    created = next(
        f
        for f in body["filterGroups"][0]["filters"]
        if f["propertyName"] == "createdate"
    )
    assert created["value"] == 1780000000000


# ---------------------------------------------------------------------------
# extract_tracking_from_blockquote — form-vs-property fallback parse
# ---------------------------------------------------------------------------


def test_extract_tracking_present_plain() -> None:
    html = "<blockquote>Tracking Number: 1Z999AA10123456784</blockquote>"
    assert bf.extract_tracking_from_blockquote(html) == "1Z999AA10123456784"


def test_extract_tracking_tolerates_html_tags_and_whitespace() -> None:
    # tags + whitespace between the label and the value
    html = (
        "<div><strong>Tracking&nbsp;Number:</strong>"
        "  <span>  1Z999AA10123456784 </span></div>"
    )
    assert bf.extract_tracking_from_blockquote(html) == "1Z999AA10123456784"


def test_extract_tracking_case_insensitive_label() -> None:
    html = "tracking number:    9400111899223817200000"
    assert bf.extract_tracking_from_blockquote(html) == "9400111899223817200000"


def test_extract_tracking_missing_field_returns_none() -> None:
    html = "<blockquote>Order Number: 10456<br>Customer: Jane</blockquote>"
    assert bf.extract_tracking_from_blockquote(html) is None


def test_extract_tracking_labeled_but_empty_returns_none() -> None:
    html = "<blockquote>Tracking Number: <br>Order Number: 10456</blockquote>"
    assert bf.extract_tracking_from_blockquote(html) is None


def test_extract_tracking_none_input_returns_none() -> None:
    assert bf.extract_tracking_from_blockquote(None) is None
    assert bf.extract_tracking_from_blockquote("") is None


# ---------------------------------------------------------------------------
# chunk — input-id chunking ≤100
# ---------------------------------------------------------------------------


def test_chunk_250_into_100() -> None:
    chunks = list(bf.chunk(list(range(250)), 100))
    assert [len(c) for c in chunks] == [100, 100, 50]
    # no element lost or duplicated
    flat = [x for c in chunks for x in c]
    assert flat == list(range(250))


def test_chunk_empty() -> None:
    assert list(bf.chunk([], 100)) == []


def test_chunk_smaller_than_size() -> None:
    assert list(bf.chunk([1, 2, 3], 100)) == [[1, 2, 3]]


# ---------------------------------------------------------------------------
# watermark round-trip — resumable + corrupt-tolerant
# ---------------------------------------------------------------------------


def test_watermark_round_trip(tmp_path: Path) -> None:
    path = tmp_path / ".complaints_backfill_state.json"
    bf.save_watermark(path, 1780000000000)
    assert bf.load_watermark(path) == 1780000000000


def test_watermark_missing_file_returns_zero(tmp_path: Path) -> None:
    assert bf.load_watermark(tmp_path / "nope.json") == 0


def test_watermark_corrupt_json_returns_zero(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert bf.load_watermark(path) == 0


def test_watermark_wrong_shape_returns_zero(tmp_path: Path) -> None:
    """Valid JSON but missing/non-int last_createdate → 0 (free re-sweep)."""
    path = tmp_path / "shape.json"
    path.write_text(json.dumps({"other": "x"}), encoding="utf-8")
    assert bf.load_watermark(path) == 0


# ---------------------------------------------------------------------------
# brand_from_batches — ticket→company association → company name (ER-3)
# ---------------------------------------------------------------------------


def _assoc_response(ticket_id: str, company_id: str | None) -> dict:
    """Shape of POST /crm/v4/associations/tickets/companies/batch/read."""
    if company_id is None:
        return {"results": [{"from": {"id": ticket_id}, "to": []}]}
    return {
        "results": [
            {"from": {"id": ticket_id}, "to": [{"toObjectId": company_id}]}
        ]
    }


def _companies_response(company_id: str, name: str | None) -> dict:
    """Shape of POST /crm/v3/objects/companies/batch/read."""
    props = {} if name is None else {"name": name}
    return {"results": [{"id": company_id, "properties": props}]}


def test_brand_resolves_company_name() -> None:
    assoc = _assoc_response("t1", "c1")
    companies = _companies_response("c1", "WKR")
    assert bf.brand_from_batches(assoc, companies, "t1") == "WKR"


def test_brand_no_association_returns_none() -> None:
    assoc = _assoc_response("t1", None)
    companies = {"results": []}
    assert bf.brand_from_batches(assoc, companies, "t1") is None


def test_brand_company_empty_name_returns_none() -> None:
    assoc = _assoc_response("t1", "c1")
    companies = _companies_response("c1", "")
    assert bf.brand_from_batches(assoc, companies, "t1") is None


def test_brand_company_whitespace_name_returns_none() -> None:
    assoc = _assoc_response("t1", "c1")
    companies = _companies_response("c1", "   ")
    assert bf.brand_from_batches(assoc, companies, "t1") is None


def test_brand_ticket_not_in_assoc_returns_none() -> None:
    assoc = _assoc_response("other", "c1")
    companies = _companies_response("c1", "WKR")
    assert bf.brand_from_batches(assoc, companies, "t1") is None
