# Mispack / Wrong Item Investigation

Covers: `mispack_wrong_item`.

## What "mispack" vs "mispick" means

- **Mispick** = warehouse pulled the wrong SKU, wrong quantity, or wrong variant from the pick location.
- **Mispack** = correct items picked, but packed into the wrong order / wrong box / missing from the box at pack time.

From the customer's view, they look the same ("I got the wrong thing"). The investigation separates them.

## Required info from merchant / customer

1. Order number.
2. SKU (or product name) **expected**.
3. SKU (or product name) **received**.
4. Quantity received vs. expected.
5. Photo of the shipping label if available (confirms the package matches the order we think it does).

## Investigation flow

1. **Pull the pick record** for the order — identifies the wave, picker, time, pick location.
2. **Cross-check against receiving** — was the correct SKU physically in the pick location? If not, the error happened upstream (putaway to wrong slot).
3. **Pull camera footage** of the pick/pack station at the timestamp if cameras cover that zone. If applicable.
4. **Check if a paired order** received the inverse mispack — often one mispack implies another customer got the opposite wrong item. If so, proactively contact that merchant too.
5. **Audit the picker** — if the picker has multiple errors in a rolling window, flag for retraining.

**TODO (Pack'N):** document actual SOP — how to pull camera footage, which system holds pick records, who runs picker audits.

## Reship decision tree

- **Single item, low value, customer has the wrong item in hand** →
  - Reship correct item at Pack'N cost.
  - Issue a return label for the wrong item.
  - Disposition on return: restock if resellable, otherwise follow disposition policy.
- **Entire order wrong** → same as above but full order.
- **Customer doesn't want the replacement** → refund (needs approval), issue return label for wrong item.
- **Multiple units, B2B / wholesale context** → escalate to AM; reship logistics may need freight.

## Cost owner

- **Pack'N pays** for: reship shipping, replacement product (if a Pack'N inventory unit is lost / shrinkage), picker training time.
- **Merchant pays** for: nothing in a clean mispick scenario. If the issue turns out to be an ASN / receiving error (merchant sent bad data), cost may revert to merchant.

**TODO (Pack'N):** confirm actual cost-absorption policy for mispicks — some 3PLs cap at a per-incident dollar amount or an annual pool.

## Standard language

**Initial reply:**
> Apologies for the wrong shipment. I'm pulling the pick record and camera footage for order {order_number}'s wave now. I'll have a root cause and a concrete next step within 4 business hours.
>
> In the meantime, can you confirm (1) the SKU received vs. expected and (2) a photo of the shipping label if it's handy? That lets me rule out a package-swap scenario immediately.

**After investigation, Pack'N at fault:**
> Confirmed on our side — {picker/location/SOP detail} caused the error. I'm issuing a reship of {SKU} at our cost today (tracking will follow) and sending a return label for the incorrect item. {Picker audit / SOP update} is queued internally so this pattern doesn't recur.

**When info is insufficient:**
> Before I can investigate I need the SKU you actually received and a photo of the shipping label. Once I have those I can pull the pick record and tell you exactly what happened.
