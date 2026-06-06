#!/bin/bash
# Phase 15.1 (2026-06-04) — ticket pass + deterministic action-item forward.
#
# The crontab points HERE instead of calling `claude -p /packn-tickets`
# directly, so extracted action items ALWAYS reach Pack'N OS /tasks even if the
# agentic run skips the in-loop forward step. Diagnosed 2026-06-04: the agent
# was extracting action items and narrating "posted to /tasks" while never
# actually POSTing — 0 tasks landed for the whole cutover window. See
# scripts/forward_action_items.py for the full root-cause writeup.
#
# Crontab on the droplet (REPLACES the prior /packn-tickets line):
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

# 1) Agentic ticket pass: classify / draft / queue each ticket's action_items
#    to config/pending_actions.json (SKILL step 2g). Permissions are skipped so
#    the headless run never blocks on a prompt.
claude -p /packn-tickets --dangerously-skip-permissions

# 2) Deterministic forward: drain the queue the skill just wrote and POST each
#    ticket's non-empty action_items to the OS ingestion route. Idempotent
#    (forward-once seen-set + OS-side dedup) and non-blocking (always exits 0),
#    so it can never fail the cron. Runs regardless of the agentic pass's exit.
.venv/bin/python scripts/forward_action_items.py

# 3) Deterministic complaint mirror: re-scan outputs/kpi/mispack_log.csv and
#    INSERT customer_complaints rows (ON CONFLICT DO NOTHING — Phase 20 D-02/D-04).
#    Non-blocking (always exits 0) — can never fail the cron.
.venv/bin/python scripts/write_complaints.py
