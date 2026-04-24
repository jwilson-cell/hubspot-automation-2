# Billing FAQ

Covers: `billing_invoice`.

## Hard rule for replies

**Never quote rates or commit to adjustments in a drafted reply.** All billing disputes route to a billing specialist. The drafted reply's job is to:

1. Acknowledge the specific line item being disputed.
2. Confirm the invoice number and period.
3. Set expectations for specialist follow-up timing.
4. Flag `billing_specialist_follow_up` as the action item.

## Common dispute categories

### Storage fees

- **Standard storage** — monthly charge per pallet / bin / cubic foot.

Our storage fees are as follows with NO long term threshhold:
A bin is $7 monthly
A shelf is $13 monthly
A pallet is $30 monthly
All of these are prorated as needed for usage length

### Pick-pack / fulfillment fees

- **Per-order base fee** — Base fee is generally $0.60 for base pick and $1.00 for base order processing
- **Per-additional-item** — Each pick is $0.40, making a 1 unit order's additional picking fees $0.40

**TODO (Pack'N):** fill in actual pick-pack structure.

### Accessorials

Common extras merchants dispute:

- Address correction (carrier accessorial pass-through + Pack'N labor)- Charged to client at cost
- Residential surcharge- Charged to client at cost
- Lift-gate / liftgate delivery- Charged to client at cost
- Label reprint- Free
- Special packaging (gift wrap, branded insert, custom box)- Custom packaging and inserts included for free
- Photo request (photo of packed order before ship)- We do not offer this
- Returns receiving (see `returns_rma.md`)- Each return received is charged $2
- Kitting/assembly labor- Kits are $1.00 per kit
- Project labor (ad-hoc builds, relabeling, repackaging)- big labor assembely projects are generally $30 per labor hour per person

**TODO (Pack'N):** fill in accessorial schedule.

### Retailer chargebacks

When Amazon / Walmart / Target charge back for routing-guide violations, two questions:

1. **Was Pack'N at fault?** (e.g., missed appointment, wrong label, ASN incorrect, pallet stacking violation) → Pack'N absorbs the chargeback.
2. **Was merchant at fault?** (e.g., merchant-specified incorrect packaging, wrong product specs, booked appointment late) → merchant absorbs.
3. **Was retailer at fault?** (inconsistent routing rules, systemic platform error) → dispute via retailer vendor portal; often recoverable.

1-3 will be reviewed manually in every case

See `integration_errors.md` for compliance patterns.

### Billing errors vs. rate disputes

- **Billing error** (wrong quantity, duplicate line, wrong period) → billing adjusts the invoice, credit memo issued.
- **Rate dispute** (merchant believes the contracted rate is wrong) → contract review; AM involved, not just billing.

Both cases will be reviewed manually in every case

## What the reply should request / confirm

- Invoice number.
- Specific line item(s) disputed.
- Period (month/week) in question.
- What the merchant believes the correct charge should be.

## Language patterns

**Storage fee surprise:**
> Got it — the storage line on invoice {invoice_number} for {period} is what you're questioning. Without quoting numbers I don't have in front of me, let me loop in our billing specialist to walk you through (1) the aging breakdown by SKU for the period, (2) whether long-term storage kicked in and for which units, and (3) any adjustment applicable. You'll hear from them within 1 business day.

**Accessorial question:**
> The accessorial on invoice {invoice_number} is {line_item}. I want our billing specialist to pull the audit trail (exactly what service was rendered, when, and the rate applied) before I commit to anything. I'll have them follow up within 1 business day with a line-by-line explanation and any correction, if warranted.

**Retailer chargeback:**
> Thanks for forwarding the {retailer} chargeback. Before assigning cost, I'll need to see the chargeback document and match it against the {PO/shipment/appointment} record to determine whether the violation originated on our side or upstream. Our compliance lead will handle this, expect a root-cause finding within 2 business days.
