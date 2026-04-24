# Operator-aid claim packet — build spec (Tier 0)

**Status:** Draft — ready for execution phase
**Companion doc:** `research/carrier_claim_automation_feasibility.md` (the memo that establishes this as the right tier)
**Scope:** ~1–2 days of build. No new skill, no new external integration.

---

## 1. Overview

When `extract_actions.md` emits a `file_carrier_claim` action item, the hourly digest currently renders it as a one-liner:

```
[ ] [file_carrier_claim] File damage claim for tracking 1Z999AA10123456784
    Owner: carrier · Missing info: photos of damaged goods
    Needs HubSpot reply: no
```

The operator then has to open the HubSpot ticket, dig the order number out of the thread, infer the carrier from the tracking format, look up Pack'N's declared-value record, open the right carrier portal, and type everything in by hand. This spec replaces that one-liner with a **claim packet card** — every field the operator needs, formatted for copy-paste, with a carrier-portal deep-link and a filing-deadline clock.

The packet card still renders **inline under the ticket** (not as a new cross-cutting section) because the ticket-context surrounding it is useful and duplicating data would be worse than mild verbosity. The change is purely in how the `file_carrier_claim` action item renders when it appears.

**Target user experience:**
1. Operator opens the hourly digest email.
2. Under a ticket with a `file_carrier_claim` item, instead of the one-liner they see a packet card: carrier name + portal link, all pre-filled fields, an evidence summary (e.g., "3 photos on ticket — upload from HubSpot"), and a deadline clock.
3. Operator clicks the portal link → pastes fields from the packet → opens the HubSpot ticket in another tab → uploads photos → submits.
4. Operator manually updates the `carrier_issue_log` row (`claim_status`, `claim_number`, `carrier_filed_at`) in Sheets.

Target total operator time: ~30s per claim, down from ~10min.

---

## 2. Enhanced `file_carrier_claim` action schema

Additive fields on the existing JSON object — nothing is removed or renamed, so any queued records without these fields still render via the existing back-compat path in the digest skill.

```jsonc
{
  "action_type": "file_carrier_claim",
  "description": "File UPS damage claim for tracking 1Z999AA10123456784, declared value $TBD",
  "owner_hint": "carrier",
  "blocking_info_needed": ["declared_value", "photos_confirmed_on_ticket"],
  "severity": "urgent",   // existing — urgent if >$1k exposure
  "needs_hubspot_reply": false,

  // NEW FIELDS — all optional, nullable when unknown
  "claim_packet": {
    "inferred_carrier": "UPS",            // UPS | FedEx | USPS | DHL | LTL | unknown
    "carrier_confidence": "high",         // high | medium | low (how the classifier derived it)
    "claim_type": "damage",               // damage | loss | delay
    "tracking_number": "1Z999AA10123456784",
    "order_number": "PN-48221",
    "ship_date_hint": "2026-04-14",       // ISO date or null
    "declared_value_hint": null,          // number in USD or null
    "declared_value_source": null,        // "merchant_form" | "order_system" | "label" | null
    "damage_or_loss_description": "Box arrived crushed; contents broken per customer photos",
    "evidence_summary": "3 photos on ticket (outer box, product, interior packaging)",
    "insurance_on_package": true,         // mirrors the form field when present
    "filing_deadline_iso": "2026-06-13",  // ship_date + carrier_window; conservative if ship_date null
    "days_until_deadline": 51,            // integer; negative = past deadline
    "hubspot_ticket_url": "https://app.hubspot.com/contacts/<portal>/ticket/<id>"
  }
}
```

**Derivation rules for extractor prompt:**

**Primary source: `ticket_context.ssk_state`.** The ticket skill already hydrates ShipSidekick state via `scripts/ssk_order_lookup.py` for Carrier Issue / WISMO / shipping-delay tickets. When `ssk_state.found == true`, use it first — it's more reliable than any regex or form field:
  - `inferred_carrier` ← `ssk_state.shipments[0].carrier_code` (e.g., `"UPS"`); `carrier_confidence: "high"` when SSK matched.
  - `tracking_number` ← `ssk_state.shipments[0].tracking_code` (prefer this over the form field if they disagree — SSK is canonical).
  - `order_number` ← `ssk_state.order.name` (or `.alias` if more merchant-recognizable).
  - `ship_date_hint` ← the date portion of `ssk_state.shipments[0].created_at` — this is label-creation time, a defensible ship-date proxy (tighter than `hs_createdate`). If multiple shipments and the ticket's tracking matches a specific one, use that shipment's `created_at`.

**Fallback when SSK lookup fails** (`ssk_state.found == false` or ssk_state absent):
  - `inferred_carrier`: form field `carrier_issue` if it names a carrier, else tracking-number regex:
    - `^1Z[A-Z0-9]{16}$` → UPS
    - `^\d{12,15}$` → FedEx or USPS (ambiguous — mark `medium` confidence, prefer FedEx if 12-digit, USPS if 20-22-digit; confirm via carrier-specific prefixes)
    - `^94\d{20}$` or `^92\d{20}$` etc. → USPS
    - 10-digit alphanumeric beginning with a letter → DHL Express
    - No match → `unknown`, `low` confidence; operator picks carrier manually.
  - `ship_date_hint`: parse from ticket body / form; null if not present. Do NOT infer from `hs_createdate` — that's the ticket date, not the ship date.

**Neither SSK nor fallback gives us:**
  - `declared_value_hint`: NOT exposed by SSK in its current wrapper. Will almost always be null in Tier 0. When null, add `declared_value` to `blocking_info_needed` — operator fills it from the order system at file time. Do not guess. (See feasibility memo open question #2 — may be recoverable from a wider SSK lookup or merchant cart, but that's a follow-up.)

- `claim_type`: derived from category + keywords. `damaged_goods` → `damage`. `wismo_tracking` with keywords "lost", "never arrived", "stolen" → `loss`. `shipping_delay` → `delay`.
- `damage_or_loss_description`: one factual sentence, no speculation about root cause. This goes straight into the carrier's "what happened" field, so it must read as shipper-written, not customer-paraphrased.
- `evidence_summary`: string describing photo presence based on HubSpot association counts ONLY. Do NOT fetch or inspect the photos (invariant #7). Examples: `"3 photos on ticket (review on HubSpot before filing)"`, `"no photos on ticket — request from merchant before filing"`, `"photos mentioned in body but not attached — clarify with merchant"`.
- `filing_deadline_iso`: `ship_date_hint + carrier_window` where carrier_window defaults are UPS 60d, FedEx 60d, USPS 60d, DHL 30d, LTL 270d. If `ship_date_hint` is null, use `hs_createdate - 2 days` as a conservative ship-date proxy (tickets typically lag ship by a day or two) and flag the deadline as approximate: prefix with `~` in rendering.
- `days_until_deadline`: computed as `(filing_deadline_iso - today())` at render time in the digest skill, not at extraction time (so the number stays accurate across retries). Extraction can leave this as `null` or compute at extraction — digest re-computes authoritatively.

**Prompt change required in `prompts/extract_actions.md`:** add a dedicated section under "Allowed action types" that specifies the `claim_packet` sub-schema when `action_type == "file_carrier_claim"`. Other action types are unchanged. Keep the top-level envelope identical so queued records without `claim_packet` still parse.

---

## 3. Digest rendering

**Location of change:** `.claude/skills/hubspot-actions-digest/SKILL.md` — the section titled "Standard action-item rendering" (around line 89).

**Current rendering for any action item:**
```
[ ] [{action_type}] {description}
    Owner: {owner_hint} · Missing info: {blocking_info_needed or "none"}
    Needs HubSpot reply: {yes/no}
```

**New rendering when `action_type == "file_carrier_claim"` AND `claim_packet` is present:**

```
[ ] [file_carrier_claim] File {inferred_carrier} {claim_type} claim — {deadline_tag}
    Portal: {carrier_portal_url}   ← {claim_type_deep_link_note}
    Carrier:        {inferred_carrier}{low_confidence_marker}
    Tracking #:     {tracking_number}
    Order #:        {order_number or "—"}
    Ship date:      {ship_date_hint or "unknown — operator fills"}
    Declared value: {declared_value_hint_fmt or "unknown — operator fills from order system"}
    Description:    {damage_or_loss_description}
    Evidence:       {evidence_summary}
    Insurance:      {insurance_on_package_fmt}
    Filing deadline: {filing_deadline_iso} ({days_until_deadline_fmt})
    HubSpot ticket: {hubspot_ticket_url}
    Needs HubSpot reply: no

    ─── Copy-paste claim summary for portal ───
    Shipper: Pack'N Fulfillment
    Tracking: {tracking_number}
    Ship date: {ship_date_hint or "[fill from order system]"}
    Declared value: ${declared_value_hint_fmt or "[fill from order system]"}
    Claim type: {claim_type}
    Description: {damage_or_loss_description}
    ─────────────────────────────────────────────
```

**Rendering rules:**

- `deadline_tag`:
  - `days_until_deadline >= 21` → no tag
  - `21 > days_until_deadline >= 14` → `⏱ {n} days left`
  - `14 > days_until_deadline >= 0` → `⚠ only {n} days left — file this week`
  - `days_until_deadline < 0` → `❌ PAST DEADLINE by {|n|} days — may not be fileable`
  Prefix `~` to any deadline derived from an inferred ship date (no `ship_date_hint` in the source).
- `low_confidence_marker`: ` (⚠ low confidence — verify)` appended to carrier if `carrier_confidence == "low"`. Empty otherwise.
- `declared_value_hint_fmt`: format as `$1,234.56` when present, else empty (the template's fallback string renders).
- `insurance_on_package_fmt`: `yes` / `no` / `unknown`.
- `days_until_deadline_fmt`: `51 days left`, `1 day left`, `past deadline by 3 days`, `deadline today`, or `unknown` when filing_deadline_iso is null.
- `carrier_portal_url` and `claim_type_deep_link_note`: resolved via a new lookup table in `kb/damage_claims.md` (§4 below). If `inferred_carrier == "unknown"`, render `Portal: — (pick carrier manually before filing)` and omit the deep-link note.

**Back-compat:** if a queued record has `action_type == "file_carrier_claim"` but no `claim_packet` field (pre-migration entry), fall through to the existing one-line rendering. The digest skill's existing "back-compat" clause at line 93 of `SKILL.md` already covers this pattern; we just add a branch in the renderer.

**No change to section layout.** The packet card renders inline under the ticket it belongs to, within its owner's section (typically Charlie for `damaged_goods`). We do not add a cross-cutting "Carrier claims ready to file" section — that would duplicate ticket context and fight the existing owner-split design.

---

## 4. Carrier-portal deep-link reference

**Location of change:** `kb/damage_claims.md` — add a new section after the "Claim timeline" section titled `## Carrier portal deep-links` with:

```markdown
## Carrier portal deep-links

Use these URLs in the operator-aid claim packet. Note that most carriers do NOT accept a pre-filled
tracking number via URL parameters — operator pastes from the packet.

| Carrier | Portal URL | Pre-fill supported? |
|---|---|---|
| UPS | https://www.ups.com/us/en/support/file-a-claim.page | No — manual paste |
| FedEx | https://www.fedex.com/en-us/customer-support/claims.html | No — manual paste |
| USPS | https://www.usps.com/help/claims.htm | No — manual paste |
| DHL Express | https://mydhl.express.dhl/us/en/help-and-support/shipping-advice/file-a-claim.html | No — form is downloadable PDF |
| LTL (generic) | [merchant-specific — route to carrier portal per BOL] | N/A |

**Note:** URL formats occasionally change. If a packet renders with a broken portal link, update this
table and the fix propagates on the next hourly digest (the digest reads this KB file at render time).
```

The digest skill reads `kb/damage_claims.md` at render time via the existing KB-reading logic. No code change required to pick up URL updates — edit the KB file, next hour's digest has the new URL.

---

## 5. Persistence and operator surfaces

**Three surfaces, each with one job.** Nothing new here — this is the existing design (invariant #9, `CLAUDE.md`) spelled out for claim-filing.

### 5.1 Action surface: hourly digest email

Where the operator *files* a claim. One email per hour, packet card renders inline under each damaged-goods / lost-package ticket. The card is self-contained: portal link, every field in a copy-paste block, deadline clock, link back to the HubSpot ticket for photos. Open, click through, paste, upload photos, submit — target ~30s per claim.

### 5.2 Lifecycle surface: Google Sheet `carrier_issue_log` tab

Where the operator *manages* every claim across its whole lifecycle (open → filed → reimbursed → closed). Every ticket that emits `file_carrier_claim` gets one row. Sort/filter normally; operator-owned cells accept plain typed input.

**Current schema** (from `scripts/sheets_schema.py`, canonical column list for `carrier_issue_log`):

| Zone | Columns |
|---|---|
| **Skill-owned** (pre-filled by the ticket skill on first insert; never touched again) | `ticket_id`, `ticket_link`, `first_seen_utc`, `customer_name`, `customer_email`, `company_name`, `order_number`, `tracking_number`, `carrier_inferred`, `carrier_issue`, `insurance_on_package`, `classifier_confidence`, `priority`, `draft_note_id` |
| **Operator-owned** (empty on first insert; operator fills in by hand as claim progresses) | `claim_status`, `claim_number`, `carrier_filed_at`, `coverage_usd`, `resolution_amount_usd`, `reimbursement_received_at`, `operator_notes` |

**How the operator uses it, end-to-end:**

1. Ticket comes in → skill auto-creates a row with tracking #, carrier, insurance flag, etc.
2. Operator sees the packet card in the hourly digest and files the claim in the carrier portal.
3. Operator switches to the Sheet, finds the row (filter on `claim_status = ""`), and fills:
   - `claim_status` → `filed`
   - `claim_number` → the number the carrier assigned
   - `carrier_filed_at` → ISO timestamp
   - `coverage_usd` → what they filed for
4. Days/weeks later when the carrier resolves: fill `resolution_amount_usd` and `reimbursement_received_at`; flip `claim_status` to `paid` or `denied`.
5. Sort by `claim_status`, or filter on `carrier_filed_at = ""` to see what's still unfiled.

**No code change to `sheets_sync.py`** is required for the base Tier 0 feature — `enrich_carrier_rows()` (`sheets_sync.py:421`) already infers the carrier via tracking-number regex if the ticket skill doesn't supply one, and the dedupe-by-`ticket_id` check (`sheets_sync.py:529`) already protects operator edits.

### 5.3 Recommended schema addition: `filing_deadline_iso`

Single highest-leverage ergonomic win for lifecycle management: add `filing_deadline_iso` as a **new skill-owned column** in `carrier_issue_log` so the operator can sort the Sheet by deadline proximity and see what's about to go past its window. Safe change:

- Add `"filing_deadline_iso"` to the skill-owned list in `scripts/sheets_schema.py` `TAB_COLUMNS["carrier_issue_log"][0]`.
- Run `scripts/sheets_bootstrap.py` (or manually add the header cell) to extend the live Sheet. Existing rows get empty cells; new rows populate on write.
- No risk to invariant #8 — additions to skill-owned are safe; only operator-owned columns are inviolable.
- Existing `outputs/kpi/carrier_issue_log.csv` mirror: header-name logic at `sheets_sync.py:291-299` keeps the pre-existing CSV header on disk if one exists. Operators who want the new column in the CSV can delete `outputs/kpi/carrier_issue_log.csv` and let it re-seed on the next run with the extended header. Recovery truth is preserved either way.

### 5.4 Recovery surface: `outputs/kpi/carrier_issue_log.csv`

**CSV mirror is unchanged and still written on every run**, per invariant #9. `write_local_mirror()` in `sheets_sync.py:261` writes the CSV *before* the Sheets API is even attempted — if the Sheets API fails, the run still logs the row to disk and the payload queues to `config/sheets_pending_sync.json` for retry. Columns pin to `TAB_SCHEMAS["carrier_issue_log"]` on first creation; existing CSVs keep their original header (schema-drift tolerant).

Operators don't work from the CSV day-to-day — the Sheet is the surface. The CSV is there if Sheets ever goes sideways or you need a local grep-able audit trail.

### 5.5 The packet's richer fields (description, evidence, deadline-days-remaining)

The enriched `claim_packet` fields from §2 (`damage_or_loss_description`, `evidence_summary`, `claim_type`, etc.) live in the **digest only** — they're context for *filing*, not lifecycle columns worth persisting in the Sheet. Keeping them out of the Sheet avoids bloating rows with data that loses relevance the moment the claim is filed. The one exception — `filing_deadline_iso` — is precisely the field whose value persists across the whole lifecycle, which is why it's the one recommended addition in §5.3.

---

## 6. Explicit non-goals (each tied to a load-bearing invariant)

| Non-goal | Invariant |
|---|---|
| Do NOT fetch, download, or inline photo attachments into model context | `CLAUDE.md` invariant #7 |
| Do NOT submit anything to carrier portals | That's Tier 1 — out of scope |
| Do NOT write to operator-owned Sheets columns (`claim_status` et al.) | `CLAUDE.md` invariant #8 |
| Do NOT mutate HubSpot tickets beyond the existing draft-reply internal note | `CLAUDE.md` invariant #5 |
| Do NOT change `auto_send_form_topics` behavior | Not needed; customer-reply auto-send path remains as-is |
| Do NOT invent declared value, ship date, or deadline when source data is missing — flag as blocking_info | `CLAUDE.md` invariants #3 and #4 |
| Do NOT commit to filing outcome in the customer-facing drafted reply | `CLAUDE.md` invariant #4 + existing language patterns in `kb/damage_claims.md` |

---

## 7. Files to modify during execution

| File | Change |
|---|---|
| `prompts/extract_actions.md` | Add `claim_packet` sub-schema spec to `file_carrier_claim` section. ~20 lines. |
| `.claude/skills/hubspot-actions-digest/SKILL.md` | Add a rendering branch for `action_type == "file_carrier_claim"` with `claim_packet` present. Computes `days_until_deadline` at render time. ~40 lines of new skill logic + the packet card template. |
| `kb/damage_claims.md` | Add `## Carrier portal deep-links` section with the table in §4. ~15 lines. |
| `config/categories.yaml` | No change — `damaged_goods` routing is fine. |
| `scripts/sheets_sync.py` | No change. Existing regex-based `enrich_carrier_rows()` still provides a fallback for `carrier_inferred` when the ticket skill doesn't set it. |
| `scripts/sheets_schema.py` | **Recommended**: add `"filing_deadline_iso"` to `TAB_COLUMNS["carrier_issue_log"][0]` (skill-owned list). See §5.3. |
| `scripts/sheets_bootstrap.py` | Run after schema update to extend the live Sheet header; existing rows get an empty cell for the new column. |

---

## 8. Verification (for the execution phase)

1. **Unit-ish: prompt output.** Feed the extractor three representative tickets (UPS damage with full photos, USPS loss with no order #, unknown-carrier with only a form-topic string). Confirm the extractor emits a well-formed `claim_packet` with the right `inferred_carrier`, sensible `evidence_summary`, and correct `blocking_info_needed` entries for missing data. Inspect the raw action-item JSON in `outputs/runs/<latest>.md` before it hits the digest.

2. **Digest rendering.** Run `hubspot-actions-digest` with `dry_run: true` against a queue containing one synthetic ticket with a fully-populated `claim_packet`, one with `inferred_carrier == "unknown"`, one with `days_until_deadline == 5`. Inspect `outputs/digests/<latest>.dry-run.md` and confirm:
   - Packet card renders in place of the one-liner.
   - Deadline tag shows `⚠ only 5 days left` on the near-deadline case.
   - `Portal: —` with the `(pick carrier manually)` note on the unknown-carrier case.
   - Low-confidence marker renders when `carrier_confidence == "low"`.
   - Copy-paste block renders inside the fence separators.

3. **Back-compat.** Add one synthetic queued record with `action_type == "file_carrier_claim"` but no `claim_packet` field. Confirm the digest still renders the one-line fallback without errors.

4. **Live end-to-end.** Once both of the above pass, flip to `dry_run: false` with `per_run_cap: 1` and wait for a real form-sourced Carrier Issue ticket with photos. Verify:
   - The ticket hit the auto-send path (that behavior is unchanged — customer gets their reply as always).
   - The ticket ALSO queued a `file_carrier_claim` action item with a populated `claim_packet`. (Form-sourced Carrier Issue tickets currently go to auto-send and are NOT queued — this is a cross-check that may reveal the need to queue claim-filing action items separately from the customer-reply queue. If so, surface it as a follow-up; it's a queue-routing issue, not a packet-rendering issue.)
   - The next hourly digest rendered the packet card correctly.
   - The `carrier_issue_log` row in Sheets has the skill-owned columns populated and operator-owned columns empty.
   - Operator files the claim from the packet and reports the seconds-elapsed. Target: <60s.

5. **Metrics collection.** Log operator seconds-per-claim for the first ~20 real claims. If median >2 minutes, there's something wrong with the packet (wrong field, missing field, bad deep-link) — iterate.

---

## 9. Known gotcha to surface during build

Form-sourced `Carrier Issue` tickets go through the **auto-send** path (`auto_send_form_topics` in `config/categories.yaml:31-33`), which — per the existing `hubspot-tickets` SKILL — does NOT queue to `pending_actions.json`. Action items extracted from auto-send tickets today are either (a) urgent-emailed or (b) dropped. This means the current pipeline may not actually be queueing `file_carrier_claim` items for Carrier Issue form tickets at all, and they'd never reach the digest.

**Before implementing the packet rendering**, verify the auto-send path's action-item handling: read `.claude/skills/hubspot-tickets/SKILL.md` in detail, specifically the form-sourced Mispack/Carrier branch, and confirm where action items go. If they're dropped, this spec needs a companion small change: route `file_carrier_claim` (and any non-reply action items) into `pending_actions.json` even on the auto-send path, so they surface in the digest alongside the customer-reply confirmation. That's still Tier 0 scope but adds ~10 lines to the ticket skill.

This gotcha was partially visible in research but only auditable during build — flagging it here so the execute phase investigates before starting the renderer.
