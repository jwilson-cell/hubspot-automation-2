#!/bin/bash
# The tickets-process entry point for cron AND for Run now.
#
# Phase 93 (2026-10) — this script is now a thin shell around ONE Python entry
# point, scripts/run_tickets.py, followed by the two deterministic post steps:
#
#   1) scripts/run_tickets.py   reads the ticket source mode once (hubspot /
#                               shadow / os), runs that mode's path — the
#                               pre-gate decision, the engine-shadow step and
#                               the agent launch all live there now — and
#                               writes EXACTLY ONE automation_runs record,
#                               whatever happens to the agent (it never starts,
#                               it dies, it finds nothing, it finishes).
#   2) scripts/forward_action_items.py   always
#   3) scripts/write_complaints.py       always
#
# It exits with the orchestrator's status: 0 when a run record was written,
# non-zero when it could not be — so an orchestrator that could not even record
# its own failure shows up as a failed cron line, not as silence.
#
# Arguments are passed straight through. scripts/poll_manual_runs.sh calls this
# script with `--manual-request <request id>` when an operator presses Run now,
# so a manual run reads the mode, fails closed and writes its own record
# exactly as a scheduled one does.
#
# DO NOT RENAME THIS FILE. The crontab points here, and Pack'N OS finds that
# crontab line by this filename (CRON_MARKER = "run_tickets.sh" in
# scripts/pregate_tickets.py and scripts/run_tickets.py) to report the live
# schedule. A new name would blind the schedule self-report and orphan the
# owner's crontab.
#
# History, for why the steps are what they are:
#   2026-06-04 (Phase 15.1) the crontab points HERE instead of calling
#     `claude -p /packn-tickets` directly, so extracted action items ALWAYS
#     reach Pack'N OS /tasks even if the agentic run skips the in-loop forward
#     step. See scripts/forward_action_items.py for the root-cause writeup.
#   2026-07-06 a deterministic pre-gate and a pinned model unlocked a 30-min
#     cadence: idle ticks cost zero LLM tokens. The pre-gate decision is now
#     pregate_tickets.decide, called by the orchestrator; the model pin is
#     AGENT_MODEL in scripts/run_tickets.py (equal to DRAFT_MODEL in
#     ticket_pipeline/llm.py — never let the CLI default silently apply).
#
# Crontab on the droplet (unchanged):
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

# 1) The orchestrator. Its exit status is kept and returned at the end; it does
#    not decide whether the post steps run.
RUN_STATUS=0
.venv/bin/python scripts/run_tickets.py "$@" || RUN_STATUS=$?

# 2) Deterministic forward: drain the queue the skill just wrote and POST each
#    ticket's non-empty action_items to the OS ingestion route. Idempotent
#    (forward-once seen-set + OS-side dedup) and non-blocking (always exits 0),
#    so it can never fail the cron. Runs whatever the orchestrator returned
#    (and on ticks with no agent launch it simply drains any leftovers).
.venv/bin/python scripts/forward_action_items.py

# 3) Deterministic complaint mirror: re-scan outputs/kpi/mispack_log.csv and
#    INSERT customer_complaints rows (ON CONFLICT DO NOTHING — Phase 20 D-02/D-04).
#    Non-blocking (always exits 0) — can never fail the cron.
.venv/bin/python scripts/write_complaints.py

exit "$RUN_STATUS"
