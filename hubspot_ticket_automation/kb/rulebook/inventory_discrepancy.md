<!-- GENERATED from packn-os src/lib/tickets/reply-rulebook.ts (version 2026-10-15.2). Do not edit here: edit the OS file and run scripts/export-reply-rulebook.ts. -->

## inventory_discrepancy: Stock counts are off

Goal: Acknowledge it, pin down the SKUs and both counts, and tell them the warehouse is checking now.

Check first:
- Investigation findings from the warehouse.

Ask for, if missing (never re-ask what the conversation already gave):
- The SKUs, their count vs ours, and when they last synced.

Situations:
- When: Count mismatch, no findings yet. Reply: The warehouse is checking the count this shift and correcting it on the spot; we'll share what we find. Action item: warehouse_investigation.
- When: Findings in the context. Reply: State the findings in plain words: what was counted and what was corrected.
- When: They ran out while expecting stock. Reply: The warehouse is checking it now. Action item: warehouse_investigation.
- When: Their storefront shows a different number than ours. Reply: Stock levels sync on a schedule, and we're checking this SKU now.

Never:
- Concede a loss or a credit before the check.
- Quote an accuracy SLA or a count cadence.

Not decided yet. If the reply needs one of these, write "let me confirm with the team":
- The storefront sync interval. Don't quote one.
