# Tracking & WISMO ("Where is my order?")

Covers: `wismo_tracking`, `shipping_delay`, `address_recipient`.

## Decision tree for stalled tracking

1. **In Transit, scans < 24h old** → normal. Reassure customer, no action needed beyond setting expectations.
2. **In Transit, last scan 24–72h old** → still usually normal for ground. Acknowledge the gap, tell customer you're monitoring, but DO NOT promise an autonomous check-in on a specific future date (the system won't remind anyone). Frame the next step as customer-initiated: "reply back if you don't see an update in the next 24–48 hours and we'll escalate to the carrier."
3. **In Transit, last scan 3+ business days old** → escalate. File a **carrier trace/investigation** with the carrier. Don't promise a reship until the trace results come back (typically 3–5 business days).
4. **Delivered, customer says not received** → check POD for signature, delivery location note, photo (if carrier captured one). Steps:
   - Ask customer to check with neighbors, household, leasing office.
   - Look at GPS coordinates on the POD if available.
   - If still missing after 48h, file a **lost-package claim** with the carrier.
5. **Exception status** (weather, damage, address) → read the exception reason. Most resolve within 1–2 business days without intervention.
6. **Return to Sender** → usually triggered by bad address or refused delivery. Ask merchant if they want to reship to a corrected address (accessorial fee likely) or refund.

## Claim / trace timelines by carrier (industry norms)

**TODO (Pack'N):** replace with your carrier contract specifics if they differ.

| Carrier | Trace window | Lost-package claim window | Typical resolution |
|---|---|---|---|
| UPS | Open 24h after expected delivery | 60 days from ship date | 3–5 business days |
| FedEx | Open 24h after expected delivery | 60 days from ship date | 3–5 business days |
| USPS | 7 days domestic / 14 days intl for "Missing Mail" search | 60 days (60–180 for intl) | 7–14 business days |
| DHL | 24h | 30 days from ship date | 3–7 business days |

## Address correction

- **Before shipment** — free. Correct in the system before the wave picks.
- **After shipment** — carrier accessorial fee applies (typically $15–20 per intercept). Pack'N will attempt the intercept but cannot guarantee success; some packages are already past the sort facility.

**TODO (Pack'N):** confirm your actual pass-through policy on address-correction accessorials (charge merchant, absorb, cap).

## What to ask for when info is missing

- Order number (merchant store format) OR Pack'N shipment ID.
- Tracking number (if they already have it).
- Delivery address as it should have been.
- For "delivered but not received": whether the POD photo was checked.

## Language patterns for replies

**IMPORTANT**: these patterns never commit the automation to a **specific future date** for a follow-up action. The system does not schedule or remind; a silently-broken "I'll check back on 4/28" is worse than an honest "reply if you don't hear from us in the next few days." Where a future threshold is needed, either (a) take the action in the current operator shift, (b) frame it as customer-initiated ("reply back if X"), or (c) keep the phrasing vague ("in the next few business days").

**When carrier is genuinely stalled (action taken now):**
> Your order shipped on {date} via {carrier} tracking {number}. The most recent scan is {status} at {location} on {date}. That gap is outside the typical range — I'm opening a trace with the carrier now. Traces typically resolve in 3–5 business days, and we'll reach out as soon as we have results.

**When carrier is stalled but trace threshold not yet reached (customer-initiated):**
> Your order shipped on {date} via {carrier} tracking {number}. The most recent scan is {status} at {location} on {date}. For {carrier} ground this is still within the normal window. If there's no movement in the next 2–3 business days, reply back here and we'll open a trace immediately.

**When delivered but not received:**
> The carrier marked this delivered on {date} at {time}. Could you check with anyone who might have accepted it (household, office, neighbors, leasing office)? If you can't locate it after checking, reply here — we'll file a lost-package claim with {carrier} right away. The claim process itself takes about {X} business days to resolve once filed, and we'll align on reship or refund from there.

**When it's a simple "still in transit, be patient":**
> Tracking shows your order {current status} as of {last scan date}. For {carrier} ground service, {N}–{M} business days is normal. If you haven't seen an update past that window, reply back and we'll open a trace.
