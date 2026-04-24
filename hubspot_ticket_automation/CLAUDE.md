# Project context for Claude

This project is **Pack'N's HubSpot help desk ticket automation**. Pack'N is a 3PL (third-party logistics / fulfillment provider). Most ticket traffic comes from merchants whose orders Pack'N fulfills; a minority comes from end consumers.

## Architecture at a glance

Two skills, run on cron on a DigitalOcean Ubuntu droplet (server IP `167.99.229.91`, runs as `packn` user, project rooted at `/opt/packn/hubspot_ticket_automation`):

1. **`hubspot-tickets`** — every 30 minutes. Pulls new/updated tickets, classifies, drafts a reply grounded in `kb/`, hydrates live ShipSidekick order state when applicable, then either (a) auto-sends the reply to the customer via Gmail + logs an `[AUTO-SENT TO CUSTOMER]` note (for FORM + Mispack/Carrier tickets) or (b) posts a `[DRAFT — REVIEW BEFORE SENDING]` internal note (everything else). Action items: `urgent` → solo Gmail email immediately; `normal` → stays on the HubSpot ticket note with a `PACKN_METADATA_V1` block.
2. **`hubspot-actions-digest`** — 8am / 12pm / 3pm ET on weekdays. Queries HubSpot for tickets whose automation notes haven't been digested yet, composes a single email split into Luca (billing/account/escalation) and Charlie (warehouse/general) sections, sends via Gmail, and posts `[DIGESTED at ...]` marker notes on each included ticket so the next run skips them. **HubSpot is the source of truth for the digest queue** (no local queue file) — this lets the skill run anywhere that has the private-app token + Gmail OAuth.

Helpers on the server:
- `scripts/send_customer_reply.py` — one-step Gmail send + HubSpot v1 engagement + `[AUTO-SENT]` note (auto-send path).
- `scripts/send_digest_email.py` — one-step Gmail send for the digest (no Drafts intermediary).
- `scripts/send_draft.py` — Gmail draft-to-sent promoter (legacy; used by the urgent email path).
- `scripts/ssk_order_lookup.py` — ShipSidekick order + shipment state, injected into ticket_context before drafting.
- `scripts/sheets_sync.py` — non-blocking Google Sheets export (KPI + mispack + carrier_issue rollups).

GitHub repo: `https://github.com/jwilson-cell/hubspot-automation-2`. The server pulls from `main` each time cron starts Claude Code; doc/skill/config changes land by `git push` from the laptop clone + `git pull` on the server.

## Invariants (do not violate)

1. **Dry-run default.** `config/settings.yaml` starts with `dry_run: true`. Do not flip to `false` until a dry-run pass has been inspected.
2. **All categories start in `draft` mode.** No auto-send anywhere until a category has been reviewed for a week.
3. **Never invent rates, SLAs, dollar amounts, or policies** in a drafted reply. If the KB lacks it, the draft says "let me confirm with the team" and flags a `billing_specialist_follow_up` or similar action item.
4. **Never commit** to credits, refunds, or reships in the drafted reply. Always flag as action item for the reviewer.
5. **Read-only on HubSpot tickets except:** the skill MAY create internal notes (engagement records) associated to a ticket. It must NOT change stage, owner, priority, or any other ticket property. It MUST NOT close or delete tickets.
6. **Keep secrets out of `kb/`**. KB content is loaded into every prompt — no credentials, no PII.
7. **Never hydrate customer attachments into model context.** Do not fetch `hs_file_upload`, file URLs, or file contents via any MCP tool. The HubSpot MCP returns attachments as inline image blocks that poison the conversation with `400 Could not process image` errors, forcing a /clear. Detect attachment presence only (via association count or body mentions); draft reviewers open the HubSpot ticket directly to inspect files. See the attachment-handling section at the top of `.claude/skills/hubspot-tickets/SKILL.md`.

8. **Operator-owned columns in the Sheets workbook are inviolable.** The `mispack_log` and `carrier_issue_log` tabs are two-zone — skill-owned columns (ticket_id, order_number, tracking_number, ...) and operator-owned columns (claim_status, claim_number, coverage_usd, investigation_status, cost_absorbed_usd, customer_credit_usd, root_cause, filed_at, closed_at, resolution_amount_usd, reimbursement_received_at, carrier_filed_at, operator_notes). The skill writes operator-owned cells as empty strings **only on first insert** of a row; it must never overwrite them afterward. Dedupe by `ticket_id` in `scripts/sheets_sync.py` enforces this — if a row already exists for a ticket, the entire row is skipped, not updated. Operator edits never get clobbered.

9. **Sheets sync is non-blocking.** `scripts/sheets_sync.py` writes the local CSV mirror (`outputs/kpi/*.csv`) first, then attempts the Sheets API. API failures queue the payload to `config/sheets_pending_sync.json` — they never fail the skill run. The next successful run flushes the queue before appending new rows. The local CSV mirror is the recovery source of truth for skill-owned data; the Google Sheet is the operator surface for claim lifecycle management.

10. **Column order in the Sheet is operator-controlled.** Operators can reorder columns, insert their own (e.g., "escalation owner", "internal notes"), or re-theme rows — all of that survives skill writes because `sheets_sync.py` resolves column positions by exact header-name match at write time. Never refer to columns by position in the sheet. Never rename or delete a skill-owned header — that breaks the header-name lookup and the skill will log an error.

11. **HubSpot Help Desk ticket timelines only render conversation-thread messages and note engagements — NOT email engagements.** Form-sourced tickets have no conversation thread, so any customer-facing reply that's *not* an internal note is invisible to the reviewer on the ticket. Consequences:
    - Auto-sent customer emails (form Mispack/Carrier path via `scripts/send_customer_reply.py`) MUST create a companion `[AUTO-SENT TO CUSTOMER]` note engagement alongside the email engagement. The note is the reviewer's only visual record.
    - The legacy v1 engagements endpoint (`POST /engagements/v1/engagements`) is the preferred path for creating both email and note engagements from the auto-send helper. The v3 `POST /crm/v3/objects/emails` endpoint does not correctly parse synthetic `hs_email_headers`, so `metadata.to/from` come out empty and the engagement is doubly-hidden.
    - HubSpot's Conversations Custom Channels API could create native threads that *would* render on the timeline, but requires an OAuth app (not a private app) plus a 24/7 webhook receiver and inbound email pipeline — scope of ~4-7 days of integration work, rejected as disproportionate for two ticket categories.
    - Customer replies to an auto-sent email land at `customercare@gopackn.com` and create a *new* ticket rather than threading onto the original form ticket (HubSpot has no thread to attach them to). Enable Help Desk reply-grouping or live with the two-ticket pattern.

## Files by purpose

| Path | Purpose |
|---|---|
| `.claude/skills/hubspot-tickets/SKILL.md` | Main processing skill |
| `.claude/skills/hubspot-actions-digest/SKILL.md` | Hourly digest skill |
| `config/settings.yaml` | Runtime config (dry_run, caps, pipeline, notify email, urgent signals) |
| `config/categories.yaml` | Form-topic mapping, auto-send topic allow-list, category → KB file mapping |
| `config/state.json` | last_run_at, processed ticket IDs |
| `config/pending_actions.json` | Queue for hourly digest |
| `prompts/classify.md` | Classifier prompt |
| `prompts/draft_reply.md` | Drafter prompt |
| `prompts/extract_actions.md` | Action-item extractor prompt |
| `kb/brand_voice.md` | Voice + sign-off (has Pack'N TODOs) |
| `kb/glossary.md` | 3PL terminology |
| `kb/categories.md` | Category reference |
| `kb/tracking_wismo.md` | Tracking decision tree, carrier timelines |
| `kb/damage_claims.md` | Damage claim flow |
| `kb/mispack_process.md` | Mispack investigation |
| `kb/inventory_accuracy.md` | Count reconciliation |
| `kb/returns_rma.md` | RMA flow |
| `kb/billing_faq.md` | Billing dispute handling |
| `kb/integration_errors.md` | Unknown SKU, address, EDI, retailer chargebacks |
| `.claude/commands/packn-tickets.md` | Slash command for ticket processing (invoked by cron via `claude -p /packn-tickets`). |
| `.claude/commands/packn-digest.md` | Slash command for the digest (invoked at 8/12/3 ET weekdays). |
| `.claude/settings.json` | Pre-authorized tool allow-list so headless cron runs don't hang on permission prompts. |
| `scripts/send_customer_reply.py` | Auto-send helper for form Mispack/Carrier tickets — Gmail send + v1 email engagement + v1 note engagement |
| `scripts/send_digest_email.py` | Sends the digest directly via Gmail (one-step send, no draft). Invoked by hubspot-actions-digest SKILL step 8. Requires `--send` for live send. |
| `scripts/send_draft.py` | Promotes Gmail drafts to Sent (used by urgent solo emails) |
| `scripts/sheets_sync.py` | Non-blocking Sheets export (KPI + mispack + carrier_issue rollups) |
| `scripts/ssk_order_lookup.py` | WISMO hydration — looks up ShipSidekick order + shipment state by order_number or tracking_number; output injected into `ticket_context.ssk_state` ahead of the drafter |
| `docs/server-setup.md` | DigitalOcean Ubuntu server provisioning + deployment runbook |
| `config/.secrets/hubspot_token.txt` | HubSpot private-app access token (gitignored) |
| `config/.secrets/shipsidekick_token.txt` | ShipSidekick API bearer token (gitignored) |
| `config/.secrets/token.json` | Gmail OAuth refresh token (gitignored) |
| `outputs/runs/` | Per-run logs (`cron-tickets.log` collects all server cron stdout/stderr for /packn-tickets; digests under `cron-digest.log`) |
| `outputs/digests/` | Per-digest archives (subset of historical runs; the HubSpot-first digest no longer writes these but they're preserved for audit) |

## Pack'N-specific fill-ins (TODOs)

Search the KB for `TODO (Pack'N):` markers to find what still needs real Pack'N policy filled in:

- Sign-off and escalation contacts (`kb/brand_voice.md`)
- Carrier claim windows if different from industry norms (`kb/damage_claims.md`, `kb/tracking_wismo.md`)
- Mispack cost-absorption policy (`kb/mispack_process.md`)
- Cycle count cadence + SLA (`kb/inventory_accuracy.md`)
- RMA receipt-to-disposition SLA (`kb/returns_rma.md`)
- Storage / pick-pack / accessorial rate structure (`kb/billing_faq.md`)
- Retailer chargeback dispute portals (`kb/integration_errors.md`)
- Address-correction pass-through policy (`kb/tracking_wismo.md`)

The automation works without these filled in — drafts will be slightly more hedged ("let me confirm with the team") where data is missing.

## When making changes

- Editing `kb/` → takes effect on next skill run. No redeploy.
- Editing `prompts/` → takes effect on next skill run.
- Editing `config/categories.yaml` (e.g., graduating a category to `auto-send`) → takes effect on next run. Review at least one week of drafted output for that category first.
- Adding a new category → add it to `categories.yaml`, `prompts/classify.md`, and `kb/categories.md`; create a matching `kb/<name>.md` if it warrants its own doc.

## Verification ritual

Before running anything live:

1. `dry_run: true` run → inspect `outputs/runs/<latest>.md`.
2. Run Gmail auth if not already done.
3. Set `dry_run: false` with `per_run_cap: 1` → confirm note posts and, if applicable, urgent/digest email lands.
4. Scale up `per_run_cap` and schedule the cron.
