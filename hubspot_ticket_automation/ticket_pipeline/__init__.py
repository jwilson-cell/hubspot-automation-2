"""Direct-API ticket pipeline (2026-07-06) — recommendation #3 of the
efficiency rework.

Replaces the agentic `claude -p /packn-tickets` loop with deterministic
hydration + three plain Anthropic API calls per ticket:

    classify  -> claude-haiku-4-5   (prompts/classify.md, form-topic prior)
    draft     -> claude-sonnet-4-5  (prompts/draft_reply.md + kb/ context)
    extract   -> claude-haiku-4-5   (prompts/extract_actions.md)

The prompts are the SAME files the agent reads — this package only changes
WHO fills the placeholders and WHERE the tool calls run, not what the model
is asked. Target cost is ~10-20x fewer tokens per ticket than the agent loop
(no Claude Code system prompt, no MCP schemas, no 56KB SKILL.md, no
conversation accumulation across tickets).

ROLLOUT: shadow-first. In shadow mode the pipeline runs BEFORE the agent on
the same candidate set, makes the three LLM calls, and writes comparison
artifacts to outputs/shadow/ — but performs ZERO side effects (no
automation_drafts writes, no pending_actions queue, no urgent emails, no
state.json mutation, no HubSpot property backfill, no Sheets rows). The
agent stays the system of record until the operator has compared shadow
drafts against agent drafts and flips the cutover. Gated by
`pipeline.shadow_enabled` in config/settings.yaml.
"""

__version__ = "0.1.0"
