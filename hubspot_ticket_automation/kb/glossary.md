# 3PL Glossary

Use these terms naturally in replies. If a customer uses consumer language ("package", "order"), mirror them but pivot to precise terms when giving ops details.

## Units & identifiers

- **SKU** — Stock Keeping Unit, a unique product identifier.
- **UPC** — Universal Product Code (barcode).
- **Serial / lot** — unit-level or batch-level tracking identifier.
- **Order #** — merchant-store order identifier (customer-facing).
- **Shipment ID / WRO** — Pack'N's internal identifier for an inbound or outbound movement.
- **PO** — Purchase Order, often used for B2B inbound or retailer shipments.
- **BOL** — Bill of Lading, carrier-signed document for freight shipments.
- **POD** — Proof of Delivery, signed receipt confirming carrier delivery.

## Inbound (receiving)

- **ASN** — Advanced Shipment Notification. The inbound data file (EDI 856 or API) the merchant sends ahead of a physical shipment so we can schedule receiving.
- **Putaway** — moving received goods from the dock into their storage location.
- **Dock appointment** — scheduled time slot to unload an inbound truck.
- **Receiving discrepancy** — the quantity or SKU physically received differs from the ASN.

## Outbound (fulfillment)

- **Pick** / **Mispick** — selecting items for an order; mispick = wrong SKU/quantity/variant pulled.
- **Pack** / **Mispack** — boxing the order; mispack = correct pick but wrong packaging or contents.
- **Wave** — a batch of orders released for picking together (for efficiency).
- **Cartonization** — the algorithm choosing box size given the order's items.
- **Kitting** — pre-assembling a bundle (e.g., subscription box) before picking.
- **Cutoff** — the daily time after which new orders ship the next business day.

## Inventory

- **Cycle count** — ongoing partial physical count used to maintain accuracy.
- **Full count** — periodic total physical count (often annual).
- **Phantom stock** — system says stock exists but physical count disagrees.
- **Shrinkage** — unexplained inventory loss (theft, damage, miscounts).
- **Inventory accuracy** — % of SKUs whose system qty matches physical. Industry norm: 95–99%.

## Carrier / shipping

- **In Transit** — carrier has scanned the package en route.
- **Out for Delivery** — package is on the final truck for delivery today.
- **Delivered** — carrier marked delivered (customer may still report not received — see `tracking_wismo.md`).
- **Exception** — carrier event that delayed delivery (weather, damage, address).
- **Return to Sender** — carrier returning a package (bad address, refused).
- **Accessorial** — extra carrier fee (residential, lift-gate, oversize, address correction).

## Returns

- **RMA** — Return Merchandise Authorization.
- **Disposition** — what happens to the returned item: **restock** (resell), **refurb** (clean/repair then resell), **dispose** (destroy), **return-to-vendor**.
- **Reverse logistics** — end-to-end returns handling.

## Billing

- **Pick-pack fee** — per-order fulfillment labor charge.
- **Storage fee** — pallet/bin/cubic-foot monthly charge.
- **Long-term storage fee** — surcharge for goods stored beyond a threshold (often 6–12 months), typically 1.5–3x standard.
- **Accessorial** — ad-hoc services: kitting labor, photo requests, project work.
- **Chargeback** — penalty from a retailer (e.g., Amazon, Walmart, Target) for a compliance violation.

## SLAs

- **Ship-time SLA** — time from order receipt to carrier handoff (e.g., same-day if before cutoff, next-day otherwise).
- **Response-time SLA** — how quickly Pack'N support acknowledges a ticket.
- **Inventory accuracy SLA** — target accuracy rate for cycle counts.
