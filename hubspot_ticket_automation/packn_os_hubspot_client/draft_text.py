"""A drafted reply is the email and nothing else (owner, 2026-10-16: "I just
want it to create the draft").

clean_draft_body strips what a model adds around and inside the email:
markdown (bold/italic asterisks, headings, code fences, rules), a "Here's a
draft" preamble, a subject line, [N] citation markers with a closing
"Sources:" block, and the legacy "DRAFTED WITHOUT SOP SUPPORT" line. The
email's own words stay as written. Idempotent.

Mirror of Pack'N OS src/lib/tasks/clean-draft-body.ts — keep the two in step.
Applied where every draft is stored (client.write_draft and
client.replace_pending_draft) and where the engine drafts (llm.draft), so a
draft is clean whichever path wrote it.
"""
from __future__ import annotations

import re

_NO_SOP_LINE = re.compile(r"^\s*DRAFTED WITHOUT SOP SUPPORT\s*\n*")
# From a line that is just "Sources:" to the end.
_SOURCES_BLOCK = re.compile(r"\n[ \t]*\**Sources:?\**[ \t]*(\n[\s\S]*)?$")
_CITATION_MARKER = re.compile(r"[ \t]*\[(?:\d+(?:\s*,\s*\d+)*|citation removed)\]")
# Only a short first line that names the draft AND ends in a colon: an email
# that opens "Here's an update on your order." keeps its first line.
_PREAMBLE_LINE = re.compile(
    r"^\s*(?:(?:sure|okay|ok)[,!.]?\s*)?(?:here(?:'|’)?s|here is|below is)\b"
    r"[^\n]{0,80}?\b(?:draft|reply|response|email)\b[^\n]{0,40}:[ \t]*\n+",
    re.IGNORECASE,
)
_SUBJECT_LINE = re.compile(r"^\s*\**subject:\**[^\n]*\n+", re.IGNORECASE)
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def _link(match: re.Match) -> str:
    label, url = match.group(1), match.group(2)
    return url if label.strip() == url else f"{label} ({url})"


def clean_draft_body(text: str) -> str:
    out = text.replace("\r\n", "\n")

    out = _NO_SOP_LINE.sub("", out, count=1)
    out = _SOURCES_BLOCK.sub("", out, count=1)
    out = _CITATION_MARKER.sub("", out)

    # Code fences and horizontal rules go as whole lines.
    out = re.sub(r"(?m)^[ \t]*```[^\n]*\n?", "", out)
    out = re.sub(r"(?m)^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$", "", out)

    # Wrapper lines a model adds around the email.
    out = _PREAMBLE_LINE.sub("", out, count=1)
    out = _SUBJECT_LINE.sub("", out, count=1)

    out = _LINK.sub(_link, out)

    # Headings keep their words; "* item" / "• item" bullets become "- item".
    out = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]+", "", out)
    out = re.sub(r"(?m)^([ \t]*)[*•][ \t]+", r"\1- ", out)
    # Bold and italics keep their words; any asterisk left over goes.
    out = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", out)
    out = re.sub(r"__([^_\n]+)__", r"\1", out)
    out = re.sub(r"\*([^*\n]+)\*", r"\1", out)
    out = out.replace("*", "")
    # Inline code keeps its text.
    out = re.sub(r"`([^`\n]*)`", r"\1", out)

    # A removed marker can leave " ." or " ,".
    out = re.sub(r"[ \t]+([.,;:!?])", r"\1", out)
    out = re.sub(r"(?m)[ \t]+$", "", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()
