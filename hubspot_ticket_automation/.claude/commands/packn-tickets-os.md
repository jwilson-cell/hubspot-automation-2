---
description: Run the hubspot-tickets-os skill — process the Pack'N OS ticket bundle of an os-mode run and write the result file.
---

Run the `hubspot-tickets-os` skill against this project. It serves the `os` ticket source only, and it is started by `scripts/run_tickets.py`, never by hand.

Follow `.claude/skills/hubspot-tickets-os/SKILL.md` exactly:

1. Run the source guard FIRST, from the project root: `py -m ticket_pipeline.source --require os; echo "guard_exit=$?"`. On any exit other than 0, stop at once and do nothing else.
2. Check `PACKN_BUNDLE_PATH`. If it is unset or empty, stop at once and do nothing else: there is no bundle, so this run was not started by the wrapper.
3. Open the bundle at `PACKN_BUNDLE_PATH`, and `config/settings.yaml` and `config/categories.yaml`.
4. For each ticket in the bundle, in order: classify, retrieve KB context, draft a reply grounded in `kb/`, and extract action items — all from the entry's `ticket_context`. Nothing about a ticket is looked up anywhere else.
5. Evaluate urgency (the urgent solo email goes out only for a ticket whose entry has a `hubspot_ticket_id`), queue the ticket's record to `config/pending_actions.json`, and buffer its Sheets row.
6. After the loop: run the Sheets sync per `settings.sheets_export.enabled`, write the run log to `outputs/runs/<ISO-timestamp>.md`, and write the result file at `PACKN_RESULT_PATH` with one entry per bundle ticket.

You name a ticket by the bundle entry's `ref`, copied character for character, everywhere you write one.

You do not search for or look up tickets, store drafts, write the state file, write a run record, forward action items or update a ticket. Code does those after you exit.

Respect `settings.dry_run` — if true, log what would happen but do NOT send any email.

When finished, print the one-line summary: `Processed N bundle tickets — M replies drafted, U urgent emails, Q tickets queued for digest, F failed.`
