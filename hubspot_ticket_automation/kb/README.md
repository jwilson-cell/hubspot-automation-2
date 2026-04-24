# Knowledge Base — HubSpot Ticket Automation

This folder is the grounding source for every drafted reply. The automation reads these files per-ticket based on the classifier category.

## How to use

1. Every file here is loaded into the reply-drafting prompt when its category matches.
2. `brand_voice.md` and `glossary.md` are loaded on **every** reply.
3. All files ship with **industry-standard 3PL content** from the start. Look for `**TODO (Pack'N):**` markers — those are the places to fill in Pack'N-specific policy (rates, SLA commitments, team contacts, escalation paths). The automation works without filling TODOs, but drafts will say "let me confirm with the team" where Pack'N specifics are missing.

## Files

| File | Category mapping | Purpose |
|---|---|---|
| `brand_voice.md` | always | Tone, sign-off, do/don't |
| `glossary.md` | always | 3PL terminology |
| `categories.md` | always (reference) | Narrative of each category + typical customer phrasing |
| `tracking_wismo.md` | wismo_tracking, shipping_delay, address_recipient | Carrier states, when to escalate |
| `damage_claims.md` | damaged_goods | Claim flow, photo requirements, timelines |
| `mispack_process.md` | mispack_wrong_item | Investigation + reship decision tree |
| `inventory_accuracy.md` | inventory_discrepancy | Cycle count norms, reconciliation language |
| `returns_rma.md` | returns_rma | Disposition options, refund timing |
| `billing_faq.md` | billing_invoice | Storage, accessorial, chargeback FAQ |
| `integration_errors.md` | integration_system, inbound_asn_receiving, retailer_compliance | Unknown SKU, address errors, EDI, routing guides |

## Editing guidance

- Prefer short, scannable bullets over prose.
- When you add a new policy, add it under the relevant existing file rather than creating a new one (keeps retrieval simple).
- Never put secrets, customer PII, or system credentials here. The KB gets loaded into every prompt.
