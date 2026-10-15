<!-- GENERATED from packn-os src/lib/tickets/reply-rulebook.ts (version 2026-10-15.1). Do not edit here: edit the OS file and run scripts/export-reply-rulebook.ts. -->

## mispack_wrong_item: You sent the wrong thing

Goal: Get what we need in one ask, and move fast to make it right.

Check first:
- The order's line items, and the SKU received vs expected in the ticket.
- Whether the form asked for an immediate reship or a credit amount.
- Investigation findings, and a replacement order.

Ask for, if missing (never re-ask what the conversation already gave):
- The SKU received, the SKU expected, the quantity, and a photo of the shipping label.

Situations:
- When: Information missing. Reply: Apologise, ask for exactly the missing items, and say why (it rules out a package mix-up). Action item: request_missing_info_from_merchant.
- When: Information complete, not investigated yet. Reply: We're pulling the pick record this shift and escalating a reship for review. Action item: mispack_investigation + reship_order.
- When: The form asked for an immediate reship. Reply: Acknowledge the reship request; it's being processed for review. Action item: reship_order (priority).
- When: The form asked for a credit. Reply: Acknowledge it; billing will review it. Name no amount and no approval. Action item: billing_specialist_follow_up.
- When: Findings confirm our error. Reply: Own it plainly. A reship of the right SKU is being arranged (give tracking if it shipped). Ask whether they'd like to send the wrong item back; that is the filer's call. Credit questions go to billing. Action item: billing_specialist_follow_up, if a credit is asked for.
- When: Findings show the merchant's data or ASN was wrong. Reply: Explain plainly what the record shows, and ask how they want to proceed. Action item: escalate_to_ops_manager.
- When: A unit is missing but the order only partially shipped. Reply: This is not a mispack. Answer it as a partially shipped order: what shipped, and that the rest hasn't yet. Action item: expedite_fulfillment.

Never:
- Commit to a credit, a cost cover or a cap.
- Insist the wrong item is returned.
