"""Unit tests for ticket_pipeline.llm's pure layer — added by the 2026-07-23
cost/accuracy audit alongside the structured-outputs + single-pass
fill_template changes."""
import pytest

anthropic = pytest.importorskip("anthropic")  # llm.py imports it at module level

from ticket_pipeline import llm  # noqa: E402


# ---------------------------------------------------------------- fill_template

def test_fill_template_basic():
    out = llm.fill_template("a {x} b {y}", {"x": "1", "y": "2"})
    assert out == "a 1 b 2"


def test_fill_template_leaves_unknown_placeholders():
    out = llm.fill_template('{"literal": "json"} {x}', {"x": "v"})
    assert out == '{"literal": "json"} v'


def test_fill_template_single_pass_no_injection():
    """A placeholder-looking string INSIDE a substituted value must not be
    re-substituted (customer-controlled ticket bodies can contain '{kb_context}')."""
    template = "CTX: {ticket_context}\nKB: {kb_context}"
    out = llm.fill_template(
        template,
        {"ticket_context": "body says {kb_context}", "kb_context": "SECRET_KB"},
    )
    assert out == "CTX: body says {kb_context}\nKB: SECRET_KB"


def test_fill_template_empty_values():
    assert llm.fill_template("{x}", {}) == "{x}"


# ---------------------------------------------------------- parse_json_response

def test_parse_json_fenced():
    assert llm.parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}


def test_parse_json_with_prose():
    assert llm.parse_json_response('Sure! {"a": 1} there you go') == {"a": 1}


def test_parse_json_unparseable_raises():
    with pytest.raises(ValueError):
        llm.parse_json_response("no json here")


# ------------------------------------------------------- apply_confidence_floor

def test_confidence_floor_forces_other():
    out = llm.apply_confidence_floor({"category": "wismo_tracking", "confidence": 0.3})
    assert out["category"] == "other_unclassified"
    assert "floor_applied" in out


def test_unknown_category_forced():
    out = llm.apply_confidence_floor({"category": "not_a_category", "confidence": 0.9})
    assert out["category"] == "other_unclassified"


def test_confident_valid_category_passes():
    out = llm.apply_confidence_floor({"category": "billing_invoice", "confidence": 0.92})
    assert out["category"] == "billing_invoice"
    assert out["priority"] == "normal"  # defaulted


# ------------------------------------------------------------- schema integrity

def test_classify_schema_matches_valid_categories():
    enum = llm.CLASSIFY_SCHEMA["properties"]["category"]["enum"]
    assert set(enum) == llm.VALID_CATEGORIES


def test_extract_schema_action_types_complete():
    """The 17-type taxonomy from prompts/extract_actions.md, verbatim."""
    assert len(llm.ACTION_TYPES) == 17
    items = llm.EXTRACT_SCHEMA["properties"]["actions"]["items"]
    assert items["properties"]["action_type"]["enum"] == llm.ACTION_TYPES
    assert items["properties"]["owner_hint"]["enum"] == llm.OWNER_HINTS
    # structured outputs require these on every object schema
    assert items["additionalProperties"] is False
    assert set(items["required"]) == set(items["properties"].keys())


def test_classify_schema_structured_output_constraints():
    assert llm.CLASSIFY_SCHEMA["additionalProperties"] is False
    assert set(llm.CLASSIFY_SCHEMA["required"]) == set(
        llm.CLASSIFY_SCHEMA["properties"].keys()
    )


# --------------------------------------------------------- strip_null_optionals

def test_strip_null_optionals_drops_only_named_null_keys():
    obj = {"a": 1, "claim_packet": None, "b": None}
    out = llm.strip_null_optionals(obj, ("claim_packet",))
    assert out == {"a": 1, "b": None}


def test_strip_null_optionals_keeps_populated():
    obj = {"claim_packet": {"x": 1}}
    assert llm.strip_null_optionals(obj, ("claim_packet",)) == obj
