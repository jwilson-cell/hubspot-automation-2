"""Pure-logic pytest for the ticket_pipeline package (efficiency rework #3).

NO HTTP, NO DB, NO LLM CALLS. Pins:
  - hydrate.html_to_text / parse_form_fields — the form-blockquote parse the
    whole hydration parity rests on (memory
    packn-hubspot-form-ticket-data-model: values live in the first email's
    HTML blockquote, not on ticket properties)
  - hydrate.build_thread / latest_customer_message — thread ordering + body
    fallback (hs_email_text preferred, stripped HTML fallback)
  - llm.fill_template — MUST tolerate the prompts' literal JSON braces
    (str.format would raise KeyError on them)
  - llm.parse_json_response — bare / fenced / prose-wrapped JSON
  - llm.apply_confidence_floor — SKILL 2b confidence<0.5 + unknown-category
    forcing to other_unclassified
  - kb.kb_files_for_category / load_kb_context — always-include ordering +
    category slot preserved under the char cap
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ticket_pipeline import hydrate, kb, llm  # noqa: E402


# ---------------------------------------------------------------------------
# hydrate — html_to_text / parse_form_fields
# ---------------------------------------------------------------------------

FORM_BLOCKQUOTE = """
<blockquote>
  <p>Topic of Ticket: Carrier Issue (Provide Tracking)</p>
  <p>Order Number: 7679500</p>
  <p>Tracking Number:&nbsp;1Z02D293YW35246090</p>
  <p>Inquiry Description: My package says delivered but I never got it. Please help ASAP.</p>
  <div>Insurance on Package: true</div>
</blockquote>
"""


def test_html_to_text_strips_tags_and_entities() -> None:
    text = hydrate.html_to_text("<p>Hello&nbsp;world</p><br><div>second   line</div>")
    assert "Hello world" in text
    assert "second line" in text
    assert "<" not in text


def test_parse_form_fields_extracts_snake_keys() -> None:
    fields = hydrate.parse_form_fields(FORM_BLOCKQUOTE)
    assert fields["topic_of_ticket"] == "Carrier Issue (Provide Tracking)"
    assert fields["order_number"] == "7679500"
    assert fields["tracking_number"] == "1Z02D293YW35246090"
    assert fields["insurance_on_package"] == "true"
    assert "delivered" in fields["inquiry_description"]


def test_parse_form_fields_tolerates_garbage() -> None:
    assert hydrate.parse_form_fields(None) == {}
    assert hydrate.parse_form_fields("<p>no labeled lines here</p>") == {}


def test_build_thread_orders_and_falls_back_to_html() -> None:
    emails = [
        {
            "properties": {
                "hs_email_text": "",
                "hs_email_html": "<p>from html body</p>",
                "hs_email_direction": "INCOMING_EMAIL",
                "hs_email_from_email": "cust@example.com",
                "hs_createdate": "2026-07-02T00:00:00Z",
            }
        },
        {
            "properties": {
                "hs_email_text": "earlier plain text",
                "hs_email_direction": "INCOMING_EMAIL",
                "hs_email_from_email": "cust@example.com",
                "hs_createdate": "2026-07-01T00:00:00Z",
            }
        },
    ]
    thread = hydrate.build_thread(emails)
    assert [e["body_text"] for e in thread] == ["earlier plain text", "from html body"]
    assert hydrate.latest_customer_message(thread) == "from html body"


def test_detect_attachments_mentions_only() -> None:
    assert hydrate.detect_attachments("see the attached photos") is True
    assert hydrate.detect_attachments("screenshot below") is True
    assert bool(hydrate.detect_attachments("no files mentioned", None)) is False


# ---------------------------------------------------------------------------
# llm — fill_template / parse_json_response / confidence floor
# ---------------------------------------------------------------------------


def test_fill_template_survives_literal_json_braces() -> None:
    template = 'Context:\n{ticket_context}\n\nReturn: {"category": "<x>", "confidence": 0.9}'
    out = llm.fill_template(template, {"ticket_context": '{"id": "1"}'})
    assert '{"id": "1"}' in out
    assert '{"category": "<x>", "confidence": 0.9}' in out  # untouched


def test_parse_json_response_bare_fenced_and_prose() -> None:
    obj = {"category": "wismo_tracking", "confidence": 0.9}
    import json as _json

    raw = _json.dumps(obj)
    assert llm.parse_json_response(raw) == obj
    assert llm.parse_json_response(f"```json\n{raw}\n```") == obj
    assert llm.parse_json_response(f"Here is the result:\n{raw}\nHope that helps!") == obj
    assert llm.parse_json_response("[]") == []
    with pytest.raises(ValueError):
        llm.parse_json_response("no json here at all")


def test_confidence_floor_forces_other_unclassified() -> None:
    low = llm.apply_confidence_floor({"category": "billing_invoice", "confidence": 0.3})
    assert low["category"] == "other_unclassified"
    assert "floor_applied" in low

    unknown = llm.apply_confidence_floor({"category": "made_up_thing", "confidence": 0.95})
    assert unknown["category"] == "other_unclassified"

    good = llm.apply_confidence_floor({"category": "billing_invoice", "confidence": 0.85})
    assert good["category"] == "billing_invoice"
    assert "floor_applied" not in good

    bad_conf = llm.apply_confidence_floor({"category": "billing_invoice", "confidence": "n/a"})
    assert bad_conf["category"] == "other_unclassified"


# ---------------------------------------------------------------------------
# kb — file selection + cap
# ---------------------------------------------------------------------------

SETTINGS = {"kb_always_include": ["kb/brand_voice.md", "kb/glossary.md"]}
CATEGORIES = {
    "damaged_goods": {"kb_file": "kb/damage_claims.md"},
    "sla_escalation": {"kb_file": "kb/brand_voice.md"},
}


def test_kb_files_always_include_first_then_category() -> None:
    files = kb.kb_files_for_category("damaged_goods", SETTINGS, CATEGORIES)
    assert files == ["kb/brand_voice.md", "kb/glossary.md", "kb/damage_claims.md"]


def test_kb_files_dedupes_category_already_in_always() -> None:
    files = kb.kb_files_for_category("sla_escalation", SETTINGS, CATEGORIES)
    assert files == ["kb/brand_voice.md", "kb/glossary.md"]


def test_load_kb_context_includes_category_file_under_cap() -> None:
    ctx = kb.load_kb_context("damaged_goods", SETTINGS, CATEGORIES)
    assert "kb/brand_voice.md" in ctx
    assert "kb/damage_claims.md" in ctx
    assert len(ctx) <= kb.KB_CHAR_CAP + 400  # headers/truncation markers slack
