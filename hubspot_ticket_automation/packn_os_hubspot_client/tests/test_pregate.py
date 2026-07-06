"""Pure-logic pytest for scripts/pregate_tickets.py (2026-07-06).

NO HTTP, NO DB. Pins the testable core of the cron pre-gate:
  - build_search_body — must mirror SKILL.md Step 1's filters exactly
    (lastmodified GT, stage IN, optional visitor EQ, ASC sort, cap limit);
    drift here would make the pre-gate see a DIFFERENT queue than the agent
    and silently skip real work.
  - parse_last_run_at — ISO round-trip + the 24h-ago fallback for
    empty/missing/corrupt state (SKILL.md Preconditions #3).
  - unprocessed_candidates — the fail-open fingerprint dedupe: every
    ambiguous case must count as a candidate (false positives cost one
    agent launch; false negatives drop work).

The script module is imported via sys.path.insert (scripts/ is not a
package), the same trick test_backfill_complaints.py uses.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import pregate_tickets as pg  # noqa: E402

NOW_MS = 1_751_800_000_000  # fixed "now" for deterministic fallback tests
DAY_MS = 24 * 60 * 60 * 1000


# ---------------------------------------------------------------------------
# build_search_body — must mirror SKILL.md Step 1
# ---------------------------------------------------------------------------


def _filters(body: dict) -> list[dict]:
    return body["filterGroups"][0]["filters"]


def test_search_body_core_filters_and_shape() -> None:
    body = pg.build_search_body(123456, ["1", "3"], True, 25)

    by_prop = {f["propertyName"]: f for f in _filters(body)}
    assert by_prop["hs_lastmodifieddate"] == {
        "propertyName": "hs_lastmodifieddate",
        "operator": "GT",
        "value": 123456,
    }
    assert by_prop["hs_pipeline_stage"] == {
        "propertyName": "hs_pipeline_stage",
        "operator": "IN",
        "values": ["1", "3"],
    }
    assert by_prop["hs_last_message_from_visitor"] == {
        "propertyName": "hs_last_message_from_visitor",
        "operator": "EQ",
        "value": "true",
    }

    # Decision-only hydration + agent-identical ordering/limit.
    assert body["properties"] == ["hs_lastmodifieddate"]
    assert body["sorts"] == [
        {"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}
    ]
    assert body["limit"] == 25


def test_search_body_visitor_filter_omitted_when_disabled() -> None:
    body = pg.build_search_body(0, ["1"], False, 10)
    props = [f["propertyName"] for f in _filters(body)]
    assert "hs_last_message_from_visitor" not in props
    assert len(props) == 2


def test_search_body_stringifies_stage_values() -> None:
    # settings.yaml stages could be parsed as ints by YAML edits; the HubSpot
    # filter expects strings.
    body = pg.build_search_body(0, [1, 3], False, 10)  # type: ignore[list-item]
    by_prop = {f["propertyName"]: f for f in _filters(body)}
    assert by_prop["hs_pipeline_stage"]["values"] == ["1", "3"]


# ---------------------------------------------------------------------------
# parse_last_run_at — ISO parse + 24h fallback
# ---------------------------------------------------------------------------


def test_parse_last_run_at_iso_z() -> None:
    assert pg.parse_last_run_at("1970-01-01T00:00:01Z", now_ms=NOW_MS) == 1000


def test_parse_last_run_at_iso_offset_and_millis() -> None:
    assert (
        pg.parse_last_run_at("1970-01-01T00:00:01.500+00:00", now_ms=NOW_MS) == 1500
    )


def test_parse_last_run_at_fallback_on_empty_none_garbage() -> None:
    expected = NOW_MS - DAY_MS
    assert pg.parse_last_run_at(None, now_ms=NOW_MS) == expected
    assert pg.parse_last_run_at("", now_ms=NOW_MS) == expected
    assert pg.parse_last_run_at("not-a-date", now_ms=NOW_MS) == expected


# ---------------------------------------------------------------------------
# unprocessed_candidates — fail-open dedupe
# ---------------------------------------------------------------------------


def _hit(ticket_id: str, lastmod: str | None) -> dict:
    return {"id": ticket_id, "properties": {"hs_lastmodifieddate": lastmod}}


def test_no_fingerprint_is_a_candidate() -> None:
    assert pg.unprocessed_candidates([_hit("1", "2026-07-01T00:00:00Z")], {}) == ["1"]


def test_fingerprint_covering_current_lastmod_is_skipped() -> None:
    fps = {"1": {"num_notes": 1, "hs_lastmodifieddate": "2026-07-01T00:00:00Z"}}
    # equal → skip; older ticket than fingerprint → skip
    assert pg.unprocessed_candidates([_hit("1", "2026-07-01T00:00:00Z")], fps) == []
    assert pg.unprocessed_candidates([_hit("1", "2026-06-30T00:00:00Z")], fps) == []


def test_newer_lastmod_than_fingerprint_is_a_candidate() -> None:
    fps = {"1": {"hs_lastmodifieddate": "2026-07-01T00:00:00Z"}}
    assert pg.unprocessed_candidates([_hit("1", "2026-07-02T00:00:00Z")], fps) == ["1"]


def test_ambiguous_fingerprints_fail_open() -> None:
    hits = [_hit("1", "2026-07-01T00:00:00Z"), _hit("2", None), _hit("3", "garbage")]
    fps = {
        # fingerprint with null lastmod AND null/absent processed_at → candidate
        "1": {"num_notes": 1, "hs_lastmodifieddate": None},
        # fingerprint present but ticket lastmod missing → candidate
        "2": {"hs_lastmodifieddate": "2026-07-01T00:00:00Z"},
        # unparseable ticket lastmod → candidate
        "3": {"hs_lastmodifieddate": "2026-07-01T00:00:00Z"},
    }
    assert pg.unprocessed_candidates(hits, fps) == ["1", "2", "3"]


def test_backfill_echo_covered_by_processed_at() -> None:
    """The echo-launch hole (observed live 2026-07-06): step 2a.6 bumps the
    ticket's lastmodified DURING processing, so the fingerprint's recorded
    lastmod is stale — but processed_at (stamped at ticket completion) is
    later than the bump, and must suppress the candidate."""
    fps = {
        "1": {
            "hs_lastmodifieddate": "2026-07-06T18:00:10Z",  # pre-backfill snapshot
            "processed_at": "2026-07-06T18:03:00Z",  # ticket finished
        }
    }
    # backfill bumped lastmod to 18:01 — BETWEEN snapshot and completion → skip
    assert pg.unprocessed_candidates([_hit("1", "2026-07-06T18:01:00Z")], fps) == []
    # a customer reply at 18:10 lands AFTER processed_at → candidate
    assert pg.unprocessed_candidates([_hit("1", "2026-07-06T18:10:00Z")], fps) == ["1"]


def test_backfilled_fingerprint_with_null_lastmod_uses_processed_at() -> None:
    """Live state.json has run-#1 backfilled fingerprints with
    hs_lastmodifieddate: None but a real processed_at — the processed_at
    branch must still dedupe those when nothing changed since."""
    fps = {"1": {"hs_lastmodifieddate": None, "processed_at": "2026-07-06T18:03:00Z"}}
    assert pg.unprocessed_candidates([_hit("1", "2026-07-06T18:01:00Z")], fps) == []
    assert pg.unprocessed_candidates([_hit("1", "2026-07-06T18:10:00Z")], fps) == ["1"]


def test_non_dict_fingerprint_and_missing_id_handled() -> None:
    hits = [{"properties": {}}, _hit("9", "2026-07-01T00:00:00Z")]
    fps = {"9": "corrupt-string-not-dict"}
    # missing id dropped entirely; corrupt fingerprint fails open
    assert pg.unprocessed_candidates(hits, fps) == ["9"]
