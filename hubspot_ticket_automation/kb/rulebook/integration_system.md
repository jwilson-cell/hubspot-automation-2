<!-- GENERATED from packn-os src/lib/tickets/reply-rulebook.ts (version 2026-10-15.1). Do not edit here: edit the OS file and run scripts/export-reply-rulebook.ts. -->

## integration_system: Something broke in the integration

Goal: Confirm the scope (which orders, which channel) and that someone is on it.

Ask for, if missing (never re-ask what the conversation already gave):
- The affected order numbers, the exact error text, and the channel (Shopify, Amazon, EDI, API).

Situations:
- When: Orders aren't flowing. Reply: Ask for one or two example order numbers. Action item: escalate_to_ops_manager.
- When: Unknown SKU. Reply: It isn't in our catalog yet; ask for the SKU details. Action item: sync_sku_master.
- When: Address rejected. Reply: Ask for the corrected address. Action item: edit_order.
