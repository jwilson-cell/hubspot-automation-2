"""Queue operator rerun requests for one or more tickets.

Inserts rows into Pack'N OS `automation_rerun_requests` — the same queue the
Pack'N OS UI writes to (CONTEXT D-09). The next `tickets-process` tick picks
them up BEFORE the normal HubSpot poll and re-drafts each ticket against a
fresh snapshot, even while the routine is paused.

Usage (droplet):
    .venv/bin/python scripts/request_rerun.py 47088863618 47091187006

Requires PACKN_OS_DATABASE_URL (source /etc/environment under a bare shell).
Requests expire after 24h if no tick processes them.
"""
from __future__ import annotations

import sys
import uuid

from packn_os_hubspot_client.db import get_pool
from packn_os_hubspot_client.tenant import TENANT_ID

ROUTINE = "tickets-process"
REQUESTED_BY = "cli:request_rerun"


def main(argv: list[str]) -> int:
    tickets = [t.strip() for t in argv if t.strip()]
    if not tickets or any(not t.isdigit() for t in tickets):
        print("usage: request_rerun.py <ticket_id> [<ticket_id> ...]  (numeric HubSpot ids)", file=sys.stderr)
        return 2
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            for t in tickets:
                cur.execute(
                    """
                    INSERT INTO automation_rerun_requests
                      (id, tenant_id, routine_name, ticket_id, requested_by,
                       requested_at, processed, expires_at)
                    VALUES (%s, %s, %s, %s, %s, NOW(), false, NOW() + interval '24 hours')
                    """,
                    (str(uuid.uuid4()), TENANT_ID, ROUTINE, t, REQUESTED_BY),
                )
    print(f"queued {len(tickets)} rerun request(s): {', '.join(tickets)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
