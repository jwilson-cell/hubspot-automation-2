#!/bin/bash
# Phase 3 (2026-05-19) — manual-run poll wrapper.
#
# Operator clicks "Run now" in Pack'N OS → an automation_run_requests row
# lands in Postgres. This script runs on a tight cron (~every 5 min),
# atomically claims the oldest pending row, and invokes the matching
# Claude routine.
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
# Linking the resulting automation_runs row back to the request via
# resulting_run_id is a v2 enhancement (would require GRANT SELECT on
# automation_runs which the role doesn't have today). For now, operators
# correlate by timestamp in the routine detail page's "Recent runs" list.

set -u

cd "$(dirname "$0")/.."

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
        exec claude -p /packn-tickets --model claude-sonnet-4-6 --dangerously-skip-permissions
        ;;
    digest)
        exec claude -p /packn-digest --dangerously-skip-permissions
        ;;
    *)
        echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] manual-run poll: ERROR unknown routine '$ROUTINE' — request $REQUEST_ID was claimed but no dispatch path; row stays processed=true so it won't re-fire" >&2
        exit 1
        ;;
esac
