# Carrier claim filing automation — feasibility memo

**Date:** 2026-04-23
**Author:** Luca (via Claude)
**Status:** Draft — awaiting Pack'N input on open questions in §6
**Companion doc:** `research/operator_aid_claim_packet_spec.md` (the Tier 0 build spec this memo recommends)

---

## 1. Problem statement

Today when a damaged / lost / missing-package ticket comes through HubSpot, the ticket automation already does most of the thinking: it classifies the ticket as `damaged_goods` (or `wismo_tracking` for loss), drafts a customer reply grounded in `kb/damage_claims.md`, and — critically — emits a structured `file_carrier_claim` action item into the hourly digest queue (`prompts/extract_actions.md:23`).

But the queue is where the pipeline ends. A human (Luca or the ops team) then opens the ticket in HubSpot, re-reads the thread, re-copies the tracking number, opens the correct carrier's claim portal in a new tab, logs in with Pack'N's carrier-account credentials, retypes the tracking number, order number, declared value, damage description, uploads photos one-by-one, and submits. Empirically this is **~5–12 minutes per claim** depending on carrier, plus the context-switch tax of getting into "claim-filing mode" vs. ticket-triage mode.

The `carrier_issue_log` tab in Pack'N's Sheets workbook has been designed around this flow — operator-owned columns `claim_status`, `claim_number`, `carrier_filed_at`, `coverage_usd`, `reimbursement_received_at`, `resolution_amount_usd`, `reimbursement_received_at` are explicitly reserved for manual fill-in after filing (`CLAUDE.md` invariant #8). Everything is in place for an automation to close the last mile — *if* one is technically feasible.

**The question this memo answers:** how much of the end-to-end "ticket → filed claim" flow can actually be automated today, given (a) carrier APIs, (b) Pack'N's existing invariants, (c) shipper-of-record constraints?

**The short answer:** end-to-end auto-filing is not viable today. The highest-leverage win is **Tier 0** — have the automation assemble a ready-to-file claim packet per ticket, rendered inline in the existing digest with a carrier-portal deep-link. Operator reviews and files in ~30s instead of ~10min. Tiers 1–3 are worth tracking but not worth building yet.

---

## 2. Per-carrier feasibility

| Carrier | Public claims API? | Filing channel | Required inputs | Filing window (damage) | Shipper-of-record gating | Source |
|---|---|---|---|---|---|---|
| **UPS** | No | [UPS File a Claim dashboard](https://www.ups.com/us/en/support/file-a-claim) | Tracking #, damage photos (outer box + product + interior), item description, declared value (receipt/invoice), contact | 60 days from delivery (damage); 9 months (loss) | Shipper-of-record or receiver only. Third-party filing requires letter of authorization. | ups.com/us/en/support/file-a-claim |
| **FedEx** | No — [dev portal](https://developer.fedex.com/) has Ship / Track / Rate APIs but no Claims endpoint | FedEx.com claims portal (authenticated) | Tracking #, photos, declared value, commercial invoice, damage description, proof of value | 60 days (damage), 9 months (loss), 21 days for international | Account holder only | developer.fedex.com (Claims not listed); fedex.com/en-us/customer-support/claims.html |
| **USPS** | No | [USPS claims portal](https://www.usps.com/help/claims.htm) | Tracking #, original mailing receipt, photos, proof of value (invoice or online order), damage description | 60 days from mailing (insured domestic); varies for intl | Either sender or recipient can file | usps.com/help/claims.htm |
| **DHL Express** | No — dev portal has Track / Ship / Rate; no Claims endpoint | [DHL Express claim form (PDF)](https://mydhl.express.dhl/content/dam/downloads/us/en/claims/dhl_express_claim_form_us_en.pdf.coredownload.pdf), emailed to claims office | Tracking (waybill) #, invoice, photos, damage description | 30 days from delivery for damage; varies for loss | Account holder only | mydhl.express.dhl claims form |
| **LTL (XPO, ArcBest, Saia, Old Dominion, etc.)** | Emerging only — NMFTA Digital LTL Council has an API roadmap in progress but no live cargo-claims API | Carrier-specific portals or emailed claim forms; some accept OCR'd BOL via portal | BOL, delivery receipt, photos, commercial invoice, damage/shortage description, weight | 9 months (Carmack Amendment federal default) | Bill-to party or shipper on BOL | nmfta.org Digital LTL Council roadmap |

**Takeaways:**
- Zero major US parcel carriers expose a public claims API in 2026. This is a stable reality — it has not changed meaningfully in 3+ years.
- Filing windows are tight enough that a missed-window risk is real: UPS/FedEx damage at 60 days, DHL at 30 days. A backlogged operator could silently cost Pack'N money.
- Every carrier requires photos. Pack'N's invariant #7 forbids hydrating photo attachments into model context, so the automation physically cannot build a complete submission payload on its own — the operator must be in the loop to handle photo upload at minimum.
- Shipper-of-record is confirmed for Pack'N (per user). That clears the authorization hurdle that would otherwise block third-party filing.

---

## 3. Third-party aggregators

Services that file claims across multiple carriers on the shipper's behalf.

| Service | Integration shape | Fee model (typical) | Fit for Pack'N |
|---|---|---|---|
| [LateShipment.com](https://www.lateshipment.com/) | Dashboard + event-driven (carrier webhooks trigger claim detection); no public REST API for filing submissions | Revenue-share on recovered claims (~25–50%) | Possible Tier 2 — useful if Pack'N wants to offload filing entirely and pay a share. Losses: data residency, lead time, no code-level control. |
| [Shipsurance](https://www.shipsurance.com/) | Has an API for *insuring* shipments at label-creation time; claims filing is semi-automated via their eReport software. Requires Shipsurance to be the insurance provider on the shipment. | Per-shipment insurance premium + claim payout | Only a fit if Pack'N moves package insurance to Shipsurance. Large architectural change. |
| [Refund Retriever](https://www.refundretriever.com/) | Human-ops service, no API. Primarily Amazon FBA + UPS/FedEx late-delivery refunds. | Revenue-share | Not a fit for damage/loss claim automation at Pack'N's scope. |
| [Loop Returns](https://www.loopreturns.com/) / Loop Marketplace | Returns-focused, not claims. Label API exists; claims not a documented product. | N/A | Not a fit. |

**Takeaway:** Aggregators are a viable outsourcing path if Pack'N ever wants to stop owning carrier claims. None of them offer a clean "POST /claim" API that Pack'N could code against. For Tier 0 (operator-aid), aggregators are not in the picture. For Tier 2 (fully outsourced), LateShipment is the realistic candidate to evaluate if/when the decision gets made.

---

## 4. Tiered roadmap

### Tier 0 — Packet assembly (RECOMMENDED, ship now)

**What:** Enrich the existing `file_carrier_claim` action item with every field the operator needs to file. Render it inline in the hourly digest as a ready-to-paste packet, with a carrier-portal deep-link. Operator reviews, clicks through, pastes, uploads photos from HubSpot, submits. Updates `carrier_issue_log` operator-owned columns after filing.

**Why now:** All infrastructure exists. Action type exists, queue plumbing exists, Sheets tab exists with reserved columns. Only gaps are (a) action-item schema enrichment, (b) digest rendering for the enriched fields, (c) a small carrier-portal URL lookup in `kb/damage_claims.md`. Estimated 1–2 days of build.

**Expected payoff:** ~10 min → ~30s per claim. If Pack'N files ~15 claims/week, that's ~2 hours/week saved, plus a material reduction in missed-window risk (packet shows a filing-deadline clock).

**Detailed spec:** `research/operator_aid_claim_packet_spec.md`.

**Risks:** Low. No new external integrations, no credential management, no automated submission. Honors every invariant in `CLAUDE.md` by construction.

### Tier 1 — Browser automation for a single carrier (defer 6–12 months)

**What:** After Tier 0 is stable, pick the carrier where Pack'N files the most claims (open question — see §6). Build a Playwright script that takes a reviewed packet, logs into that carrier's portal with Pack'N's account (credentials via OS keyring or 1Password CLI), fills the form, uploads photos fetched via Gmail-authenticated HubSpot-file URLs, submits, captures the claim number. Requires an explicit human "approve and file" click per claim — not a cron.

**Why defer:** (a) we don't yet know which carrier dominates volume, (b) browser automation against vendor portals is brittle (DOM drift, CAPTCHA, session timeouts), (c) needs a secrets-management story we don't have yet. Tier 0 will give us the volume data to justify which carrier to target, and possibly prove the remaining friction is small enough that Tier 1 isn't worth the brittleness.

**Risks:** Medium. Carrier ToS on automation vary — UPS and FedEx both discourage scripted portal access in their terms. A fail-closed behavior + per-claim human approval mitigates but doesn't eliminate the risk of an account flag.

### Tier 2 — Third-party aggregator (alternative to Tier 1)

**What:** Integrate LateShipment.com (or equivalent). Hand off every `file_carrier_claim` action item to the aggregator via whatever integration they support (likely a CSV/API push or shared-inbox email). Pay the revenue-share. Stop running portal automation.

**Why consider:** If Tier 0 metrics show filing is still the bottleneck and Tier 1's brittleness is unattractive, outsourcing is a clean answer. Also a better fit if claim volume grows to where dedicated ops headcount would otherwise be needed.

**Risks:** Low operationally, higher in lost recovery economics (aggregator takes 25–50% of recovered value). Also data-residency implications worth reviewing with Pack'N legal.

### Tier 3 — Native carrier APIs (monitor)

**What:** Subscribe to UPS / FedEx / USPS / DHL developer-portal changelogs and NMFTA Digital LTL Council announcements. When any carrier ships a claims API, re-scope.

**Why monitor:** Unlikely within 12–18 months based on roadmap signals, but the LTL space is moving faster than parcel.

---

## 5. Compliance & constraint notes

All design choices below are load-bearing constraints for Tiers 0 and 1.

- **Shipper-of-record:** Pack'N confirmed. No authorization letter dance needed for the carriers where Pack'N ships on its own accounts. For merchants on their own carrier accounts, the automation should flag that claim as "merchant-files, send packet to AM" rather than queue it for Pack'N to file. (Not in Tier 0 scope — see open questions.)
- **Claim windows:** UPS/FedEx parcel 60 days (damage), DHL 30 days, LTL 9 months federal default. Pack'N's internal `kb/damage_claims.md` currently notes "typically 60 days from ship date" with a Pack'N TODO to confirm stricter internal windows. The operator-aid packet should render a **filing deadline clock** (days remaining) so near-deadline claims get visible priority.
- **Photo-evidence requirement:** every carrier requires photos. `CLAUDE.md` invariant #7 forbids hydrating attachments into model context. Consequence: the automation CANNOT package a complete submission. The operator must always be the one handling photo upload from the HubSpot ticket directly. This kills the dream of 100% unattended filing even under Tier 1 — the best Tier 1 can do is a browser-automated form fill that prompts the operator to attach photos.
- **Invariant #3/#4 (never invent policy, never commit to outcomes):** the drafted customer reply must not promise that the claim will be filed, approved, or paid. This is already enforced in `kb/damage_claims.md` language patterns — just re-confirm in the Tier 0 spec.
- **Invariant #5 (read-only on HubSpot tickets except notes):** the automation cannot write the carrier claim number back onto the HubSpot ticket as a property. It can mention the claim number in a new internal note after filing, but Tier 0 does not automate post-filing note-writing — operator updates Sheets, and that's the system of record for claim lifecycle.
- **Invariant #8 (operator-owned Sheet columns are inviolable):** `claim_status`, `claim_number`, `carrier_filed_at`, `coverage_usd`, `resolution_amount_usd`, `reimbursement_received_at` are operator-fill-only. The skill writes them as empty strings on first insert and never touches them again — `sheets_sync.py` already enforces this. Tier 0 needs zero changes in `sheets_sync.py`.

---

## 6. Open questions for Pack'N

These do not block Tier 0 build but sharpen its design and are prerequisites for Tier 1 scoping.

1. **Claim volume by carrier.** Roughly how many claims per week, split by UPS / FedEx / USPS / DHL / LTL? The answer picks the target carrier for Tier 1. A back-of-envelope can be pulled from six months of `carrier_issue_log` after Tier 0 runs, but a current estimate would help scope.
2. **Canonical source for declared value.** Where should the automation look? Merchant-provided per-SKU data? Order total from the shipping label? The Shopify / merchant cart? ShipSidekick (hydrated via `scripts/ssk_order_lookup.py`) does NOT expose declared value in its current response shape — only order/fulfillment status, target delivery date, and per-shipment tracking/carrier metadata. So Tier 0 will default to "operator fills in" and flag it as `blocking_info_needed`. Worth asking Pack'N whether SSK exposes a declared-value field that the current wrapper isn't pulling, or whether this truly requires a separate lookup (merchant cart / Shopify order total / declared-value on the shipping label).
3. **Insurance-on-package default.** The intake form captures `insurance_on_package` as a boolean. What's Pack'N's policy for uninsured packages — still file, or skip? The packet should surface this per claim so the operator doesn't waste time filing a guaranteed-denial.
4. **High-value claim escalation threshold.** `kb/damage_claims.md` has a Pack'N TODO — default is $1,000. Any claim above threshold should auto-tag `escalate_to_ops_manager` alongside the `file_carrier_claim` action. Confirm the number.
5. **Merchant-files-their-own-claims exceptions.** Any merchants on their own carrier accounts where Pack'N is NOT shipper-of-record? If so, those tickets need a different packet variant ("email this to the merchant's AM") rather than the Pack'N-files default.
6. **Carrier-account credential storage (Tier 1 prerequisite).** If/when Tier 1 is greenlit, where do Pack'N's UPS / FedEx / etc. account credentials live? Is there a 1Password Business account or similar? Not blocking now, but worth surfacing.

---

## 7. Recommendation

Build Tier 0 per `research/operator_aid_claim_packet_spec.md`. Let it run for 4–8 weeks. Then re-evaluate Tier 1 vs. Tier 2 with actual data:

- If operator seconds-per-claim on Tier 0 is already <1 minute and missed-window rate is near-zero → stop. The juice isn't worth squeezing.
- If a single carrier dominates (>60% of claim volume) and filing friction is still the bottleneck → scope Tier 1 for that carrier only.
- If claim volume has grown past ~30/week or spans 4+ carriers → evaluate Tier 2 (LateShipment) because Tier 1's per-carrier build cost doesn't amortize.

Do not build Tiers 1–3 speculatively.
