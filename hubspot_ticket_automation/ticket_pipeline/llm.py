"""The three LLM calls — same prompt files the agent reads, filled
deterministically.

Models (pinned — 2026-07-23 Claude 5 refresh):
    classify -> claude-haiku-4-5   (cheap; the form-topic prior does most of
                                    the work and confidence<0.5 falls back to
                                    other_unclassified downstream)
    draft    -> claude-sonnet-5    (matches the agent pin in run_tickets.sh /
                                    poll_manual_runs.sh and the write_draft
                                    stamp in SKILL.md — keep all four in sync)
    extract  -> claude-haiku-4-5   (structured extraction against a fixed
                                    17-type taxonomy)

claude-sonnet-5 request-shape notes: omitting `thinking` runs ADAPTIVE
thinking by default and thinking tokens count against max_tokens, so the
draft call disables it explicitly (well-specified single-shot generation —
the migration guide's sanctioned use of disabled). Sampling params and
prefills are never sent from here, so no other Sonnet 5 breaking change
applies.

classify + extract use structured outputs (output_config.format json_schema
with enum'd category / action_type / owner_hint), so responses are
schema-valid by construction — no more invalid action_type values silently
downgrading to `other` on OS ingestion. The lenient parse + ONE repair retry
is kept as a belt-and-suspenders fallback (it costs nothing on the happy
path: the first candidate parses).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional

import anthropic

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"

CLASSIFY_MODEL = "claude-haiku-4-5"
DRAFT_MODEL = "claude-sonnet-5"
EXTRACT_MODEL = "claude-haiku-4-5"

CLASSIFY_MAX_TOKENS = 700
DRAFT_MAX_TOKENS = 1200
EXTRACT_MAX_TOKENS = 2500

VALID_CATEGORIES = {
    "wismo_tracking",
    "shipping_delay",
    "damaged_goods",
    "mispack_wrong_item",
    "inventory_discrepancy",
    "inbound_asn_receiving",
    "returns_rma",
    "integration_system",
    "billing_invoice",
    "sla_escalation",
    "account_onboarding",
    "kitting_project",
    "address_recipient",
    "retailer_compliance",
    "other_unclassified",
}

# The 17-type action taxonomy from prompts/extract_actions.md — enum'd into
# the extract schema so an out-of-set value can never reach OS ingestion
# (where it would be downgraded to `other` and lose its routing).
ACTION_TYPES = [
    "expedite_fulfillment",
    "edit_order",
    "create_order",
    "reship_order",
    "warehouse_investigation",
    "mispack_investigation",
    "carrier_trace",
    "file_carrier_claim",
    "create_rma",
    "issue_credit_or_refund",
    "sync_sku_master",
    "update_asn_or_appointment",
    "request_missing_info_from_merchant",
    "billing_specialist_follow_up",
    "escalate_to_ops_manager",
    "escalate_to_account_manager",
    "other",
]

OWNER_HINTS = [
    "warehouse",
    "account_manager",
    "billing",
    "integration",
    "ops_manager",
    "merchant",
    "carrier",
]


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


# Structured-output schemas (output_config.format json_schema). Structured
# outputs require additionalProperties: false and every property listed in
# `required`, so optional fields are modeled as nullable and stripped when
# null before returning (preserving the "omit when absent" downstream
# contract for override_reason / claim_packet).
CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": sorted(VALID_CATEGORIES)},
        "form_topic": _nullable({"type": "string"}),
        "priority": {"type": "string", "enum": ["urgent", "normal"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "override_reason": _nullable({"type": "string"}),
    },
    "required": [
        "category",
        "form_topic",
        "priority",
        "confidence",
        "reason",
        "override_reason",
    ],
    "additionalProperties": False,
}

_CLAIM_PACKET_SCHEMA = {
    "type": "object",
    "properties": {
        "inferred_carrier": {
            "type": "string",
            "enum": ["UPS", "FedEx", "USPS", "DHL", "Amazon", "OnTrac", "LTL", "unknown"],
        },
        "carrier_confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "claim_type": {"type": "string", "enum": ["damage", "loss", "delay"]},
        "tracking_number": _nullable({"type": "string"}),
        "order_number": _nullable({"type": "string"}),
        "ship_date_hint": _nullable({"type": "string"}),
        "declared_value_hint": _nullable({"type": "number"}),
        "declared_value_source": _nullable({"type": "string"}),
        "damage_or_loss_description": {"type": "string"},
        "evidence_summary": {"type": "string"},
        "insurance_on_package": _nullable({"type": "boolean"}),
        "filing_deadline_iso": _nullable({"type": "string"}),
    },
    "required": [
        "inferred_carrier",
        "carrier_confidence",
        "claim_type",
        "tracking_number",
        "order_number",
        "ship_date_hint",
        "declared_value_hint",
        "declared_value_source",
        "damage_or_loss_description",
        "evidence_summary",
        "insurance_on_package",
        "filing_deadline_iso",
    ],
    "additionalProperties": False,
}

_ACTION_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "action_type": {"type": "string", "enum": ACTION_TYPES},
        "description": {"type": "string"},
        "owner_hint": {"type": "string", "enum": OWNER_HINTS},
        "blocking_info_needed": {"type": "array", "items": {"type": "string"}},
        "severity": {"type": "string", "enum": ["urgent", "normal"]},
        "needs_hubspot_reply": {"type": "boolean"},
        "claim_packet": _nullable(_CLAIM_PACKET_SCHEMA),
    },
    "required": [
        "action_type",
        "description",
        "owner_hint",
        "blocking_info_needed",
        "severity",
        "needs_hubspot_reply",
        "claim_packet",
    ],
    "additionalProperties": False,
}

# Top-level structured outputs must be an object, so the array is wrapped;
# extract_actions() unwraps it (and still tolerates the bare-array shape the
# prompt describes, for the non-structured fallback path).
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"actions": {"type": "array", "items": _ACTION_ITEM_SCHEMA}},
    "required": ["actions"],
    "additionalProperties": False,
}

_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()  # ANTHROPIC_API_KEY from env
    return _client


def _log(msg: str) -> None:
    print(f"[pipeline.llm] {msg}", file=sys.stderr)


# ===========================================================================
# PURE LAYER (unit-tested)
# ===========================================================================


def fill_template(template: str, values: dict) -> str:
    """Replace {placeholder} tokens verbatim. str.format would explode on the
    prompts' literal JSON braces, so this is a targeted replace: only the
    exact keys provided are substituted; everything else is untouched.

    Single-pass substitution: substituted values are never re-scanned, so a
    placeholder-looking string inside customer-controlled content (e.g. a
    ticket body containing the literal text "{kb_context}") cannot pull a
    later section's content into its position."""
    if not values:
        return template
    pattern = re.compile("|".join(re.escape("{" + k + "}") for k in values))
    return pattern.sub(lambda m: values[m.group(0)[1:-1]], template)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_response(text: str):
    """Parse a model response that should be ONLY JSON but may arrive fenced
    or with stray prose. Raises ValueError when nothing parseable is found."""
    candidates = [text.strip()]
    candidates += [m.strip() for m in _FENCE_RE.findall(text)]
    # widest brace/bracket span as a last resort
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])
    for cand in candidates:
        if not cand:
            continue
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    raise ValueError(f"no parseable JSON in model response ({text[:120]!r}...)")


def apply_confidence_floor(classification: dict) -> dict:
    """SKILL 2b: confidence < 0.5 forces other_unclassified. Unknown category
    values are treated the same (SKILL failure behavior: 'Unknown category
    from classifier -> force other_unclassified')."""
    out = dict(classification)
    category = out.get("category")
    try:
        confidence = float(out.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if category not in VALID_CATEGORIES or confidence < 0.5:
        if category in VALID_CATEGORIES and confidence < 0.5:
            out["floor_applied"] = f"confidence {confidence} < 0.5 (was {category})"
        else:
            out["floor_applied"] = f"unknown category {category!r}"
        out["category"] = "other_unclassified"
    out.setdefault("priority", "normal")
    return out


# ===========================================================================
# CALL LAYER
# ===========================================================================


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _call(
    model: str,
    max_tokens: int,
    prompt: str,
    output_schema: Optional[dict] = None,
    thinking_disabled: bool = False,
) -> tuple[str, dict]:
    """One messages.create; returns (text, usage-dict)."""
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if output_schema is not None:
        kwargs["output_config"] = {
            "format": {"type": "json_schema", "schema": output_schema}
        }
    if thinking_disabled:
        # claude-sonnet-5 runs adaptive thinking when `thinking` is omitted,
        # and thinking tokens count against max_tokens — disable explicitly
        # for single-shot generation so the budget is all answer.
        kwargs["thinking"] = {"type": "disabled"}
    resp = _get_client().messages.create(**kwargs)
    if resp.stop_reason == "max_tokens":
        _log(f"WARNING: {model} hit max_tokens={max_tokens} — output truncated")
    text = "".join(b.text for b in resp.content if b.type == "text")
    usage = {
        "model": model,
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }
    return text, usage


def _call_json(
    model: str,
    max_tokens: int,
    prompt: str,
    usages: list[dict],
    output_schema: Optional[dict] = None,
):
    """Call + parse, with one repair retry feeding the parse error back.
    With output_schema set the response is schema-valid JSON by construction
    and the repair path is dead code kept as a fallback."""
    text, usage = _call(model, max_tokens, prompt, output_schema=output_schema)
    usages.append(usage)
    try:
        return parse_json_response(text)
    except ValueError as first_err:
        _log(f"JSON parse failed on {model}, retrying once: {first_err}")
        repair = (
            prompt
            + "\n\nYour previous response could not be parsed as JSON ("
            + str(first_err)[:200]
            + "). Respond again with ONLY the valid JSON, no prose, no fences."
        )
        text, usage = _call(model, max_tokens, repair, output_schema=output_schema)
        usages.append(usage)
        return parse_json_response(text)


def strip_null_optionals(obj: dict, keys: tuple[str, ...]) -> dict:
    """The structured-output schemas model optional fields as nullable (the
    API requires every property in `required`); downstream contracts expect
    them OMITTED when absent — drop the null keys."""
    return {k: v for k, v in obj.items() if not (k in keys and v is None)}


def classify(ticket_context: dict, settings: dict, categories_cfg: dict, usages: list[dict]) -> dict:
    prompt = fill_template(
        _load_prompt("classify.md"),
        {
            "ticket_context": json.dumps(ticket_context, indent=2, ensure_ascii=False),
            "form_topic_mapping": json.dumps(
                categories_cfg.get("form_topic_mapping") or {}, indent=2
            ),
            "urgent_signals": json.dumps(settings.get("urgent_signals") or [], indent=2),
        },
    )
    raw = _call_json(
        CLASSIFY_MODEL, CLASSIFY_MAX_TOKENS, prompt, usages, output_schema=CLASSIFY_SCHEMA
    )
    if not isinstance(raw, dict):
        raise ValueError(f"classifier returned non-object: {type(raw).__name__}")
    raw = strip_null_optionals(raw, ("override_reason",))
    return apply_confidence_floor(raw)


def draft(ticket_context: dict, category: str, kb_context: str, usages: list[dict]) -> str:
    prompt = fill_template(
        _load_prompt("draft_reply.md"),
        {
            "ticket_context": json.dumps(ticket_context, indent=2, ensure_ascii=False),
            "category": category,
            "kb_context": kb_context,
        },
    )
    text, usage = _call(DRAFT_MODEL, DRAFT_MAX_TOKENS, prompt, thinking_disabled=True)
    usages.append(usage)
    return text.strip()


def extract_actions(
    ticket_context: dict, category: str, drafted_reply: str, usages: list[dict]
) -> list[dict]:
    prompt = fill_template(
        _load_prompt("extract_actions.md"),
        {
            "ticket_context": json.dumps(ticket_context, indent=2, ensure_ascii=False),
            "category": category,
            "drafted_reply": drafted_reply,
        },
    )
    raw = _call_json(
        EXTRACT_MODEL, EXTRACT_MAX_TOKENS, prompt, usages, output_schema=EXTRACT_SCHEMA
    )
    if isinstance(raw, dict):
        # structured-output wrapper {"actions": [...]}; a bare single-object
        # action (legacy fallback path) is tolerated as a one-element list
        raw = raw.get("actions") if "actions" in raw else [raw]
    if not isinstance(raw, list):
        raise ValueError(f"extractor returned non-array: {type(raw).__name__}")
    return [
        strip_null_optionals(a, ("claim_packet",))
        for a in raw
        if isinstance(a, dict)
    ]
