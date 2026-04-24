# Action-item extraction prompt — HubSpot ticket

You are extracting actionable items for Pack'N's internal team from a customer ticket. The output feeds an hourly digest email and an urgent immediate-email path.

## Input

Ticket context:
```
{ticket_context}
```

Category: `{category}`

Drafted reply (for context, since it often names next steps):
```
{drafted_reply}
```

## Allowed action types

Pick from this set. If none fit cleanly, use `other` and be specific in the description.

- `file_carrier_claim` — file a damage or loss claim with the carrier (needs tracking #, photos, declared value). **Emit with the extended `claim_packet` sub-schema — see §"Extended schema: file_carrier_claim" below.**
- `warehouse_investigation` — pull camera footage, audit picker, recount cycle (needs wave ID or date range)
- `create_rma` — issue a return label / RMA
- `reship_order` — send replacement goods (specify cost owner: 3PL / merchant / carrier)
- `issue_credit_or_refund` — process credit memo or refund (needs approval)
- `sync_sku_master` — add/update SKU in WMS, fix integration mapping
- `escalate_to_ops_manager` — route to operations leadership
- `escalate_to_account_manager` — route to merchant's AM
- `update_asn_or_appointment` — fix ASN data or rebook receiving appointment
- `request_missing_info_from_merchant` — ask merchant for photos, PO#, order #, etc.
- `billing_specialist_follow_up` — invoice or rate question needing billing's input
- `other` — describe

## Output

Return ONLY a JSON array (possibly empty). Each element:

```json
{
  "action_type": "<one of the above>",
  "description": "<what needs to happen, in an imperative sentence>",
  "owner_hint": "warehouse" | "account_manager" | "billing" | "integration" | "ops_manager" | "merchant" | "carrier",
  "blocking_info_needed": ["<list of missing inputs, empty if none>"],
  "severity": "urgent" | "normal",
  "needs_hubspot_reply": true | false
}
```

## Rules

1. **Empty array is valid.** Informational replies (FAQ, policy clarifications) often have no internal action item. Do not invent work.
2. **One action per item.** If a ticket triggers both a warehouse investigation AND a carrier claim, emit two objects.
3. **Severity = urgent** when: >$1k financial exposure, active chargeback, legal threat, contract cancellation risk, retailer compliance violation with financial penalty, or merchant explicitly escalating. Otherwise `normal`.
4. **`blocking_info_needed`** should list specific missing inputs (e.g., `["photos of damaged goods", "tracking number"]`), not vague wishes.
5. **Do not duplicate** the customer-facing reply content. This is for internal routing, not the customer.
6. **`needs_hubspot_reply`** — set `true` if completing this action requires sending a follow-up message to the customer in the HubSpot ticket thread. Set `false` if it's a backend task whose outcome may or may not produce a future customer-facing reply.
   - Typically `true`: `request_missing_info_from_merchant`, `issue_credit_or_refund` (customer must be told), `billing_specialist_follow_up` when the answer needs to be conveyed back, `create_rma` (customer needs the label/instructions).
   - Typically `false`: `file_carrier_claim`, `warehouse_investigation`, `sync_sku_master`, `update_asn_or_appointment`, `escalate_to_ops_manager`, `escalate_to_account_manager` (those are internal routing).
   - Use judgment — the question is whether the reviewer must compose a customer-facing reply to close the loop on THIS action item.

## Extended schema: `file_carrier_claim`

When emitting a `file_carrier_claim` action item, attach a `claim_packet` sub-object with the fields the operator needs to file the claim from the hourly digest. The packet lets the operator paste straight into the carrier portal in ~30 seconds instead of re-digging everything out of the ticket.

```json
{
  "action_type": "file_carrier_claim",
  "description": "File UPS damage claim for tracking 1Z999AA10123456784",
  "owner_hint": "carrier",
  "blocking_info_needed": ["declared_value"],
  "severity": "urgent" | "normal",
  "needs_hubspot_reply": false,
  "claim_packet": {
    "inferred_carrier": "UPS" | "FedEx" | "USPS" | "DHL" | "Amazon" | "OnTrac" | "LTL" | "unknown",
    "carrier_confidence": "high" | "medium" | "low",
    "claim_type": "damage" | "loss" | "delay",
    "tracking_number": "1Z999AA10123456784",
    "order_number": "PN-48221",
    "ship_date_hint": "2026-04-14" | null,
    "declared_value_hint": null,
    "declared_value_source": null,
    "damage_or_loss_description": "Box arrived crushed; contents broken per customer photos",
    "evidence_summary": "3 photos on ticket (review on HubSpot before filing)",
    "insurance_on_package": true | false | null,
    "filing_deadline_iso": "2026-06-13" | null
  }
}
```

### Derivation rules

**Primary source: `ticket_context.ssk_state`.** The ticket skill hydrates ShipSidekick state for Carrier Issue / WISMO / damaged-goods tickets before this prompt runs. When `ssk_state.found == true`, trust it — it's canonical:

- `inferred_carrier` ← `ssk_state.shipments[0].carrier_code` (e.g., `"UPS"`). `carrier_confidence: "high"`.
- `tracking_number` ← `ssk_state.shipments[0].tracking_code`. Prefer this over the form field if they disagree.
- `order_number` ← `ssk_state.order.name` (or `.alias` if more recognizable).
- `ship_date_hint` ← date portion of `ssk_state.shipments[0].created_at` (label-creation time; defensible ship-date proxy). If the ticket's tracking number matches a specific shipment in the array, use that one.

**Fallback when SSK lookup failed** (`ssk_state.found == false` or `ssk_state` absent):

- `inferred_carrier`: use the form field `ticket_context.form_fields.carrier_issue` if it names a carrier. Else infer from `tracking_number` pattern:
  - `^1Z[A-Z0-9]{16}$` → UPS (`high` confidence)
  - `^(EA|EC|LK|RA|RB|RD|RR|VA)[0-9]{9}US$` → USPS (`high`)
  - `^JD[A-Z0-9]{16,20}$` → DHL (`high`)
  - `^TBA[0-9]{9,12}$` → Amazon (`high`)
  - `^[CD][0-9]{14}$` → OnTrac (`high`)
  - 12-or-15-digit all numeric → FedEx (`medium`)
  - 20-or-22-digit starting `9` → USPS (`medium`)
  - no match → `"unknown"`, `low` confidence; operator picks carrier manually.
- `ship_date_hint`: null. Don't infer from `hs_createdate` — that's the ticket date, not the ship date. The digest renders an approximate deadline with a `~` prefix in this case.

**Neither source provides:**

- `declared_value_hint`: always null in the current wrapper. Do NOT guess. Add `"declared_value"` to `blocking_info_needed`. Operator fills from the order system at file time.
- `declared_value_source`: null.

**Other fields:**

- `claim_type`:
  - `damaged_goods` → `"damage"`
  - `wismo_tracking` with "lost", "never arrived", "stolen" keywords → `"loss"`
  - `shipping_delay` → `"delay"`
- `damage_or_loss_description`: one factual sentence, shipper voice (goes straight into the carrier's "what happened" field). No speculation about root cause. Example: *"Box arrived crushed; customer reports all 4 units inside broken."* — NOT *"Carrier likely mishandled the package."*
- `evidence_summary`: based ONLY on the `has_attachments` flag / association count already on `ticket_context`. Do NOT fetch or describe photos. Examples:
  - `"3 photos on ticket (review on HubSpot before filing)"`
  - `"No photos yet — request from customer before filing"`
  - `"Photos mentioned in body but not attached — clarify with merchant"`
- `insurance_on_package`: mirror `ticket_context.form_fields.insurance_on_package` when present (`true`/`false`); else `null`.
- `filing_deadline_iso`: computed as `ship_date_hint + carrier_window_days`. Carrier windows: UPS 60, FedEx 60, USPS 60, DHL 30, Amazon 30, OnTrac 90, LTL 270, unknown → 60 (conservative). If `ship_date_hint` is null, leave `filing_deadline_iso` null — the digest will handle approximation at render time.

### When to omit the packet

If `tracking_number` is missing AND cannot be derived from SSK, emit the action item WITHOUT the `claim_packet` sub-object. The digest has a fallback one-line renderer for pre-packet records. Adding an empty `blocking_info_needed: ["tracking_number"]` alerts the operator that the first step is recovering the tracking number.
