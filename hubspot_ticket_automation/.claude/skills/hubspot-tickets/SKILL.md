---
name: hubspot-tickets
description: Process new/updated HubSpot help desk tickets — classify, draft a reply grounded in the local 3PL KB, then either (a) auto-send the reply to the customer via Gmail + log it as a HubSpot email engagement when the ticket is form-sourced with a Mispack or Carrier Issue topic, or (b) post it as an internal draft note and queue the ticket for the hourly digest for reviewer send. Action items route to either immediate urgent email or the hourly digest queue. Use when the user asks to run the ticket automation, process the ticket queue, or when invoked on cron.
---

# HubSpot Tickets — Processing Skill

You are running Pack'N's help desk ticket automation. Pack'N is a **3PL / fulfillment provider** — treat customers as merchants/brands unless a ticket clearly originates from an end consumer.

## CRITICAL: Attachment and image handling

**Never hydrate customer attachments into the model context.** This is a hard invariant — violating it causes `400 Could not process image` errors that poison the entire conversation and require a /clear to recover.

Specifically:

- **Do NOT request `hs_file_upload`** in any HubSpot MCP `properties` list. It is excluded from `settings.ticket_custom_properties` by design — do not add it back.
- **Do NOT fetch file URLs, file contents, or file IDs** via any MCP tool (HubSpot files API, URL fetchers, `WebFetch`, etc.) for attachments discovered on a ticket.
- **If any MCP response returns image content blocks**, ignore the image payload entirely. Treat the response as text-only. Do not describe, analyze, or re-reference the image in subsequent turns.
- **Detect attachment presence only** by checking whether `hs_num_associated_deals` / associated-files count is non-zero, or by noting that the ticket body mentions an attachment. Record this in `ticket_context.has_attachments: true` and include a reviewer note — nothing more.
- **Draft language when attachments are present**: "Customer indicated files were attached — the draft reviewer should open the HubSpot ticket to view them before sending." Do not write "thanks for the screenshot" or otherwise pretend to have seen the file.

**If you encounter a `Could not process image` or similar image-processing error mid-run:** abandon the current ticket immediately, record the ticket_id in the run log under `skipped_image_error`, and move to the next ticket. Never retry an image-poisoned request.

## Preconditions

Before doing anything, verify:

1. Working directory contains `config/settings.yaml`, `config/categories.yaml`, `config/state.json`, `prompts/`, and `kb/`. If any are missing, stop and tell the user which path is missing.
2. Read `config/settings.yaml`. Note these fields:
   - `dry_run` (bool) — if true, do NOT call any HubSpot write tool or Gmail send tool. Log intended actions only.
   - `per_run_cap` (int) — max tickets to process this run.
   - `pipeline_id` / `active_stages` (list) — filter for tickets needing response. If blank on first run, run the **discovery step** below.
   - `hubspot_portal_id` (int) — used to build ticket links.
   - `urgent_signals` (list of strings/regex) — keywords/phrases that force priority=urgent.
3. Read `config/state.json` to get `last_run_at`. If empty, use the timestamp from 24h ago.

## Discovery step (first run only)

If `pipeline_id` or `active_stages` are empty in `settings.yaml`:

1. Call `mcp__claude_ai_HubSpot__get_crm_objects` with `objectType=tickets`, limit 1, to confirm API access.
2. Call `mcp__claude_ai_HubSpot__search_properties` to list ticket pipelines and stages.
3. Propose values to the user and pause. Do NOT guess pipeline IDs.

## Pack'N OS Control Panel integration (Phase 4)

This skill is now coordinated with **Pack'N OS** (the operator control panel
that sits alongside this automation). At every tick the skill MUST:

1. **Read `automation_routines.enabled` from Pack'N OS BEFORE doing anything**
   (CONTEXT D-01 — cooperative DB-poll pause). If `enabled = false`, write a
   `status='skipped'` run record and exit cleanly. The pause/resume toggle in
   Pack'N OS becomes a real on/off switch via this gate.

2. **Acquire a Redis rate-limit token BEFORE every `mcp__claude_ai_HubSpot__*`
   call** (HUB-07 — cross-process bucket coordination). The cron acquires a
   token from `rate:hubspot:existing-automation` (capacity 3/s); Pack'N OS
   shares the parent budget at `rate:hubspot:packn-os` (capacity 2/s); the
   sum is `≤ 5/s` (HubSpot's account-wide Search cap).

3. **Replace customer-facing auto-send with `client.write_draft(...)`**
   (CONTEXT D-04, D-05 — no shadow mode). For all customer-facing replies
   (the FORM + Mispack/Carrier path that previously hit `scripts/
   send_customer_reply.py`), this skill no longer auto-sends. Instead it
   writes a `pending` row to `automation_drafts`; Pack'N OS owns the
   approve→Gmail+HubSpot send chain. Internal sends (urgent solo emails,
   `/packn-digest` recipient sends) are UNCHANGED.

4. **Write an `automation_runs` row at tick start AND tick finish**
   (CONTEXT D-03 — single source of truth for status panel + alert detection).

5. **Process operator-initiated rerun requests at tick start** (CONTEXT D-09
   — even when paused, operators can re-fetch a single ticket and have the
   skill re-draft against the fresh snapshot). The `automation_rerun_requests`
   table is the queue.

The Pack'N OS helper module that exposes these is at the repo root:
`packn_os_hubspot_client/`. See `packn_os_hubspot_client/README.md` for the
install + env-var + Postgres GRANT setup.

**Insertion points used in this file:**

- Step 0 (NEW, below) — the routine_enabled gate + run_record start +
  rerun_requests poll.
- Step 1 (Fetch candidate tickets) — wraps `mcp__claude_ai_HubSpot__search_
  crm_objects` with a token acquire.
- Step 2a (Hydrate context) — wraps `mcp__claude_ai_HubSpot__search_crm_
  objects` calls (for emails / contact / company) with a token acquire each.
- Step 2f (Post the reply) — BOTH the auto-send branch AND the draft branch
  now invoke `client.write_draft` (Phase 4.1 D-02.a hard-cut). No HubSpot
  note engagement is created for either path; Pack'N OS is the single
  operator surface for both.
- Step 5 (NEW, at end of run) — the run_record finish.

## Step 0: Pack'N OS routine gate + run-record start (NEW — Phase 4)

> **Pre-gate note (2026-07-06):** cron invocations arrive via
> `scripts/run_tickets.sh`, which runs `scripts/pregate_tickets.py` FIRST.
> The pre-gate performs the Step 0a schedule report, the Step 0b routine
> gate, the rerun-queue check, and the Step 1 HubSpot poll deterministically
> — and skips launching this skill entirely when there is no work. If you
> are running, either work likely exists or the pre-gate failed open. Still
> execute Steps 0–1 exactly as written below (manual `/packn-tickets` runs
> bypass the pre-gate, and the double-check is cheap); a zero-ticket result
> here remains a normal clean exit.

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

BEFORE entering the main loop, run this gate. The shell-out exit code
discriminates:

```bash
py -c "from packn_os_hubspot_client import client; import sys; sys.exit(0 if client.read_routine_enabled('tickets-process') else 99)"
```

- **Exit 0** → routine is enabled; proceed to the rerun-queue poll below
  and then to Step 1.
- **Exit 99** → routine is paused. Write a status='skipped' run record:

  ```bash
  py -c "from packn_os_hubspot_client import client; client.write_run_record('tickets-process', 'skipped', None, 0, 0, '<run_started_at_utc ISO>', '<now ISO>')"
  ```

  Then exit the skill cleanly with a one-line note: "Routine paused via
  Pack'N OS — skipping this tick." Do not call any HubSpot MCP. Do not
  fetch tickets. The next cron fire will repeat the gate check.

- **Exit anything else** (DB unreachable, role permission error, network
  timeout) → fail-closed posture per CONTEXT D-01: treat as `skipped` so
  the cron does not auto-send during a Pack'N OS outage. Same shell-out
  for the `status='skipped'` run record (with `error_summary` set to the
  exit-code reason), then exit.

After the gate passes, poll the rerun-request queue. Operators can request
a re-run via Pack'N OS even WHILE the routine is paused (D-09 — paused for
new HubSpot polling, NOT for explicit operator-initiated work). The
following are processed BEFORE the normal HubSpot poll:

```bash
py -c "
import json
from packn_os_hubspot_client import client
rows = client.read_pending_rerun_requests('tickets-process')
print(json.dumps([{'id': str(r['id']), 'ticket_id': r['ticket_id']} for r in rows]))
"
```

For each row in the JSON output:
1. Hydrate ticket context for `ticket_id` (steps 2a + 2a.5 below).
2. Run classify + draft + extract_actions (steps 2b–2e).
3. Call `client.write_draft(...)` with the draft (Step 2f auto-send branch
   below — same path).
4. Mark the rerun processed:
   ```bash
   py -c "from packn_os_hubspot_client import client; client.mark_rerun_processed('<rerun_request_id>', '<draft_id>')"
   ```

Reruns count toward `tickets_processed` + `drafts_created` in the run
record but do NOT consume from `per_run_cap` (operator-initiated work
is always allowed to land regardless of the cron's per-tick budget).

## Main loop

At the start of the run, capture a **run envelope** you'll use for step 3.5:

- `run_id`: generate a UUID4 string.
- `run_started_at_utc`: current UTC ISO timestamp, captured BEFORE the first MCP call.
- `last_run_at_used`: the `last_run_at` value you read from `state.json` (or the 24h-ago fallback).
- `run_mode`: `"dry_run"` if `settings.dry_run` is true, else `"live"`.

Also initialize in-memory counters (all integers starting at 0) and an empty confidence list:

```
counters = {
  tickets_matched, tickets_processed, tickets_skipped_dedupe,
  tickets_skipped_error, notes_posted, auto_sends, auto_send_failures,
  urgent_emails_drafted, tickets_queued_for_digest, image_errors, mcp_errors
}
confidences = []
sheets_rows = { mispack: [], carrier: [] }
```

- `notes_posted` counts only DRAFT notes (path 2f draft branch).
- `auto_sends` counts successful customer-facing auto-sends (path 2f auto-send branch, exit 0).
- `auto_send_failures` counts Gmail-send failures (exit 4) that fell through to the draft path.
- `tickets_queued_for_digest` is one per queued ticket (drafted tickets always; auto-sent tickets when they carry non-empty action_items). Not one per action item — schema is one-record-per-ticket with nested action_items.

Update these as you move through the loop; step 3.5 flushes them to the Sheets workbook.

### 1. Fetch candidate tickets

**BEFORE the call, acquire a HubSpot rate-limit token (Phase 4 HUB-07):**

```bash
py -m packn_os_hubspot_client.rate_limit
```

This blocks until a token is available against
`rate:hubspot:existing-automation` (capacity 3/s; coordinated with
Pack'N OS's `rate:hubspot:packn-os` so the sum is ≤ 5/s — HubSpot's
account-wide Search cap). Exit 0 = token acquired (or degraded to
passthrough on Redis outage — proceeds anyway, better to over-call
than wedge the cron tick). Apply the same shell-out before EVERY
`mcp__claude_ai_HubSpot__*` call below.

Then call `mcp__claude_ai_HubSpot__search_crm_objects`:
- objectType: `tickets`
- filters (all AND'd):
  - `hs_lastmodifieddate > {last_run_at}`
  - `hs_pipeline_stage IN {active_stages}` (typically `["1", "3"]` — New + Waiting on us)
  - `hs_last_message_from_visitor EQ "true"` (only if `settings.require_last_message_from_visitor`)
- properties to return: `subject`, `content`, `hs_pipeline_stage`, `hs_ticket_priority`, `hs_lastmodifieddate`, `createdate`, `source_type`, `hs_last_message_from_visitor`, plus all `settings.ticket_custom_properties` (topic_of_ticket, order_number, tracking_number, inquiry_description, supporting_information, hs_all_associated_contact_emails). **Do NOT include `hs_file_upload` or any other file/attachment property** — see the attachment-handling rule at the top of this file.
- limit: `per_run_cap`
- sorted by `hs_lastmodifieddate` ASCENDING (process oldest-unreplied first)

If zero tickets, write a short run log saying "no new tickets" and exit cleanly.

### 2. For each ticket

Process sequentially. If an error occurs on a ticket, log it and continue with the next — never abort the whole run on a single failure.

#### 2a-pre. Early-exit dedupe check (before hydrating)

Before any hydration or classification, check `state.json:ticket_fingerprints[ticket_id]`:

- If **no fingerprint exists** → this is a first-time (or newly-reset) ticket, proceed to hydration.
- If **a fingerprint exists** AND `current_num_notes <= fingerprint.num_notes` → **skip this ticket without hydrating**. Log as `{ticket_id, skipped: "dedupe_no_new_notes", num_notes: N, last_processed_at: "<ISO>"}` in the run log and continue to the next ticket.
- If **a fingerprint exists** AND `current_num_notes > fingerprint.num_notes` → a new note was added since our last pass (either by us on a prior run that failed to commit, or by a human). Proceed to hydration and re-draft.

**Why this matters:** tickets on which we've only posted an internal note keep `hs_last_message_from_visitor = true` indefinitely (until someone posts a customer-visible reply), so the main-loop filter re-matches them every run. Without this check, each run wastes MCP calls hydrating a ticket we can't meaningfully progress.

**Writing the fingerprint (success path):** after successfully posting a draft (step 2f) — OR after successfully completing the loop for a ticket even if no note was posted (e.g., informational replies with no draft mode) — update `state.json:ticket_fingerprints[ticket_id] = { num_notes: <current_num_notes_plus_posted_notes>, processed_at: "<ISO now>", hs_lastmodifieddate: "<ticket's hs_lastmodifieddate at time of processing>" }`.

**Fingerprint reset**: if a ticket's `hs_last_message_from_visitor` flips back to `true` AFTER being `false` (i.e., customer replied after an agent's public response), the implicit dedupe still triggers if num_notes hasn't changed. In that case, the operator can manually clear the ticket's fingerprint from `state.json` to force re-processing. Document this in run-log observations if you spot the pattern.

#### 2a. Hydrate context

**For each `mcp__claude_ai_HubSpot__*` call in this hydration step (and ALL
HubSpot MCP calls hereafter), acquire a rate-limit token first via the
`py -m packn_os_hubspot_client.rate_limit` shell-out (Phase 4 HUB-07).
The token-bucket protects against bursts above the HubSpot 5/s account-wide
Search cap when Pack'N OS is also making calls in parallel.**

**For form-sourced tickets (`source_type == "FORM"`):**

The message body lives on associated `emails` objects, not on the `content` property.

1. Call `search_crm_objects` on `emails` associated with the ticket:
   - `associatedWith: [{objectType: "tickets", operator: "EQUAL", objectIdValues: [ticket_id]}]`
   - properties: `hs_email_subject`, `hs_email_text`, `hs_email_html`, `hs_email_direction`, `hs_email_from_email`, `hs_email_to_email`, `hs_createdate`
   - sorted by `hs_createdate` ASCENDING
   - limit: `conversation_email_limit` (default 10)
2. For each email, prefer `hs_email_text` if present; else strip HTML tags from `hs_email_html` to plain text.
3. The **first email** on form-sourced tickets is the form submission itself — its body is an HTML blockquote containing the form field values (e.g., "Topic of Ticket: Carrier Issue", "Order Number: 7679500"). Parse this as structured form data.

**For email-sourced tickets (`source_type != "FORM"`):**

Same pattern — pull associated `emails` and sort by date. The `content` property may be populated; if so, use it as a fallback.

**For all tickets:**

- Read associated contact via `search_crm_objects` with associations filter → get `firstname`, `lastname`, `email`, `phone`.
- Read associated company via associations → get `name`, `domain`.
- Note: `hs_all_associated_contact_emails` on the ticket is a quick shortcut if you just need the email.

**Build the ticket_context object:**

```json
{
  "ticket_id": "<id>",
  "ticket_link": "https://app.hubspot.com/contacts/{portal_id}/ticket/{ticket_id}",
  "subject": "<ticket subject>",
  "topic_of_ticket": "<form dropdown value or null>",
  "form_fields": {
    "order_number": "...",
    "tracking_number": "...",
    "inquiry_description": "...",
    "supporting_information": "...",
    "other_form_fields_parsed_from_first_email_blockquote": "..."
  },
  "has_attachments": true | false,
  "stage": "1" | "3",
  "priority_from_hubspot": "LOW" | "MEDIUM" | "HIGH",
  "contact": { "name": "...", "email": "...", "phone": "..." },
  "company": { "name": "...", "domain": "..." },
  "thread": [
    { "from": "customer" | "agent", "email": "...", "date": "...", "body_text": "..." },
    ...
  ],
  "latest_customer_message": "<the most recent visitor-from message body>",
  "related_tickets": [
    {
      "ticket_id": "...",
      "ticket_link": "...",
      "subject": "...",
      "stage": "1" | "3" | "4",
      "createdate": "<ISO>",
      "hs_lastmodifieddate": "<ISO>",
      "topic_of_ticket": "<form value or null>",
      "snippet": "<first ~200 chars of latest email body on that ticket, or null>"
    }
  ]
}
```

**Flag attachment presence, never hydrate contents.** Detect attachments by checking whether the ticket body / form fields mention uploaded files or by association count — DO NOT fetch `hs_file_upload`, file URLs, or file contents (see the attachment-handling rule at the top). Set `has_attachments: true` in `ticket_context` when detected. The reply drafter must NOT claim to have read the attachment; it should write: *"I see files were attached to this ticket — one of our reviewers will look at them before responding."* or similar. The draft reviewer opens the HubSpot ticket to inspect attachments before sending.

#### 2a.4. Hydrate related tickets (cross-ticket continuation context)

If `settings.cross_ticket_lookup.by_order_number.enabled` is true AND
`ticket_context.form_fields.order_number` is non-empty AND non-whitespace,
look up other tickets sharing that order number. This bridges the
two-ticket pattern documented in CLAUDE.md invariant 11: when a customer
re-submits the form (or replies to an auto-sent email) about an order
they've already opened a ticket about, HubSpot creates a new ticket
unlinked from the original. Folding prior tickets into `ticket_context`
lets the drafter acknowledge continuity instead of re-asking for info
already provided.

**Acquire a HubSpot rate-limit token first** (Phase 4 HUB-07):

```bash
py -m packn_os_hubspot_client.rate_limit
```

Then call `mcp__claude_ai_HubSpot__search_crm_objects`:
- objectType: `tickets`
- filters (all AND'd):
  - `order_number EQ "<ticket_context.form_fields.order_number>"`
  - `hs_lastmodifieddate > <now - settings.cross_ticket_lookup.by_order_number.lookback_days>`
  - `hs_object_id NEQ "<ticket_context.ticket_id>"` (exclude self)
  - Stage filter: omit entirely if `include_closed_stages: true`; else apply same `active_stages` filter as the main loop.
- properties to return: `subject`, `hs_pipeline_stage`, `createdate`, `hs_lastmodifieddate`, `topic_of_ticket`
- limit: `settings.cross_ticket_lookup.by_order_number.max_results`
- sorted by `hs_lastmodifieddate` DESCENDING (most recent prior first)

For each match, pull the latest email body via one more
`search_crm_objects` on `emails` (associatedWith the matched ticket,
limit 1, sort `hs_createdate` DESC, properties `hs_email_text` +
`hs_email_html`) to populate `snippet` — keep it to ~200 chars, plain
text (prefer `hs_email_text`; strip HTML tags from `hs_email_html` as
fallback). Each snippet call also requires a fresh rate-limit token
acquire. If the snippet call fails or returns empty, set
`snippet: null` and continue.

Fold results into `ticket_context.related_tickets[]`. If zero matches,
set `related_tickets: []` (empty list, not null) so the drafter
prompt's "if non-empty" check works cleanly.

**Log to run log**: include `related_tickets_found: <count>` per ticket
so we can measure hit rate after a week of runs. If 0 across many
tickets, the lookup is wasted cost and the `cross_ticket_lookup.
by_order_number.enabled` knob in `settings.yaml` can be flipped off.

**On any failure** (MCP error, rate-limit timeout, malformed response,
filter rejected because `order_number` is not a searchable property):
set `related_tickets: []`, log the error against the ticket, continue.
Never abort the ticket's processing — this is enrichment, not a hard
dependency.

#### 2a.5. Hydrate ShipSidekick order state (WISMO / Carrier Issue / Mispack)

If `settings.shipsidekick.enabled` is true AND the ticket is likely to benefit from live order data, call `scripts/ssk_order_lookup.py` to pull the order + shipment state and inject into `ticket_context.ssk_state` before drafting.

**Trigger** (OR any):
- `ticket_context.topic_of_ticket` ∈ `settings.shipsidekick.hydrate_topics`
- `ticket_context.form_fields.tracking_number` is non-empty
- `ticket_context.form_fields.order_number` is non-empty AND the ticket body mentions a tracking-like term ("tracking", "shipment", "delivered", "UPS", "FedEx", "USPS", "DHL") OR the classifier (run ahead-of-time in a quick-pass if needed) returns a category in `settings.shipsidekick.hydrate_categories`.

Simplest implementation: trigger whenever the ticket has an `order_number` OR `tracking_number` on a form ticket. The cost is one GET per matching ticket (<200ms typical) and the payoff is a specific, credible reply.

**Call**:

```
echo '{"order_number": "<form_fields.order_number>", "tracking_number": "<form_fields.tracking_number>"}' | py scripts/ssk_order_lookup.py
```

Pass whichever identifiers are present; the helper prefers `order_number` and falls back to `tracking_number`.

**On exit code 0**:
- Parse stdout JSON.
- If `found: true` → set `ticket_context.ssk_state = <full JSON>`. The drafter will cite it.
- If `found: false` → set `ticket_context.ssk_state = {"found": false, "not_found_reason": "<reason>"}` so the drafter knows the lookup was attempted and can hedge honestly ("I couldn't locate that order in our WMS — can you double-check the number?") rather than going quiet.

**On exit code 3** (token missing): log a warning in the run log under this ticket, do NOT set `ssk_state`, continue processing (drafter will fall back to the pre-SSK behavior).

**On exit code 4** (SSK API error): log the error in the run log, do NOT set `ssk_state`, continue. Do not abort the run.

**On exit code 2** (bad payload): shouldn't happen — if it does, log and continue without hydration.

Note: this step runs BEFORE classification (2b) because the ssk_state can inform classification too — e.g., if SSK says the shipment is `delivered` and the customer is complaining it didn't arrive, that's a strong signal for the `wismo_tracking` / "delivered but not received" branch.

#### 2a.6. Backfill structured identifiers to the HubSpot ticket

**Why this exists:** HubSpot's form-to-ticket-property mapping does NOT populate the native `tracking_number` / `order_number` ticket properties — the values arrive in the first email's HTML blockquote and are parsed into `ticket_context.form_fields.*` (step 2a). Downstream Pack'N OS surfaces (e.g. `/shipments/[tracking]` HubSpot Tickets tile) search by those native properties, so this step backfills them.

**Trigger** (each independently):
- `ticket_context.form_fields.tracking_number` is non-empty AND the ticket's existing `tracking_number` property is null/empty/whitespace
- `ticket_context.form_fields.order_number` is non-empty AND the ticket's existing `order_number` property is null/empty/whitespace

**Action:** acquire a rate-limit token (same pattern as the rest of 2a), then call `mcp__claude_ai_HubSpot__manage_crm_objects` with:

```
objectType: "tickets"
operation: "update"
objectId: "<ticket_id>"
properties: { "tracking_number": "<form_fields.tracking_number>" }   # or order_number, or both in one call if both apply
```

**Idempotency:** the trigger check (only fire when the existing property is null/empty) makes this naturally idempotent — re-runs against the same ticket are no-ops because the property is already set. Do NOT overwrite an existing non-empty value; the operator may have edited it.

**On error:** log the failure against the ticket, continue with classification (2b). This is a best-effort backfill — a write failure here MUST NOT block draft generation. The next ticket the customer touches that re-fires the dedupe path will retry.

**Dry-run:** if `settings.dry_run` is true, log what WOULD be written but do NOT make the HubSpot call. Mirror the existing dry-run posture in 2f.

**No PACKN_METADATA_V1 note needed** — this is a structured-property write, not a customer-visible action, so it does not need to appear in the digest stream.

#### 2b. Classify

Read `prompts/classify.md`. Fill in `{ticket_context}`, `{urgent_signals}`, and `{form_topic_mapping}` from `categories.yaml`.

**Form-topic prior:** if `ticket_context.topic_of_ticket` is set, look it up in `categories.yaml:form_topic_mapping`:
- If `primary` is non-null, that's the default category. Only override if the free-text evidence strongly contradicts (e.g., topic says "Carrier Issue" but body is clearly a billing question). The classifier should document the override reason.
- If `primary` is null, use the `sub-category candidates` to restrict the LLM's choice to those categories.

Expected classifier output:
```json
{
  "category": "<one of the 15 automation categories>",
  "form_topic": "<topic_of_ticket value, null if absent>",
  "priority": "urgent" | "normal",
  "confidence": 0.0-1.0,
  "reason": "<one sentence>"
}
```

If confidence < 0.5, force `category = other_unclassified` regardless of the model's initial guess.

#### 2c. Retrieve KB context

- Always include `kb/brand_voice.md` and `kb/glossary.md`.
- Always include the KB file that matches the category (e.g., `damaged_goods` → `kb/damage_claims.md`). See mapping in `kb/categories.md`.
- If relevant, also grep `kb/**/*.md` for any domain terms mentioned in the ticket (SKU, carrier name, retailer name).
- Cap total KB context to ~4000 tokens; prefer the category-specific file over broad matches.

#### 2d. Draft reply

Read `prompts/draft_reply.md`. Fill in `{ticket_context}`, `{kb_context}`, `{category}`. Produce a reply that:

- Opens by restating what you understand the issue to be.
- References specific identifiers the customer provided (order #, PO#, tracking #).
- Uses 3PL vocabulary where appropriate (SKU, ASN, wave, cycle count, disposition, BOL, POD).
- Includes next steps or requests for missing info.
- Ends with a standard sign-off from `brand_voice.md` (until Pack'N-specific sign-off is filled in, use a neutral placeholder).
- Never invents facts (rate numbers, SLA commitments, policies not in the KB). If the KB lacks it, say "let me confirm with the team" and add a corresponding action item.

#### 2e. Extract action items

Read `prompts/extract_actions.md`. Output one or more action items each shaped like:
```json
{
  "action_type": "<one of the built-in action-item types>",
  "description": "<what needs doing>",
  "owner_hint": "warehouse" | "account_manager" | "billing" | "integration" | "merchant",
  "blocking_info_needed": ["<list of missing inputs, if any>"]
}
```

An empty array is valid — not every ticket needs an action item (pure informational replies may have none).

#### 2f. Post the reply (routing: auto-send vs draft)

Routing is **runtime**, not per-category. Compute:

```
is_auto_send =
  ticket_context.source_type == "FORM"
  AND ticket_context.topic_of_ticket in categories.yaml:auto_send_form_topics
```

The `auto_send_form_topics` list in `categories.yaml` currently contains:
- `"Carrier Issue (Provide Tracking)"`
- `"Mispack (Provide Order Number)"`

Any ticket that is non-form, or form with any other topic (Operations Inquiry, General Inquiry, Other), falls to the draft path.

Also compose an **auto-send subject** (used in both paths so the queued record has it):

- If `ticket_context.subject` is empty, equals `"New ticket created from form submission"`, or otherwise looks generic (regex match `^\s*new ticket\b`, `\bform submission\b`, or < 10 chars), construct:
  `Re: {topic_of_ticket short label} — Order #{order_number if present}` (fall back to `Re: {category}` if order_number missing)
- Else: `Re: {ticket_context.subject}`

**Auto-send path** (`is_auto_send == true`):

> **Phase 4 cutover (CONTEXT D-04, D-05 — no shadow mode):** customer-facing
> auto-send is REPLACED with a Pack'N OS draft write. The legacy
> `scripts/send_customer_reply.py` invocation is no longer used for
> customer-facing replies; Pack'N OS owns the approve→Gmail+HubSpot send
> chain after the operator reviews the draft. The skill no longer auto-sends
> customer replies. Internal sends (urgent solo emails to lconner+chansen,
> `/packn-digest` recipient sends) are UNCHANGED — those still use Gmail
> directly.

1. Build the snapshot of the HubSpot ticket fields the draft was generated
   against (CONTEXT D-05 — `hubspot_ticket_snapshot` JSONB on
   `automation_drafts`). Required keys: `category`, `captured_at` (ISO-8601);
   recommended: `subject`, `body`, `contact`, `custom_properties`.

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

2. Acquire a HubSpot rate-limit token (the `client.write_draft` call itself
   does not need one — it's a Postgres write — but defensively re-acquire
   anyway in case any post-draft MCP call follows in this iteration):

   *(no token required for Postgres writes; skip the rate_limit shellout)*

3. Call `client.write_draft` to INSERT a `pending` row into
   `automation_drafts`. Idempotency triple is `(tenant_id, routine_name,
   ticket_id, prompt_version)` — replays return the existing draft_id, so
   it's safe to re-run the cron over a ticket that's already been drafted
   in a prior tick.

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
       hubspot_ticket_snapshot=<the JSON dict from step 1, dumped via json.dumps()>,
   )
   print(draft_id)
   "
   ```

4. If `dry_run: true` → log the would-be call (full payload) in the run log;
   do NOT actually call `client.write_draft`. Continue to step 2g for action
   items + digest queue.

5. If `dry_run: false` → run the shell-out above. Capture stdout (the new
   draft_id, a UUID string). On exit 0, record `posted_as: "draft"` and
   `draft_id` in the run log. Increment counter `notes_posted`.

   - **Replay safety (CONTEXT D-05):** the unique constraint
     `automation_drafts_idem_uniq` guarantees the same `(routine_name,
     ticket_id, prompt_version)` triple INSERTs at most one row. A
     re-fired cron tick over the same un-resolved ticket returns the
     existing draft_id without writing a duplicate.
   - **No customer email is sent.** The operator reviews the draft via
     `/automation/drafts/<draft_id>` in Pack'N OS and clicks Approve+send;
     Pack'N OS then sends via its own Gmail OAuth + writes a HubSpot
     engagement note (Phase 2 D-31, D-38).

6. **Exception during write_draft** (e.g., Postgres unreachable, ValueError
   from missing `category` or `captured_at` in the snapshot) → log the
   error with full traceback in the run log under this ticket's entry.
   Counter `auto_send_failures` (kept for legacy semantics — now means
   "write_draft failed"). Do NOT fall through to a HubSpot internal note
   — the operator surface for this is the run-log + the eventual
   `automation_runs.error_summary` field. Continue to step 2g.

**Auto-sent tickets with non-empty `action_items` ARE queued** to
`pending_actions.json` so the digest can surface claim-filing,
investigation, and other backend work to the reviewer. With Phase 4
cutover the customer reply is no longer sent automatically (it's a
draft) — but the operator-facing action items still need to land in the
digest. Set `posted_as: "draft"` on the queued record (since the
customer-facing reply is now in `automation_drafts`, awaiting operator
approval). Auto-sent tickets with `action_items == []` are NOT queued —
the digest has nothing to add for them, and the draft itself is the
operator's surface in Pack'N OS.

**Draft path** (`is_auto_send == false`, or auto-send fell through):

- If `dry_run: true` → log what would have been posted to `outputs/runs/<timestamp>.md`.
- Else → use `py -c` to invoke `packn_os_hubspot_client.client.write_draft` with the ticket snapshot. This writes a row to Pack'N OS `automation_drafts` with `state='pending'`; the OS-side approval queue picks it up at `/automation/drafts`. **The draft branch NO LONGER creates a HubSpot draft engagement note.** The Pack'N OS UI is the operator surface (Phase 4 D-04 — ALL customer replies now in automation_drafts; Phase 4.1 D-02 closes the partial cutover).

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
      hubspot_ticket_snapshot=<the JSON dict from the auto-send branch step 1, dumped via json.dumps()>,
  )
  print(draft_id)
  "
  ```

- Build the snapshot dict the same way the auto-send branch above does (step 1) — required keys `category` + `captured_at`; recommended `subject`, `body`, `contact`, `custom_properties`. The draft branch uses the same snapshot shape so both branches feed automation_drafts uniformly.
- Do NOT change ticket stage or assignee. **Do NOT create a HubSpot note engagement for the draft (Phase 4.1 D-02.a hard-cut).**
- Action items continue to be appended to `config/pending_actions.json` per the existing pipeline (option B per Phase 4.1 D-02 RESEARCH Open Question 5). The digest JOINs by `ticket_id`.
- Record `posted_as: "draft"` and the returned `draft_id` (NOT a `posted_note_id` — there's no HubSpot engagement anymore) for step 2h. Increment counter `notes_posted`.

**Per Phase 4.1 D-02.b:** the `hubspot-actions-digest` skill now reads pending drafts via `client.read_pending_drafts(...)` directly from `automation_drafts`. The PACKN_METADATA_V1 block is no longer emitted (the digest reads structured columns from the DB instead).

**Exception during write_draft** (e.g., Postgres unreachable, ValueError from missing `category` or `captured_at` in the snapshot) → log the error with full traceback in the run log under this ticket's entry. Do NOT fall back to a HubSpot internal note — the operator surface for this is the run-log + the eventual `automation_runs.error_summary` field. Continue to step 2g.

**Legacy note:** prior to the Phase 4.1 D-02.a hard-cut, the draft branch posted a HubSpot note engagement containing a `PACKN_METADATA_V1` block that the digest scraped. Neither the block nor the engagement is emitted anymore; the digest reads from `automation_drafts` directly. (Full legacy block format: git history of this file, pre-2026-07-06.)

#### 2g. Route action items + queue for digest

**Key shape from prior versions:** the digest queue is **one record per ticket** (not one per action item), with nested `action_items[]` and the full `drafted_reply_body`. Drafted tickets are always queued, even when `action_items == []`, so the digest surfaces every ticket the reviewer needs to send. Auto-sent tickets are queued ONLY when they carry backend action items (e.g., `file_carrier_claim`) — the customer already received the reply, but the reviewer still needs a handle on the follow-up work. See the routing rules in the **Queue for digest** subsection below.

**Urgent evaluation (unchanged trigger, additive queuing):**

Evaluate urgency across the action-item set: if `classifier.priority == "urgent"` OR any action item's description matches any `urgent_signals` pattern OR any item has `severity == "urgent"`, the ticket is urgent-emailed.

**Urgent solo-email path** (unchanged from prior behavior — still fires immediately):

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
  - Promote draft → Sent: capture the draft `id` from the MCP response, then run `py scripts/send_draft.py <draft_id>`.
    - Exit 0 → record the sent message id in the run log; set `urgent_emailed: true` on the queued record below.
    - Non-zero exit → log the error in the run log under this ticket's entry with a `!! URGENT SEND FAILED` marker, include the draft id. Set `urgent_emailed: false` and add an `urgent_email_send_failed: true` flag on the queued record. Continue processing.
  - If `dry_run: true`, log the would-be draft in the run log; do not call the send helper. Set `urgent_emailed: true` only on a real send; in dry_run, mark `urgent_emailed_dry_run: true`.

**Queue for digest:**

Queue exactly ONE record to `config/pending_actions.json` when EITHER of these holds:
- `posted_as == "draft"` — any non-auto-send ticket, whether or not `action_items` is empty (reviewer still needs to see the draft).
- `posted_as == "auto_send"` AND `action_items` is non-empty — customer already got the reply, but backend work (carrier claim, warehouse investigation, etc.) still needs reviewer attention. The digest renders these under a "Auto-sent — follow-up action needed" header and suppresses the reply-reminder.

Skip the queue entirely when `posted_as == "auto_send"` AND `action_items == []` — nothing to surface.

The record shape is identical in both cases:

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
  "posted_note_id": "<for draft: engagement id from 2f. For auto_send: the note_engagement_id from send_customer_reply.py (the [AUTO-SENT TO CUSTOMER] note). Empty string on post-failure.>",
  "drafted_reply_body": "<the full reply text posted in 2f, no [DRAFT] prefix>",
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

Increment counter `tickets_queued_for_digest` (one per queued ticket, regardless of action_items count).

For auto-send tickets with empty `action_items`: log the auto-send in the run log only and do NOT queue.

#### 2g.5. Action items reach Pack'N OS /tasks via a deterministic post-run step — do NOTHING here

Forwarding `action_items` to the Pack'N OS `/tasks` hub (the Phase 15.1 cutover bridge that
replaces the legacy action-item email digest) is handled **deterministically by
`scripts/forward_action_items.py`, which the cron runs AUTOMATICALLY after this skill** (the
crontab calls `scripts/run_tickets.sh`, which runs `/packn-tickets` then the forwarder). The
forwarder reads the `config/pending_actions.json` records you wrote in step 2g and POSTs each
ticket's non-empty `action_items` to the OS ingestion route via `scripts/post_action_items.py`.

**You (the agent) do NOTHING in this step.** Specifically:

- Do **NOT** run `post_action_items.py`, `forward_action_items.py`, or any `--send` command here.
  The deterministic forwarder owns the POST. (Running it here too would be a harmless no-op — the
  OS dedups — but it is not your job and wastes a turn.)
- Do **NOT** claim in your run summary that action items were "posted to Pack'N OS /tasks",
  "sent to /tasks", or "forwarded". You CANNOT know the result — the forwarder runs AFTER you
  exit. Stating success here is exactly how this cutover silently failed for days (2026-06-04):
  the agentic run narrated "all action items posted to /tasks" while never POSTing, and 0 tasks
  landed. The source of truth for what reached /tasks is the forwarder's own
  `[forward_action_items] ...` line in `cron-tickets.log`, NOT your summary.

Your ONLY responsibility for the /tasks bridge is upstream: ensure step 2g's
`config/pending_actions.json` write is complete and accurate (correct `ticket_id` + the full
`action_items` array per the record shape above). The deterministic forwarder does the rest.

Why deterministic: a forward step buried mid-loop in an agentic run is unreliable — it was
skipped silently for the entire parallel-run window. A plain script draining
`pending_actions.json` after the run guarantees delivery and surfaces real POST errors
(secret/url/zod/network) in the cron log instead of swallowing them behind a false "posted" claim.

**`ticket_closed` auto-resolve (D-15) — DEFERRED for the parallel run.** The OS route also accepts a
`ticket_closed` signal that auto-resolves a ticket's open action-item tasks. This skill only processes
active-stage tickets (stages 1 + 3; stage 4 = Closed is filtered out at fetch in Step 1), so it does not
currently observe closes. Until a close-detection pass is added (follow-up), operators resolve completed
action-item tasks directly in `/tasks`. The `post_ticket_closed(...)` helper + CLI already exist and are
ready to wire when that pass lands.

#### 2h. Emit structured row for Sheets export

If `settings.sheets_export.enabled` is true AND the classifier's category matches a tracked rollup, append one row dict to the appropriate in-memory buffer. Use values from `ticket_context`, `classifier`, and the `posted_note_id` from step 2f (empty string on dry_run). **Do NOT** call any Sheets MCP or script here — buffer only; step 3.5 flushes in a single batch.

**Shared field semantics** (apply to both rollups below):

- `first_seen_utc`: use `ticket_context.createdate` (when the *customer submitted* the ticket), NOT the current time. SLA claim clocks key off this.
- `customer_name`: `" ".join(x for x in [firstname, lastname] if x)`. HubSpot contacts frequently have only one populated — don't emit "Jacob None" or "None Wilson".
- `draft_note_id`: the ID of the note posted in step 2f; empty string on dry_run or post failure.
- Boolean form fields (`reshipment_needed`, `insurance_on_package`): emit as the raw string HubSpot returns (`"true"` / `"false"`) — the sync script normalizes to `TRUE`/`FALSE` in the sheet.
- `requested_credit_usd`: emit as whatever the form captured; sync strips `$`/commas/whitespace and float-parses so the sheet cell is a real number.

**Mispack rollup** — only when `classifier.category == "mispack_wrong_item"`. Append to `sheets_rows.mispack`:

```json
{
  "ticket_id":             "<ticket_context.ticket_id>",
  "ticket_link":           "<ticket_context.ticket_link>",
  "first_seen_utc":        "<ticket_context.createdate>",
  "customer_name":         "<per shared-field semantics above>",
  "customer_email":        "<ticket_context.contact.email>",
  "company_name":          "<ticket_context.company.name>",
  "order_number":          "<ticket_context.form_fields.order_number>",
  "tracking_number":       "<ticket_context.form_fields.tracking_number>",
  "sku_mentioned":         "<any SKU identified by classifier/drafter; else empty>",
  "issue_description":     "<classifier.reason (one sentence from 2b)>",
  "requested_credit_usd":  "<ticket_context.form_fields.requested_credit_amount>",
  "reshipment_needed":     "<ticket_context.form_fields.check_box_to_reship_order_immediately>",
  "classifier_confidence": "<classifier.confidence>",
  "priority":              "<classifier.priority>",
  "draft_note_id":         "<posted_note_id from 2f>"
}
```

**Carrier-issue rollup** — only when `classifier.category` is one of `wismo_tracking`, `shipping_delay`, `damaged_goods`. Append to `sheets_rows.carrier`:

```json
{
  "ticket_id":             "<...>",
  "ticket_link":           "<...>",
  "first_seen_utc":        "<ticket_context.createdate>",
  "customer_name":         "<per shared-field semantics above>",
  "customer_email":        "<...>",
  "company_name":          "<...>",
  "order_number":          "<ticket_context.form_fields.order_number>",
  "tracking_number":       "<ticket_context.form_fields.tracking_number>",
  "carrier_inferred":      "<see rule below; empty string when unknown>",
  "carrier_issue":         "<ticket_context.form_fields.carrier_issue>",
  "insurance_on_package":  "<ticket_context.form_fields.insurance_on_package>",
  "filing_deadline_iso":   "<see rule below; empty string when ship_date is unknown>",
  "classifier_confidence": "<...>",
  "priority":              "<...>",
  "draft_note_id":         "<...>"
}
```

**`carrier_inferred`**: set from `ticket_context.ssk_state.shipments[0].carrier_code` when `ssk_state.found == true` and a shipment is present (high-confidence canonical source). Otherwise emit empty string — `scripts/sheets_sync.py:infer_carrier()` fills it from `tracking_number` regex at write time as fallback.

**`filing_deadline_iso`**: compute as `ship_date + carrier_window_days` when both are known. Ship date comes from `ssk_state.shipments[0].created_at` (date portion). Carrier windows: UPS/FedEx/USPS 60 days, DHL 30 days, Amazon 30, OnTrac 90, LTL 270 days, unknown carrier → 60 (conservative). Emit as ISO date string (`YYYY-MM-DD`). If ship date is not known, emit empty string — the digest will approximate from `first_seen_utc` at render time and flag with a `~` prefix.

**Categories outside these rollups** (e.g. returns_rma, billing_invoice, address_recipient) do not emit a row. The KPI row is emitted unconditionally in step 3.5.

**Never populate operator-owned columns** (`claim_status`, `claim_number`, `coverage_usd`, `investigation_status`, `cost_absorbed_usd`, etc.). The sync script writes them as empty strings on first insert; operators fill them in later. Even if you notice the customer mentioned a claim number in the body, do not drop it into the `claim_number` field here.

### 3. Persist state

After the loop:

1. Update `config/state.json`:
   ```json
   {
     "last_run_at": "<max hs_lastmodifieddate seen this run>",
     "processed_ticket_ids": ["<appended from this run, dedupe, keep last 500>"],
     "run_count": <increment>,
     "ticket_fingerprints": {
       "<ticket_id>": {
         "num_notes": <int; num_notes on the ticket after this run's note post>,
         "processed_at": "<ISO timestamp of this run>",
         "hs_lastmodifieddate": "<ticket's hs_lastmodifieddate at time of processing>"
       }
     }
   }
   ```
   `ticket_fingerprints` drives the early-exit dedupe check in step 2a-pre. Write one entry per ticket successfully processed this run; preserve entries from prior runs (do not overwrite the whole object — merge). Skipped tickets do NOT get a fresh fingerprint (the old one, if any, stays in place).
2. Write `config/pending_actions.json` with the updated queue.

### 3.5. Sync to Google Sheets

If `settings.sheets_export.enabled` is true:

1. Compute `avg_classifier_confidence`: mean of `confidences` list, or empty string if the list is empty.

2. Compute `run_duration_sec`: current UTC minus `run_started_at_utc` from the run envelope, as a float.

3. Build the KPI row (one object) by combining the run envelope, counters, avg confidence, and duration — keys match the `kpi_system` tab headers exactly.

4. Assemble the payload:

    ```json
    {
      "run_id":  "<run envelope run_id>",
      "kpi":     { <the KPI row you just built> },
      "mispack": [ <contents of sheets_rows.mispack> ],
      "carrier": [ <contents of sheets_rows.carrier> ]
    }
    ```

5. Write the payload to `outputs/runs/<run_started_at_utc>_sheets_payload.json` (same ISO timestamp prefix as the run log in step 4 — use a filesystem-safe variant: replace `:` with `-`).

6. Invoke the sync helper:

    ```
    py scripts/sheets_sync.py outputs/runs/<timestamp>_sheets_payload.json
    ```

    Capture stdout and stderr. Record both under a "Sheets sync" section in the run log (step 4).

7. **Non-blocking semantics** — the script deliberately returns exit code 0 even when the Sheets API is unreachable; in that case it writes the local CSV mirror under `outputs/kpi/` and queues the failed payload in `config/sheets_pending_sync.json` for the next run to retry.

    Non-zero exit codes are structural (missing token, missing payload file, local mirror write failed) — surface those prominently in the run log. Do NOT treat them as a reason to abort the overall skill run; state is already persisted by step 3.

8. If sheets_export is disabled in settings, skip this step entirely.

### 4. Write run log

Create `outputs/runs/<ISO timestamp>.md` with:
- Run summary (count of tickets seen, classified, notes posted, urgent emails sent, normal items queued).
- Per-ticket: ticket id + link, category + confidence, priority, what was done (note posted / dry-run only), any errors.
- Any MCP errors encountered.

### 4.5. Pack'N OS run-record finish (NEW — Phase 4)

After the run log is written, persist the tick's outcome to the Pack'N OS
`automation_runs` table. This row is the source of truth for the OS-side
status panel + alert detection (CONTEXT D-03, D-13).

Decide `status` based on counters:
- `success` — at least one ticket processed AND no MCP errors AND no
  image_errors AND no auto_send_failures.
- `partial` — at least one ticket processed AND ANY of the above non-fatal
  errors occurred (some tickets succeeded, some hit issues).
- `failure` — zero tickets processed AND `mcp_errors > 0` (the run was
  blocked entirely by MCP / auth / rate-limit issues).
- (`skipped` is written by Step 0 BEFORE the main loop runs — never reached
  here, since we only get to Step 4.5 if the routine was enabled.)

Build `error_summary` (free-text, ≤ 8KB; the helper truncates the LAST
8000 chars on overflow per UI-SPEC discretionary item 9). Include any
MCP errors, image errors, write_draft failures, urgent-send failures,
and Sheets sync failures observed during this run. If no errors, pass
`None`.

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

`drafts_created` is the sum of `notes_posted + auto_sends` because both
counters represent draft writes in the Phase 4 model (`auto_sends` is
the legacy name retained for backward compatibility — every former
auto-send is now a draft per CONTEXT D-04).

If the `client.write_run_record` shell-out itself fails (e.g., Postgres
unreachable), log the failure to the run log with a `!! RUN_RECORD_WRITE_
FAILED` marker and continue to Step 5. The run log on the local
filesystem is the durable backup; Pack'N OS will surface a "no run
record in last 60 min" alert via the alert-scan worker if this happens
repeatedly.

### 5. Exit

Tell the user (if running interactively) a one-line summary:
"Processed N tickets — A auto-sent to customer, M drafts posted, U urgent emails, Q tickets queued for digest."
(Where A = `auto_sends`, M = `notes_posted`, U = `urgent_emails_drafted`, Q = `tickets_queued_for_digest`. If `auto_send_failures > 0`, append: ", F auto-send failures fell back to draft.")

## Output contract

Everything this skill writes outside of HubSpot/Gmail MCP calls must land inside the project folder. Never write to the user's home directory or the parent workspace.

## Failure behavior

- **Image-processing error** (`Could not process image`, 400 from the API, or any vision-related error) → **immediately** stop processing the current ticket, record `{ticket_id, error: "image_processing_error", skipped: true}` in the run log, move to the next ticket. Do NOT retry. Do NOT re-read the offending tool result. If errors persist across multiple tickets, abort the run entirely — a property or tool is leaking image content and needs fixing before any further runs.
- MCP rate-limit or auth error → log in run log, stop processing remaining tickets this run, update `last_run_at` ONLY to the last successfully-processed ticket's modified date (so the next run retries the skipped ones), exit with a clear error message.
- Gmail auth missing → skip emails for this run, leave items in queue, surface a message asking the user to run the Gmail auth flow.
- Unknown category from classifier → force `other_unclassified` and still draft a reply (it'll be generic and flagged for review).
