"""Helper to invoke the SKILL.md draft branch in a testable way.

The SKILL.md draft branch uses a `py -c` shellout to invoke client.write_draft.
For testing, we extract the shellout payload into a Python function so we can
mock client.write_draft directly without subprocess overhead.

When SKILL.md changes its shellout signature, update this helper to match.

Phase 4.1 D-02.a — TDD harness for the draft-branch hard-cut.
"""
import json
from typing import Any


def invoke_draft_branch(ticket_context: dict, draft_body: str) -> str:
    """Mirror the SKILL.md step 2f draft path shellout, in-process.

    The SKILL.md draft branch now invokes client.write_draft (Phase 4.1 D-02.a)
    with the same args the auto-send branch already uses. This helper imitates
    that invocation so tests can assert call shape without spawning subprocess.
    """
    from packn_os_hubspot_client import client

    snapshot = {
        "subject": ticket_context["subject"],
        "body": ticket_context["body"],
        "category": ticket_context["category"],
        "topic_of_ticket": ticket_context.get("topic_of_ticket"),
        "source_type": ticket_context["source_type"],
        "contact": ticket_context.get("contact"),
        "custom_properties": ticket_context.get("custom_properties", {}),
        "captured_at": ticket_context["captured_at"],
    }

    draft_id = client.write_draft(
        ticket_id=ticket_context["ticket_id"],
        routine_name="tickets-process",
        draft_body=draft_body,
        model="claude-sonnet-5",
        prompt_version="v3.3.0",
        hubspot_ticket_snapshot=snapshot,
    )

    if "action_items" in ticket_context and ticket_context["action_items"]:
        _append_to_pending_actions(ticket_context["ticket_id"], ticket_context["action_items"])

    return draft_id


def _append_to_pending_actions(ticket_id: str, action_items: list[dict[str, Any]]) -> None:
    """Append per-ticket action_items to pending_actions.json (option B per RESEARCH Q5)."""
    from packn_os_hubspot_client.client import PENDING_ACTIONS_PATH

    try:
        existing = json.loads(PENDING_ACTIONS_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        existing = []
    for item in action_items:
        existing.append({"ticket_id": ticket_id, **item})
    PENDING_ACTIONS_PATH.write_text(json.dumps(existing, indent=2))
