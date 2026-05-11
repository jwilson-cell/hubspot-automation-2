"""Phase 4.1 D-02.a — TDD test for SKILL.md draft-branch migration to client.write_draft.

Tests assert that when SKILL.md's draft branch path executes (low-confidence
non-FORM tickets or FORM with topic outside auto_send_form_topics), the
invocation calls client.write_draft instead of creating a HubSpot draft
engagement via mcp__claude_ai_HubSpot__manage_crm_objects.

RED before the SKILL.md edit, GREEN after.

Per CONTEXT D-02.e: TDD required; failing test before SKILL.md change.
"""
import json
from unittest.mock import patch

import pytest


@pytest.fixture
def synthetic_draft_branch_ticket_context():
    """A ticket_context dict that routes to the draft branch per SKILL.md
    routing logic: is_auto_send=false (low-confidence, non-FORM)."""
    return {
        "ticket_id": "TICKET-DRAFT-1",
        "is_auto_send": False,
        "subject": "Where is my package?",
        "body": "Hi, my tracking shows no movement for 3 days.",
        "category": "tracking",
        "topic_of_ticket": None,
        "source_type": "EMAIL",
        "contact": {"email": "customer@example.com", "name": "Test Customer"},
        "custom_properties": {},
        "captured_at": "2026-05-11T12:00:00Z",
    }


@pytest.fixture
def expected_write_draft_kwargs():
    return {
        "ticket_id": "TICKET-DRAFT-1",
        "routine_name": "tickets-process",
        "draft_body": "<drafted reply text>",
        "model": "claude-sonnet-4-5",
        "prompt_version": "v3.2.1",
        "hubspot_ticket_snapshot": {
            "subject": "Where is my package?",
            "body": "Hi, my tracking shows no movement for 3 days.",
            "category": "tracking",
            "topic_of_ticket": None,
            "source_type": "EMAIL",
            "contact": {"email": "customer@example.com", "name": "Test Customer"},
            "custom_properties": {},
            "captured_at": "2026-05-11T12:00:00Z",
        },
    }


@patch("packn_os_hubspot_client.client.write_draft")
def test_skill_draft_branch_uses_write_draft(
    mock_write_draft, synthetic_draft_branch_ticket_context, expected_write_draft_kwargs
):
    """The draft branch invocation MUST call client.write_draft with the expected kwargs."""
    mock_write_draft.return_value = "draft-uuid-abc-123"
    from tests.helpers.skill_invoker import invoke_draft_branch

    result = invoke_draft_branch(
        synthetic_draft_branch_ticket_context, draft_body="<drafted reply text>"
    )

    assert mock_write_draft.call_count == 1, "write_draft must be called exactly once"
    call_kwargs = mock_write_draft.call_args.kwargs
    assert call_kwargs["ticket_id"] == expected_write_draft_kwargs["ticket_id"]
    assert call_kwargs["routine_name"] == expected_write_draft_kwargs["routine_name"]
    assert call_kwargs["draft_body"] == expected_write_draft_kwargs["draft_body"]
    assert call_kwargs["model"] == expected_write_draft_kwargs["model"]
    assert call_kwargs["prompt_version"] == expected_write_draft_kwargs["prompt_version"]
    assert call_kwargs["hubspot_ticket_snapshot"] == expected_write_draft_kwargs["hubspot_ticket_snapshot"]
    assert result == "draft-uuid-abc-123"


@patch("packn_os_hubspot_client.client.write_draft")
def test_skill_draft_branch_does_not_create_hubspot_note(
    mock_write_draft, synthetic_draft_branch_ticket_context
):
    """Negative assertion: draft branch must NOT create a HubSpot draft engagement."""
    mock_write_draft.return_value = "draft-uuid-xyz"
    with patch("packn_os_hubspot_client.client.mcp_manage_crm_objects", create=True) as mock_mcp:
        from tests.helpers.skill_invoker import invoke_draft_branch

        invoke_draft_branch(synthetic_draft_branch_ticket_context, draft_body="<reply>")
        assert mock_mcp.call_count == 0
    assert mock_write_draft.call_count == 1


@patch("packn_os_hubspot_client.client.write_draft")
def test_skill_draft_branch_preserves_action_items_to_pending_actions_json(
    mock_write_draft, synthetic_draft_branch_ticket_context, tmp_path
):
    """Per RESEARCH Open Question 5 → option B: action_items flow through pending_actions.json
    (NOT into automation_drafts)."""
    mock_write_draft.return_value = "draft-uuid"
    pending_actions_path = tmp_path / "pending_actions.json"
    pending_actions_path.write_text("[]")

    with patch("packn_os_hubspot_client.client.PENDING_ACTIONS_PATH", pending_actions_path):
        from tests.helpers.skill_invoker import invoke_draft_branch

        invoke_draft_branch(
            {
                **synthetic_draft_branch_ticket_context,
                "action_items": [
                    {
                        "action_type": "ask_tracking",
                        "description": "Request updated tracking",
                        "severity": "normal",
                        "needs_hubspot_reply": True,
                    }
                ],
            },
            draft_body="<reply>",
        )

    pending_actions = json.loads(pending_actions_path.read_text())
    assert any(a.get("ticket_id") == "TICKET-DRAFT-1" for a in pending_actions)
