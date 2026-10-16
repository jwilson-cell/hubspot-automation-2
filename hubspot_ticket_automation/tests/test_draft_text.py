"""2026-10-16: a drafted reply is the email alone. clean_draft_body strips the
markdown, wrapper lines and citation leftovers a model adds and keeps the
email's own words; llm.draft returns it cleaned. Same cases as the OS's
tests/unit/tasks/clean-draft-body.test.ts. No network."""
from __future__ import annotations

import pytest

from packn_os_hubspot_client.draft_text import clean_draft_body
from ticket_pipeline import llm


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        (
            "Hi there,\n\nYour order shipped via UPS [1]. Returns are not filed by Pack'N [2, 3].\n\n"
            "Pack'N Support\n\nSources:\n[1] WISMO SOP — fresh\n[2] Returns SOP",
            "Hi there,\n\nYour order shipped via UPS. Returns are not filed by Pack'N.\n\nPack'N Support",
        ),
        ("Thanks.\n\nPack'N Support\n\nSources:", "Thanks.\n\nPack'N Support"),
        ("It left our dock today [citation removed].", "It left our dock today."),
        ("DRAFTED WITHOUT SOP SUPPORT\n\nHi there,\n\nIt shipped.", "Hi there,\n\nIt shipped."),
        (
            "## Update\n**Your order** shipped *today* under tracking __1Z999__.\nNote* this.",
            "Update\nYour order shipped today under tracking 1Z999.\nNote this.",
        ),
        (
            "We need:\n* a photo of the box\n• a photo of the label",
            "We need:\n- a photo of the box\n- a photo of the label",
        ),
        (
            "Here's a draft reply for the customer:\n\nSubject: Your order\n\nHi there,\n\nIt shipped.",
            "Hi there,\n\nIt shipped.",
        ),
        (
            "```\nTrack it [here](https://ups.com/t/1Z) or at [https://ups.com/t/1Z](https://ups.com/t/1Z).\n---\n```",
            "Track it here (https://ups.com/t/1Z) or at https://ups.com/t/1Z.",
        ),
    ],
)
def test_cleans(raw: str, clean: str) -> None:
    assert clean_draft_body(raw) == clean


@pytest.mark.parametrize(
    "kept",
    [
        "Here's an update on your order email from Monday. It shipped today.",
        "Hi there,\n\nOrder #7679500 is in_transit. Estimated delivery 4/30.\n\nPack'N Support",
    ],
)
def test_keeps_plain_email(kept: str) -> None:
    assert clean_draft_body(kept) == kept


def test_idempotent() -> None:
    once = clean_draft_body("**Hi** there [1].\n\n\n\nIt shipped.\n\nSources:\n[1] X")
    assert once == "Hi there.\n\nIt shipped."
    assert clean_draft_body(once) == once


def test_llm_draft_returns_the_clean_email(monkeypatch) -> None:
    monkeypatch.setattr(llm, "_load_prompt", lambda name: "{ticket_context}{category}{kb_context}")
    monkeypatch.setattr(
        llm, "_call", lambda *a, **k: ("Here's the reply:\n\n**Hi there,**\n\nIt shipped.\n", {"in": 1})
    )
    usages: list[dict] = []
    assert llm.draft({}, "wismo_tracking", "", usages) == "Hi there,\n\nIt shipped."
    assert usages == [{"in": 1}]
