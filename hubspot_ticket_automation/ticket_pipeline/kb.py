"""KB retrieval — SKILL.md step 2c, deterministic.

Always include settings.kb_always_include (brand_voice + glossary), plus the
category's kb_file from config/categories.yaml. Total capped (~4000 tokens
per the SKILL; approximated as chars since the exact tokenizer doesn't
matter for a cap whose purpose is 'don't dump the whole kb/ dir')."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ~4 chars/token heuristic on prose -> 4000 tokens ~ 16000 chars.
KB_CHAR_CAP = 16000

# The reply rulebook (2026-10-15): kb/rulebook/_global.md plus one file per
# category, GENERATED from Pack'N OS (src/lib/tickets/reply-rulebook.ts via
# scripts/export-reply-rulebook.ts there) -- never edit them here. It leads the
# KB context and sits OUTSIDE KB_CHAR_CAP: the cap may trim the KB docs, never
# the rules.
RULEBOOK_DIR = ROOT / "kb" / "rulebook"


def _log(msg: str) -> None:
    print(f"[pipeline.kb] {msg}", file=sys.stderr)


def kb_files_for_category(category: str, settings: dict, categories_cfg: dict) -> list[str]:
    """Ordered, deduped file list: always-include first (earlier = higher
    priority per settings.yaml comment), then the category's kb_file."""
    files = list(settings.get("kb_always_include") or [])
    cat_entry = categories_cfg.get(category) or {}
    kb_file = cat_entry.get("kb_file")
    if kb_file and kb_file not in files:
        files.append(kb_file)
    return files


def _read_rulebook_file(name: str) -> str:
    try:
        text = (RULEBOOK_DIR / name).read_text(encoding="utf-8")
    except OSError:
        return ""
    # Drop the GENERATED header comment: provenance for humans, noise for the model.
    return "\n".join(
        line for line in text.splitlines() if not line.startswith("<!-- GENERATED")
    ).strip()


_SLUG_RE = re.compile(r"[a-z_]+")


def load_rulebook(category: str) -> str:
    """The global rules plus the category's section, or "" when the folder is
    missing. An unknown category gets the global rules alone."""
    parts = [_read_rulebook_file("_global.md")]
    # A slug only: the name becomes a file path.
    if category and _SLUG_RE.fullmatch(category):
        parts.append(_read_rulebook_file(f"{category}.md"))
    body = "\n\n".join(p for p in parts if p)
    if not body:
        _log("rulebook missing; drafting without it")
    return body


def load_kb_context(category: str, settings: dict, categories_cfg: dict) -> str:
    """The rulebook for the category, then the KB docs (capped)."""
    rulebook = load_rulebook(category)
    docs = _load_kb_docs(category, settings, categories_cfg)
    if not rulebook:
        return docs
    return (
        f"----- RULEBOOK for {category} (Pack'N policy: follow it; it outranks the KB docs "
        f"below and the category hints where they disagree) -----\n{rulebook}\n\n{docs}"
    )


def _load_kb_docs(category: str, settings: dict, categories_cfg: dict) -> str:
    """Concatenate the KB files with per-file headers, capped at KB_CHAR_CAP.
    The category file is guaranteed a slot: if always-include alone would
    blow the cap, they are truncated to preserve room for the category file
    (SKILL 2c: 'prefer the category-specific file over broad matches')."""
    files = kb_files_for_category(category, settings, categories_cfg)
    sections: list[tuple[str, str]] = []
    for rel in files:
        path = ROOT / rel
        try:
            sections.append((rel, path.read_text(encoding="utf-8")))
        except OSError:
            _log(f"kb file unreadable, skipped: {rel}")

    if not sections:
        return "(no KB context available)"

    # Reserve at least 40% of the cap for the LAST section (the category
    # file) when there is more than one section.
    reserved = int(KB_CHAR_CAP * 0.4) if len(sections) > 1 else 0
    budget_head = KB_CHAR_CAP - reserved
    out_parts: list[str] = []
    used = 0
    for i, (rel, body) in enumerate(sections):
        remaining = (KB_CHAR_CAP - used) if i == len(sections) - 1 else (budget_head - used)
        if remaining <= 0:
            break
        chunk = body[:remaining]
        if len(chunk) < len(body):
            chunk += "\n[... truncated for context budget ...]"
        out_parts.append(f"----- {rel} -----\n{chunk}")
        used += len(chunk)
    return "\n\n".join(out_parts)
