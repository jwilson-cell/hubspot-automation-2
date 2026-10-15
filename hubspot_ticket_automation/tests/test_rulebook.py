"""The reply rulebook (2026-10-15): kb/rulebook/ is GENERATED from Pack'N OS
(src/lib/tickets/reply-rulebook.ts, via scripts/export-reply-rulebook.ts there),
and the OS test suite fails while the two differ. Here: every category slug in
the shared contract has its file, and the KB context leads with the rulebook,
outside the KB cap."""
from __future__ import annotations

import json
from pathlib import Path

from ticket_pipeline import kb

ROOT = Path(__file__).resolve().parent.parent
SLUGS = json.loads((ROOT / "tests" / "fixtures" / "ticket-category-slugs.v1.json").read_text())["slugs"]


def test_every_category_slug_has_a_rulebook_file() -> None:
    assert (kb.RULEBOOK_DIR / "_global.md").is_file()
    for slug in SLUGS:
        assert (kb.RULEBOOK_DIR / f"{slug}.md").is_file(), slug


def test_the_rulebook_is_the_global_rules_plus_the_category_section() -> None:
    body = kb.load_rulebook("damaged_goods")
    assert body.startswith("## Every ticket type")
    assert "## damaged_goods:" in body
    assert "<!-- GENERATED" not in body


def test_an_unknown_or_path_like_category_gets_the_global_rules_alone() -> None:
    for category in ("Operations Inquiry", "../config/settings", ""):
        body = kb.load_rulebook(category)
        assert "## Every ticket type" in body
        assert all(f"## {slug}:" not in body for slug in SLUGS)


def test_the_kb_context_leads_with_the_rulebook_outside_the_cap() -> None:
    settings = {"kb_always_include": ["kb/brand_voice.md", "kb/glossary.md"]}
    categories = {"wismo_tracking": {"kb_file": "kb/tracking_wismo.md"}}
    ctx = kb.load_kb_context("wismo_tracking", settings, categories)
    assert ctx.startswith("----- RULEBOOK for wismo_tracking")
    rulebook = kb.load_rulebook("wismo_tracking")
    assert rulebook in ctx
    # The docs after the rulebook still respect the cap on their own.
    docs = ctx.split(rulebook, 1)[1]
    assert len(docs) <= kb.KB_CHAR_CAP + 200


def test_a_missing_rulebook_folder_leaves_the_kb_docs_unchanged(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(kb, "RULEBOOK_DIR", tmp_path / "absent")
    settings = {"kb_always_include": ["kb/glossary.md"]}
    ctx = kb.load_kb_context("wismo_tracking", settings, {})
    assert ctx.startswith("----- kb/glossary.md -----")
