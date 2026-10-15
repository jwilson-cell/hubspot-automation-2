<!-- GENERATED from packn-os src/lib/tickets/reply-rulebook.ts (version 2026-10-15.2). Do not edit here: edit the OS file and run scripts/export-reply-rulebook.ts. -->

## address_recipient: Wrong address

Goal: Find out whether it shipped: correct it before it ships, or attempt an intercept after.

Check first:
- Whether the order has shipped.

Situations:
- When: Not shipped yet. Reply: We'll correct the address before it ships. Action item: edit_order.
- When: Already shipped. Reply: We can ask the carrier to intercept it. It isn't guaranteed, and the carrier may charge for it. Action item: escalate_to_ops_manager.
- When: Returned to sender. Reply: Ask whether to reship to the corrected address. Action item: edit_order.

Never:
- Guarantee an intercept.
- Quote an intercept fee or say who pays it.

Not decided yet. If the reply needs one of these, write "let me confirm with the team":
- Who pays an intercept fee.
