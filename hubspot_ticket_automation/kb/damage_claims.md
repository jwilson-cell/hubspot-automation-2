# Damage Claims

Covers: `damaged_goods`.

## Required evidence (ask for all of these)

1. **Outer box photo** showing all visible damage AND the shipping label clearly.
2. **Product photos** showing the damage itself.
3. **Internal packaging photo** — how it was packed when received (dunnage, void fill, placement).
4. **Tracking number and delivery date.**

No evidence = no claim. Set this expectation early.

## Claim timeline

- **Customer must report within:** typically 10 calendar days of delivery (varies by carrier contract).
- **Pack'N filing window:** typically 60 days from ship date.
- **Resolution:** 10–30 business days depending on carrier.

**TODO (Pack'N):** confirm your actual claim windows and reporting requirements. Commonly stricter than carrier defaults.

## Carrier portal deep-links

Used by the operator-aid claim packet in the hourly digest. Most carriers do NOT accept pre-filled
tracking numbers via URL parameters — the packet renders a copy-paste block next to the portal link
so the operator can paste into the portal in one motion.

| Carrier | Portal URL | Pre-fill? | Filing window (damage) |
|---|---|---|---|
| UPS | https://www.ups.com/us/en/support/file-a-claim.page | No — manual paste | 60 days from delivery |
| FedEx | https://www.fedex.com/en-us/customer-support/claims.html | No — manual paste | 60 days domestic / 21 intl |
| USPS | https://www.usps.com/help/claims.htm | No — manual paste | 60 days from mailing |
| DHL Express | https://mydhl.express.dhl/us/en/help-and-support/shipping-advice/file-a-claim.html | No — downloadable PDF form | 30 days from delivery |
| Amazon Logistics | (file via Seller Central A-to-z / Shipment Performance — route by BOL type) | N/A | varies |
| OnTrac | https://www.ontrac.com/claims.asp | No — manual paste | 90 days from ship date |
| LTL (generic) | Route per carrier on the BOL | N/A | 9 months (Carmack Amendment federal default) |

If a portal URL breaks, update this table — the digest skill reads this file at render time, so the fix lands on the next hourly run without any code change.

## Who pays — root cause decision tree

1. **Carrier-caused damage** (crushed, dropped, visible external box damage) → carrier claim, carrier pays.
2. **Packaging-caused damage** (Pack'N underpacked — insufficient void fill, wrong box size, no fragile labeling on known-fragile SKU) → Pack'N absorbs cost, reships at Pack'N expense, root-cause update to packaging SOP for that SKU.
3. **Merchant-specified packaging failure** (merchant provided the packaging spec or forbade upgrades) → merchant absorbs cost.
4. **Concealed damage** (outer box fine, inner product damaged, product fragile) → typically Pack'N packaging issue unless product arrived that way from the factory. Investigate before assigning cost.
5. **Act of nature** (weather event, major carrier incident) → usually carrier claim, possibly no recovery.

## Standard customer response pattern

Never commit to a specific outcome in the first reply — you don't know the root cause yet. Do:

1. Express concern briefly ("Sorry to hear this landed damaged").
2. Request the full photo set.
3. Explain the process and timeline.
4. Set the next-contact expectation.

## High-value claim threshold

**TODO (Pack'N):** set an internal threshold (e.g., >$500 or >$1000) above which damage claims automatically escalate to ops manager for faster review and direct merchant contact.

Until set, default threshold in the automation is **$1,000**.

## Language patterns

**Initial reply:**
> Sorry to hear your order arrived damaged. To move this forward I need three photos from your customer (or from you if you have them):
>
> 1. The outer box showing the damage and the shipping label in the same frame.
> 2. The damaged product itself.
> 3. The internal packaging — how it was padded when opened.
>
> Please send these within 10 calendar days of delivery so we stay inside the carrier's claim window. Once I have the photos I'll open the carrier claim and investigate our packaging record in parallel. I'll have a root-cause proposal and a credit/reship recommendation within 3 business days of receiving the full photo set.

**When Pack'N is clearly at fault (e.g., known mispack SOP failure):**
> I've reviewed the wave record — this one's on us; the packaging spec wasn't followed for SKU {sku}. I'm escalating a reship at no cost to you {today/tomorrow} and flagging the packaging SOP for review so it doesn't recur. I'll confirm tracking within {timeline}.
