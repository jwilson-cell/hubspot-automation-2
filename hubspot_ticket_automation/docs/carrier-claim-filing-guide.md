# Carrier claim filing — operator guide

How to use the claim-packet feature end-to-end. Built on top of the existing `hubspot-tickets` + `hubspot-actions-digest` automation; nothing new to invoke.

## The flow at a glance

1. **Ticket arrives** → automation builds the packet
2. **Hourly digest email** → packet card shows up inline under the ticket
3. **File the claim** (~30s) → click portal link, paste from the card, upload photos, submit
4. **Log the filing** in the `carrier_issue_log` Google Sheet tab
5. **Close out** when the carrier resolves

---

## 1. Ticket arrives → packet gets built automatically

Customer submits a Carrier Issue form (damage / loss / delay) with a tracking # and, ideally, damage photos. Within 15 min `hubspot-tickets` runs on cron and:

- Hydrates ShipSidekick state (real carrier, ship date, tracking URL)
- Drafts + auto-sends the customer reply via Gmail
- Emits a `file_carrier_claim` action item with a populated `claim_packet`
- Queues the ticket to `config/pending_actions.json` with `posted_as: "auto_send"`
- Writes a row to the `carrier_issue_log` Sheet tab with tracking, carrier, insurance, and `filing_deadline_iso`

You do nothing in this step. It all happens on cron.

## 2. You get the packet in the hourly digest email

Top of the next hour, `hubspot-actions-digest` sends you an email. Under the Carrier Issue ticket you'll see the packet card:

```
[ ] [file_carrier_claim] File UPS damage claim — ⏱ 18 days left
    Portal: https://www.ups.com/us/en/support/file-a-claim.page
    Carrier:         UPS
    Tracking #:      1Z999AA10123456784
    Order #:         #17049
    Ship date:       2026-04-14
    Declared value:  unknown — operator fills from order system
    Description:     Box arrived crushed; 4 units broken per customer photos
    Evidence:        3 photos on ticket (review on HubSpot before filing)
    Insurance:       yes
    Filing deadline: 2026-06-13 (18 days left)
    HubSpot ticket:  https://app.hubspot.com/contacts/.../ticket/12345

    ─── Copy-paste claim summary for portal ───
    Shipper: Pack'N Fulfillment
    Tracking: 1Z999AA10123456784
    Ship date: 2026-04-14
    Declared value: [fill from order system]
    Claim type: damage
    Description: Box arrived crushed; 4 units broken per customer photos
    ─────────────────────────────────────────────
```

## 3. File the claim (~30 seconds)

1. Click the **Portal** link → carrier claim page opens
2. Open the **HubSpot ticket** link in another tab → photos are there; download them and upload to the portal
3. Paste the copy-paste block into the portal's damage description field
4. Fill declared value from your order system (the one field automation can't populate)
5. Submit

## 4. Log the outcome in the Google Sheet

Open the `carrier_issue_log` tab. Filter `claim_status` column = empty. Your just-filed row is there. Type into the operator-owned cells:

| Column | Value |
|---|---|
| `claim_status` | `filed` |
| `claim_number` | (number the carrier gave you) |
| `carrier_filed_at` | today's ISO timestamp |
| `coverage_usd` | amount you filed for |

## 5. When the carrier resolves (days/weeks later)

Same row, fill the final cells:

| Column | Value |
|---|---|
| `claim_status` | `paid` or `denied` |
| `resolution_amount_usd` | final payout |
| `reimbursement_received_at` | when the money hit |

## Daily triage view

Sort the Sheet by `filing_deadline_iso` ascending with filter `claim_status = ""`. Anything close to deadline floats to the top. The digest's deadline clock (`⚠ only N days left — file this week`) duplicates this signal in the email.

## Edge cases the packet handles

| Situation | What you see |
|---|---|
| ShipSidekick lookup fails | Packet falls back to tracking-regex inference, marked `carrier_confidence: "low"` with a `(⚠ low confidence — verify)` marker next to the carrier name |
| Carrier can't be inferred at all | `Portal: — (pick carrier manually before filing)` — you open the right portal yourself |
| No tracking # on the ticket | No packet card — just a one-liner with `Missing info: tracking_number`. Your step 1 is recovering the tracking # from the merchant |
| Ship date unknown | Deadline is approximated from the ticket-created date, prefixed with `~` in the card |
| Customer didn't attach photos | `Evidence: No photos yet — request from customer before filing`. Don't file until photos arrive |

## Carrier portal reference

Deep-links live in `kb/damage_claims.md` under "Carrier portal deep-links" and are read fresh on every digest render — update the KB file if a URL breaks and the fix lands on the next hourly run, no code change needed.

## Why it's split across email + Sheet

Different surfaces, different jobs:

- **Hourly digest email** — action surface. The packet card gives you everything to *file* a claim in one motion.
- **`carrier_issue_log` Google Sheet** — lifecycle surface. Persistent record of every claim from filed → paid/denied → reimbursed. Operator-owned cells (claim_status, claim_number, etc.) are yours; the skill never touches them after first insert (invariant #8).
- **`outputs/kpi/carrier_issue_log.csv`** — local recovery mirror. Written on every run *before* the Sheets API is even attempted. You don't open it day-to-day; it exists so data isn't lost if Google is down.

## Related docs

- `research/carrier_claim_automation_feasibility.md` — why this design exists (no carrier claims APIs today), tiered roadmap, open questions for Pack'N
- `research/operator_aid_claim_packet_spec.md` — the build spec for this feature
- `kb/damage_claims.md` — evidence requirements, root-cause decision tree, carrier portal URLs
- `CLAUDE.md` invariants #5, #7, #8, #9 — the load-bearing constraints this design respects
