<!-- GENERATED from packn-os src/lib/tickets/reply-rulebook.ts (version 2026-10-15.2). Do not edit here: edit the OS file and run scripts/export-reply-rulebook.ts. -->

## wismo_tracking: Where is my order?

Goal: Tell them where the package is according to the live data, and what happens next.

Check first:
- The order's WMS state: shipped, partially shipped, or not shipped yet.
- Carrier status, last scan, carrier ETA, delivery proof and signer.
- A claim on file, and earlier tickets about the same order.

Ask for, if missing (never re-ask what the conversation already gave):
- The order number. Nothing else up front.

Situations:
- When: Not shipped yet. Reply: It hasn't shipped. Describe the state plainly (held for inventory, picks generated, carrier rate bound). Quote only the carrier's ETA. Action item: warehouse_investigation, only if the state is unexplained.
- When: Partially shipped. Reply: Give the carrier and tracking for the part that shipped, and say plainly that the rest has not shipped yet. Never say the order was fulfilled. Action item: expedite_fulfillment.
- When: In transit, last scan under 3 business days old. Reply: The current scan and the carrier ETA. Ask them to reply here if there's no movement in the next 2-3 business days.
- When: In transit, no scan for 3 or more business days. Reply: It has stalled. We're opening a trace now, and a reship or refund is being escalated for review so they don't wait on the carrier. Action item: carrier_trace + reship_order.
- When: Delivered, but the customer says it never arrived. Reply: The delivery date and time, and the signer if there is one. Ask them to check with their household, neighbours or building office; if it's still missing, reply here and we'll make it right. Action item: none yet; when they reply that it is still missing: reship_order + file_carrier_claim.
- When: Carrier exception or return to sender. Reply: The exception reason in plain words. For a return to sender, ask whether to reship to a corrected address. Action item: edit_order, if returned to sender.
- When: Order not found. Reply: We couldn't find it. Ask them to double-check the number (a store prefix is sometimes missing). Action item: request_missing_info_from_merchant.

Never:
- Say the status is "unknown".
- Make a reship depend on a trace or claim result.
- Paste raw scan history.
