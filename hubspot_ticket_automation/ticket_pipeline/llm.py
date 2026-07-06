"""The three LLM calls — same prompt files the agent reads, filled
deterministically.

Models (pinned):
    classify -> claude-haiku-4-5   (cheap; the form-topic prior does most of
                                    the work and confidence<0.5 falls back to
                                    other_unclassified downstream)
    draft    -> claude-sonnet-4-5  (the model the agentic pipeline was
                                    validated on and stamps on drafts)
    extract  -> claude-haiku-4-5   (structured extraction against a fixed
                                    17-type taxonomy)

JSON outputs are parsed leniently (fenced or bare) with ONE repair retry that
feeds the parse error back — matching the agent's practical behavior without
changing the prompt files.
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
DRAFT_MODEL = "claude-sonnet-4-5"
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
    exact keys provided are substituted; everything else is untouched."""
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


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


def _call(model: str, max_tokens: int, prompt: str) -> tuple[str, dict]:
    """One messages.create; returns (text, usage-dict)."""
    resp = _get_client().messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    usage = {
        "model": model,
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }
    return text, usage


def _call_json(model: str, max_tokens: int, prompt: str, usages: list[dict]):
    """Call + parse, with one repair retry feeding the parse error back."""
    text, usage = _call(model, max_tokens, prompt)
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
        text, usage = _call(model, max_tokens, repair)
        usages.append(usage)
        return parse_json_response(text)


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
    raw = _call_json(CLASSIFY_MODEL, CLASSIFY_MAX_TOKENS, prompt, usages)
    if not isinstance(raw, dict):
        raise ValueError(f"classifier returned non-object: {type(raw).__name__}")
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
    text, usage = _call(DRAFT_MODEL, DRAFT_MAX_TOKENS, prompt)
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
    raw = _call_json(EXTRACT_MODEL, EXTRACT_MAX_TOKENS, prompt, usages)
    if isinstance(raw, dict):  # tolerate a single-object response
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError(f"extractor returned non-array: {type(raw).__name__}")
    return [a for a in raw if isinstance(a, dict)]
