#!/bin/bash
# Phase 3 (2026-05-19) — manual-run poll wrapper.
#
# Operator clicks "Run now" in Pack'N OS → an automation_run_requests row
# lands in Postgres. This script runs on a tight cron (~every 5 min),
# atomically claims the oldest pending row, and starts the matching routine.
#
# Phase 93 (2026-10): a `tickets-process` request no longer launches the agent
# from here. It goes through the same door as a scheduled tick —
# scripts/run_tickets.sh → scripts/run_tickets.py — so a pressed Run now reads
# the ticket source mode, fails closed on a setting it cannot read, forwards
# action items afterwards, and writes its own run record whatever happens to
# the agent. (Before, Run now bypassed all of that.) The `digest` routine is
# still launched directly.
#
# Designed to be cheap when there's no work: the claim helper exits ~50ms
# when the queue is empty (one indexed query, no Claude invocation), so
# running every 5 min is essentially free.
#
# Required crontab entry on the droplet (operator adds by hand once):
#
#   */5 * * * * cd /opt/packn/hubspot_ticket_automation && scripts/poll_manual_runs.sh >> outputs/runs/manual-runs.log 2>&1
#
# Required DB grants on packn_os_existing_automation:
#   SELECT, UPDATE (processed, processed_at, resulting_run_id)
#     ON public.automation_run_requests
#
# Race-safe: claim_pending_manual_run uses FOR UPDATE SKIP LOCKED so this
# wrapper, the regular 12h cron, and any overlapping 5-min poll cannot
# double-claim the same row.
#
# Linking the run back to the request: for `tickets-process` the orchestrator
# does it. scripts/run_tickets.py generates the run's id itself, writes the
# automation_runs row under that id, and then calls
# client.mark_manual_run_completed(<request id>, <run id>) — no SELECT on
# automation_runs is needed, because the id is never read back. When the
# record could not be written the request is linked to no run. A `digest`
# request is still correlated by timestamp in the routine detail page's
# "Recent runs" list.

set -u

cd "$(dirname "$0")/.."

# While a tickets run holds its lock (scripts/run_tickets.sh, REV-OPEN-6), claim
# nothing: the request stays pending and the first poll after that run ends
# picks it up, instead of claiming it into a run that would be skipped. This
# also holds back a `digest` request for the length of a tickets run. Only
# flock's conflict code (75) waits; any other flock error goes on as before.
RUN_LOCK="outputs/runs/.run_tickets.lock"
mkdir -p outputs/runs
flock -n -E 75 "$RUN_LOCK" true; LOCK_STATUS=$?
if [ "$LOCK_STATUS" -eq 75 ]; then
    exit 0
fi

# Atomically claim one pending request. Prints a JSON-ish line to stdout
# on success ({routine: "...", request_id: "..."}) or nothing if the queue
# is empty.
CLAIMED=$(py -c "
import json
from packn_os_hubspot_client import client
row = client.claim_pending_manual_run()
if row:
    print(json.dumps({
        'routine': row['routine_name'],
        'request_id': str(row['id']),
        'requested_by': row['requested_by'],
    }))
")

if [ -z "$CLAIMED" ]; then
    # Empty queue — exit silently so the log doesn't fill up. The Pack'N OS
    # UI shows "Pending pickup" until a row gets claimed, so absence of
    # log lines here is itself the signal "the queue is being polled and
    # is empty."
    exit 0
fi

ROUTINE=$(echo "$CLAIMED" | py -c "import sys,json; print(json.load(sys.stdin)['routine'])")
REQUEST_ID=$(echo "$CLAIMED" | py -c "import sys,json; print(json.load(sys.stdin)['request_id'])")
REQUESTED_BY=$(echo "$CLAIMED" | py -c "import sys,json; print(json.load(sys.stdin)['requested_by'])")

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] manual-run poll: claimed request $REQUEST_ID for routine=$ROUTINE (requested_by=$REQUESTED_BY)"

case "$ROUTINE" in
    tickets-process)
        # The same entry point as the scheduled tick, with the claimed request
        # id. The agent, its model pin (AGENT_MODEL in scripts/run_tickets.py)
        # and the run record all live behind it, so a manual run and a cron
        # run cannot drift apart again (they once drafted with different
        # models). The wrapper's exit status is this script's.
        exec bash scripts/run_tickets.sh --manual-request "$REQUEST_ID"
        ;;
    digest)
        # Pinned 2026-07-23 — an unpinned invocation runs the CLI default,
        # which can silently be Opus-tier pricing.
        exec claude -p /packn-digest --model claude-sonnet-5 --dangerously-skip-permissions
        ;;
    *)
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] manual-run poll: ERROR unknown routine '$ROUTINE' — request $REQUEST_ID was claimed but no dispatch path; row stays processed=true so it won't re-fire" >&2
        exit 1
        ;;
esac
