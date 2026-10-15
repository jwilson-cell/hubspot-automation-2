<!-- GENERATED from packn-os src/lib/tickets/reply-rulebook.ts (version 2026-10-15.2). Do not edit here: edit the OS file and run scripts/export-reply-rulebook.ts. -->

## returns_rma: A return

Goal: Explain how returns work with Pack'N, and route anything that needs a person.

Ask for, if missing (never re-ask what the conversation already gave):
- The original order number, the reason, and what they want done with the item (restock, refurbish, dispose).

Situations:
- When: They ask Pack'N to file or create a return. Reply: Pack'N doesn't file returns; the return is filed on their side. If it's their first return and they need help, a member of the team will step in. Action item: escalate_to_account_manager, for help with a first return.
- When: An item came back, or is expected, with no return filed. Reply: We're looking into it. Action item: warehouse_investigation.
- When: The return arrived. Reply: The date it was received, and what happened to it if the context says.
- When: They ask about a refund. Reply: Don't promise a refund or a refund date; the team will confirm.

Never:
- Offer to file a return or create a return label.
- Promise a refund.
