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

Call `mcp__claude_ai_HubSpot__search_crm_objects`:
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
  "latest_customer_message": "<the most recent visitor-from message body>"
}
```

**Flag attachment presence, never hydrate contents.** Detect attachments by checking whether the ticket body / form fields mention uploaded files or by association count — DO NOT fetch `hs_file_upload`, file URLs, or file contents (see the attachment-handling rule at the top). Set `has_attachments: true` in `ticket_context` when detected. The reply drafter must NOT claim to have read the attachment; it should write: *"I see files were attached to this ticket — one of our reviewers will look at them before responding."* or similar. The draft reviewer opens the HubSpot ticket to inspect attachments before sending.

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

1. Determine the `to_email`:
   - Prefer `ticket_context.contact.email`.
   - Fall back to the first value in `hs_all_associated_contact_emails` if the contact email is empty.
   - If still empty → abort auto-send for this ticket and fall through to the draft path with a run-log note `auto_send_skipped: no_recipient`.
2. Build the payload (stdin JSON for `scripts/send_customer_reply.py`):
   ```json
   {
     "ticket_id":  "<ticket_id>",
     "to_email":   "<resolved recipient>",
     "to_name":    "<contact.name if present, else empty>",
     "subject":    "<composed per rule above>",
     "body_plain": "<drafted reply from step 2d>",
     "body_html":  "<drafted reply rendered as HTML; derived server-side if omitted>",
     "metadata_block": "--- PACKN_METADATA_V1 ---\n<single-line JSON with category, classifier_reason, topic_of_ticket, source_type, posted_as: \"auto_send\", urgent_emailed, action_items>\n--- PACKN_METADATA_END ---"
   }
   ```
   The helper appends `metadata_block` (verbatim) to the end of the `[AUTO-SENT TO CUSTOMER]` note body so the remote digest can reconstruct the action items. Format matches the PACKN_METADATA_V1 block on draft notes.
3. If `dry_run: true` → run `py scripts/send_customer_reply.py` **without** `--send` (it'll print the preview payload). Log the preview in the run log under the ticket's entry.
4. If `dry_run: false` → run `py scripts/send_customer_reply.py --send`. Capture stdout (JSON with `gmail_message_id`, `note_engagement_id`, `email_engagement_id`). On exit code 0, record `posted_as: "auto_send"` plus all three IDs in the run log. Increment counter `auto_sends`.
   - **Why both a note and an email engagement?** HubSpot Help Desk ticket timelines render only conversation-thread messages and note engagements — email engagements are hidden. The email engagement (v1 `type: EMAIL`) is an audit-trail artifact reachable via API / the contact timeline. The note engagement (v1 `type: NOTE`) prefixed `[AUTO-SENT TO CUSTOMER]` is what the reviewer actually sees on the ticket. Both are created by the helper in one run.
5. **Exit code 7 (partial failure: Gmail sent but timeline-visible note failed to post)** → record with a `!! AUTO_SEND_NOTE_FAILED` marker in the run log including the `gmail_message_id`. The customer got the email but the reviewer has no on-ticket record; the operator should add a note manually. Do not re-send. Continue to step 2g.
6. **Exit code 4 (Gmail API error)** → the customer did NOT receive the email. Fall through to the draft path: post as internal note instead. Mark `auto_send_failed: true` in the run log and counter `auto_send_failures`.
7. **Other non-zero exit codes** (2/3/5/6) → log the error with the error message; fall through to the draft path for safety.

**Auto-sent tickets with non-empty `action_items` ARE queued** to `pending_actions.json` so the digest can surface claim-filing, investigation, and other backend work to the reviewer — but with `posted_as: "auto_send"` so the digest skips the "send the drafted reply" reminder (the customer already got the reply). Auto-sent tickets with `action_items == []` are NOT queued — the digest has nothing to add for them.

**Draft path** (`is_auto_send == false`, or auto-send fell through):

- If `dry_run: true` → log what would have been posted to `outputs/runs/<timestamp>.md`.
- Else → use `mcp__claude_ai_HubSpot__manage_crm_objects` to create a note engagement associated to this ticket. The note body has THREE parts, in order:
  1. The `[DRAFT — REVIEW BEFORE SENDING]` header line.
  2. The drafted reply text (verbatim, customer-facing).
  3. A **PACKN_METADATA_V1** block (see below) so the remote digest can reconstruct classifier output + action items without a local queue file.
- Do NOT change ticket stage or assignee.
- Record `posted_as: "draft"` and the `posted_note_id` for step 2h. Increment counter `notes_posted`.

**PACKN_METADATA_V1 block format** (identical for draft notes AND auto-sent notes — this is the canonical embed):

```
--- PACKN_METADATA_V1 ---
{"v":1,"category":"<classifier category>","classifier_reason":"<one-sentence classifier.reason>","topic_of_ticket":"<form value or null>","source_type":"FORM|EMAIL|CHAT|...","posted_as":"draft|auto_send","urgent_emailed":false,"action_items":[{"action_type":"...","description":"...","owner_hint":"...","blocking_info_needed":[...],"severity":"urgent|normal","needs_hubspot_reply":true|false}]}
--- PACKN_METADATA_END ---
```

Rules for the block:
- Emit as a single-line JSON to make regex parsing robust. Escape newlines in strings (`\n`).
- Include even when `action_items == []` — the digest still needs `category`, `classifier_reason`, etc. to group and render.
- Reviewer ignores this block in HubSpot UI; the digest parses it.
- When regenerating a draft (re-processing after new customer note), post a NEW note with a fresh metadata block. Do not edit prior notes.

Handle the "did it succeed" check: if the MCP call returns an error on the draft path, log it in the run log under that ticket's entry and continue. (The digest now reads directly from HubSpot, so a failed note post means that ticket simply won't appear in the next digest — fixable by re-running processing.)

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
