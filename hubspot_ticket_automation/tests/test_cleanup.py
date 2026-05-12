"""Phase 4.1 D-02.c — Tests for cleanup_cutover_draft_engagements.py.

The 7 test cases cover:
    1. extract_ticket_id_from_engagement finds the first TICKET association
    2. extract_ticket_id_from_engagement returns None on missing assoc
    3. Default behavior is dry-run (does NOT call delete_engagement)
    4. --apply DOES call delete_engagement when matching draft exists
    5. Orphan (no matching draft) is SKIPPED even with --apply
    6. --max-deletes cap is respected
    7. Idempotency: 204 + 404 both treated as success (re-runs are no-ops)

All HubSpot + DB calls are mocked; no live external dependencies required.
"""
import json
import sys
from unittest.mock import patch

import pytest


@pytest.fixture
def fake_engagement():
    """A canonical fake engagement payload matching the HubSpot REST shape."""
    return {
        "id": "ENG-1",
        "properties": {
            "hs_note_body": (
                "[DRAFT — REVIEW BEFORE SENDING]\n"
                "<reply>\n"
                "--- PACKN_METADATA_V1 ---\n"
                "{}\n"
                "--- PACKN_METADATA_END ---"
            ),
            "hs_timestamp": "2026-05-11T12:00:00Z",
            "hs_engagement_associations": json.dumps(
                [{"objectId": "TICKET-1", "objectType": "TICKET"}]
            ),
        },
    }


def test_extract_ticket_id_from_engagement_finds_ticket(fake_engagement):
    """Positive: extract_ticket_id_from_engagement returns the first TICKET assoc id."""
    from scripts.cleanup_cutover_draft_engagements import extract_ticket_id_from_engagement

    assert extract_ticket_id_from_engagement(fake_engagement) == "TICKET-1"


def test_extract_ticket_id_from_engagement_returns_none_when_no_assoc():
    """Negative: returns None when associations property is missing or null."""
    from scripts.cleanup_cutover_draft_engagements import extract_ticket_id_from_engagement

    e = {"id": "X", "properties": {"hs_engagement_associations": None}}
    assert extract_ticket_id_from_engagement(e) is None


@patch("scripts.cleanup_cutover_draft_engagements.delete_engagement")
@patch("scripts.cleanup_cutover_draft_engagements.find_matching_draft")
@patch("scripts.cleanup_cutover_draft_engagements.search_draft_engagements")
@patch("scripts.cleanup_cutover_draft_engagements.get_hubspot_token")
def test_dry_run_does_not_delete(
    mock_token, mock_search, mock_find, mock_delete, fake_engagement, tmp_path, monkeypatch
):
    """Default behavior is dry-run; delete_engagement MUST NOT be called."""
    mock_token.return_value = "tok"
    mock_search.return_value = [fake_engagement]
    mock_find.return_value = True
    monkeypatch.chdir(tmp_path)
    sys.argv = ["cleanup_cutover_draft_engagements.py"]
    from scripts.cleanup_cutover_draft_engagements import main

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert mock_delete.call_count == 0, "Dry-run must not call delete_engagement"
    logs = list((tmp_path / "outputs/cleanup").glob("cleanup_*.md"))
    assert len(logs) == 1, f"Expected exactly 1 cleanup log file, got {len(logs)}"
    content = logs[0].read_text(encoding="utf-8")
    assert "migrated-dry-run" in content
    assert "Mode:** DRY-RUN" in content


@patch("scripts.cleanup_cutover_draft_engagements.delete_engagement")
@patch("scripts.cleanup_cutover_draft_engagements.find_matching_draft")
@patch("scripts.cleanup_cutover_draft_engagements.search_draft_engagements")
@patch("scripts.cleanup_cutover_draft_engagements.get_hubspot_token")
def test_apply_deletes_when_matching_draft_exists(
    mock_token, mock_search, mock_find, mock_delete, fake_engagement, tmp_path, monkeypatch
):
    """--apply DOES call delete_engagement when a matching draft is found in DB."""
    mock_token.return_value = "tok"
    mock_search.return_value = [fake_engagement]
    mock_find.return_value = True
    mock_delete.return_value = (True, None)
    monkeypatch.chdir(tmp_path)
    sys.argv = ["cleanup_cutover_draft_engagements.py", "--apply"]
    from scripts.cleanup_cutover_draft_engagements import main

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert mock_delete.call_count == 1
    logs = list((tmp_path / "outputs/cleanup").glob("cleanup_*.md"))
    assert "migrated-deleted" in logs[0].read_text(encoding="utf-8")


@patch("scripts.cleanup_cutover_draft_engagements.delete_engagement")
@patch("scripts.cleanup_cutover_draft_engagements.find_matching_draft")
@patch("scripts.cleanup_cutover_draft_engagements.search_draft_engagements")
@patch("scripts.cleanup_cutover_draft_engagements.get_hubspot_token")
def test_orphan_is_not_deleted(
    mock_token, mock_search, mock_find, mock_delete, fake_engagement, tmp_path, monkeypatch
):
    """Orphan (no matching draft) is SKIPPED even with --apply."""
    mock_token.return_value = "tok"
    mock_search.return_value = [fake_engagement]
    mock_find.return_value = False  # no matching automation_drafts row
    monkeypatch.chdir(tmp_path)
    sys.argv = ["cleanup_cutover_draft_engagements.py", "--apply"]
    from scripts.cleanup_cutover_draft_engagements import main

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert mock_delete.call_count == 0, "Orphan must NOT be deleted even with --apply"
    logs = list((tmp_path / "outputs/cleanup").glob("cleanup_*.md"))
    assert "orphan" in logs[0].read_text(encoding="utf-8")


@patch("scripts.cleanup_cutover_draft_engagements.delete_engagement")
@patch("scripts.cleanup_cutover_draft_engagements.find_matching_draft")
@patch("scripts.cleanup_cutover_draft_engagements.search_draft_engagements")
@patch("scripts.cleanup_cutover_draft_engagements.get_hubspot_token")
def test_max_deletes_cap_respected(
    mock_token, mock_search, mock_find, mock_delete, fake_engagement, tmp_path, monkeypatch
):
    """--max-deletes=2 stops after 2 even if more candidates match."""
    mock_token.return_value = "tok"
    mock_search.return_value = [{**fake_engagement, "id": f"ENG-{i}"} for i in range(5)]
    mock_find.return_value = True
    mock_delete.return_value = (True, None)
    monkeypatch.chdir(tmp_path)
    sys.argv = ["cleanup_cutover_draft_engagements.py", "--apply", "--max-deletes", "2"]
    from scripts.cleanup_cutover_draft_engagements import main

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    assert mock_delete.call_count == 2, (
        f"Expected exactly 2 deletes capped by --max-deletes, got {mock_delete.call_count}"
    )
    logs = list((tmp_path / "outputs/cleanup").glob("cleanup_*.md"))
    assert "cap-reached" in logs[0].read_text(encoding="utf-8")


@patch("scripts.cleanup_cutover_draft_engagements.delete_engagement")
@patch("scripts.cleanup_cutover_draft_engagements.find_matching_draft")
@patch("scripts.cleanup_cutover_draft_engagements.search_draft_engagements")
@patch("scripts.cleanup_cutover_draft_engagements.get_hubspot_token")
def test_idempotency_404_treated_as_success(
    mock_token, mock_search, mock_find, mock_delete, fake_engagement, tmp_path, monkeypatch
):
    """delete_engagement returns (True, None) on 204 OR 404 — re-runs are no-ops.

    This test mocks delete_engagement to simulate either a 204 (first-run success)
    or a 404 (re-run no-op) — both return (True, None) from the delete_engagement
    function per the contract. Verifies the main loop logs migrated-deleted in both
    cases.
    """
    mock_token.return_value = "tok"
    mock_search.return_value = [fake_engagement]
    mock_find.return_value = True
    mock_delete.return_value = (True, None)  # delete_engagement internally treats 404 as success
    monkeypatch.chdir(tmp_path)
    sys.argv = ["cleanup_cutover_draft_engagements.py", "--apply"]
    from scripts.cleanup_cutover_draft_engagements import main

    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
    logs = list((tmp_path / "outputs/cleanup").glob("cleanup_*.md"))
    assert "migrated-deleted" in logs[0].read_text(encoding="utf-8")
