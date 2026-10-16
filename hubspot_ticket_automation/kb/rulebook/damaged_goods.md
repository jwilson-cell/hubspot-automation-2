<!-- GENERATED from packn-os src/lib/tickets/reply-rulebook.ts (version 2026-10-16.1). Do not edit here: edit the OS file and run scripts/export-reply-rulebook.ts. -->

## damaged_goods: It arrived damaged

Goal: Get the photo set once, and move the customer to a resolution without waiting on the carrier.

Check first:
- Photos already sent in this ticket or an earlier one.
- A claim on file, and investigation findings.

Ask for, if missing (never re-ask what the conversation already gave):
- Three photos in one message: (1) the outer box with the shipping label in frame, (2) the damaged product, (3) the inside packaging as it was opened.
- The tracking number, if not given.

Situations:
- When: No photos yet. Reply: Sorry it arrived damaged. Ask for the three photos and say what happens once we have them, with no date. Action item: request_missing_info_from_merchant.
- When: Photos received. Reply: Thank them. We're reviewing our packing record and escalating a reship for review; a carrier claim runs separately on our side. Action item: reship_order + file_carrier_claim.
- When: Findings say our packing failed. Reply: Acknowledge it plainly. A reship is being arranged (state it if the replacement exists). Any cost question goes to billing. Action item: escalate_to_ops_manager + billing_specialist_follow_up.
- When: Outer box fine, product damaged inside (concealed damage). Reply: Acknowledge it and collect the photos. It goes to billing for review; say nothing about who covers the goods. Action item: billing_specialist_follow_up.
- When: Order value of $1,000 or more. Reply: As above, and say a member of the team will be in touch directly. Action item: escalate_to_ops_manager.

Never:
- Make the reship wait on the claim.
- Promise a root cause by a date.
- Commit to covering, crediting or refunding the goods.

Not decided yet. If the reply needs one of these, write "let me confirm with the team":
- How long the customer has to report damage. Don't quote a reporting window.
