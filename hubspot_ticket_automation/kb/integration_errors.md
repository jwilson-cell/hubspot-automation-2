# Integration & System Errors

Covers: `integration_system`, `inbound_asn_receiving`, `retailer_compliance`.

## Unknown SKU (new launch)

**Symptom:** orders for a new SKU fail silently — merchant launched a product on their storefront but never synced it to Pack'N's WMS.

**Flow:**
1. Check the SKU master — is the SKU in Pack'N's system?
2. If no — merchant needs to create it and assign initial inventory (via inbound ASN) before orders can flow.
3. If yes but inventory is zero — merchant needs to ship stock in.

**Customer ask:** "Why aren't my orders shipping?"

**Language:**
> Orders for SKU {sku} are holding because it isn't in our system yet. To activate: (1) add the SKU master in the portal (dimensions, weight, packaging spec), (2) send an ASN for the inbound shipment, (3) once we receive and putaway, orders will release automatically. I can send the template / link for any of these steps if useful.

## Bad address rejection

**Symptom:** orders fail at label-generation because the carrier API rejects the destination address.

**Common causes:**
- Apartment/suite number missing.
- State/ZIP mismatch.
- PO box when shipping service doesn't allow (most UPS/FedEx ground can't deliver to PO boxes).
- International address in wrong format.

**Flow:**
1. Identify the specific rejection reason from carrier API response.
2. Merchant corrects in their cart/order system.
3. Resubmit via sync OR Pack'N manually updates if merchant provides corrected address.

**Language:**
> The carrier rejected the address on order {order_number} — reason: {rejection_reason}. Once the corrected address is in your cart and resyncs (or send it here and I'll update manually), we'll release the order. Address corrections **after** label generation carry a carrier accessorial; before label is free.

## EDI / API failures

**Common patterns:**
- **EDI 940 / 943 / 945 mismatches** with retailers (Amazon, Walmart, Target).
- **Shopify / WooCommerce / BigCommerce API timeouts** on large catalog syncs.
- **Custom API 500 errors** — usually a schema-mismatch when merchant's integration side changed without coordination.

**Flow:**
1. Capture exact error (timestamp, payload if available, error code/message).
2. Identify the failing endpoint.
3. Replay the transaction once Pack'N-side is green.
4. Coordinate with merchant's tech contact if the issue is on their side.

**Language:**
> I see the integration errors you're referencing on {timestamp}. Can you send (or ack if you already have) the full error message and the affected order/PO list? Our integrations team will replay the transactions once we've fixed the underlying issue — typically within {timeframe}. I'll flag this for their immediate attention.

## Inbound ASN issues

**ASN vs. physical mismatch:**
- Quantity received ≠ quantity on ASN.
- SKU on ASN doesn't exist in SKU master.
- ASN line missing from physical pallet.
- Pallet damage on arrival.

**Flow:**
1. Log the variance on the receiving line.
2. Notify merchant within {N} hours.
3. Disposition the variance: adjust system to physical, or hold for merchant instruction.

**TODO (Pack'N):** actual SOP for variance notification timeline and who owns the merchant communication.

**Language:**
> We received your ASN {asn_number} today. Physical receipt differs from the ASN on {SKU}: ASN said {asn_qty}, received {phys_qty}. No damage observed on the pallets / {damage described}. Want me to hold the variance for your review, or adjust to physical and move on? I can also pull the BOL/driver signature if useful.

## Retailer compliance chargebacks

**Common violations:**
- **ASN accuracy** — quantity/SKU mismatch (Amazon FBA, Walmart DC).
- **Routing guide violations** — wrong label placement, wrong pallet config, wrong carton marks.
- **Appointment miss** — dock appointment missed or late.
- **Data formatting** — EDI 856 format errors, GS1-128 label errors.

**Flow:**
1. Obtain the chargeback document (retailer provides a code + description).
2. Match to Pack'N's record: shipment, appointment, label scan, ASN submitted.
3. Assign cost (see `billing_faq.md`).
4. If retailer-fault or defensible, file a dispute via the retailer's vendor portal.

**Language:**
> Got the {retailer} chargeback — code {code}, description {description}. I'm pulling our side of the record: the ASN we sent, the BOL, appointment confirmation, and the label scan. I'll have a root-cause finding and a dispute/accept recommendation within {timeframe} business days.

**TODO (Pack'N):** list each retailer you service that can issue chargebacks, and the dispute portal for each (vendor central / retail link / partners online / etc.).
