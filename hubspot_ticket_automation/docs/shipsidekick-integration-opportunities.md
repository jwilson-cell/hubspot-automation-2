# ShipSidekick API — opportunities to extend Pack'N ticket automation

Researched 2026-04-23 against https://apidocs.shipsidekick.com/docs. The public docs site is thin — sidebar lists 6 sections (Orders, Products, Inventory, Purchase orders, Shipping quotes, Shipments) and a webhooks surface, but only a subset of endpoint pages are reachable. What we have is enough to map concrete opportunities. Anything marked (inferred) should be confirmed against the dashboard or an OpenAPI export before building.

## Access facts

- **Base URL**: `https://www.shipsidekick.com/api/v1` (prod), `https://test.shipsidekick.com/api/v1` (test)
- **Auth**: `Authorization: Bearer <API_KEY>`. Multi-client orgs add `X-SSK-Client: <child_account_id>` for parent-org impersonation.
- **Error shape**: HTTP status + JSON `{ "error": { "message": "...", "details": "..." } }`
- **Webhooks**: Adjustment webhooks exist (billing/cost changes on shipments). Other event types not enumerated in the Getting Started page.
- **OpenAPI spec**: `https://docs.shipsidekick.com/openapi.json` (client-rendered — can't be WebFetched, but can probably be pulled by `curl` for a full endpoint inventory before we build)

## Confirmed endpoints (from public docs)

### Orders

| Method | Path | Purpose |
|---|---|---|
| `POST /orders` | Create order (with optional pre-paid return label) |
| `GET /orders` | List orders (pagination + filters; specific filter names not in public docs) |
| `GET /orders/{id}` | Get one order — includes `financialStatus`, `fulfillmentStatus`, `shipments[]` with `trackingCode`, line items, customer |

### Products / SKUs

| Method | Path | Purpose |
|---|---|---|
| `POST /products` | Create product |
| `POST /products/{id}` | Update product |
| `POST /products/{id}/variants` | Update variant (SKU, dims, weight, pricing, country of origin, tariff code) |
| `GET /products` / `GET /products/{id}` / `GET /products/{id}/variants` / `GET /products/{id}/variants/{variantId}` | Read product + variant catalog |
| `DELETE /products/{id}` | Remove from catalog |

### Inventory

| Method | Path | Purpose |
|---|---|---|
| `GET /inventory/levels` | Aggregate by SKU — available, committed, reserved, incoming, damaged, QC, safety stock. Filters: `warehouseId`, `childInventory`, search, sort |
| `GET /inventory/locations` | Exact bin/location for a variant — location id, qty, lot, expiration |
| `GET /inventory/levels/<product>` (inferred path) | Per-variant quantity metrics across states |
| `POST /inventory` | Add / remove / move between locations (supports `reason` field) |

### Webhooks

- **Adjustment** — billing/cost adjustment notifications for shipments (reweigh, zone correction, accessorial charges inferred). Exact payload not in docs we can fetch.

### Other sections in sidebar (404'd on sub-pages, but exist)

- Purchase orders
- Shipping quotes
- Shipments (referenced as a category — some shipment data is nested in orders response)

---

## Mapping to Pack'N ticket categories

The real question: *for each category of ticket we get, what can we do automatically with ShipSidekick instead of putting a draft in Luca or Charlie's queue?*

### High-value integrations (do these first)

#### 1. WISMO / `wismo_tracking` + `shipping_delay` — auto-hydrate tracking state before drafting

**Today**: auto-sent form tickets get a generic reply that says "I'll open a trace." The drafter doesn't know the actual tracking state.

**With ShipSidekick**: before drafting, `GET /orders/{id}` (by order number or tracking number) → get live carrier, latest scan, POD. Feed this into `{ticket_context}` so the drafted reply can say concrete things: *"Your order shipped 4/18 via UPS, last scan Louisville KY on 4/21, no movement since. That gap is past our 3-business-day threshold, so I'm opening a trace."*

**Touch points**: `prompts/draft_reply.md` inputs (add `order_live_state`), `scripts/send_customer_reply.py` or a new hydration step in `hubspot-tickets` SKILL.md step 2a.

**Also**: for `shipping_delay`, you can confirm whether the order was actually shipped late before responding. Right now the draft hedges — with SSK data it could lead with the fact.

#### 2. `mispack_wrong_item` — verify the mispack claim before offering a reship

**Today**: the auto-send reply commits to "I'll pull the pick record" but we have no actual pick record in context. The reply is sympathetic hedging.

**With ShipSidekick**: `GET /orders/{id}` → line items shipped. If customer claims "you sent me SKU X instead of SKU Y", compare against the order's shipped variants. If the customer is right, the reply can own it and commit to reship (still flag for Luca to approve credit). If the customer is wrong (we shipped what they ordered), the reply can say "I see order #XXXX shipped with SKU Y, qty 3 — can you send a photo of the item you received so I can reconcile?"

**Touch points**: this is the core enabler for non-hedged mispack replies. Also populates the mispack Sheets rollup (`sku_mentioned` field can come from actual order data, not classifier guess).

#### 3. `returns_rma` — auto-issue the RMA instead of drafting a reply asking the merchant for info

**Today**: the `create_rma` action item waits for Luca/Charlie to set up the RMA.

**With ShipSidekick**: `POST /orders` with a `returnOrder` payload (docs mention pre-paid return labels) → create the RMA programmatically, get the label URL, include it in the drafted reply. Reply becomes: *"Your return label is attached, please ship by X. Once it arrives we'll issue a refund/credit within N business days."*

**Touch points**: this moves `returns_rma` from draft-only to auto-send-with-action. Need to scope: when is auto-RMA safe? Probably: defective + wrong-item (we're at fault) → auto-RMA. "Buyer remorse" → hold for Luca approval.

#### 4. `billing_invoice` adjustment webhook — proactive customer comms

**Today**: customer gets a surprise reweigh charge, opens a ticket, Luca researches and replies.

**With ShipSidekick Adjustment webhook**: SSK fires a webhook when a shipment's cost is adjusted (reweigh, zone correction, fuel, accessorial). Pack'N can push this straight into HubSpot as an outgoing note on the merchant's most recent ticket OR as a proactive digest to the merchant — heading off the ticket before it's opened. Turns `billing_invoice` from reactive into partially proactive.

**Touch points**: new webhook receiver (needs a public endpoint — can reuse the Custom Channels infrastructure we considered earlier if we ever build it, or start with a lightweight Cloudflare Worker → HubSpot API route). Less integrated into the ticket skill, more of a sibling automation.

### Medium-value integrations

#### 5. `inventory_discrepancy` — confirm stock state before replying

**Today**: customer reports stock mismatch ("your system says 12, ours says 15"). Charlie investigates manually.

**With ShipSidekick**: `GET /inventory/levels` (filtered by SKU) → show current available / committed / reserved / incoming. The draft can lead with actual numbers: *"I show 12 available + 3 committed to open orders = 15 total on-hand for SKU X at warehouse Y. That matches your count — the 'committed' qty is the likely source of the mismatch."*

**Touch points**: `prompts/draft_reply.md` context injection, specific to this category.

#### 6. `inbound_asn_receiving` — check PO receipt status

**Today**: merchant asks "did my inbound ASN 12345 land?" Charlie checks the WMS.

**With ShipSidekick**: `GET /purchase-orders/{id}` (assuming endpoint exists) → receipt status, received qty vs expected, discrepancy flags. Drafted reply can answer with specifics.

**Touch points**: need to confirm the PO endpoints work first (docs pages 404'd).

#### 7. `address_recipient` — pre-ship address correction

**Today**: customer emails with a corrected address, we reply "we'll try to intercept, accessorial fee may apply."

**With ShipSidekick**: `POST /orders/{id}` (product-update pattern suggests this exists) → update `shipToAddress` if fulfillment hasn't started. Draft can confirm: "Address updated — $0 accessorial since we caught it before the wave." Vs. if already shipped, explain intercept fee + carrier options.

**Touch points**: needs a "fulfillmentStatus" check before deciding path. Live field on `GET /orders/{id}`.

### Lower-value / edge cases

#### 8. `integration_system` (unknown SKU) — auto-create the SKU in SSK

**Today**: merchant launches new SKU, customer orders it, order fails with "unknown SKU" — ticket opens.

**With ShipSidekick**: `POST /products` + `POST /products/{id}/variants` to register the SKU on the fly when the merchant gives us the details. Automation can draft "created SKU X with these attributes — please confirm before we ship" and Luca just approves.

**Touch points**: risky — creating products without merchant approval could pollute the catalog. Likely stays a human step but the script can pre-fill the create call.

#### 9. `damaged_goods` — carrier claim payload pre-fill

**Today**: Charlie files a claim manually. We ask customer for photos.

**With ShipSidekick**: the actual `file_carrier_claim` endpoint isn't in the sidebar I could see. If SSK exposes a claim-filing endpoint (likely under shipments), the automation could pre-populate it with shipment data once photos arrive. Until that's confirmed, this stays manual.

---

## Proposed rollout (ranked)

If Pack'N wants to pick one to start, the ROI ranking:

1. **WISMO hydration (#1)** — smallest code change, biggest quality-of-reply improvement. Every Carrier Issue auto-send today is generic — with live tracking state it becomes specific and credible. Probably 2–4 hours to wire `GET /orders/{id}` into step 2a of the ticket skill.
2. **Mispack verification (#2)** — unlocks real-fault-identification before Luca reviews, which saves credit-approval cycles when the customer is mistaken (mispack tickets where we shipped correctly can be closed without a credit).
3. **Adjustment webhook → proactive email (#4)** — kills a category of tickets before they open. Bigger build (needs hosted webhook receiver) but proportionally bigger outcome.
4. **Auto-RMA (#3)** — scope carefully on which reasons are safe to auto-issue.
5. **Inventory / ASN / address context (#5-7)** — batch these as a "context hydration" epic once #1 pattern is proven.

## Prerequisites before any integration work

- [ ] Pack'N generates a ShipSidekick API key (dashboard → integrations → API → generate). Store at `config/.secrets/shipsidekick_token.txt` (same pattern as `hubspot_token.txt`).
- [ ] Decide test env vs prod — recommend prod API key with read-only scope for the first integration (#1 is pure GET).
- [ ] Pull the real OpenAPI spec via curl (`curl -s https://docs.shipsidekick.com/openapi.json > openapi.json`) to confirm every endpoint + payload shape before we assume anything from this doc.
- [ ] Map Pack'N order numbers (as they appear on the intake form) to ShipSidekick order identifiers. The form captures "order_number" — need to confirm whether SSK's `GET /orders/{id}` accepts that same value or needs a separate alias lookup (`name`, `alias`, or `id`).

## Open questions to resolve before picking one to build

1. What's the latency of `GET /orders/{id}`? Auto-send runs on cron within 15 min of ticket submission; adding a SSK call adds X ms per ticket. Probably negligible but worth measuring.
2. How does SSK resolve "order lookup by customer-provided order number"? If the form's `order_number` doesn't match SSK's ID, we need an alias resolution path.
3. Is there a `GET /orders` filter for `hs_ticket_id` or external reference, or do we have to index by order number/SKU?
4. What's the exact adjustment webhook payload? Determines how much effort the proactive-email build takes.
5. Do Pack'N's merchants each have their own SSK child account (requiring `X-SSK-Client` header)? Or is it all under one org?

Nothing here is blocking — this is a research scratchpad. When you pick an integration, I'll spike the relevant endpoints against the real API and scope a proper plan.
