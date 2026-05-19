---
name: hubspot-tickets
description: Process new/updated HubSpot help desk tickets — classify, draft a reply grounded in the local 3PL KB, write the draft to Pack'N OS automation_drafts for operator review, and queue tickets for the hourly reviewer digest. Action items route to either immediate urgent email or the digest queue. Use when the user asks to run the ticket automation, process the ticket queue, or when invoked on cron.
---

# HubSpot Tickets — Processing Skill

You are running Pack'N's help desk ticket automation. Pack'N is a **3PL / fulfillment provider** — treat customers as merchants/brands unless a ticket clearly originates from an end consumer.

## CRITICAL: Attachment and image handling

**Never hydrate customer attachments into the model context.** This is a hard invariant — violating it causes `400 Could not process image` errors that poison the entire conversation and require a /clear to recover.

- **Do NOT request `hs_file_upload`** in any HubSpot MCP `properties` list.
- **Do NOT fetch file URLs, file contents, or file IDs** via any MCP tool (HubSpot files API, URL fetchers, `WebFetch`, etc.) for attachments discovered on a ticket.
- **If any MCP response returns image content blocks**, ignore the image payload entirely. Treat the response as text-only.
- **Detect attachment presence only** by checking whether the ticket body / form fields mention uploaded files or by association count. Record `ticket_context.has_attachments: true` and a reviewer note.
- **Draft language when attachments are present**: "Customer indicated files were attached — the draft reviewer should open the HubSpot ticket to view them before sending." Never write "thanks for the screenshot" or pretend to have seen the file.

**If you encounter a `Could not process image` error mid-run:** abandon the current ticket immediately, record `{ticket_id, error: "image_processing_error", skipped: true}` in the run log, and move on. Never retry an image-poisoned request.

## Preconditions

1. Working directory contains `config/settings.yaml`, `config/categories.yaml`, `config/state.json`, `prompts/`, and `kb/`. If any are missing, stop and tell the user which path is missing.
2. Read `config/settings.yaml`. Note `dry_run`, `per_run_cap`, `pipeline_id`, `active_stages`, `hubspot_portal_id`, `urgent_signals`.
3. Read `config/state.json` to get `last_run_at`. If empty, use the timestamp from 24h ago.

## Discovery step (first run only)

If `pipeline_id` or `active_stages` are empty in `settings.yaml`:

1. Call `mcp__claude_ai_HubSpot__get_crm_objects` with `objectType=tickets`, limit 1, to confirm API access.
2. Call `mcp__claude_ai_HubSpot__search_properties` to list ticket pipelines and stages.
3. Propose values to the user and pause. Do NOT guess pipeline IDs.

## Pack'N OS coordination

This skill coordinates with **Pack'N OS** (the operator control panel) via a Python helper at `packn_os_hubspot_client/`. Every tick must:

1. Check the routine-enabled gate (Step 0) — if Pack'N OS has paused us, exit cleanly without HubSpot calls.
2. Acquire a Redis rate-limit token before every `mcp__claude_ai_HubSpot__*` call (shared 5/s account-wide cap with Pack'N OS).
3. Write drafts to Pack'N OS `automation_drafts` via `client.write_draft(...)`. No HubSpot note engagement is created. Pack'N OS owns the operator approve→send chain.
4. Write `automation_runs` rows at tick start (Step 0) and tick finish (Step 4.5).
5. Process operator-initiated rerun requests at tick start (Step 0).

Internal sends (urgent solo emails to lconner+chansen, `/packn-digest` recipient sends) still use Gmail directly — unchanged.

## Step 0: Routine gate + run-record start + rerun queue

### Step 0a: Report current crontab schedule to Pack'N OS

Run BEFORE the gate so Pack'N OS knows the cron fired, even when the routine
is paused. Fire-and-forget — failures do NOT block the rest of Step 0.

The marker `cron_tickets.sh` is the wrapper script invoked by the droplet
crontab (`/opt/packn/hubspot_ticket_automation/scripts/cron_tickets.sh`).
If the operator renames the wrapper, update the marker here too.

```bash
py -c "from packn_os_hubspot_client import client; client.report_routine_schedule('tickets-process', 'cron_tickets.sh')"
```

### Step 0b: Routine gate

Run the gate. Exit code discriminates:

```bash
py -c "from packn_os_hubspot_client import client; import sys; sys.exit(0 if client.read_routine_enabled('tickets-process') else 99)"
```

- **Exit 0** → routine enabled; proceed to rerun-queue poll, then Step 1.
- **Exit 99** → routine paused. Write a status='skipped' run record:

  ```bash
  py -c "from packn_os_hubspot_client import client; client.write_run_record('tickets-process', 'skipped', None, 0, 0, '<run_started_at_utc ISO>', '<now ISO>')"
  ```

  Exit the skill with a one-line note: "Routine paused via Pack'N OS — skipping this tick."
- **Any other exit** (DB unreachable, role permission error, network timeout) → fail-closed: treat as `skipped` (same shell-out, with `error_summary` set to the exit-code reason), then exit.

After the gate passes, poll the rerun queue:

```bash
py -c "
import json
from packn_os_hubspot_client import client
rows = client.read_pending_rerun_requests('tickets-process')
print(json.dumps([{'id': str(r['id']), 'ticket_id': r['ticket_id']} for r in rows]))
"
```

For each row: hydrate (2a + 2a.5), classify + draft + extract_actions (2b–2e), call `client.write_draft(...)` (2f), then:

```bash
py -c "from packn_os_hubspot_client import client; client.mark_rerun_processed('<rerun_request_id>', '<draft_id>')"
```

Reruns count toward `tickets_processed` + `drafts_created` in the run record but do NOT consume from `per_run_cap` (operator work always lands).

## Main loop

At the start of the run, capture a **run envelope** for step 3.5:

- `run_id`: generate a UUID4 string.
- `run_started_at_utc`: current UTC ISO timestamp, captured BEFORE the first MCP call.
- `last_run_at_used`: the `last_run_at` value you read from `state.json` (or the 24h-ago fallback).
- `run_mode`: `"dry_run"` if `settings.dry_run` is true, else `"live"`.

Initialize counters (all 0) and an empty confidence list:

```
counters = {
  tickets_matched, tickets_processed, tickets_skipped_dedupe,
  tickets_skipped_error, notes_posted, auto_sends, auto_send_failures,
  urgent_emails_drafted, tickets_queued_for_digest, image_errors, mcp_errors
}
confidences = []
sheets_rows = { mispack: [], carrier: [] }
```

- `notes_posted` counts drafts written to `automation_drafts`.
- `auto_sends` counts customer-facing drafts written to `automation_drafts` (Phase 4 model: former auto-sends are now drafts too).
- `auto_send_failures` counts `write_draft` failures (legacy counter name).
- `tickets_queued_for_digest` is one per queued ticket, not one per action item.

### 1. Fetch candidate tickets

Acquire a HubSpot rate-limit token before the call:

```bash
py -m packn_os_hubspot_client.rate_limit
```

Exit 0 = token acquired (or degraded to passthrough on Redis outage — proceeds anyway). Apply this shell-out before EVERY `mcp__claude_ai_HubSpot__*` call below.

Then call `mcp__claude_ai_HubSpot__search_crm_objects`:

- `objectType`: `tickets`
- Filters (AND'd):
  - `hs_lastmodifieddate > {last_run_at}`
  - `hs_pipeline_stage IN {active_stages}` (typically `["1", "3"]` — New + Waiting on us)
  - `hs_last_message_from_visitor EQ "true"` (only if `settings.require_last_message_from_visitor`)
- Properties: `subject`, `content`, `hs_pipeline_stage`, `hs_ticket_priority`, `hs_lastmodifieddate`, `createdate`, `source_type`, `hs_last_message_from_visitor`, plus all `settings.ticket_custom_properties`. **Do NOT include `hs_file_upload`** — see the attachment rule.
- `limit`: `per_run_cap`
- Sort: `hs_lastmodifieddate` ASCENDING (oldest-unreplied first)

If zero tickets, write a short run log saying "no new tickets" and exit cleanly.

### 2. For each ticket

Process sequentially. On per-ticket error, log it and continue — never abort the whole run on a single failure.

#### 2a-pre. Early-exit dedupe (before hydrating)

Check `state.json:ticket_fingerprints[ticket_id]`:

- **No fingerprint** → first-time ticket, proceed to hydration.
- **Fingerprint exists AND `current_num_notes <= fingerprint.num_notes`** → skip without hydrating. Log `{ticket_id, skipped: "dedupe_no_new_notes", num_notes: N}`. Continue to next ticket.
- **Fingerprint exists AND `current_num_notes > fingerprint.num_notes`** → a new note was added since last pass; re-hydrate and re-draft.

**Why this matters:** tickets where we've only written a draft (no public reply) keep `hs_last_message_from_visitor = true`, so the main filter re-matches them every run. The fingerprint check is what prevents wasted MCP calls. **Operator escape hatch:** to force re-processing of a stuck ticket, manually clear its fingerprint from `state.json`.

**Writing the fingerprint (success path):** after a successful post (step 2f) — or after completing the loop for a ticket without posting (informational replies) — update `state.json:ticket_fingerprints[ticket_id] = { num_notes: <current_num_notes>, processed_at: "<ISO now>", hs_lastmodifieddate: "<ticket's hs_lastmodifieddate>" }`.

#### 2a. Hydrate context

Acquire a rate-limit token before each MCP call below.

**For form-sourced tickets (`source_type == "FORM"`):**

The message body lives on associated `emails` objects, not on `content`.

1. `search_crm_objects` on `emails` associated with the ticket:
   - `associatedWith: [{objectType: "tickets", operator: "EQUAL", objectIdValues: [ticket_id]}]`
   - Properties: `hs_email_subject`, `hs_email_text`, `hs_email_html`, `hs_email_direction`, `hs_email_from_email`, `hs_email_to_email`, `hs_createdate`
   - Sort: `hs_createdate` ASCENDING
   - `limit`: `conversation_email_limit` (default 10)
2. Prefer `hs_email_text`; else strip HTML to plain text.
3. The **first email** is the form submission itself — its body is an HTML blockquote with form fields (e.g., "Topic of Ticket: Carrier Issue", "Order Number: 7679500"). Parse as structured form data.

**For email-sourced tickets:** same pattern. The `content` property may be populated; if so, use as fallback.

**For all tickets:**

- Associated contact via `search_crm_objects` with associations filter → `firstname`, `lastname`, `email`, `phone`.
- Associated company → `name`, `domain`.
- `hs_all_associated_contact_emails` on the ticket is a shortcut if you just need the email.

**Build `ticket_context`:**

```json
{
  "ticket_id": "<id>",
  "ticket_link": "https://app.hubspot.com/contacts/{portal_id}/ticket/{ticket_id}",
  "subject": "<ticket subject>",
  "topic_of_ticket": "<form dropdown value or null>",
  "form_fields": {
    "order_number": "...", "tracking_number": "...",
    "inquiry_description": "...", "supporting_information": "..."
  },
  "has_attachments": true | false,
  "stage": "1" | "3",
  "priority_from_hubspot": "LOW" | "MEDIUM" | "HIGH",
  "contact": { "name": "...", "email": "...", "phone": "..." },
  "company": { "name": "...", "domain": "..." },
  "thread": [{ "from": "customer" | "agent", "email": "...", "date": "...", "body_text": "..." }],
  "latest_customer_message": "<the most recent visitor-from message body>"
}
```

**Flag attachments, never hydrate.** Set `has_attachments: true` when detected; the drafter writes *"I see files were attached — one of our reviewers will look at them before responding."* — never claims to have read them.

#### 2a.5. Hydrate ShipSidekick order state

If `settings.shipsidekick.enabled` is true AND the ticket is likely to benefit from live order data, call `scripts/ssk_order_lookup.py` and inject result into `ticket_context.ssk_state` before drafting.

**Trigger** (OR any):
- `ticket_context.topic_of_ticket` ∈ `settings.shipsidekick.hydrate_topics`
- `ticket_context.form_fields.tracking_number` is non-empty
- `ticket_context.form_fields.order_number` is non-empty AND the body mentions tracking-like terms ("tracking", "shipment", "delivered", carrier name)

Simplest: trigger whenever the ticket has `order_number` OR `tracking_number` on a form ticket. Cost is one GET per matching ticket (<200ms).

**Call:**

```
echo '{"order_number": "<form_fields.order_number>", "tracking_number": "<form_fields.tracking_number>"}' | py scripts/ssk_order_lookup.py
```

**Exit handling:**
- Exit 0 → parse stdout JSON. If `found: true`, set `ticket_context.ssk_state` to the full JSON. If `found: false`, set `ticket_context.ssk_state = {"found": false, "not_found_reason": "<reason>"}` so the drafter can hedge honestly.
- Exit 3 (token missing): log warning, do NOT set `ssk_state`, continue.
- Exit 4 (SSK API error): log error, do NOT set `ssk_state`, continue.
- Exit 2 (bad payload): log and continue without hydration.

Runs BEFORE classification because `ssk_state` can inform classification (e.g., `delivered` status + customer complaint = "delivered but not received" branch of `wismo_tracking`).

#### 2b. Classify

Read `prompts/classify.md`. Fill `{ticket_context}`, `{urgent_signals}`, `{form_topic_mapping}` from `categories.yaml`.

**Form-topic prior:** if `ticket_context.topic_of_ticket` is set, look up in `categories.yaml:form_topic_mapping`:
- `primary` non-null → default category; only override if free-text strongly contradicts (document the override reason).
- `primary` null → use `sub-category candidates` to restrict the LLM's choice.

Expected output:
```json
{
  "category": "<one of the 15 automation categories>",
  "form_topic": "<topic_of_ticket value, null if absent>",
  "priority": "urgent" | "normal",
  "confidence": 0.0-1.0,
  "reason": "<one sentence>"
}
```

If confidence < 0.5, force `category = other_unclassified`.

#### 2c. Retrieve KB context

- Always include `kb/brand_voice.md` and `kb/glossary.md`.
- Always include the KB file matching the category (e.g., `damaged_goods` → `kb/damage_claims.md`). See `kb/categories.md`.
- Cap total KB context to ~4000 tokens; prefer the category-specific file over broad matches.

#### 2d. Draft reply

Read `prompts/draft_reply.md`. Fill `{ticket_context}`, `{kb_context}`, `{category}`. Reply must:

- Open by restating what you understand the issue to be.
- Reference specific identifiers the customer provided (order #, PO#, tracking #).
- Use 3PL vocabulary where appropriate (SKU, ASN, wave, cycle count, disposition, BOL, POD).
- Include next steps or requests for missing info.
- End with the sign-off from `brand_voice.md` (use `— Pack'N Support` if placeholder still in file).
- Never invent rate numbers, SLA commitments, or policies not in KB. If missing, say "let me confirm with the team" + add an action item.

#### 2e. Extract action items

Read `prompts/extract_actions.md`. Output items:

```json
{
  "action_type": "<built-in action-item type>",
  "description": "<what needs doing>",
  "owner_hint": "warehouse" | "account_manager" | "billing" | "integration" | "merchant",
  "blocking_info_needed": ["<missing inputs>"]
}
```

Empty array is valid (pure informational replies have none).

#### 2f. Post the reply (auto-send vs draft routing)

Routing is **runtime**, not per-category. Compute:

```
is_auto_send =
  ticket_context.source_type == "FORM"
  AND ticket_context.topic_of_ticket in categories.yaml:auto_send_form_topics
```

The `auto_send_form_topics` list currently contains: `"Carrier Issue (Provide Tracking)"`, `"Mispack (Provide Order Number)"`. Any other topic, or non-form, falls to the draft path.

Compose an auto-send subject (used in both paths):
- If `ticket_context.subject` is empty, equals `"New ticket created from form submission"`, or otherwise generic (regex match `^\s*new ticket\b`, `\bform submission\b`, or < 10 chars): `Re: {topic_of_ticket short label} — Order #{order_number if present}` (fall back to `Re: {category}`).
- Else: `Re: {ticket_context.subject}`

**Build the ticket snapshot** (both paths). Required keys: `category`, `captured_at` (ISO-8601). Recommended: `subject`, `body`, `contact`, `custom_properties`.

```json
{
  "subject":           "<ticket_context.subject>",
  "body":              "<ticket_context.latest_customer_message>",
  "category":          "<classifier.category>",
  "topic_of_ticket":   "<ticket_context.topic_of_ticket>",
  "source_type":       "<ticket_context.source_type>",
  "contact":           {"name": "...", "email": "..."},
  "custom_properties": {"order_number": "...", "tracking_number": "...", ...},
  "captured_at":       "<run_started_at_utc ISO-8601>"
}
```

**Write the draft** (both auto-send AND draft branches now use the same call — Pack'N OS is the single operator surface):

```bash
py -c "
import json
from packn_os_hubspot_client import client
draft_id = client.write_draft(
    ticket_id='<ticket_id>',
    routine_name='tickets-process',
    draft_body='<drafted reply from step 2d, plain text>',
    model='claude-sonnet-4-5',
    prompt_version='v3.2.1',
    hubspot_ticket_snapshot=<the JSON dict above, dumped via json.dumps()>,
)
print(draft_id)
"
```

- If `dry_run: true` → log the would-be call; do NOT actually call `client.write_draft`. Continue to 2g.
- Else → capture stdout (the new draft_id UUID). Record `posted_as: "draft"` (both branches) and `draft_id` in the run log. Increment `notes_posted`.

**Replay safety:** `automation_drafts_idem_uniq` on `(routine_name, ticket_id, prompt_version)` guarantees re-runs return the same draft_id without duplicating.

**No customer email is sent.** The operator reviews `/automation/drafts/<draft_id>` in Pack'N OS and clicks Approve+send.

**Exception during write_draft** (Postgres unreachable, missing required key) → log full traceback in the run log under this ticket. Increment `auto_send_failures` (legacy counter name; now means "write_draft failed"). Do NOT fall through to a HubSpot note. Continue to 2g.

Do NOT change ticket stage or assignee.

#### 2g. Route action items + queue for digest

**Urgent evaluation:** if `classifier.priority == "urgent"` OR any action item description matches an `urgent_signals` pattern OR any item has `severity == "urgent"`, the ticket is urgent-emailed.

**Urgent solo-email path** (fires immediately):

If `dry_run: false` AND urgent, create a Gmail draft via `mcp__claude_ai_Gmail__create_draft`:

- to: `settings.notify_emails` (all listed recipients)
- subject: `[HubSpot URGENT · Ticket #{id}] {short_summary}`
- body:
  ```
  Priority: URGENT
  Ticket: {subject}
  Customer: {contact_name} @ {company_name}
  HubSpot link: https://app.hubspot.com/contacts/{portal_id}/ticket/{ticket_id}

  Action items:
  1. [{action_type}] {description}
     Owner hint: {owner_hint}
     Blocking info needed: {blocking_info_needed}
     Needs HubSpot reply: {yes/no}

  Classifier reason: {classifier.reason}
  ```

Promote draft → Sent: capture draft id, then `py scripts/send_draft.py <draft_id>`.
- Exit 0 → record sent message id; set `urgent_emailed: true` on queued record.
- Non-zero → log error with `!! URGENT SEND FAILED` marker, include draft id. Set `urgent_emailed: false` AND `urgent_email_send_failed: true` on queued record. Continue.

If `dry_run: true`, log would-be draft only; set `urgent_emailed_dry_run: true` on queued record.

**Queue for digest:**

Queue exactly ONE record to `config/pending_actions.json` when EITHER:
- `posted_as == "draft"` (every drafted ticket — reviewer needs to see it).
- `posted_as == "auto_send"` AND `action_items` non-empty (customer got reply, but backend work needs reviewer).

Skip the queue when `posted_as == "auto_send"` AND `action_items == []`.

Record shape:

```json
{
  "ticket_id": "...",
  "ticket_subject": "...",
  "ticket_link": "https://app.hubspot.com/contacts/{portal_id}/ticket/{ticket_id}",
  "contact": "...",
  "company": "...",
  "category": "<classifier category>",
  "topic_of_ticket": "<form dropdown value, or null>",
  "source_type": "FORM" | "EMAIL" | "CHAT" | ...,
  "posted_as": "draft" | "auto_send",
  "posted_note_id": "<draft_id from 2f. Empty string on post-failure.>",
  "drafted_reply_body": "<full reply text from 2f, no [DRAFT] prefix>",
  "classifier_reason": "<classifier.reason — one sentence>",
  "urgent_emailed": true | false,
  "action_items": [
    {
      "action_type": "...",
      "description": "...",
      "owner_hint": "...",
      "blocking_info_needed": [...],
      "severity": "urgent" | "normal",
      "needs_hubspot_reply": true | false
    }
  ],
  "queued_at": "<ISO timestamp>"
}
```

Increment `tickets_queued_for_digest` (one per queued ticket, not per action item).

#### 2h. Emit row for Sheets export

If `settings.sheets_export.enabled` is true AND the classifier's category matches a tracked rollup, append one row dict to the appropriate buffer. Buffer only — step 3.5 flushes in a single batch.

**Shared field semantics:**

- `first_seen_utc`: use `ticket_context.createdate` (when customer submitted), NOT current time. SLA clocks key off this.
- `customer_name`: `" ".join(x for x in [firstname, lastname] if x)`. Don't emit "Jacob None" or "None Wilson".
- `draft_note_id`: ID of the draft posted in 2f; empty string on dry_run or failure.
- Boolean form fields: emit raw HubSpot string (`"true"`/`"false"`); sync normalizes.
- `requested_credit_usd`: emit as form-captured; sync strips `$`/commas/whitespace and float-parses.

**Mispack rollup** — when `classifier.category == "mispack_wrong_item"`:

```json
{
  "ticket_id": "...", "ticket_link": "...", "first_seen_utc": "...",
  "customer_name": "...", "customer_email": "...", "company_name": "...",
  "order_number": "...", "tracking_number": "...",
  "sku_mentioned": "<SKU identified by classifier/drafter; else empty>",
  "issue_description": "<classifier.reason>",
  "requested_credit_usd": "<form_fields.requested_credit_amount>",
  "reshipment_needed": "<form_fields.check_box_to_reship_order_immediately>",
  "classifier_confidence": "...", "priority": "...", "draft_note_id": "..."
}
```

**Carrier-issue rollup** — when `classifier.category` ∈ `{wismo_tracking, shipping_delay, damaged_goods}`:

```json
{
  "ticket_id": "...", "ticket_link": "...", "first_seen_utc": "...",
  "customer_name": "...", "customer_email": "...", "company_name": "...",
  "order_number": "...", "tracking_number": "...",
  "carrier_inferred": "<see rule below>",
  "carrier_issue": "<form_fields.carrier_issue>",
  "insurance_on_package": "<form_fields.insurance_on_package>",
  "filing_deadline_iso": "<see rule below>",
  "classifier_confidence": "...", "priority": "...", "draft_note_id": "..."
}
```

**`carrier_inferred`**: from `ticket_context.ssk_state.shipments[0].carrier_code` when `ssk_state.found == true`. Otherwise empty — `sheets_sync.py:infer_carrier()` fills from tracking regex at write time.

**`filing_deadline_iso`**: `ship_date + carrier_window_days` when both known. Ship date from `ssk_state.shipments[0].created_at`. Windows: UPS/FedEx/USPS 60, DHL 30, Amazon 30, OnTrac 90, LTL 270, unknown → 60. Emit as `YYYY-MM-DD`; empty if ship date unknown.

Other categories don't emit a row. KPI row is emitted unconditionally in 3.5.

**Never populate operator-owned columns** (`claim_status`, `claim_number`, `coverage_usd`, `investigation_status`, `cost_absorbed_usd`, etc.) — operators fill those later.

### 3. Persist state

After the loop:

1. Update `config/state.json`:
   ```json
   {
     "last_run_at": "<max hs_lastmodifieddate seen this run>",
     "processed_ticket_ids": ["<dedupe; keep last 500>"],
     "run_count": <increment>,
     "ticket_fingerprints": {
       "<ticket_id>": {
         "num_notes": <int>,
         "processed_at": "<ISO>",
         "hs_lastmodifieddate": "<ticket's hs_lastmodifieddate>"
       }
     }
   }
   ```
   MERGE fingerprints — do NOT overwrite the whole object. Skipped tickets keep their old fingerprint.

2. Write `config/pending_actions.json` with the updated queue.

### 3.5. Sync to Google Sheets

If `settings.sheets_export.enabled` is true:

1. `avg_classifier_confidence` = mean of `confidences` (empty string if list empty).
2. `run_duration_sec` = current UTC minus `run_started_at_utc`.
3. Build the KPI row by combining run envelope + counters + avg confidence + duration. Keys match `kpi_system` tab headers exactly.
4. Assemble payload:
   ```json
   {
     "run_id":  "<run envelope run_id>",
     "kpi":     { <KPI row> },
     "mispack": [ <sheets_rows.mispack> ],
     "carrier": [ <sheets_rows.carrier> ]
   }
   ```
5. Write to `outputs/runs/<run_started_at_utc>_sheets_payload.json` (replace `:` with `-` for filesystem safety).
6. Invoke: `py scripts/sheets_sync.py outputs/runs/<timestamp>_sheets_payload.json` — capture stdout/stderr into the run log under "Sheets sync".
7. **Non-blocking semantics:** the script returns exit 0 even on Sheets API outage (writes CSV mirror + queues to `config/sheets_pending_sync.json` for next run). Non-zero exit codes are structural (missing token, missing payload, mirror write failed) — surface in run log but do NOT abort.

Skip this step entirely if sheets_export is disabled.

### 4. Write run log

Create `outputs/runs/<ISO timestamp>.md` with:
- Run summary (counts of tickets seen, classified, drafts posted, urgent emails sent, queued).
- Per-ticket: id + link, category + confidence, priority, action taken (draft posted / dry-run / skip reason), any errors.
- Any MCP errors encountered.

### 4.5. Pack'N OS run-record finish

Decide `status` from counters:
- `success` — at least one ticket processed AND no errors.
- `partial` — at least one ticket processed AND some errors occurred.
- `failure` — zero tickets processed AND `mcp_errors > 0` (run blocked entirely).
- (`skipped` is written by Step 0; never reached here.)

Build `error_summary` (free-text ≤ 8KB; helper truncates last 8000 chars). Include MCP errors, image errors, write_draft failures, urgent-send failures, Sheets sync failures. If no errors, pass `None`.

```bash
py -c "
from packn_os_hubspot_client import client
client.write_run_record(
    routine_name='tickets-process',
    status='<success|partial|failure>',
    error_summary='<free-text or None>',
    tickets_processed=<counters.tickets_processed>,
    drafts_created=<counters.notes_posted + counters.auto_sends>,
    started_at_iso='<run_started_at_utc ISO-8601>',
    finished_at_iso='<now ISO-8601>',
)
"
```

If this shell-out fails (Postgres unreachable), log with `!! RUN_RECORD_WRITE_FAILED` marker and continue. The run log on disk is the durable backup.

### 5. Exit

Tell the user (if interactive) a one-line summary:

`"Processed N tickets — A drafts written, U urgent emails, Q tickets queued for digest."`

Where A = `notes_posted + auto_sends`, U = `urgent_emails_drafted`, Q = `tickets_queued_for_digest`. If `auto_send_failures > 0`, append: `", F write_draft failures."`.

## Output contract

Everything this skill writes outside HubSpot/Gmail MCP calls must land inside the project folder. Never write to the user's home directory or the parent workspace.

## Failure behavior

- **Image-processing error** → immediately stop the current ticket, log `{ticket_id, error: "image_processing_error", skipped: true}`, move to next. Do NOT retry. If errors persist across multiple tickets, abort the run.
- **MCP rate-limit or auth error** → log, stop processing remaining tickets. Update `last_run_at` ONLY to the last successfully-processed ticket's modified date so next run retries the skipped ones. Exit with a clear error message.
- **Gmail auth missing** → skip emails for this run, leave items in queue, surface a message asking the user to run the Gmail auth flow.
- **Unknown category from classifier** → force `other_unclassified` and still draft a reply (generic, flagged for review).
