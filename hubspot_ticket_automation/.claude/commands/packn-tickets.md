---
description: Run the hubspot-tickets skill to process new/updated HubSpot help desk tickets.
---

Run the `hubspot-tickets` skill against this project.

Follow `.claude/skills/hubspot-tickets/SKILL.md` exactly:

1. Read `config/settings.yaml`, `config/categories.yaml`, `config/state.json`.
2. Fetch active-queue tickets from HubSpot: `hs_pipeline_stage IN settings.active_stages` AND `hs_last_message_from_visitor = "true"` AND `hs_lastmodifieddate > state.last_run_at`, up to `settings.per_run_cap`.
3. For each ticket: hydrate with associated `emails`, form properties, contact, and ShipSidekick order state (step 2a.5 if the ticket has an order_number or tracking_number). Classify using `topic_of_ticket` as prior. Retrieve relevant KB docs. Draft a reply grounded in `kb/` + live SSK state. Extract action items with `needs_hubspot_reply` flags.
4. Route:
   - **Auto-send path** (FORM + topic in `auto_send_form_topics`): invoke `py scripts/send_customer_reply.py --send` with the `metadata_block` payload. Posts Gmail message + HubSpot email engagement + `[AUTO-SENT TO CUSTOMER]` note with PACKN_METADATA_V1 block.
   - **Draft path** (everything else): post `[DRAFT — REVIEW BEFORE SENDING]` note via `manage_crm_objects`, appending a PACKN_METADATA_V1 block so the remote digest can reconstruct action items.
5. Urgent action items fire the solo Gmail email path immediately (`mcp__claude_ai_Gmail__create_draft` → `py scripts/send_draft.py`).
6. Queue drafted tickets (and auto-send tickets with non-empty action_items) to `config/pending_actions.json` as a local audit log. Sheets export per `settings.sheets_export.enabled`.
7. Update `config/state.json` with `last_run_at`, `processed_ticket_ids`, and `ticket_fingerprints`.
8. Write a run log to `outputs/runs/<ISO-timestamp>.md`.

Respect `settings.dry_run` — if true, log what would happen but do NOT call any HubSpot write or Gmail send. If any MCP call errors, log and continue rather than aborting the whole run.

When finished, print the one-line summary: `Processed N tickets — A auto-sent, M drafts posted, U urgent emails, Q tickets queued for digest.`
