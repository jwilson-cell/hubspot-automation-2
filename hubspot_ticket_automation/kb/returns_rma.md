# Returns / RMA

Covers: `returns_rma`.

## RMA flow (standard)

1. **Request received** — customer or merchant initiates.
2. **Authorization** — RMA issued with a unique number; return label generated.
3. **Customer ships back** — label pre-paid or merchant-paid (per contract).
4. **Receipt at warehouse** — returns receiving line logs the arrival.
5. **Inspection** — condition assessed against customer's stated reason.
6. **Disposition** — one of:
   - **Restock** — item is sellable, returns to active inventory.
   - **Refurb** — needs cleaning, re-packaging, or minor repair; returns to active inventory after.
   - **Dispose** — destroyed and written off (damaged, expired, hazmat, food safety).
   - **Return to Vendor (RTV)** — shipped back to merchant or supplier.
7. **Refund/credit** — issued by merchant on their side (Pack'N typically doesn't process the customer refund).

## Timelines (typical)

- **Label issuance:** same-day if requested during business hours.
- **Transit back:** 3–10 business days depending on carrier and distance.
- **Receipt to disposition:** 2–5 business days.
- **Refund timing:** once the merchant sees the receipt, they process the refund on their cart — that step is usually not Pack'N's responsibility.


## Disposition criteria

| Condition | Typical disposition |
|---|---|
| Unopened, resellable packaging | Restock |
| Opened, product unused, clean | Restock or Refurb (merchant-dependent) |
| Opened, used, working | Refurb |
| Damaged in transit on return | Dispose (file return-shipment carrier claim) |
| Damaged before return (customer abuse) | Dispose, notify merchant |
| Expired / near-expired | Dispose |
| Hazmat / regulated | Dispose per disposal protocol |
| High-value or collectable | Return to Vendor (manual inspection) |

## Fees

Returns handling typically incurs a per-return fee (label cost + receiving labor + disposition labor). Fee is $2 per return

**TODO (Pack'N):** fill in per-return accessorial structure or point to the merchant's specific agreement.

## What the reply should request / answer

- Order number (original outbound).
- Reason for return.
- Customer or merchant paying return shipping?
- Desired disposition (restock/refurb/dispose) if merchant has a policy.

## Language patterns

**RMA request:**
> To issue the RMA for order {order_number}, I just need: (1) the reason code your customer selected (damaged / wrong item / no longer wanted / other), (2) confirmation the return label should be billed to {your account / customer}, and (3) your preferred disposition on receipt (restock if resellable, otherwise dispose). I can have the label generated within {timeframe} once I have those.

**Refund timing question:**
> Your customer's refund is triggered by your cart/store once we mark the return received. Our target from carrier-delivery to receipt-logged is {X} business days, and you'll see it in the portal at that point. Refund processing time after that depends on your cart's settings — not something I can see from our side.

**Disposition rule reminder:**
> Default disposition for this SKU is {restock/refurb/dispose} based on your account settings. If you'd like me to override for this specific return (e.g., dispose even if technically resellable), let me know before the inspection step.
