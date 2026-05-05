"""Pack'N OS HubSpot helper client.

Public surface (called by the existing automation's SKILL.md):
    from packn_os_hubspot_client import rate_limit, client
    rate_limit.acquire_hubspot_token()
    client.read_routine_enabled('tickets-process')
    client.write_draft(...)

`client` imports psycopg via db.py; rate-limit-only invocations should use
`python -m packn_os_hubspot_client.rate_limit` directly to avoid the psycopg
import cost (the Plan 04-03 plan brief calls this out as the SKILL.md
shell-out path).
"""

from . import client  # noqa: F401
from . import rate_limit  # noqa: F401
