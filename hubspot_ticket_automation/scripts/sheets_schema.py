"""Single source of truth for the Pack'N HubSpot Automation workbook schema.

Both sheets_bootstrap.py (creates tabs + seeds headers) and sheets_sync.py
(appends rows, validates integrity) import from here. Keeping the schema in
one place means: adding a new column only touches this file; bootstrap and
sync stay in lockstep.

Each tab has two zones:
  - skill_owned:    columns the skill writes on first insert and never revisits.
                    Deleting or renaming one of these in the Sheet breaks sync.
  - operator_owned: columns the skill writes as empty string on first insert,
                    then never touches. Operators fill these in by hand and
                    the skill must never clobber their edits.

Column order here is the CANONICAL order for a fresh bootstrap. Operators
may freely reorder columns in the live Sheet — sync resolves by exact header
name at write time, not by position.
"""
from __future__ import annotations


# Columns that should be parsed as booleans at write time.
# Values come from HubSpot form checkboxes as strings ("true" / "false" /
# occasionally "yes" / "no" / "" depending on the form field type).
BOOLEAN_COLUMNS: frozenset[str] = frozenset({
    "reshipment_needed",
    "insurance_on_package",
})

# Columns that should be parsed as numbers at write time. Form inputs arrive
# as strings ("250.00", "$250", " 250 "); we strip $/commas/whitespace and
# float-parse. Empty-or-unparseable → empty string (not zero).
NUMERIC_COLUMNS: frozenset[str] = frozenset({
    "requested_credit_usd",
    "cost_absorbed_usd",
    "customer_credit_usd",
    "coverage_usd",
    "resolution_amount_usd",
    "classifier_confidence",
    "avg_classifier_confidence",
    "run_duration_sec",
    "tickets_matched",
    "tickets_processed",
    "tickets_skipped_dedupe",
    "tickets_skipped_error",
    "notes_posted",
    "urgent_emails_drafted",
    "items_queued_for_digest",
    "image_errors",
    "mcp_errors",
})

# Columns where free-text might begin with "=", "+", "-", or "@" and be
# misinterpreted as a formula by Sheets' USER_ENTERED parser. We prefix these
# defensively with an apostrophe at write time.
# (Numeric columns are deliberately excluded — we want Sheets to parse them.)
FREETEXT_COLUMNS: frozenset[str] = frozenset({
    "issue_description",
    "customer_name",
    "company_name",
    "operator_notes",
    "root_cause",
    "sku_mentioned",
    "carrier_issue",
    "claim_number",
    "ticket_id",       # HubSpot IDs are numeric-looking; pin as text to avoid scientific notation
    "order_number",
    "tracking_number",
    "draft_note_id",
})


# Tab name → (skill_owned cols, operator_owned cols). Order matters on first
# bootstrap; after that, operators can rearrange.
TAB_COLUMNS: dict[str, tuple[list[str], list[str]]] = {
    "kpi_system": (
        [
            "run_id", "run_started_at_utc", "last_run_at_used", "run_mode",
            "tickets_matched", "tickets_processed", "tickets_skipped_dedupe",
            "tickets_skipped_error", "notes_posted", "urgent_emails_drafted",
            "items_queued_for_digest", "image_errors", "mcp_errors",
            "avg_classifier_confidence", "run_duration_sec",
        ],
        [],  # KPI tab has no operator-owned columns
    ),
    "mispack_log": (
        [
            "ticket_id", "ticket_link", "first_seen_utc", "customer_name",
            "customer_email", "company_name", "order_number", "tracking_number",
            "sku_mentioned", "issue_description", "requested_credit_usd",
            "reshipment_needed", "classifier_confidence", "priority", "draft_note_id",
        ],
        [
            "investigation_status", "root_cause", "cost_absorbed_usd",
            "customer_credit_usd", "filed_at", "closed_at", "operator_notes",
        ],
    ),
    "carrier_issue_log": (
        [
            "ticket_id", "ticket_link", "first_seen_utc", "customer_name",
            "customer_email", "company_name", "order_number", "tracking_number",
            "carrier_inferred", "carrier_issue", "insurance_on_package",
            "filing_deadline_iso",
            "classifier_confidence", "priority", "draft_note_id",
        ],
        [
            "claim_status", "claim_number", "carrier_filed_at", "coverage_usd",
            "resolution_amount_usd", "reimbursement_received_at", "operator_notes",
        ],
    ),
}


# Column-name list per tab (skill_owned + operator_owned concatenated, in canonical order).
TAB_SCHEMAS: dict[str, list[str]] = {
    tab: skill + op for tab, (skill, op) in TAB_COLUMNS.items()
}


def skill_owned(tab: str) -> list[str]:
    return TAB_COLUMNS[tab][0]


def operator_owned(tab: str) -> list[str]:
    return TAB_COLUMNS[tab][1]


# Sanity checks — run at import so a mangled schema fails fast rather than
# surfacing as a runtime bug at the first live sync.
for _tab, _cols in TAB_SCHEMAS.items():
    assert len(_cols) == len(set(_cols)), f"duplicate column in {_tab}: {_cols}"
for _col in BOOLEAN_COLUMNS | NUMERIC_COLUMNS | FREETEXT_COLUMNS:
    assert any(_col in _cols for _cols in TAB_SCHEMAS.values()), \
        f"coercion rule references unknown column: {_col!r}"
