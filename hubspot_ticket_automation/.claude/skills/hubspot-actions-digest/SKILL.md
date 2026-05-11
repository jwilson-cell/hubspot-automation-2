---
name: hubspot-actions-digest
description: Compose and send a reviewer digest email of all HubSpot tickets with drafted (or auto-sent backend-action) replies not yet digested. Reads pending drafts directly from Pack'N OS automation_drafts via packn_os_hubspot_client (Phase 4.1 D-02.b — replaces the prior HubSpot search for PACKN_METADATA_V1 blocks). Sends via Gmail using the local OAuth helper (scripts/send_digest_email.py) so the email lands in reviewer inboxes, not drafts. Designed to run on the server under cron at 8am/12pm/3pm ET M-F, also invokable manually.
---

# HubSpot Digest Skill — Pack'N OS-first (post-Phase 4.1 D-02.b)

This skill runs **on the server** under cron (`/packn-digest` at 8am/12pm/3pm ET M-F). It does NOT read `config/pending_actions.json` for the *draft list* (that file is still the canonical source for `action_items` per RESEARCH Open Question 5 option B — see Step 4 below). The single source of truth for the **draft queue** is the `automation_drafts` table in Pack'N OS, read via the `packn_os_hubspot_client.client.read_pending_drafts` helper.

Sends the composed digest via Gmail using `scripts/send_digest_email.py` (local OAuth, same token as auto-send). The From line is `Pack'N Customer Care <customercare@gopackn.com>` via the Gmail "Send mail as" alias — matches the sender identity used for customer auto-sends.

## Required connectors

- Postgres access via `packn_os_hubspot_client` (PACKN_OS_DATABASE_URL env var; the `packn_os_existing_automation` role has SELECT on `automation_drafts` per Phase 4.1 D-04.c GRANT)
- No HubSpot MCP needed for the draft queue read (Phase 4.1 D-02.b — zero HubSpot API calls for the discovery step)
- No Gmail MCP dependency — the skill sends through the local Python helper, not through an MCP connector

## Inputs read from the repo

- `config/settings.yaml` — `notify_emails` (list), `hubspot_portal_id`, `hubspot_timezone`.
- `config/categories.yaml` — `category_owners` (map: category → "luca" | "charlie"), `suppress_reply_reminder_categories` (list).
- `config/pending_actions.json` — per-ticket action_items (option B per RESEARCH Open Question 5); the digest JOINs by `ticket_id`.

## Flow

### Step 1: Read pending drafts from Pack'N OS

Invoke the Python helper to read all pending drafts from `automation_drafts` (Phase 4.1 D-02.b — replaces the prior HubSpot-search-for-`PACKN_METADATA_V1` approach):

```bash
py -c "
import json
from packn_os_hubspot_client import client
drafts = client.read_pending_drafts('tickets-process', since_hours=48)
print(json.dumps([
    {
        'draft_id': str(d['id']),
        'ticket_id': d['ticket_id'],
        'draft_body': d['draft_body'],
        'snapshot': d['hubspot_ticket_snapshot'],
        'created_at': d['created_at'].isoformat() if d.get('created_at') else None,
    }
    for d in drafts
]))
"
```

Each draft includes the ticket snapshot (`hubspot_ticket_snapshot` JSONB column) captured at draft-generation time. The snapshot has `subject`, `body`, `category`, `topic_of_ticket`, `source_type`, `contact`, `custom_properties`, `captured_at` — enough to render the digest without any live HubSpot calls.

The helper returns rows ordered by `created_at` ASC (oldest first) so the digest renders consistently. The 48-hour lookback is wider than any single digest gap; state-machine dedup (Step 2 below) handles already-handled drafts naturally.

**Side effect:** ZERO HubSpot API calls for the discovery step (vs. ~1 search call per digest run in the legacy path). Phase 4.1 D-08.b verification target.

### Step 2: (DELETED — natural dedup via state machine)

Drafts in `automation_drafts` flow through state transitions: `pending → approved → sent` OR `pending → rejected` OR `pending → superseded`. Step 1 filters on `state='pending'`, so once an operator approves/rejects/supersedes a draft via Pack'N OS UI, the next digest run won't surface it. No legacy dedup marker notes needed.

(Pre-Phase-4.1 the digest posted legacy dedup marker notes to HubSpot to de-dupe against the scraped PACKN_METADATA_V1 search. Post-Phase-4.1 D-02.b, those markers are no longer posted — the state machine on `automation_drafts.state` is the source of truth.)

### Step 3: Use the snapshot for ticket context

Each draft from Step 1 includes its `snapshot` field. Use directly for digest rendering: `subject`, `body`, `category` (owner routing), `topic_of_ticket`, `contact`, `source_type`, `captured_at`. No live HubSpot SDK call needed.

If a snapshot lacks a non-essential field (e.g., older drafts pre-dating a snapshot-schema bump), substitute a sensible default at render time rather than skipping the ticket — the digest is a reviewer surface, not a downstream consumer.

### Step 4: Read action_items from config/pending_actions.json

Per RESEARCH Open Question 5 option B (no `automation_drafts` schema migration for action_items), action_items continue to flow through the sibling repo's local file:

```bash
py -c "
import json
from pathlib import Path
actions = json.loads(Path('config/pending_actions.json').read_text() or '[]')
print(json.dumps(actions))
"
```

JOIN by `ticket_id` against the drafts list from Step 1. A draft without matching action_items renders with empty `action_items` (and falls into the `suppress_reply_reminder_categories` branch in Step 6 if its category matches).

NO `PACKN_METADATA_V1` block parsing needed — that block is no longer emitted on the draft path post-Phase-4.1 D-02.a hard-cut.

### 5. Build the per-ticket record

Assemble one record per draft (from Steps 1 + 4) with the fields the digest body needs. The draft's `snapshot` (from `hubspot_ticket_snapshot`) supplies subject/contact/category; the JOINed entry from `pending_actions.json` supplies action_items + urgent_emailed:

```
{
  ticket_id:           <draft.ticket_id>,
  ticket_subject:      <snapshot.subject>,
  ticket_link:         f"https://app.hubspot.com/contacts/{hubspot_portal_id}/ticket/{ticket_id}",
  contact:             <snapshot.contact.name + " <" + snapshot.contact.email + ">">,
  company:             <derive from contact or pending_actions entry if present>,
  category:            <snapshot.category>,
  topic_of_ticket:     <snapshot.topic_of_ticket or null>,
  source_type:         <snapshot.source_type>,
  posted_as:           "draft"  (post-Phase-4.1 D-02.a, ALL automation_drafts rows are drafts —
                                 the auto-send branch ALSO writes drafts now; see SKILL.md hubspot-tickets
                                 Phase 4 cutover narrative for the both-branches-uniform model)
  draft_id:            <draft.draft_id>,
  drafted_reply_body:  <draft.draft_body>,
  classifier_reason:   <look up by ticket_id in pending_actions.json if present; else empty>,
  urgent_emailed:      <look up in pending_actions.json; default false>,
  action_items[]:      <list from pending_actions.json filtered by ticket_id; default []>,
  queued_at:           <draft.created_at>
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

If `N == 0`, do NOT create a Gmail draft. (Post-Phase-4.1 D-02.b: there are no legacy dedup markers to omit either — the digest no longer writes to HubSpot at all.) Emit a one-line log message ("queue empty, no digest sent at {timestamp}") and exit cleanly. Empty-run archives don't need to be written for remote runs — the routine log in Claude Code captures the run.

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

**If the helper exits non-zero, log the error and EXIT.** The next digest run will retry. (Post-Phase-4.1 D-02.b: drafts remain in `state='pending'` until the operator actions them via Pack'N OS, so a failed digest send is recovered by simply re-running — no dedup-marker bookkeeping needed.)

Rationale for using a local helper instead of the Gmail MCP: the Gmail MCP connector can only create drafts — it cannot send. On the server we have filesystem access to the OAuth token and can send directly in one step.

### Step 9: (DELETED — natural state-machine dedup makes markers unnecessary)

Pre-Phase-4.1 the digest posted legacy dedup marker notes on each included ticket via HubSpot, so the next digest run's PACKN_METADATA_V1 search could de-dupe against them.

Post-Phase-4.1 D-02.b, the state machine on `automation_drafts.state` handles dedup naturally: once the operator approves/rejects/supersedes the draft via Pack'N OS UI, the next `read_pending_drafts` call won't surface it. No HubSpot writes from the digest path.

If an operator wants to "skip" a draft from the digest without actioning it via Pack'N OS, that's currently a no-op — they should mark the draft as `rejected` or `superseded` in Pack'N OS UI (or wait for the 48h lookback window to expire). Defer any "skip-from-digest" UX to a future plan if operators request it.

### 10. Summary

Return a one-line status:

- `"Sent digest with N tickets across C categories ({L} Luca · {C} Charlie). Gmail message id: {sent_message_id}"`
- `"Queue empty — no digest sent"` (on empty queue)
- `"Digest composed ({L} Luca · {C} Charlie) but Gmail send failed: {error}. Drafts remain pending in Pack'N OS — next run will retry."` (on Gmail error)

## Safety / invariants

- Never write to HubSpot from this skill (post-Phase-4.1 D-02.b). The digest is a pure-read flow against Pack'N OS `automation_drafts` + local `pending_actions.json`. No ticket stage/priority/owner changes, no engagement notes, no legacy dedup markers.
- The `automation_drafts.state` column is the dedup source of truth. If `read_pending_drafts` returns drafts that the operator already actioned (race condition between digest send and operator approve), the worst case is a duplicate digest entry — tolerable, and the next digest run won't surface the now-non-`pending` draft.
- On any single-ticket error (can't parse snapshot, can't find pending_actions.json entry, can't render), log and continue. Never let one bad draft block the rest of the digest.
- Gmail send happens via the local helper script, not via the Gmail MCP connector. The helper uses the same OAuth token (`config/.secrets/token.json`) that powers customer-facing auto-sends. Digest emails land in reviewer inboxes directly — no manual click-send step.

## Back-compat with local runs (optional)

If someone runs this skill LOCALLY (not via the scheduled routine), the flow is identical — it queries Pack'N OS Postgres via `read_pending_drafts`, not the local queue file's draft list. Local `config/pending_actions.json` IS still read (for action_items per option B), but only as a JOIN target keyed by `ticket_id` from the Pack'N OS draft list. The ticket-processing skill writes to it for action-items routing; this skill reads it but does not write.
