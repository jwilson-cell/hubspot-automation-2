# HubSpot Ticket Automation — Pack'N

Automated triage of HubSpot help desk tickets for a 3PL. Drafts grounded replies, auto-sends low-risk categories end-to-end, extracts action items, and delivers a split reviewer digest three times a weekday.

Deployed on a DigitalOcean Ubuntu droplet (`packn@167.99.229.91`) under cron. See `docs/server-setup.md` for the provisioning runbook.

## What it does

- **Every 30 minutes** (`hubspot-tickets` skill): pulls new/updated tickets from the HubSpot help desk pipeline, classifies them into one of 15 3PL-specific categories, hydrates live ShipSidekick order state where relevant, and drafts a reply grounded in the local KB. Then:
  - **FORM + topic Mispack or Carrier Issue** → auto-sends the reply to the customer from `customercare@gopackn.com` via Gmail, plus logs both a v1 email engagement and an `[AUTO-SENT TO CUSTOMER]` note on the ticket for reviewer visibility.
  - **Everything else** (form with any other topic, email-sourced, WhatsApp-sourced, chat) → posts the reply as a `[DRAFT — REVIEW BEFORE SENDING]` internal note with an embedded `PACKN_METADATA_V1` JSON block so the digest can reconstruct action items.
- **Urgent items** (P0/legal/major escalations / contract cancellation risk) fire a solo Gmail email immediately with ticket context + action items.
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

# Ticket processing — every 30 minutes
*/30 * * * *  cd /opt/packn/hubspot_ticket_automation && claude -p /packn-tickets --dangerously-skip-permissions >> outputs/runs/cron-tickets.log 2>&1

# Digest — 8am / 12pm / 3pm ET weekdays (UTC during EDT: 12:00, 16:00, 19:00)
0 12 * * 1-5  cd /opt/packn/hubspot_ticket_automation && claude -p /packn-digest --dangerously-skip-permissions >> outputs/runs/cron-digest.log 2>&1
0 16 * * 1-5  cd /opt/packn/hubspot_ticket_automation && claude -p /packn-digest --dangerously-skip-permissions >> outputs/runs/cron-digest.log 2>&1
0 19 * * 1-5  cd /opt/packn/hubspot_ticket_automation && claude -p /packn-digest --dangerously-skip-permissions >> outputs/runs/cron-digest.log 2>&1
```

DST: these UTC hours are correct for EDT (UTC−4). When EST resumes on Sunday, Nov 1 2026, shift the digest hours by +1 → `13`, `17`, `20`. The ticket cron stays as-is. A scheduled email reminder is queued for Nov 1 morning.

## Auth / secrets

The server carries all credentials in `config/.secrets/` (gitignored):

- `hubspot_token.txt` — HubSpot private-app access token. Scopes needed: `crm.objects.tickets.read/write`, `crm.objects.contacts.read`, `crm.objects.companies.read`, `crm.objects.notes.read/write`, `crm.objects.emails.read/write` (for v1 engagements), `tickets`, `conversations.read`.
- `shipsidekick_token.txt` — ShipSidekick private-app bearer token (read orders + shipments).
- `token.json` + `credentials.json` — Gmail OAuth with `gmail.modify` scope, authenticated against `lconner@gopackn.com` with a verified "Send mail as `customercare@gopackn.com`" alias. SPF/DKIM for gopackn.com include Google, so the alias sends cleanly.
- `sheets_token.json` + `sheets_client.json` — Google Sheets OAuth for the rollup export.

HubSpot MCP is set up on the server via `@hubspot/mcp-server` npm package, registered in Claude Code CLI at packn's user level via `claude mcp add hubspot --env HUBSPOT_ACCESS_TOKEN=...`.

## First-time local run (optional development setup)

To iterate on skills/KB/prompts from your laptop without affecting the server:

1. `git clone https://github.com/jwilson-cell/hubspot-automation-2.git`
2. Copy `.secrets/` from the server (or ask lconner for a debug set).
3. Flip `config/settings.yaml:dry_run` to `true` and run locally via `claude /packn-tickets`.
4. Commit changes → push → server picks them up on the next cron fire.

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
| `/packn-tickets` | Manually run the ticket-processing skill right now (classify, draft internal notes, queue action items) |
| `/packn-digest` | Manually flush the action-item queue to a Gmail digest draft right now |
| `/packn-status` | Read-only local status check — queue depth, last run, recent drafts, any categories graduated to auto-send |

Use `/packn-status` any time to see where things stand without touching HubSpot or Gmail.

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
