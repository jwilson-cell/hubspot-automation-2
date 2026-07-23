# Cost & Accuracy Audit — 2026-07-23 (Claude 5 refresh)

Full-repo audit triggered by the Claude 5 family release. Scope: every LLM
touchpoint (model pins, stamps, prompts, the direct-API shadow pipeline) plus
a correctness sweep of the deterministic helper scripts. Everything below is
implemented in this commit series unless marked **operator action** or
**deferred**.

## Current pricing context (USD per 1M tokens, in/out)

| Model | Price | Notes |
|---|---|---|
| claude-haiku-4-5 | $1 / $5 | cheapest tier; unchanged — no newer cheap model exists |
| claude-sonnet-5 | **$2 / $10 intro through 2026-08-31**, then $3 / $15 | new tokenizer: ~30% more tokens for the same text vs 4.x Sonnets |
| claude-sonnet-4-5 | $3 / $15 | previous draft/agent pin (legacy model) |
| claude-opus-4-8 | $5 / $25 | what an *unpinned* `claude -p` run can silently cost |
| claude-fable-5 | $10 / $50 | evaluated; no stage in this pipeline warrants it |

Net effect of Sonnet 4.5 → Sonnet 5: **cheaper until 2026-08-31** (≈ −10-15%
after the tokenizer offset), roughly **+30% after** — with a clear accuracy
gain (near-Opus quality on agentic work, which is exactly what the ticket
skill is). Revisit the pin at the end of August if cost dominates.

## Findings and fixes

### Cost

1. **Digest agent ran UNPINNED** (`claude -p /packn-digest` with no
   `--model` in the droplet crontab per docs, and in
   `scripts/poll_manual_runs.sh`) — the CLI default can silently be
   Opus-tier pricing, the exact failure mode `run_tickets.sh` warns about.
   → Pinned `--model claude-sonnet-5` in `poll_manual_runs.sh`, README, and
   `docs/server-setup.md`. **Operator action:** the droplet crontab's three
   digest lines need the same `--model claude-sonnet-5` added by hand
   (`ssh packn@167.99.229.91 crontab -e`).
2. **Model drift between run paths**: cron pinned `claude-sonnet-4-5`,
   manual "Run now" runs pinned `claude-sonnet-4-6`. A manual re-draft was
   produced by a different model than the cron draft it replaced.
   → All four sites unified on `claude-sonnet-5`: `run_tickets.sh`,
   `poll_manual_runs.sh` (both routines), `ticket_pipeline/llm.py`
   `DRAFT_MODEL`, and the `write_draft` stamps in SKILL.md + test helpers.
   `prompt_version` stays `v3.3.0` — prompts/*.md are unchanged, so pending
   tickets keep their existing drafts (no queue churn).
3. **Shadow pipeline double-spend** (`pipeline.shadow_enabled: true` since
   2026-07-06): every tick with work runs classify/draft/extract twice —
   once in the agent, once direct-API. Deliberately **left enabled**: the
   direct calls cost ~$0.02–0.03/ticket (now visible, see #4) while the
   agentic pass costs an order of magnitude more per ticket — the shadow
   data is what justifies the eventual cutover, which is the single biggest
   cost lever in this repo (**deferred: operator decision**). What the
   cutover still needs: porting the write side (write_draft, fingerprints,
   run records, sheets rows, action-item queue, urgent emails, 2a.6
   backfill) into `ticket_pipeline/`.
4. **No cost observability**: shadow summaries logged tokens but not spend.
   → `ticket_pipeline/run.py` now computes `cost_usd` per ticket and per
   run (pricing table inline) in `_summary.json` and the cron log line.
5. **Stale runbook docs**: README + server-setup still showed the crontab
   calling `claude -p /packn-tickets` directly (no wrapper, no pin) —
   anyone re-provisioning from the docs would have rebuilt the unpinned,
   pre-gate-less world. → Docs now match production reality.

### Accuracy

6. **No schema enforcement on classify/extract** (shadow pipeline): invalid
   `action_type` values are downgraded to `other` on OS ingestion and lose
   routing; invalid categories relied on the confidence-floor fallback.
   → `ticket_pipeline/llm.py` now uses **structured outputs**
   (`output_config.format` json_schema) with enums for `category` (15),
   `action_type` (17), `owner_hint` (7), `severity`, `carrier_confidence`,
   `claim_type`, and the full `claim_packet` sub-schema — responses are
   schema-valid by construction. The lenient parse + repair retry is kept
   as fallback. Optional fields (`override_reason`, `claim_packet`) are
   modeled nullable and stripped when null to preserve the "omit when
   absent" downstream contract.
7. **Sonnet 5 request-shape hazard**: omitting `thinking` on Sonnet 5 runs
   adaptive thinking by default, and thinking tokens count against
   `max_tokens` (1200 for drafts — truncation risk). → The draft call sets
   `thinking: {"type": "disabled"}` explicitly (sanctioned for single-shot
   well-specified generation). Also added a `stop_reason == "max_tokens"`
   truncation warning to every call.
8. **`fill_template` sequential-replace injection**: a customer body
   containing the literal text `{kb_context}` would have had the KB section
   substituted *into the customer's message position* on the next
   replacement pass. → Single-pass regex substitution; substituted values
   are never re-scanned. Unit-tested.

### Deterministic-script sweep (parallel audit agent, findings verified)

9. **`pregate_tickets.py` saturated-page deadlock**: with a full result
   page (≥ per_run_cap) where every row dedupes out, pregate skipped the
   agent AND never advanced `last_run_at` — newer tickets beyond the page
   could be hidden **permanently**, not just one tick. → Fail-open: a
   saturated fully-deduped page now launches the agent.
10. **`ssk_order_lookup.py` had no HTTP timeout**: a stalled SSK socket
    would hang the hydration step (the agent path calls it via the same
    helper). → `timeout=30`, matching `hubspot_api.py`.
11. **`ssk_order_lookup.py` crash instead of refuse**: a merchant entry
    with `name`/`match` but no `token_path` raised KeyError (exit 1)
    instead of the documented exit-3 refusal. → Refuses with exit 3.
12. **`write_complaints.py` false stall alarm**: the
    `COMPLAINT_MIRROR_STALL` marker fired when all CSV rows were
    caller-side-skipped (nothing to land ≠ failed to land). → Alarm now
    requires parseable rows to have existed.
13. Clean bills: `forward_action_items.py`, `sheets_sync.py` (operator-
    column invariant holds: append-only + ticket_id dedupe),
    `hubspot_api.py` (candidate-selection truth correctly shared).

### Evaluated, not adopted

- **Fable 5 / Opus anywhere in the pipeline** — no stage's difficulty
  justifies 5–10× Sonnet pricing; classify/extract are priors-driven,
  drafting is KB-grounded template work.
- **Prompt caching in the shadow pipeline** — requires reordering the
  prompt files (static instructions before variable context) which are
  shared with the validated agent path; modest savings (~$0.10/full run)
  at meaningful revalidation risk. Revisit at cutover, when the prompt
  files can be restructured for the direct pipeline alone.
- **Batch API** — 50% off but up-to-1h latency; incompatible with the
  30-minute reply-time posture.
- **Trimming classify/extract context** (each call re-sends the full
  ticket_context) — would diverge shadow inputs from agent inputs and
  invalidate the parity comparison; fold into the cutover design instead.

## Verification

- `python -m pytest tests/` — **25 passed** (10 pre-existing + 15 new in
  `tests/test_llm_pure.py` covering fill_template injection, JSON parsing,
  confidence floor, schema integrity, null-stripping).
- Shell syntax checked (`bash -n`) on both wrappers; `ast.parse` on all
  edited Python.
- **Operator action (droplet, after pull):** one-ticket shadow smoke to
  confirm the API accepts the structured-output schemas:
  `.venv/bin/python scripts/run_pipeline.py --limit 1` then inspect the
  newest `outputs/shadow/*/_summary.json` (expect `status: ok` rows and a
  `cost_usd` total). The agentic pass is unaffected either way (shadow
  always exits 0).

## Deploy checklist

1. `git push` (droplet pulls `main` on every cron start — push = deploy).
2. Edit the droplet crontab: add `--model claude-sonnet-5` to the three
   `/packn-digest` lines (tickets line already goes through the wrapper).
3. Next tick: check `outputs/runs/cron-tickets.log` for the shadow
   `~$<cost>` line and a normal agent pass; spot-check one new draft in
   Pack'N OS (stamped `claude-sonnet-5`).
4. ~2026-08-31: revisit the Sonnet 5 pin when intro pricing ends
   (post-intro it costs ~+30% vs a 4.x Sonnet for the same text).
