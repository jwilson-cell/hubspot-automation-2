# Category Reference

Narrative description of each ticket category, the customer language that usually signals it, and what info the reply should request if missing.

## wismo_tracking — "Where is my order?"

**Typical phrasing:** "I still haven't gotten my package", "tracking hasn't updated in X days", "it says delivered but nothing is here", "been stuck on In Transit forever".

**What the reply usually needs:** tracking number (to look up carrier state), order number, delivery address confirmation. See `tracking_wismo.md` for the decision tree on when to recommend waiting vs filing a lost-package investigation.

## shipping_delay — "Why hasn't my order shipped yet?"

**Typical phrasing:** "order placed X days ago, hasn't shipped", "you missed the cutoff again", "my customer is angry — when will this go out?".

**What the reply usually needs:** order number, when it was placed. Check cutoff rules and current warehouse capacity. If genuinely a Pack'N miss, own it.

## damaged_goods — "It arrived broken."

**Typical phrasing:** "arrived damaged", "box crushed", "product broken", "customer is returning it".

**What the reply usually needs:** tracking number, photos (outer box w/ label, product, internal packaging). Claim window is usually 10 days from delivery. See `damage_claims.md`.

## mispack_wrong_item — "You sent the wrong thing."

**Typical phrasing:** "received wrong SKU", "missing item in my kit", "quantity is short", "got someone else's order".

**What the reply usually needs:** order number, SKU expected, SKU received, ideally a photo of the shipping label. See `mispack_process.md` for investigation flow.

## inventory_discrepancy — "Stock counts are off."

**Typical phrasing:** "my system shows X units but your portal shows Y", "ran out even though I should have had stock", "cycle count was short".

**What the reply usually needs:** SKU(s) in question, the merchant's system count vs. Pack'N's count, the date of last sync. See `inventory_accuracy.md`.

## inbound_asn_receiving — "My inbound shipment had issues."

**Typical phrasing:** "my PO hasn't been received yet", "you received the wrong quantity on my ASN", "can't schedule a dock appointment", "unknown SKU on receiving".

**What the reply usually needs:** PO/ASN number, carrier tracking for the inbound, expected delivery date, SKU list.

## returns_rma — "Customer wants to return this."

**Typical phrasing:** "need a return label", "how long until the refund processes", "what happens to the returned item".

**What the reply usually needs:** original order number, reason for return, desired disposition (restock/refurb/dispose). See `returns_rma.md`.

## integration_system — "Something broke in the integration."

**Typical phrasing:** "orders aren't flowing", "unknown SKU error", "address was rejected", "EDI failed", "API returned 500".

**What the reply usually needs:** affected order numbers, error message/screenshot, the channel/integration involved (Shopify, Amazon, EDI, custom API). See `integration_errors.md`.

## billing_invoice — "Your invoice is wrong."

**Typical phrasing:** "overcharged for storage", "what's this accessorial", "pick-pack rate seems high", "long-term storage fee surprise".

**What the reply usually needs:** invoice number, specific line item being disputed. Never commit to adjustments — route to billing specialist. See `billing_faq.md`.

## sla_escalation — "You're not responding fast enough."

**Typical phrasing:** "escalating this", "third time I've asked", "your SLA says 24 hours", "speaking to your manager".

**What the reply usually needs:** acknowledge the escalation explicitly, own any miss, give a concrete next-contact time, flag as `escalate_to_account_manager` action item.

## account_onboarding — "Help me get started / change my setup."

**Typical phrasing:** "we're onboarding", "need SOPs", "changing our bill-to", "updating label requirements".

**What the reply usually needs:** what stage of onboarding, what's blocking them. Usually route to the merchant's AM.

## kitting_project — "Need a special build / promo."

**Typical phrasing:** "subscription box", "holiday kit", "promo insert", "need a custom project".

**What the reply usually needs:** component SKUs, quantities, timeline, special packaging requirements. Usually route to AM + projects team.

## address_recipient — "Wrong address / address issue."

**Typical phrasing:** "wrong address on the order", "customer moved", "PO box issue", "carrier couldn't deliver".

**What the reply usually needs:** order number, corrected address, whether the package has already shipped. If already shipped, address correction may incur a carrier accessorial.

## retailer_compliance — "Amazon/Walmart/Target chargeback."

**Typical phrasing:** "got a chargeback from Walmart", "routing guide violation", "Amazon shipment reject".

**What the reply usually needs:** chargeback document/reference, PO number, SKUs. Treat seriously — financial exposure. See `integration_errors.md` for compliance section.

## other_unclassified — Fallback.

**When used:** classifier confidence < 0.5 OR ticket genuinely doesn't fit any of the above (e.g., general inquiries, marketing outreach, partnership proposals, off-topic).

**What the reply usually needs:** acknowledge receipt, ask one clarifying question, flag for human triage.

## order_split — "Split Ship/Partial Ship"

**Typical phrasing:** "I need this partially shipped", "Split ship", "Ship what is available", "Ship and disgregard [PLACEHOLDER_ITEM]".

**What the reply usually needs:** acknowledge receipt, confirmation on order split, flag for human triage.