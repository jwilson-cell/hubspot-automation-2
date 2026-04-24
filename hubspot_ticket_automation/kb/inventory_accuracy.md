# Inventory Accuracy

Covers: `inventory_discrepancy`.

## Industry norms

- **Target inventory accuracy:** 95–99% SKU-level match between system quantity and physical count.
- **Cycle counts** — ongoing partial counts; most 3PLs run daily or weekly cycles on a rotating SKU subset.
- **Full counts** — typically annual, sometimes aligned with fiscal year-end.
- **Tolerance** — small variance (single units on high-volume SKUs) is expected between syncs; large variance or pattern variance (same SKU repeatedly off) warrants investigation.

**TODO (Pack'N):** fill in your actual cycle count cadence and accuracy SLA from the MSA.

## Common root causes (diagnostic)

1. **Sync lag** — merchant's storefront or ERP hasn't synced with Pack'N's WMS in the last {X} hours. Often the "discrepancy" resolves on next sync.
2. **Misreceive** — receiving counted wrong on inbound. Usually caught by cycle count later but can persist for weeks.
3. **Mispick pulling wrong SKU** — decrements system inventory for the *wrong* SKU, so one SKU shows phantom surplus and another shows phantom shortage.
4. **Returns not received / not dispositioned** — customer returned, carrier logged, but Pack'N hasn't fully processed the return into stock.
5. **Damage in warehouse, not written off** — units physically destroyed but not removed from system count.
6. **Shrinkage** — actual loss (theft, damage not reported). Harder to diagnose without trend data.
7. **Slotting move** — SKU moved to a new bin but the system wasn't updated.

## Reconciliation process

1. Snapshot: current merchant count, current Pack'N system count, timestamp of last sync.
2. Run a **spot cycle count** on the disputed SKU(s).
3. Compare: physical vs. Pack'N system vs. merchant system.
4. Identify direction of discrepancy:
   - Physical < System → investigate shrinkage / returns / misreceive.
   - Physical > System → investigate write-offs not applied, returns-not-accounted.
5. Issue an **inventory adjustment** with a documented reason code.
6. Report the adjustment back to the merchant.

**TODO (Pack'N):** your actual reason codes and who approves >N-unit adjustments.

## What the reply should request

- Specific SKU(s) in dispute.
- Merchant's count and where it comes from (storefront, ERP, last portal pull).
- When they last looked at Pack'N's reported count.
- Whether there's been recent unusual activity (big return wave, large inbound, SKU reslotting).

## Language patterns

**Initial reply:**
> Thanks for flagging the count on SKU {sku}. Can you confirm: your count of {merchant_qty}, and the source (store / ERP / our portal), plus the timestamp? I'll queue a spot cycle count on that SKU and compare against both systems. I'll have a reconciliation summary — physical vs. our system vs. yours — within {timeframe}.

**After reconciliation, Pack'N side off:**
> Our count was off by {n} units on SKU {sku}. Root cause: {reason}. I've issued an adjustment (reason code: {code}) and your portal should reflect the correct number within {timeframe}. I've also {put-in-place / flagged / escalated} to prevent recurrence on this pattern.

**When root cause is sync lag:**
> Confirmed this is a sync timing issue — our physical count matches {pack'n system / merchant system} as of the most recent pull. The delta you saw was a {X}-hour gap between the event and the downstream system reflecting it. Next sync is at {time}; the counts should converge then.
