---
name: hubspot-actions-digest
description: Compose and send a reviewer digest email of all HubSpot tickets with drafted (or auto-sent backend-action) replies not yet digested. Reads directly from HubSpot via MCP (no local state file). Sends via Gmail using the local OAuth helper (scripts/send_digest_email.py) so the email lands in reviewer inboxes, not drafts. Designed to run on the server under cron at 8am/12pm/3pm ET M-F, also invokable manually.
---

# HubSpot Digest Skill — HubSpot-first (no local state)

This skill runs **on the server** under cron (`/packn-digest` at 8am/12pm/3pm ET M-F). It does NOT read `config/pending_actions.json` or any local queue file. The single source of truth for the queue is the set of note engagements on HubSpot tickets posted by the `hubspot-tickets` skill, each carrying a `PACKN_METADATA_V1` block with the category, classifier reason, and action items.

Sends the composed digest via Gmail using `scripts/send_digest_email.py` (local OAuth, same token as auto-send). The From line is `Pack'N Customer Care <customercare@gopackn.com>` via the Gmail "Send mail as" alias — matches the sender identity used for customer auto-sends.

## Required MCP connectors

- `mcp__claude_ai_HubSpot__*` (search + manage_crm_objects)

No Gmail MCP dependency — the skill sends through the local Python helper, not through an MCP connector.

## Inputs read from the repo

- `config/settings.yaml` — `notify_emails` (list), `hubspot_portal_id`, `hubspot_timezone`.
- `config/categories.yaml` — `category_owners` (map: category → "luca" | "charlie"), `suppress_reply_reminder_categories` (list).

## Flow

### 1. Fetch candidate notes from HubSpot

Call `mcp__claude_ai_HubSpot__search_crm_objects` with:

- `objectType: notes`
- Filter: `hs_note_body CONTAINS_TOKEN "PACKN_METADATA_V1"` (only notes posted by our automation carry this token).
- Filter: `hs_timestamp > now - 48h` (two-day lookback — wider than any single digest gap to catch anything missed. The `[DIGESTED]` marker check below de-dupes.)
- `properties`: `hs_note_body`, `hs_timestamp`, `hs_createdate`
- `limit`: 200 (one page; if hit, raise a warning — Pack'N volume shouldn't need pagination for a single digest window)
- `sorts`: `hs_timestamp` ascending

For each note returned, also fetch its `tickets` association (via `associatedWith` or a follow-up `search_crm_objects` with `hs_engagement_associations.ticket`). Discard any note with zero ticket associations.

### 2. De-dupe against `[DIGESTED]` markers

For each candidate note's associated ticket, look up any sibling notes on that ticket:

- `objectType: notes`
- `associatedWith: [{objectType: "tickets", operator: "EQUAL", objectIdValues: [<ticket_id>]}]`
- `properties: hs_note_body, hs_timestamp`
- `limit: 50`

If ANY sibling note has `hs_note_body` starting with `[DIGESTED at ` AND `hs_timestamp > <candidate_note.hs_timestamp>`, that ticket has already been digested since this candidate was created — SKIP.

Otherwise, keep the ticket as an active queue entry.

### 3. Hydrate ticket + contact for display

For each surviving ticket, fetch:

- Ticket properties: `subject`, `source_type`, `topic_of_ticket`, `order_number`, `tracking_number` (for context).
- Associated contact (first): `firstname`, `lastname`, `email`.
- Associated company (first, if any): `name`.

Use a single batched `get_crm_objects` per object type to keep MCP calls low.

### 4. Parse the PACKN_METADATA_V1 block from each note

For each kept note, extract the block between lines:

```
--- PACKN_METADATA_V1 ---
<single-line JSON>
--- PACKN_METADATA_END ---
```

Parse the JSON. If parsing fails, log a warning and SKIP the ticket (back-compat with any malformed note).

Extract the drafted reply body as the text BETWEEN the header line (`[DRAFT — REVIEW BEFORE SENDING]` or `[AUTO-SENT TO CUSTOMER] ...`) and the `PACKN_METADATA_V1` block. Preserve line breaks.

### 5. Build the per-ticket record

Assemble one record per ticket with the fields the digest body needs:

```
{
  ticket_id, ticket_subject, ticket_link, contact, company,
  category, topic_of_ticket, source_type, posted_as,
  posted_note_id, drafted_reply_body, classifier_reason,
  urgent_emailed, action_items[], queued_at (note.hs_timestamp)
}
```

Sort records by (`owner` per step 6), then by `category`, then by `queued_at` ascending within each category.

### 6. Compose the digest (owner split + suppression rules)

**Split into owner sections** using `config/categories.yaml:category_owners`:

- Lookup `category_owners[record.category]`. If found → `"luca"` or `"charlie"`. If missing → default `"charlie"` and include an inline warning: `⚠ Category "{category}" has no owner mapping — defaulted to Charlie. Update config/categories.yaml.`
- Compute `L = count of luca records`, `C = count of charlie records`, `N = L + C`.

**Subject**: `[HubSpot Digest] {N} tickets to review ({L} Luca · {C} Charlie) — {hour_window}`

- `hour_window` = YYYY-MM-DD HH:00–HH:59 in `settings.hubspot_timezone` (America/New_York) based on the earliest and latest `queued_at`. If all records are from the same hour use that single hour; otherwise emit the span (`HH:00–HH:59` of the latest hour).

**Body** — identical format to the prior local-file version:

```
{N} tickets need review this hour — {L} for Luca, {C} for Charlie.

##### LUCA — billing, account, escalation ({L}) #####

=== {category_1} ({count}) ===

1. Ticket #{ticket_id} · {source_type} · topic: {topic_of_ticket or "—"}
   {ticket_subject}
   From: {contact} @ {company}
   {if urgent_emailed: "⚠ Urgent solo email was already sent for this ticket."}
   {if posted_as == "auto_send": "✓ Auto-sent reply — customer already received. Follow up on backend items below."}

   {reply_header}:
   ---
   {drafted_reply_body}
   ---

   Action items:
   [ ] Send the drafted reply in HubSpot (edit first if needed).     ← suppressed if posted_as == "auto_send" OR category ∈ suppress_reply_reminder_categories
   [ ] [{action_type}] {description}
       Owner: {owner_hint} · Missing info: {blocking_info_needed or "none"}
       Needs HubSpot reply: {yes/no}
   ...

   Link: {ticket_link}
   Classifier: {classifier_reason}

2. ...

=== {category_2} ({count}) ===
...

##### CHARLIE — warehouse + general ({C}) #####

=== {category_1} ({count}) ===

1. Ticket ...

---
Generated {timestamp} by hubspot-actions-digest (remote).
```

**Formatting rules** (preserved from the prior version):

- LUCA section first, CHARLIE second. Skip a section header entirely if its count is 0 (adjust the opening line: e.g., "3 tickets need review — all for Charlie.").
- `{reply_header}`: `Drafted reply (posted as internal note — reply in HubSpot to send)` when `posted_as == "draft"`, `Auto-sent reply (customer already received — shown for context)` when `posted_as == "auto_send"`.
- **Suppress** the `[ ] Send the drafted reply in HubSpot (edit first if needed).` line when EITHER `posted_as == "auto_send"` OR `category` is in `suppress_reply_reminder_categories`.
- Render each action item with owner_hint, blocking_info_needed, needs_hubspot_reply.
- If a record has `action_items == []` AND its category is in `suppress_reply_reminder_categories` → render the synthetic line `(No action items — backend resolution via category handler.)` instead of the "Action items:" header. (This case only applies to drafted tickets with suppressed categories; auto-sent tickets with empty action_items aren't queued.)
- **Elapsed-time language is forbidden** inside per-ticket text — use only carrier-provided or merchant-provided ETAs (and the drafted reply body is already in the note, so the digest just relays it).

### 7. Empty-queue handling

If `N == 0`, do NOT create a Gmail draft. Do NOT post any `[DIGESTED]` markers. Emit a one-line log message ("queue empty, no digest sent at {timestamp}") and exit cleanly. Empty-run archives don't need to be written for remote runs — the routine log in Claude Code captures the run.

### 8. Send the digest via Gmail

Invoke `scripts/send_digest_email.py --send` with a UTF-8 JSON payload on stdin:

```
echo '{"to_emails": <settings.notify_emails>, "subject": "<subject from step 6>", "body_plain": "<composed body from step 6>"}' | py scripts/send_digest_email.py --send
```

The `--send` flag is required for live sends — without it, the helper returns a dry-run preview and does not actually email. This mirrors the pattern used by `send_customer_reply.py`.

The helper:
- Loads the Gmail OAuth token from `config/.secrets/token.json` and refreshes if expired.
- Reads `config/settings.yaml:auto_send` for the Send-mail-as alias (`customercare@gopackn.com`) + display name + Reply-To. The From line matches the customer-facing auto-sends.
- Calls `users().messages().send()` — one call, no draft step. The email lands in the recipients' **inbox**, not Drafts.
- On exit 0, prints `{"sent_message_id": "...", "from": "...", "to": [...]}` on stdout. Capture `sent_message_id` for the archive.
- Exit codes: 2 = bad payload, 3 = Gmail token missing, 4 = Gmail API error.

**If the helper exits non-zero, log the error and EXIT WITHOUT posting `[DIGESTED]` markers.** The next digest run will retry. Do NOT silently lose data by marking tickets digested if the email was never sent.

Rationale for using a local helper instead of the Gmail MCP: the Gmail MCP connector can only create drafts — it cannot send. On the server we have filesystem access to the OAuth token and can send directly in one step.

### 9. Mark each included ticket as digested

For each ticket in the digest, post a `[DIGESTED at <ISO timestamp>]` note via `mcp__claude_ai_HubSpot__manage_crm_objects`:

- `objectType: notes`
- `properties.hs_timestamp`: current UTC ISO (ms precision)
- `properties.hs_note_body`: `[DIGESTED at <ISO timestamp>] Included in digest {hour_window}. Gmail message {sent_message_id}. {N_in_this_digest} tickets total.`
- `associations`: `[{targetObjectId: <ticket_id>, targetObjectType: "tickets"}]`

Do this AFTER the Gmail draft was successfully created. On individual `[DIGESTED]` post failure: log the error but continue — the worst case is that ticket shows up in the NEXT digest as well, which is a tolerable dupe (better than missing it).

### 10. Summary

Return a one-line status:

- `"Sent digest with N tickets across C categories ({L} Luca · {C} Charlie). Gmail message id: {sent_message_id}"`
- `"Queue empty — no digest sent"` (on empty queue)
- `"Digest composed ({L} Luca · {C} Charlie) but Gmail send failed: {error}. No tickets marked digested — next run will retry."` (on Gmail error)

## Safety / invariants

- Never change ticket stage, priority, owner, or any property beyond posting the `[DIGESTED]` note.
- The PACKN_METADATA_V1 block is the reconstitution key. If parsing fails on a note, skip the ticket rather than inventing data.
- The `[DIGESTED]` marker note is append-only — do not modify existing markers.
- On any single-ticket error (can't parse metadata, can't fetch contact, can't post marker), log and continue. Never let one bad ticket block the rest of the digest.
- Gmail send happens via the local helper script, not via the Gmail MCP connector. The helper uses the same OAuth token (`config/.secrets/token.json`) that powers customer-facing auto-sends. Digest emails land in reviewer inboxes directly — no manual click-send step.

## Back-compat with local runs (optional)

If someone runs this skill LOCALLY (not via the scheduled routine), the flow is identical — it still queries HubSpot, not the local queue file. Local `config/pending_actions.json` is no longer read by this skill. (The ticket-processing skill may still write to it for its own debugging/audit, but this skill ignores that file.)
