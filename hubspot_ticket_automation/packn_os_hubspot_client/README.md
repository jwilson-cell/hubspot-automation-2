# packn_os_hubspot_client

Pack'N OS helper module for the existing HubSpot ticket-automation. Provides:

1. **Postgres I/O** — read routine enabled state, write run records, write
   drafts (idempotent on D-05 triple), read pending rerun requests, mark
   reruns processed.
2. **Redis Lua rate-limit token acquire** — coordinates with Pack'N OS's
   shared HubSpot rate-limit budget per Phase 4 Open Q1 Shape A.

The module is owned by Pack'N OS (this repo) and consumed by the existing
HubSpot ticket-automation via a one-PR change to its `SKILL.md` cron skill.
The existing automation imports the helper and shells out to it
(`py -m packn_os_hubspot_client.rate_limit`) before each MCP HubSpot call.

## Installation on the existing-automation droplet (`packn@167.99.229.91`)

```bash
cd /opt/packn/hubspot_ticket_automation
git pull   # one-PR change brings in python/packn_os_hubspot_client/ + SKILL.md updates
pip install psycopg[binary,pool]>=3.2 redis>=5
# Or: add the same lines to scripts/requirements.txt and `pip install -r scripts/requirements.txt`
```

## Required env vars (set in `/opt/packn/hubspot_ticket_automation/.env` mode 0600)

| Var | Description |
| --- | --- |
| `PACKN_OS_DATABASE_URL` | Postgres URL pointing at Pack'N OS DB; must use the role created below |
| `REDIS_URL` | Same Redis instance Pack'N OS uses (shared rate-limit + queue infrastructure) |
| `HUBSPOT_RATE_LIMIT_AUTOMATION` | Token bucket capacity (default `3`; sum with `HUBSPOT_RATE_LIMIT_OS` MUST be ≤ 5) |

## Postgres role + grants (run ONCE pre-cutover by lconner)

```sql
-- Create role with explicit password (replace placeholder)
CREATE ROLE packn_os_existing_automation WITH LOGIN PASSWORD 'REPLACE_ME';

-- Read-only on routines + rerun requests
GRANT SELECT ON automation_routines TO packn_os_existing_automation;
GRANT SELECT ON automation_rerun_requests TO packn_os_existing_automation;

-- Insert on runs + drafts (no UPDATE/DELETE — append-only from this side)
GRANT INSERT ON automation_runs TO packn_os_existing_automation;
GRANT INSERT ON automation_drafts TO packn_os_existing_automation;

-- Update on rerun_requests, only the processed/processed_at/resulting_draft_id columns
GRANT UPDATE (processed, processed_at, resulting_draft_id)
  ON automation_rerun_requests TO packn_os_existing_automation;

-- 2026-05-19: schedule self-reporting (client.report_routine_schedule).
-- Helper reads `crontab -l` on the droplet and writes the live schedule
-- back so the Pack'N OS UI never shows the stale seed value.
GRANT UPDATE (cron_schedule, last_schedule_report_at)
  ON automation_routines TO packn_os_existing_automation;

-- Sequence usage for SERIAL/UUID PKs
GRANT USAGE ON SCHEMA public TO packn_os_existing_automation;

-- NO access to: audit_log, tasks, claims, adjustments, or any other Pack'N OS table.
```

Connection-string template:
`postgresql://packn_os_existing_automation:REPLACE_ME@<host>:5432/<db>?sslmode=require`

## Smoke tests (post-install)

```bash
# Routine enabled check (must print True after Plan 04-02 seed runs)
python -c "from packn_os_hubspot_client import client; print(client.read_routine_enabled('tickets-process'))"

# Rate-limit token acquire smoke (against the dev/prod docker Redis)
python -m packn_os_hubspot_client.rate_limit
echo $?   # expect 0
```

## SKILL.md integration (the one-PR change in `.claude/skills/hubspot-tickets/SKILL.md`)

Four insertion points (Wave 4 plan codifies the full diff):

1. **Tick start — check routine enabled (CONTEXT D-01).** Before any HubSpot
   MCP call:

   ```text
   First, run:
     python3 -c "from packn_os_hubspot_client import client; import sys; sys.exit(0 if client.read_routine_enabled('tickets-process') else 99)"
   If exit code is 99 (paused), write a status='skipped' run record:
     python3 -c "from packn_os_hubspot_client import client; client.write_run_record('tickets-process', 'skipped', None, 0, 0, '<ISO>', '<ISO>')"
   then exit the skill.
   ```

2. **Before each MCP HubSpot call — acquire a token (Pitfall 1).**

   ```text
   bash: python3 -m packn_os_hubspot_client.rate_limit
   ```

3. **Replace auto-send with `write_draft` (D-05).**

   ```text
   bash: python3 -c "from packn_os_hubspot_client import client; print(client.write_draft(ticket_id='<id>', routine_name='tickets-process', draft_body='<body>', model='claude-sonnet-4-5', prompt_version='v3.2.1', hubspot_ticket_snapshot=<json>))"
   ```

4. **Tick finish — write the run record (D-03).**

   ```text
   bash: python3 -c "from packn_os_hubspot_client import client; client.write_run_record('tickets-process', 'success', None, <count>, <drafts>, '<start_iso>', '<finish_iso>')"
   ```

## Pre-cutover verification (lconner UAT — Wave 4 plan)

See `.planning/phases/04-hubspot-automation-control-panel/04-HUMAN-UAT.md`
for the full cutover checklist.

## Module layout

| File | Purpose |
| --- | --- |
| `__init__.py` | Public surface; exports `rate_limit` (lazy-imports `client`) |
| `tenant.py` | `TENANT_ID = 'packn'` constant (mirrors `src/lib/tenant.ts`) |
| `db.py` | psycopg connection pool (`min_size=1, max_size=2`) |
| `client.py` | 5 helper functions (read/write the automation_* tables) |
| `rate_limit.py` | Redis Lua rate-limit token acquire (parity with `src/lib/rate-limit.ts`) |

## Pitfall reference

| Pitfall | Mitigation |
| --- | --- |
| 1 (Lua script drift) | Cross-language CI test asserts byte-equality of the Lua body between markers (`tests/unit/automation/lua-parity.test.ts` + `tests/test_lua_parity.py`) |
| 3 (HubSpot 5/s account cap) | Env-time guard in `src/schemas/env.ts` enforces `HUBSPOT_RATE_LIMIT_OS + HUBSPOT_RATE_LIMIT_AUTOMATION <= 5` |
| 4 (psycopg connection leak) | Every DB op uses `with pool.connection() as conn:` context manager |
