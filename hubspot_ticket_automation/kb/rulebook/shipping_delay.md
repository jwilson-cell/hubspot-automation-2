<!-- GENERATED from packn-os src/lib/tickets/reply-rulebook.ts (version 2026-10-15.2). Do not edit here: edit the OS file and run scripts/export-reply-rulebook.ts. -->

## shipping_delay: Why hasn't my order shipped?

Goal: Give the true pre-ship state, and own it plainly when Pack'N missed.

Check first:
- The order's pre-ship state: held for inventory (and on which PO), picks generated or complete, carrier rate bound.
- The order's ship-window status from /sla, when the context has it (LATE, DUE TODAY, ON TRACK, HELD, EXCUSED, SHIPPED ON TIME, SHIPPED LATE, NOT SCORED). It alone decides whether Pack'N missed its commitment.
- An open ship request on the ticket.

Ask for, if missing (never re-ask what the conversation already gave):
- The order number.

Situations:
- When: Held for inventory. Reply: It's held for inventory on the PO named in the context and is waiting on inbound stock. State it as fact, without blame.
- When: Picks complete, not shipped. Reply: Picks are done and it's close to going out. Give the carrier and ETA if a rate is bound. Action item: expedite_fulfillment.
- When: Ship-window status LATE. Reply: Own it plainly: it missed our ship window, and it's being prioritised this shift. Action item: expedite_fulfillment.
- When: Ship-window status SHIPPED LATE. Reply: Give the tracking, and own plainly that it shipped later than it should have.
- When: Ship-window status ON TRACK, DUE TODAY, HELD or EXCUSED. Reply: Say it's within our ship window (or why it's held). Don't apologise for a miss that didn't happen.
- When: Ship-window status NOT SCORED, or none in the context. Reply: Don't say whether it's late. Describe the state only.
- When: Merchant asks to ship what's available (a split). Reply: Confirm the available units will ship and the rest will follow, pending review. Action item: edit_order.

Never:
- Say "wave N", "picking now", "out the door today" or "X hours ago".
- Quote a cutoff time or a ship-by date.
