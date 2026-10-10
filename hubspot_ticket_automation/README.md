# HubSpot Ticket Automation — Pack'N

Automated triage of help desk tickets for a 3PL. Drafts grounded replies for an operator to approve, extracts action items, and delivers a split reviewer digest three times a weekday.

Deployed on a DigitalOcean Ubuntu droplet (`packn@167.99.229.91`) under cron. See `docs/server-setup.md` for the provisioning runbook.

## What it does

- **On each scheduled ticket run** (the wrapper `scripts/run_tickets.sh`; the crontab example below uses every 30 minutes): takes new/updated tickets from the help desk — from HubSpot, or from Pack'N OS, depending on the ticket source (see "Phase 93 — ticket source") — classifies them into one of 15 3PL-specific categories, hydrates live ShipSidekick order state where relevant, and drafts a reply grounded in the local KB. Then:
  - **Every reply** → is written as a pending draft in Pack'N OS (`automation_drafts`). An operator reviews it at `/automation/drafts` and Pack'N OS sends it. Nothing is sent to a customer by this automation, and no note is posted on the HubSpot ticket.
  - **Action items** → are queued and forwarded to the Pack'N OS task hub (`/tasks`) after the run.
- **Urgent items** (P0/legal/major escalations / contract cancellation risk) fire a solo Gmail email to the team immediately with ticket context + action items. (When the ticket source is `os`, only for a ticket that has a HubSpot id — see "What an operator should know".)
- **Digest at 8am / 12pm / 3pm ET weekdays** (`hubspot-actions-digest` skill): queries HubSpot for undigested `PACKN_METADATA_V1` notes, composes a single email split into **Luca** (billing, account, escalation) and **Charlie** (warehouse, general) sections, sends from `customercare@gopackn.com`, and marks each included ticket with a `[DIGESTED at ...]` note so subsequent runs skip it. Email lands in lconner + chansen inboxes — no Drafts step, nothing to click send on.

## What ships out of the box

- 15-category 3PL taxonomy (WISMO, mispack, damage claims, inventory, billing, retailer chargebacks, etc.).
- Pre-populated knowledge base in `kb/` with industry-standard flows for each category — marked TODOs where Pack'N-specific policy needs to be filled in.
- Classifier, drafter, and action-item extractor prompts in `prompts/`.
- Dry-run-by-default config so nothing ships to HubSpot or Gmail until you verify a log.

## Runtime

Cron entries on the server (`crontab -l` as `packn`):

```cron
MAILTO=lconner@gopackn.com
PATH=/usr/local/bin:/opt/packn/hubspot_ticket_automation/.venv/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/bin
ANTHROPIC_API_KEY=...

# Ticket processing — every 30 minutes, via the wrapper (pre-gate + shadow
# pipeline + pinned-model agent + deterministic forwarders)
*/30 * * * *  cd /opt/packn/hubspot_ticket_automation && bash scripts/run_tickets.sh >> outputs/runs/cron-tickets.log 2>&1

# Digest — 8am / 12pm / 3pm ET weekdays (UTC during EDT: 12:00, 16:00, 19:00)
# --model pinned so the CLI default can't silently be Opus-tier pricing
0 12 * * 1-5  cd /opt/packn/hubspot_ticket_automation && claude -p /packn-digest --model claude-sonnet-5 --dangerously-skip-permissions >> outputs/runs/cron-digest.log 2>&1
0 16 * * 1-5  cd /opt/packn/hubspot_ticket_automation && claude -p /packn-digest --model claude-sonnet-5 --dangerously-skip-permissions >> outputs/runs/cron-digest.log 2>&1
0 19 * * 1-5  cd /opt/packn/hubspot_ticket_automation && claude -p /packn-digest --model claude-sonnet-5 --dangerously-skip-permissions >> outputs/runs/cron-digest.log 2>&1
```

DST: these UTC hours are correct for EDT (UTC−4). When EST resumes on Sunday, Nov 1 2026, shift the digest hours by +1 → `13`, `17`, `20`. The ticket cron stays as-is. A scheduled email reminder is queued for Nov 1 morning.

## Pack'N OS Control Panel integration (Phase 4)

This automation is now coordinated with **Pack'N OS** — a separate
internal web app at `https://os.gopackn.com` that wraps a control
panel around this cron. After Phase 4 cutover:

- **Pause/resume routines** via the Pack'N OS UI (`/automation`). When
  paused, the next cron tick reads `automation_routines.enabled = false`
  from the OS Postgres, writes a `status='skipped'` run record (its
  summary reads `routine_paused`), and
  exits without polling HubSpot or Pack'N OS. Pause latency is bounded by the cron
  interval (max ~30 min for `/packn-tickets`).
- **Customer-facing replies are no longer auto-sent.** This automation
  now writes a `pending` row to `automation_drafts` for every reply.
  Operators review + approve via `/automation/drafts/<id>` in Pack'N OS;
  Pack'N OS sends via its own Gmail OAuth + writes a HubSpot engagement
  note. Internal sends (urgent solo emails, `/packn-digest`) are
  UNCHANGED — those still go through this repo's Gmail + scripts.
- **Manual rerun** — operators can request a rerun of a single ticket
  from Pack'N OS even while the routine is paused. The next cron tick
  picks up rerun rows from `automation_rerun_requests` and processes
  them BEFORE the normal HubSpot poll.
- **Failure alerts** — the Pack'N OS alert-scan worker fires when this
  cron has 2+ consecutive failures, no successes in 2× the cron
  interval, or pending-draft count > 50. Email lands at
  `lconner@gopackn.com` + dashboard banner in Pack'N OS.
- **Centralized rate limit** — every HubSpot MCP call the agent makes
  (`mcp__hubspot__*` on the droplet) is
  preceded by a Redis Lua token acquire from
  `rate:hubspot:existing-automation` (capacity 3/s); Pack'N OS shares
  the parent budget at `rate:hubspot:packn-os` (capacity 2/s). Sum is
  ≤ 5/s — HubSpot's account-wide Search cap.

### New env vars (set in `/opt/packn/hubspot_ticket_automation/.env`, mode 0600)

```
PACKN_OS_DATABASE_URL=postgresql://packn_os_existing_automation:<password>@<host>:5432/<db>?sslmode=require
REDIS_URL=<production_redis_url>
HUBSPOT_RATE_LIMIT_AUTOMATION=3
```

`HUBSPOT_RATE_LIMIT_AUTOMATION` MUST sum with `HUBSPOT_RATE_LIMIT_OS`
(set on the Pack'N OS side) to ≤ 5; Pack'N OS's env-schema enforces
this at boot time. Default split: AUTOMATION=3, OS=2.

### Postgres role + grants (run ONCE pre-cutover by lconner against the prod DB)

This list, and the privilege audit table below it, are as they stood at the
Phase 4 / 4.1 cutover, when the role had four tables. The role has gained grants
since (the schedule report, Run now requests, the complaint mirror, and the two
Phase 93 grants) and reaches seven tables today. The complete, current list is in
`packn_os_hubspot_client/README.md`; use that file when creating or auditing the
role.

```sql
-- Create role with explicit password (replace placeholder)
CREATE ROLE packn_os_existing_automation WITH LOGIN PASSWORD 'REPLACE_ME';

-- Read-only on routines + rerun requests + drafts (write_draft replay path
-- + Phase 4.1 read_pending_drafts for digest skill)
GRANT SELECT ON automation_routines       TO packn_os_existing_automation;
GRANT SELECT ON automation_rerun_requests TO packn_os_existing_automation;
GRANT SELECT ON automation_drafts         TO packn_os_existing_automation; -- ADDED Phase 4.1 D-04

-- Insert on runs + drafts (append-only from this side; OS owns state transitions)
GRANT INSERT ON automation_runs   TO packn_os_existing_automation;
GRANT INSERT ON automation_drafts TO packn_os_existing_automation;

-- Update on rerun_requests, only the processed/processed_at/resulting_draft_id columns
GRANT UPDATE (processed, processed_at, resulting_draft_id)
  ON automation_rerun_requests TO packn_os_existing_automation;

-- Sequence usage for SERIAL/UUID PKs
GRANT USAGE ON SCHEMA public TO packn_os_existing_automation;

-- NO access to: audit_log, tasks, claims, adjustments, merchants, or any other
-- Pack'N OS table. The role is scoped to the 4 automation_* tables only.
```

The role has zero access to operator data outside the four
`automation_*` tables. Verify with `\dp` after running the GRANTs.

### Per-table privilege audit (Phase 4.1 D-04.b)

| Table | Privilege | Rationale |
|-------|-----------|-----------|
| automation_routines | SELECT | `read_routine_enabled` — fail-closed pause gate (returns False if row missing → tick exits early without HubSpot calls) |
| automation_rerun_requests | SELECT, UPDATE (processed, processed_at, resulting_draft_id only) | `read_pending_rerun_requests` (operator-initiated reruns) + `mark_rerun_processed` (sets the 3 marker columns; no other column writable) |
| automation_runs | INSERT | `write_run_record` — append-only audit of each tick (status, error_summary, tickets_processed, drafts_created) |
| automation_drafts | INSERT, SELECT | `write_draft` (INSERT) + `write_draft` ON CONFLICT replay-path (SELECT for idempotency lookup) + Phase 4.1 `read_pending_drafts` (SELECT for digest skill, replaces HubSpot draft-engagement scraping per D-02.b) |

The role is scoped strictly to these 4 tables. It has NO access to `audit_log`, `tasks`, `claims`, `adjustments`, `merchants`, or any other Pack'N OS table. Column-level UPDATE on `automation_rerun_requests` prevents privilege creep beyond the 3 marker columns the sibling repo needs to write.

### Install (one-PR cutover)

```bash
cd /opt/packn/hubspot_ticket_automation
git pull   # picks up the one-PR cutover diff
pip install -r scripts/requirements.txt   # adds psycopg + redis
pip install -e packn_os_hubspot_client/   # the helper module
```

### Smoke tests (post-install)

```bash
# Routine enabled check (must print True after Pack'N OS Plan 04-02 seed runs)
python -c "from packn_os_hubspot_client import client; print(client.read_routine_enabled('tickets-process'))"
# Expected: True

# Returns False for unknown routines (fail-closed posture)
python -c "from packn_os_hubspot_client import client; print(client.read_routine_enabled('nonexistent-routine'))"
# Expected: False

# Rate-limit token acquire (against the prod docker Redis)
python -m packn_os_hubspot_client.rate_limit
echo $?
# Expected: 0
```

### Rollback playbook (if anything goes wrong post-cutover)

The Pack'N OS infrastructure (the new tables + worker + UI) keeps
working even after the existing-automation cutover is rolled back —
pause/resume become no-ops because the existing automation no longer
reads `automation_routines.enabled`, but no data is lost.

1. Revert the merged PR commit on `hubspot-automation-2`:
   ```bash
   git log --oneline | head -5     # find the cutover merge SHA
   git revert <sha> --no-edit       # creates a revert commit on main
   git push                         # ships the revert
   ```
2. SSH to the droplet:
   ```bash
   cd /opt/packn/hubspot_ticket_automation
   git pull                                 # picks up the revert
   pip install -r scripts/requirements.txt  # restores the pre-cutover deps
   ```
3. Old SKILL.md path resumes — auto-send returns. Customer-facing replies
   are auto-sent again.
4. Document the rollback time + reason in
   `.planning/phases/04-hubspot-automation-control-panel/04-HUMAN-UAT.md`
   (in the Pack'N OS repo) under the Rollback section.

Total time-to-rollback: < 5 minutes.

### Module layout (helper at the repo root)

| File | Purpose |
| --- | --- |
| `packn_os_hubspot_client/__init__.py` | Public surface; exports `client` + `rate_limit` |
| `packn_os_hubspot_client/tenant.py` | `TENANT_ID = 'packn'` constant |
| `packn_os_hubspot_client/db.py` | psycopg connection pool (min=1, max=2) |
| `packn_os_hubspot_client/client.py` | 5 helper functions (read/write the automation_* tables) |
| `packn_os_hubspot_client/rate_limit.py` | Redis Lua rate-limit token acquire |
| `packn_os_hubspot_client/README.md` | Detailed module docs (canonical Pack'N OS source) |

The copy in this repo is the live one: it is what the droplet runs, and new
helper functions land here. The Pack'N OS repo
(`https://github.com/jwilson-cell/packn-os`, private) tracks an older copy at
`python/packn_os_hubspot_client/`, which its cross-language contract test runs.


## Phase 93 — ticket source

Since Phase 93 the automation can take its ticket list from HubSpot or from Pack'N OS. One owner-controlled switch in Pack'N OS decides which: the **Ticket source** control on the Ticket flow card of `/automation`. It is stored as the `app_settings` row `ticket_source_mode` and has three values. The automation reads it once at the start of every run and finishes the run in the source it started in.

| Source | Where the list comes from | What the run does |
|---|---|---|
| `hubspot` | A HubSpot search (the path the rest of this README describes) | The agent (`/packn-tickets`) fetches, drafts and keeps `config/state.json`, as before. An absent setting means this source. |
| `shadow` | HubSpot, with Pack'N OS read beside it | The same HubSpot run. In the same run, code also fetches the Pack'N OS list first, classifies both inputs with one classify step and posts a comparison to Pack'N OS before the agent starts. Afterwards it reports each pass of the agent, completed or failed, for a ticket Pack'N OS knows. No draft comes from the Pack'N OS half. |
| `os` | Pack'N OS (`GET /api/tickets/unprocessed`) | Code fetches the list and, for a ticket that has a HubSpot id, its email thread; hands the agent (`/packn-tickets-os`) a finished bundle; then writes each draft, reports each pass back to Pack'N OS, forwards action items and keeps `config/state.json` current. The agent searches and looks up nothing. |

In `os` the only HubSpot requests are the email-thread fetch for a HubSpot-linked ticket and the one-off fill of an empty order or tracking number. Both are made by code and counted on the run record. A setting the automation cannot read, or does not recognise, ends the run as a failure record; it never falls back to a mode. A Pack'N OS list that cannot be had in `os` ends the run the same way, and HubSpot is not used instead.

The HubSpot path, the HubSpot MCP registration and the HubSpot token all stay in place until Phase 94. They are what a rollback uses.

### One run

Cron and Run now enter through the same door:

```text
crontab line / scripts/poll_manual_runs.sh (Run now)
  -> bash scripts/run_tickets.sh [--manual-request <uuid>]
       -> python scripts/run_tickets.py [--manual-request <uuid>]   the orchestrator: reads the source once,
                                                                    runs that source's runner,
                                                                    writes exactly one automation_runs record
       -> python scripts/forward_action_items.py                     always
       -> python scripts/write_complaints.py                         always
```

`scripts/run_tickets.sh` is the supported entry for every scheduled and manual run. Do not rename it: Pack'N OS finds the crontab line by that file name. The orchestrator exits 0 when the run's record was written, whatever the run's status, and 1 when the record could not be written. A run that never launches its agent, or whose agent dies, therefore still leaves a `failure` record that names the cause.

The three runners are registered in `RUNNER_SPECS` of `scripts/run_tickets.py`: `hubspot` → `run_hubspot` (in the script itself), `os` → `ticket_pipeline.os_pass.run_os`, `shadow` → `ticket_pipeline.source_shadow.run_source_shadow`.

The agent is launched from the package root with one of two command lines:

| Source | Launch |
|---|---|
| `hubspot`, `shadow` | `claude -p /packn-tickets --model claude-sonnet-5 --dangerously-skip-permissions` |
| `os` | `claude -p /packn-tickets-os --model claude-sonnet-5 --dangerously-skip-permissions --disallowedTools mcp__hubspot` |

Every child of a run gets `PACKN_RUN_ID`, `PACKN_SOURCE_MODE`, `PACKN_RESULT_PATH` and `PACKN_RUN_CONTEXT`; the `os` launch also gets `PACKN_BUNDLE_PATH`. The orchestrator sets them for one run. None of them is a line in `.env`, and none is to be set by hand.

Both skills begin with a source guard, `py -m ticket_pipeline.source --require <modes>`: exit 0 when the source is one the skill serves, 3 with `SOURCE_MODE_NOT_SERVED <mode>` when it is not, 4 when the setting is unknown or unreadable, 2 for a usage error. A skill started by hand, or by an old scheduler line, therefore cannot work from the wrong source.

**What removing the HubSpot tools does and does not do.** `--disallowedTools mcp__hubspot` removes every tool of the MCP server registered on the droplet as `hubspot` from the `os`-source agent. Removing the HubSpot MCP tools closes the tool path, not the shell path: the agent still has Bash and the HubSpot token file is on the droplet. What this phase delivers is structural: code does every HubSpot request and counts it, the `os`-source skill holds no HubSpot instruction, and the run record carries the counts. An agent that chose to call HubSpot from its shell would not be counted. Closing that means removing the token, which is Phase 94. The installed `claude` version on the droplet has not been checked against this flag; confirm there, before the source is ever set to Pack'N OS, that a session started with it lists no tool of that server.

### What an operator should know

- **`os`: an urgent ticket with no HubSpot id sends no urgent solo email.** The urgent email is sent only for a ticket that has a HubSpot id. A ticket without one (a portal filing, for example) is still drafted and queued for the digest.
- **`os`: when the agent leaves no result at all, nothing is reported as failed.** The run is one failure record (`agent_launch_failed` or `agent_exited_abnormally`) and no failed pass is written for the listed tickets, so those tickets are retried on the next run rather than parked. A ticket that fails inside a run that did finish is reported as failed and does count toward parking.
- **`os`: a write-back Pack'N OS refuses makes the run `partial`.** When Pack'N OS answers a ticket's write-back with 404, 400 or 413, the draft is in place but the pass was not recorded as reported. The run record is then `partial` and its summary carries `writeback_ticket_gone` (404) or `writeback_rejected` (400, 413) with a count, the same as for a write-back that could not be delivered at all. A 409 (`writeback_conflict`: Pack'N OS already holds that pass) does not change the status.
- **`shadow`: a classifier outage means no comparison is posted for that run.** After three classify failures in a row, or when every classify call failed, the run posts no observation (`source_shadow_skipped:classify_unavailable` on the run record) and does not count as a compared run of its day. The HubSpot half of the run is not affected.
- **`shadow`: a run whose pre-gate failed open is not compared.** When the settings, the HubSpot token or the HubSpot search could not be read, or an operator rerun is pending, the agent is launched as usual, but nothing is compared and nothing is reported back for that run.
- **`shadow`: the first run is slower.** It classifies up to 100 Pack'N OS tickets once before the agent starts. Expect minutes of delay on that one run.
- **After a switch back to `hubspot`, start a hand-run through the wrapper.** A ticket pass started by hand outside `scripts/run_tickets.sh` has no run context, writes plain drafts, and can leave two pending drafts for a ticket that still holds a draft from `os` or `shadow`. The same can happen for one run when the wrapper's read of those drafts fails (`rollback_entries_unreadable` on the run record).
- **A dry run posts no action items.** With `dry_run: true` in `config/settings.yaml`, nothing is posted to Pack'N OS `/tasks` in any source: the `os` finalizer counts what it would have posted (`would_action_item_posts` on the run record), and `scripts/forward_action_items.py`, which runs after every ticket run, posts nothing and logs what it would have forwarded. The forwarder also posts nothing when it cannot read `dry_run` from the settings file. The queue is left as it is, so the first run with `dry_run: false` forwards what is still queued and not yet delivered.
- **`hubspot` and `shadow` runs show no HubSpot call counts.** In those sources the agent calls HubSpot through MCP, which code cannot count. Only an `os` run records them.

### Secrets and runtime files

| File | What it is |
|---|---|
| `config/.secrets/tickets_api_secret.txt` | The shared secret that signs the automation's requests to the Pack'N OS tickets routes. Read only by `packn_os_hubspot_client/tickets_api.py`. The owner provisions it on the droplet; it is at least 32 characters, never committed and never pasted into a chat. |
| `config/.action_items_forwarded.json` | The action-item forwarder's seen-set. Runtime state of the droplet, git-ignored. |
| `config/.action_items_retry.json` | The ids the forwarder needs to resend an unconfirmed action-item post. Runtime state of the droplet, git-ignored. |
| `outputs/runs/os/<run id>/` | One run's scratch directory (bundle, run context, result file). It holds ticket text. It is created for the owner only (directory mode 700; the files code writes there are created with mode 600) and is removed at the end of the run, on every way out the process controls. A run that is killed outright cannot remove its own: each run first removes any run directory there that is more than a day old. Git-ignored. |
| `outputs/runs/source_shadow_seen.json` | The `shadow` comparison's cache of categories it has already worked out. Ids and category slugs only. Git-ignored. |
| `config/state.json`, `config/pending_actions.json` | Tracked in git AND rewritten on the droplet by every run. See the pull procedure below. |

The secret file holds the same value as `TICKETS_API_SECRET` in the Pack'N OS environment. When it is missing or too short, every `os` run is a `secret_missing` failure record and every `shadow` run skips its comparison.

The Pack'N OS origin the signed client talks to is `os_ingest_url` in `config/settings.yaml` (scheme and host only).

### Database grants

The automation's database role (`packn_os_existing_automation`) needs two grants it did not have before Phase 93:

1. `SELECT` on `app_settings` — to read the ticket source. Without it every run, in every source, ends as a failure record with `source_unreadable`.
2. `UPDATE (state, superseded_at, superseded_by_draft_id, updated_at)` on `automation_drafts` — to supersede a ticket's earlier pending draft when the ticket is processed again.

The exact statements, the reason for each, and the seven `has_table_privilege` / `has_column_privilege` checks are in `packn_os_hubspot_client/README.md` ("Phase 93: the cron reads OS tickets"). That file holds the complete, current grant list for the role. Run the checks on the production database before this code is deployed.

### Deploying on the droplet

Order matters. Deploy the whole of Phase 93 in one pull: all three sources ship together, and a part of it must never be deployed alone.

1. Pack'N OS first: its Phase 93 release, with the tickets routes live and its tickets secret set in its environment.
2. Confirm the two grants with the checks in `packn_os_hubspot_client/README.md`.
3. Create `config/.secrets/tickets_api_secret.txt` on the droplet (mode 600), holding the value Pack'N OS has.
4. Check what the droplet has changed, and copy the two tracked, rewritten files aside:

   ```bash
   cd /opt/packn/hubspot_ticket_automation
   git status --short config/
   cp config/state.json config/state.json.bak
   cp config/pending_actions.json config/pending_actions.json.bak
   ```

5. Pull. The Phase 93 code must be on `main` first. No commit of this phase touches either file, so the pull keeps the droplet's copies:

   ```bash
   git pull
   ```

   If the pull refuses because of local changes to those two files:

   ```bash
   git stash push -- config/state.json config/pending_actions.json
   git pull
   git stash pop
   ```

6. Re-run the installs. No dependency was added; this is a check that the helper's editable install sees the new modules:

   ```bash
   .venv/bin/pip install -r scripts/requirements.txt
   .venv/bin/pip install -e packn_os_hubspot_client/
   ```

7. Check the agent CLI and the MCP name the `os` launch relies on:

   ```bash
   claude --version
   claude mcp list        # the HubSpot server must be registered under the name `hubspot`
   ```

8. Leave the crontab alone: both lines keep their script names.
9. The source is still `hubspot`. Watch one scheduled run: it leaves one `automation_runs` record whose source is `hubspot`.
10. The owner sets the source to Shadow on the Ticket flow card. The switch to Pack'N OS comes later, from the same card, once the comparison gate is met and signed off; make that first `os` run a watched Run now.

### Rolling back

Set the source back to HubSpot on the Ticket flow card. That is all the owner does: no code change, no pull, no restart. The next run reads the setting and works from HubSpot. Do it at a quiet time, after an `os` run has finished, and start any hand-run through `scripts/run_tickets.sh`.

What the HubSpot runs do after a rollback:

- **The HubSpot search starts where the last HubSpot-source run left it.** An `os` run records the tickets it completed in `config/state.json` as fingerprints and never moves `last_run_at`, the start of the HubSpot search. So the first HubSpot runs after a rollback search a wider stretch of time than usual. Expect them to be longer.
- **A ticket an `os` run completed is not drafted again.** Its fingerprint keeps it out, until HubSpot reports a change later than the moment that pass finished.
- **A ticket no `os` run completed is selected again.** That includes a ticket Pack'N OS had parked after two failed passes, one beyond the per-run cap, and one Pack'N OS had not mirrored yet. Parked tickets are the ones a rollback is usually for; they are worked from HubSpot as if `os` had never been on.

### Tests

The automation's gate, run from `hubspot_ticket_automation/`:

```bash
py -3 -m pytest tests/ packn_os_hubspot_client/tests/test_pregate.py packn_os_hubspot_client/tests/test_pipeline.py -q -p no:cacheprovider
```

The helper's database tests (`packn_os_hubspot_client/tests/test_drafts.py`, `test_run_records.py`) are run on their own. They refuse any database but the local dev Postgres (`localhost` or `127.0.0.1`, port 5433) and abort the whole session with `REFUSING_NON_LOCAL_DATABASE` otherwise. No test in either directory can open a connection that leaves the machine.


## Auth / secrets

The server carries all credentials in `config/.secrets/` (gitignored):

- `hubspot_token.txt` — HubSpot private-app access token. Scopes needed: `crm.objects.tickets.read/write`, `crm.objects.contacts.read`, `crm.objects.companies.read`, `crm.objects.notes.read/write`, `crm.objects.emails.read/write` (for v1 engagements), `tickets`, `conversations.read`.
- `shipsidekick_token.txt` — ShipSidekick private-app bearer token (read orders + shipments).
- `token.json` + `credentials.json` — Gmail OAuth with `gmail.modify` scope, authenticated against `lconner@gopackn.com` with a verified "Send mail as `customercare@gopackn.com`" alias. SPF/DKIM for gopackn.com include Google, so the alias sends cleanly.
- `sheets_token.json` + `sheets_client.json` — Google Sheets OAuth for the rollup export.
- `tickets_api_secret.txt` — the shared secret for the Pack'N OS tickets routes (Phase 93; see "Secrets and runtime files" above). Provisioned on the droplet by the owner, not copied from a laptop.

HubSpot MCP is set up on the server via `@hubspot/mcp-server` npm package, registered in Claude Code CLI at packn's user level via `claude mcp add hubspot --env HUBSPOT_ACCESS_TOKEN=...`.

> **Read this before the sections below.** From here to "Safety posture", parts of this README describe the first local setup of April 2026: a Windows laptop, replies posted as HubSpot draft notes, a Gmail digest of drafts, and categories that could graduate to auto-send. The droplet does not work that way any more. Every reply is a pending draft in Pack'N OS, nothing is auto-sent, and the wrapper script is the supported entry of every ticket run. Where a section below disagrees, the Pack'N OS and Phase 93 sections above win.

## First-time local run (optional development setup)

To iterate on skills/KB/prompts from your laptop without affecting the server:

1. `git clone https://github.com/jwilson-cell/hubspot-automation-2.git`
2. Copy `.secrets/` from the server (or ask lconner for a debug set).
3. Flip `config/settings.yaml:dry_run` to `true` and run locally via `claude /packn-tickets`.
4. Commit changes → push → `git pull` on the server. The server does not update itself and the cron does not pull.

Fill in `settings.yaml`:
- `hubspot_portal_id` (integer — from your HubSpot URL)
- `pipeline_id` (help desk pipeline)
- `active_stages` (pipeline stages where tickets need a response)

### 3. Dry-run review

Run the skill. It will process up to 25 tickets (no writes), and write a log to `outputs/runs/<timestamp>.md`. Review:
- Are tickets being classified correctly?
- Are drafted replies reasonable?
- Are action items picked up?

### 4. Limited live run

Edit `config/settings.yaml`:
- `dry_run: false`
- `per_run_cap: 1`

Run the skill. Verify:
- The internal note appears on one ticket in HubSpot.
- If the classifier marked it `urgent`, an email arrived.
- If `normal`, the action item is in `config/pending_actions.json`.

### 5. Test the digest

Run the `hubspot-actions-digest` skill. Verify you receive one consolidated email.

### 6. Schedule both

**Current cadence** (based on observed low real-ticket volume): hourly for tickets, every 3 hours for digest.

| Routine | Cadence | Cron (local TZ) | Skill |
|---|---|---|---|
| Process tickets | hourly | `7 * * * *` | `hubspot-tickets` |
| Action-item digest | every 3 hours | `47 */3 * * *` | `hubspot-actions-digest` |

**Two ways to schedule these — pick based on your needs:**

#### Option 1 (fastest, but requires Claude Code to be running)

Ask Claude in an open Claude Code session:
> "Schedule the hubspot-tickets skill to run at `7 * * * *` and the hubspot-actions-digest skill at `47 */3 * * *`"

Claude will use its internal `CronCreate` to set up in-session schedules. Caveats:
- Jobs only fire while Claude Code is **open AND idle** (not mid-query).
- Jobs are session-scoped — they die when you quit Claude Code.
- Recurring jobs also auto-expire after 7 days.

Good for testing or development. Not suitable for true unattended operation.

#### Option 2 (recommended for production) — Windows Task Scheduler

> **Note (Phase 93).** The example below launches the skill with a direct agent command line. That is how the first Windows setup ran; it is not how the droplet runs. `scripts/run_tickets.sh` is the supported entry for every scheduled and manual run: it reads the ticket source, writes the run's one record and forwards action items. A direct launch like the one below is stopped by the skill's first step, its source guard, whenever the owner's source switch is not one that skill serves, and it gets none of the wrapper's other steps.

Run these two commands in an **elevated PowerShell** (Run as Administrator) to create persistent OS-level scheduled tasks that fire even when Claude Code is closed:

```powershell
# Hourly ticket processing (fires at :07 past each hour)
schtasks /create /tn "HubSpot-Tickets-Hourly" `
  /tr "cmd /c cd /d C:\Users\sonia\claude_code\projects\hubspot_ticket_automation && claude -p `"Run the hubspot-tickets skill following .claude/skills/hubspot-tickets/SKILL.md exactly.`" > outputs\runs\cron-last-stdout.txt 2>&1" `
  /sc hourly /mo 1 /st 00:07 /f

# Every-3-hours digest (fires at :47 past, every 3 hours)
schtasks /create /tn "HubSpot-Digest-3h" `
  /tr "cmd /c cd /d C:\Users\sonia\claude_code\projects\hubspot_ticket_automation && claude -p `"Run the hubspot-actions-digest skill following .claude/skills/hubspot-actions-digest/SKILL.md exactly.`" > outputs\digests\cron-last-stdout.txt 2>&1" `
  /sc daily /st 00:47 /ri 180 /du 23:59 /f
```

To verify:
```powershell
schtasks /query /tn "HubSpot-Tickets-Hourly" /v
schtasks /query /tn "HubSpot-Digest-3h" /v
```

To remove later:
```powershell
schtasks /delete /tn "HubSpot-Tickets-Hourly" /f
schtasks /delete /tn "HubSpot-Digest-3h" /f
```

**Caveat on Option 2**: the Claude CLI invoked non-interactively (`claude -p "..."`) uses your logged-in Claude Code auth. Make sure your machine stays awake (disable sleep, or use `powercfg /change standby-timeout-ac 0`) or the scheduled task will skip fires while asleep.

### 7. Pack'N policy TODOs

Search `kb/` for `TODO (Pack'N):` — fill in the real policies (rates, SLAs, contacts). Not blocking; drafts will be slightly more hedged without them.

## Slash commands (manual trigger)

Three user-level slash commands are installed at `C:\Users\sonia\.claude\commands\` — invoke from any Claude Code session:

| Command | What it does |
|---|---|
| `/packn-tickets` | Manually run the ticket-processing skill right now, when the ticket source is `hubspot` or `shadow` (classify, write pending drafts in Pack'N OS, queue action items) |
| `/packn-digest` | Manually flush the action-item queue to a Gmail digest draft right now |
| `/packn-status` | Read-only local status check — queue depth, last run, recent drafts, any categories graduated to auto-send |

Use `/packn-status` any time to see where things stand without touching HubSpot or Gmail.

On the droplet, start a ticket run by hand with `bash scripts/run_tickets.sh`, not with `/packn-tickets`: the wrapper reads the ticket source, writes the run's record and gives the run its context. A bare `/packn-tickets` stops at its source guard when the source is `os`, and after a switch back to `hubspot` it can leave two pending drafts for a ticket (see "What an operator should know"). `/packn-tickets-os` is never started by hand: it needs the bundle the wrapper builds and stops without one.

## Day-to-day operating guide

### Where to look when something runs

| What | Where |
|---|---|
| Drafted reply on a specific ticket | HubSpot ticket timeline — note prefixed `[DRAFT — REVIEW BEFORE SENDING]` |
| Urgent alerts | Gmail Drafts folder — subject prefix `[HubSpot URGENT · Ticket #...]` |
| Hourly/3-hourly digest | Gmail Drafts folder — subject prefix `[HubSpot Digest]` |
| Why a specific run did what it did | `outputs/runs/<timestamp>.md` |
| Archive of past digest emails | `outputs/digests/<timestamp>.md` |
| What's currently queued for the next digest | `config/pending_actions.json` |
| What tickets have already been processed | `config/state.json` → `processed_ticket_ids` |

### Reviewing a draft reply

1. Open the ticket in HubSpot.
2. Look for the note with `[DRAFT — REVIEW BEFORE SENDING]` at the top.
3. If it's good: copy the body (above the automation-metadata footer), paste into a real ticket reply, send. Then delete or archive the draft note.
4. If it needs edits: edit in the reply composer, send. Delete the draft note.
5. If the draft is bad: tell Claude what was wrong (edit the relevant `kb/*.md` or `prompts/*.md`). Delete the draft note.

### Filing carrier claims from the digest

When a damage / loss / delay ticket surfaces a `file_carrier_claim` action item, the hourly digest renders a packet card with the carrier portal link, tracking #, ship date, a copy-paste block, and a filing-deadline clock. Full operator walkthrough: **[`docs/carrier-claim-filing-guide.md`](docs/carrier-claim-filing-guide.md)**. Lifecycle columns (`claim_status`, `claim_number`, `carrier_filed_at`, `coverage_usd`, `resolution_amount_usd`, `reimbursement_received_at`) live in the `carrier_issue_log` tab of the Google Sheet.

### Clearing a digest email from Drafts

Once you've processed a digest:
- Delete the draft in Gmail (keeps the inbox clean).
- The archive in `outputs/digests/` remains as a historical record.

### Adjusting which categories auto-send

All categories start in `draft` mode. To graduate one to `auto-send` (after a week of good drafts), edit `config/categories.yaml`:

```yaml
wismo_tracking:
  mode: auto-send   # was: draft
  kb_file: kb/tracking_wismo.md
```

Next run picks up the change. **Caveat**: auto-send currently posts an `[AUTO-SEND PROPOSED]`-prefixed note because the HubSpot MCP may not support direct public ticket replies. Verify on first use.

### Pausing the automation

Edit `config/settings.yaml`:
```yaml
dry_run: true
```
Or disable the scheduled tasks. Both stop all writes; the skill will still log what it would have done.

### Updating the KB / prompts

- Edit any file in `kb/` → takes effect on next skill run.
- Edit any file in `prompts/` → takes effect on next skill run.
- No redeploy / restart needed.

Search for `TODO (Pack'N):` in `kb/` — those are places where Pack'N-specific policy (rates, SLAs, contacts, escalation paths) should be filled in to make drafts less hedged.

### Known limitations

1. **Chat-sourced tickets** can't be hydrated via the HubSpot MCP — the connector doesn't expose the `conversations` / chat-thread model. You've already disabled chat as a source, so this shouldn't recur.
2. **Marketing emails** landing in the help desk inbox create ticket records. The automation classifies them as `other_unclassified` and doesn't draft replies, but they clutter the queue. Recommend filtering at the HubSpot inbound-email level.
3. **Gmail MCP auth scope is draft-only** — no `send` capability. Action items land as Gmail drafts, not sent messages. To get real inbox notifications you'd need to either re-auth with a broader scope (if available) or switch to a different delivery channel (Slack, HubSpot task creation, etc.).
4. **Scheduled tasks via CronCreate are session-only.** Use Windows Task Scheduler (Option 2 above) for persistence.
5. **Per-email authorship isn't fetched.** All outbound emails show as from `customercare@gopackn.com` (shared account). To attribute specific replies to Luca vs. Jacob vs. other team members, the skill would need to pull `hubspot_owner_id` on each email and resolve via `search_owners`. Not currently enabled.

## One-week graduation review

After the automation has been running for a week:

1. Review `outputs/runs/` and the posted internal notes.
2. Identify categories where the drafted replies have been consistently strong.
3. Consider graduating one or two low-risk categories (e.g., `wismo_tracking` with straightforward FAQ-like replies) from `draft` to `auto-send` by editing `config/categories.yaml`.

## Directory layout

See `CLAUDE.md` for the full file map and invariants.

## Safety posture

- Dry-run default on first run.
- All categories start in draft mode (never auto-send at launch).
- The skill never changes ticket stage, owner, priority, or closes tickets.
- No secrets in `kb/` — KB content is loaded into every prompt.
- MCP write failures don't lose state; the next run retries the skipped tickets.
