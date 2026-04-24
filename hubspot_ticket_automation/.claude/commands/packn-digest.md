---
description: Run the hubspot-actions-digest skill to compose and send the reviewer digest email.
---

Run the `hubspot-actions-digest` skill against this project.

Follow `.claude/skills/hubspot-actions-digest/SKILL.md` exactly:

1. Query HubSpot via MCP for note engagements containing `PACKN_METADATA_V1` posted in the last 48 hours. De-dupe against tickets that already have a `[DIGESTED at ...]` marker note.
2. For each surviving note: hydrate ticket + contact + company, parse the PACKN_METADATA_V1 JSON, build a per-ticket record.
3. Apply owner split (Luca vs Charlie) per `config/categories.yaml:category_owners`. Apply reply-reminder suppression per `config/categories.yaml:suppress_reply_reminder_categories`.
4. Compose the digest body (LUCA section first, CHARLIE section second, grouped by category within each, draft body rendered verbatim, action items with `Needs HubSpot reply: yes/no` flag).
5. Compute subject: `[HubSpot Digest] N tickets to review (L Luca · C Charlie) — hour_window`.
6. Send via `py scripts/send_digest_email.py --send` with `{to_emails: settings.notify_emails, subject, body_plain}` as stdin JSON. Capture `sent_message_id` from stdout.
7. Post `[DIGESTED at <ISO timestamp>] Included in digest {hour_window}. Gmail message {sent_message_id}. {N} tickets total.` note on each included ticket via `manage_crm_objects`.

If the queue is empty, log `queue empty, no digest sent` and exit without sending or marking anything.

Respect `settings.dry_run` — if true, write the composed body to `outputs/digests/<ISO>.dry-run.md` and skip the send + marker posts.

When finished, print one of:
- `Sent digest with N tickets across C categories (L Luca · C Charlie). Gmail message id: {sent_message_id}`
- `Queue empty — no digest sent`
- `Digest composed but Gmail send failed: {error}. No tickets marked digested — next run will retry.`
