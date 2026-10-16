<!-- GENERATED from packn-os src/lib/tickets/reply-rulebook.ts (version 2026-10-16.1). Do not edit here: edit the OS file and run scripts/export-reply-rulebook.ts. -->

## inbound_asn_receiving: My inbound had issues

Goal: Give the status of the receipt, and state any variance as a fact with a choice.

Ask for, if missing (never re-ask what the conversation already gave):
- The PO or ASN number, the inbound tracking or BOL, and the affected SKUs.

Situations:
- When: Not received yet. Reply: Ask for the inbound tracking or BOL so we can watch for it.
- When: Received, not put away yet. Reply: We aim to have inbound put away within a week of receiving it.
- When: Received with a variance. Reply: State the expected quantity vs the counted quantity, and ask whether to hold it for their review or adjust to the physical count. Action item: update_asn_or_appointment.
- When: Unknown SKU on the inbound. Reply: It isn't in our catalog yet; ask for the SKU details. Action item: sync_sku_master.

Always:
- The context has no live receiving data. When the receipt status isn't in it, write "let me confirm with the receiving team".

Never:
- Promise a putaway date sooner than "within a week of receipt".
