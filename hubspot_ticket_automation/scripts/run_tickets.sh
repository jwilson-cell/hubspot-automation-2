#!/bin/bash
# Phase 15.1 (2026-06-04) — ticket pass + deterministic action-item forward.
# 2026-07-06 — deterministic pre-gate + pinned model, unlocking a 30-min cadence:
# the expensive agentic run only launches when the pre-gate finds candidate
# tickets (or pending operator reruns); idle ticks cost zero LLM tokens.
#
# The crontab points HERE instead of calling `claude -p /packn-tickets`
# directly, so extracted action items ALWAYS reach Pack'N OS /tasks even if the
# agentic run skips the in-loop forward step. Diagnosed 2026-06-04: the agent
# was extracting action items and narrating "posted to /tasks" while never
# actually POSTing — 0 tasks landed for the whole cutover window. See
# scripts/forward_action_items.py for the full root-cause writeup.
#
# Crontab on the droplet (30-min cadence is now safe — idle ticks skip the LLM):
#   */30 * * * * cd /opt/packn/hubspot_ticket_automation && bash scripts/run_tickets.sh >> outputs/runs/cron-tickets.log 2>&1
#
# (Calling via `bash scripts/run_tickets.sh` avoids depending on the +x bit.)
set -u
cd "$(dirname "$0")/.."

# Cron daemons do NOT reliably inherit /etc/environment — source it explicitly
# so PACKN_OS_DATABASE_URL resolves under cron's minimal env (Phase 20: without
# this, the complaint writer would log-and-exit-0 forever under D-04's
# never-fail posture — a silent total outage).
if [ -r /etc/environment ]; then set -a; . /etc/environment; set +a; fi

# 0) Deterministic pre-gate: routine gate + rerun-queue poll + HubSpot search
#    + fingerprint dedupe WITHOUT launching Claude. Exit codes:
#      0  = work exists (or pre-gate unsure — fail-open) -> run the agent
#      10 = no new tickets (success/0 run record written) -> skip the agent
#      11 = routine paused (skipped run record written)   -> skip the agent
PREGATE_EXIT=0
.venv/bin/python scripts/pregate_tickets.py || PREGATE_EXIT=$?

if [ "$PREGATE_EXIT" -eq 10 ] || [ "$PREGATE_EXIT" -eq 11 ]; then
  echo "[run_tickets] pregate exit ${PREGATE_EXIT} — skipping agentic pass this tick"
else
  # 0.5) Shadow pipeline (efficiency rework #3): direct-API classify/draft/
  #      extract over the SAME candidates the agent is about to process.
  #      Writes comparison artifacts to outputs/shadow/ only — zero side
  #      effects; gated by settings pipeline.shadow_enabled; always exits 0.
  #      Runs BEFORE the agent so both see identical state.json.
  .venv/bin/python scripts/run_pipeline.py

  # 1) Agentic ticket pass: classify / draft / queue each ticket's action_items
  #    to config/pending_actions.json (SKILL step 2g). Permissions are skipped so
  #    the headless run never blocks on a prompt. Model is PINNED (and must match
  #    the write_draft stamp in SKILL.md, the pin in poll_manual_runs.sh, and
  #    DRAFT_MODEL in ticket_pipeline/llm.py) — never let the CLI default
  #    silently upgrade us to Opus-tier pricing.
  #    2026-07-23: claude-sonnet-4-5 -> claude-sonnet-5 (Claude 5 refresh;
  #    introductory $2/$10 per MTok through 2026-08-31, then $3/$15).
  claude -p /packn-tickets --model claude-sonnet-5 --dangerously-skip-permissions
fi

# 2) Deterministic forward: drain the queue the skill just wrote and POST each
#    ticket's non-empty action_items to the OS ingestion route. Idempotent
#    (forward-once seen-set + OS-side dedup) and non-blocking (always exits 0),
#    so it can never fail the cron. Runs regardless of the agentic pass's exit
#    (and on skipped ticks it simply drains any leftovers from prior runs).
.venv/bin/python scripts/forward_action_items.py

# 3) Deterministic complaint mirror: re-scan outputs/kpi/mispack_log.csv and
#    INSERT customer_complaints rows (ON CONFLICT DO NOTHING — Phase 20 D-02/D-04).
#    Non-blocking (always exits 0) — can never fail the cron.
.venv/bin/python scripts/write_complaints.py
