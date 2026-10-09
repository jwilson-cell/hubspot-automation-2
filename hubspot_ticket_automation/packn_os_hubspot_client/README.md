# packn_os_hubspot_client

Pack'N OS helper module for the existing HubSpot ticket-automation. Provides:

1. **Postgres I/O** — read routine enabled state, write run records, write
   drafts (idempotent on D-05 triple), read pending rerun requests, mark
   reruns processed.
2. **Redis Lua rate-limit token acquire** — coordinates with Pack'N OS's
   shared HubSpot rate-limit budget per Phase 4 Open Q1 Shape A.

**Where this package lives.** This copy, at
`hubspot_ticket_automation/packn_os_hubspot_client/` in the cron repo
(`hubspot-automation-2`), is the live one: it is what the droplet runs. The
Pack'N OS repo tracks an older copy at `python/packn_os_hubspot_client/`, which
its cross-language contract test runs; new helper functions land here, not there.

The existing automation imports the helper and shells out to it
(`py -m packn_os_hubspot_client.rate_limit`) before each MCP HubSpot call.

## Installation on the existing-automation droplet (`packn@167.99.229.91`)

```bash
cd /opt/packn/hubspot_ticket_automation
git pull   # the helper is tracked in this repo at hubspot_ticket_automation/packn_os_hubspot_client/
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

-- 2026-05-19 Phase 3: manual full-routine run requests. Operators click
-- "Run now" in Pack'N OS → a row lands in automation_run_requests; the
-- droplet's poll_manual_runs.sh cron (every 5 min) atomically claims +
-- invokes Claude. The UPDATE columns are limited to processed-state
-- bookkeeping; the role cannot create or delete rows in this table.
GRANT SELECT, UPDATE (processed, processed_at, resulting_run_id)
  ON automation_run_requests TO packn_os_existing_automation;

-- 2026-06-06: live-role drift reconcile. The live prod role additionally
-- carries SELECT ON automation_drafts (observed in pg_class privileges);
-- documenting it here so a role recreation from this list matches the
-- live state instead of silently dropping a read the automation relies on.
GRANT SELECT ON automation_drafts TO packn_os_existing_automation;

-- 2026-06-06 Phase 20: complaint mirror (scripts/write_complaints.py live writer
-- + scripts/backfill_complaints.py one-shot). SELECT+INSERT only — the mirror is
-- append-only; soft-delete/UPDATE stay OS-side (D-01a least privilege).
GRANT SELECT, INSERT ON customer_complaints TO packn_os_existing_automation;

-- Sequence usage for SERIAL/UUID PKs
GRANT USAGE ON SCHEMA public TO packn_os_existing_automation;

-- NO access to: audit_log, tasks, claims, adjustments, or any other Pack'N OS table.
```

Connection-string template:
`postgresql://packn_os_existing_automation:REPLACE_ME@<host>:5432/<db>?sslmode=require`

### Phase 93: the cron reads OS tickets (two more grants)

Pack'N OS migration `0181_phase93_cron_reads_os_tickets.sql` issues both
statements itself, guarded on the role existing. A guarded grant in a migration
that has already been applied is not issued again, and a role created after the
apply gets nothing from it. So the statements are listed here too, as owner-run
statements, and the checks below are run on prod **before** the Phase 93 cron is
deployed and before the ticket source is ever set to Shadow.

```sql
-- 1. Read the ticket source setting (`ticket_source_mode`).
--    Why: the cron reads its source mode from this table on every run and stops
--    when it cannot (it never assumes a mode). Without this grant every run, in
--    every mode, ends as a failure record with `source_unreadable`.
--    The table holds flags and thresholds only.
GRANT SELECT ON "app_settings" TO "packn_os_existing_automation";

-- 2. Supersede a pending draft (`client.replace_pending_draft`).
--    Why: when a ticket is processed again, the earlier pending draft becomes
--    `superseded` in the same transaction that inserts the new one. The helper's
--    one statement that changes an existing draft assigns these four columns and
--    no other, so the grant is column-scoped. The role still cannot change a
--    draft's body, approve, send, reject or delete one.
GRANT UPDATE ("state", "superseded_at", "superseded_by_draft_id", "updated_at")
  ON "automation_drafts" TO "packn_os_existing_automation";
```

Checks the owner runs to confirm them (each line prints the value shown):

```sql
SELECT has_table_privilege('packn_os_existing_automation', 'public.app_settings', 'SELECT');                                  -- t
SELECT has_column_privilege('packn_os_existing_automation', 'public.automation_drafts', 'state', 'UPDATE');                   -- t
SELECT has_column_privilege('packn_os_existing_automation', 'public.automation_drafts', 'superseded_at', 'UPDATE');           -- t
SELECT has_column_privilege('packn_os_existing_automation', 'public.automation_drafts', 'superseded_by_draft_id', 'UPDATE');  -- t
SELECT has_column_privilege('packn_os_existing_automation', 'public.automation_drafts', 'updated_at', 'UPDATE');              -- t
SELECT has_column_privilege('packn_os_existing_automation', 'public.automation_drafts', 'draft_body', 'UPDATE');              -- f
SELECT has_table_privilege('packn_os_existing_automation', 'public.automation_drafts', 'UPDATE');                             -- f
```

What needs **no** new grant:

- The new run-record columns (`source_mode`, `failure_reason_code`,
  `hubspot_calls`, and the run id written as the row's `id`) and `os_ticket_id`
  on a draft: table-level `INSERT` already covers new columns. The role still
  cannot read `automation_runs`, so the helper never reads a run record back.
- The rollback rule (`client.rollback_cycle_entries`): it is one read of
  `automation_drafts`, which the role can already select.

New secret file (the name only; the value is never written down anywhere in this
repo): `config/.secrets/tickets_api_secret.txt`, read by `tickets_api.py`. It
holds the same value as the OS's `TICKETS_API_SECRET`, at least 32 characters.

### Phase 93: what the helper gained

| Function | What it does |
| --- | --- |
| `client.replace_pending_draft(*, draft_key, os_ticket_id, routine_name, draft_body, model, base_prompt_version, cycle_key, hubspot_ticket_snapshot)` | Inserts this cycle's draft and supersedes the ticket's other pending drafts, in one transaction under a per-ticket advisory lock. Returns `{"draft_id", "created", "superseded"}`. |
| `client.write_cron_draft(ticket_id, routine_name, draft_body, model, prompt_version, hubspot_ticket_snapshot)` | What the cron calls in place of `write_draft`. With no cycle for the ticket in the run context it **is** `write_draft`, statement for statement. |
| `client.rollback_cycle_entries(run_id, routine_name="tickets-process")` | The run-context entries a HubSpot-mode run needs after a flip back from `os` or `shadow`, so it replaces a cycle-keyed pending draft and does not add a second one beside it. |
| `client.write_run_record(..., *, run_id=None, source_mode=None, failure_reason_code=None, hubspot_calls=None)` | The four keyword-only values are optional. Without them the statement is the one it always sent. Against a database without the new columns it writes the old column list plus the id. |

The run context is a JSON file whose path the orchestrator exports as
`PACKN_RUN_CONTEXT` for one run (it is not a line in `.env`):

```json
{ "<draft key>": { "os_ticket_id": "<uuid or null>", "cycle_key": "<text or null>",
                   "signal_at": "<ISO or null>", "signal_id": "<uuid or null>" } }
```

The draft key is the HubSpot ticket id when the ticket has one, else the OS
ticket id as text. A draft written through the replace shows a prompt version
with a suffix (`v3.3.0#<cycle key>`); the unique key of the drafts table is
unchanged.

With no run context (the variable unset, or a file that cannot be read) every
draft is written the plain way. After a flip back to `hubspot` that means a
ticket still holding a cycle-keyed pending draft can get a second pending draft
beside it; the run context is what prevents that.

### The helper's database tests run only against the local dev Postgres

`packn_os_hubspot_client/tests/` inserts tickets, drafts and run records. A
session fixture in its `conftest.py` aborts the whole test session, before any
connection is opened, unless `PACKN_OS_DATABASE_URL` names `localhost` or
`127.0.0.1` on port `5433` (the dev Docker Postgres). The message starts with:

```text
REFUSING_NON_LOCAL_DATABASE
```

If you see that token in a terminal, a database URL other than the local dev
one is exported in that shell; unset it. The suite cannot be pointed at another
database on purpose either. The rule judges host and port only, so never bind a
tunnel to another database on local port 5433.

Both test roots (`tests/` and `packn_os_hubspot_client/tests/`) also refuse any
network connection that is not loopback (`tests/helpers/network_guard.py`); a
test that would reach HubSpot or the OS fails with `a cron test tried to open a
network connection` instead of sending a request.

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
   bash: python3 -c "from packn_os_hubspot_client import client; print(client.write_draft(ticket_id='<id>', routine_name='tickets-process', draft_body='<body>', model='claude-sonnet-5', prompt_version='v3.3.0', hubspot_ticket_snapshot=<json>))"
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
