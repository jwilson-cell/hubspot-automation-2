---
description: Run the hubspot-tickets skill — process the HubSpot ticket queue when the ticket source is hubspot or shadow, and write a pending draft in Pack'N OS for every reply.
---

Run the `hubspot-tickets` skill against this project. It serves the `hubspot` and `shadow` ticket sources. It does not serve `os`: in that source the wrapper starts `/packn-tickets-os` instead, and this command must stop at its first step.

This command is normally started by the wrapper (`scripts/run_tickets.sh`, which runs `scripts/run_tickets.py`) for a scheduled run and for Run now. The wrapper has already read the source, passed the routine gate and decided there may be work.

Follow `.claude/skills/hubspot-tickets/SKILL.md` exactly:

1. Run the source guard FIRST, from the project root: `py -m ticket_pipeline.source --require hubspot,shadow; echo "guard_exit=$?"`. Exit `0` = the source is served, continue. Any other exit: stop at once and do nothing else. `3` = `SOURCE_MODE_NOT_SERVED <mode>` (the source is `os`); `4` = `SOURCE_MODE_UNKNOWN` or `SOURCE_MODE_UNREADABLE` (the setting could not be established; never assume `hubspot`); `2` = a usage error in the command line.
2. Read `config/settings.yaml`, `config/categories.yaml`, `config/state.json`. Process any pending operator rerun requests first.
3. Fetch active-queue tickets from HubSpot: `hs_pipeline_stage IN settings.active_stages` AND `hs_last_message_from_visitor = "true"` AND `hs_lastmodifieddate > state.last_run_at`, up to `settings.per_run_cap`. Apply the skill's dedupe step (2a-pre) to each one before hydrating it. If the search returns no ticket and no rerun was processed, write a short run log and go to step 10 (under the wrapper: the result file with status `success`, both counts 0, an empty `tickets` list); never exit before the result file is written.
4. For each ticket: hydrate with associated `emails`, form properties, contact, and ShipSidekick order state (step 2a.5 if the ticket has an order_number or tracking_number). Classify using `topic_of_ticket` as prior. Retrieve relevant KB docs. Draft a reply grounded in `kb/` + live SSK state. Extract action items with `needs_hubspot_reply` flags.
5. Write the reply as a pending draft in Pack'N OS with `client.write_cron_draft` (step 2f), for every ticket and every form topic. Nothing is sent to a customer and no note is posted on the HubSpot ticket: an operator reviews the draft in Pack'N OS and sends it from there.
6. Urgent action items fire the solo Gmail email path immediately (`mcp__claude_ai_Gmail__create_draft` → `py scripts/send_draft.py`). That email goes to the team, never to the customer.
7. Queue each drafted ticket to `config/pending_actions.json`. The wrapper forwards its action items to Pack'N OS after you exit; do not forward them yourself. Sheets export per `settings.sheets_export.enabled`.
8. Update `config/state.json` with `last_run_at`, `processed_ticket_ids`, and `ticket_fingerprints`.
9. Write a run log to `outputs/runs/<ISO-timestamp>.md`.
10. Under the wrapper (`PACKN_RESULT_PATH` is set): write the result file at that path (step 4.5) and do not write a run record; the wrapper writes the run's one record from the file. On a hand-run (`PACKN_RUN_ID` and `PACKN_RESULT_PATH` unset): write the run record yourself, as step 4.5 says.

In `shadow` you do exactly what you do in `hubspot`. The comparison with Pack'N OS, and the report of each pass back to it, are done by code before and after you run.

A hand-run of this command still works in those two sources, but it has no run context from the wrapper. Start a run with `bash scripts/run_tickets.sh` whenever you can.

Respect `settings.dry_run` — if true, log what would happen but do NOT write a draft, update a HubSpot ticket or send any email. If any MCP call errors, log and continue rather than aborting the whole run.

When finished, print the one-line summary: `Processed N tickets — M drafts written to Pack'N OS, U urgent emails, Q tickets queued for digest.`
