"""An OS ticket payload, turned into the ticket context the classifier knows.

Phase 93, plan 22. In `os` mode (and for the OS half of `shadow` mode) the cron
reads its tickets from Pack'N OS, not from HubSpot. This module is the one place
that reshapes an OS payload (the 24 keys pinned by
tests/fixtures/tickets-api-list.v1.json) into the `ticket_context` that
`hydrate.hydrate_ticket` builds from HubSpot, so the same classifier and drafter
see the same shape from either source.

THE CATEGORY LIST
-----------------
`config/categories.yaml` keeps the fifteen categories as top-level keys, beside
four top-level keys that are not categories. `category_keys` picks the
categories by an exclude-list on purpose: it needs no knowledge of the list it
is compared with, so tests/test_contract_fixtures.py comparing it with the
contract's slugs is a real check. A fifth non-category key added to the file
comes back from `category_keys` and turns that test red until it is added to
NON_CATEGORY_KEYS deliberately.
"""
from __future__ import annotations

# The top-level keys of config/categories.yaml that are not categories.
NON_CATEGORY_KEYS = (
    "form_topic_mapping",
    "auto_send_form_topics",
    "category_owners",
    "suppress_reply_reminder_categories",
)


def category_keys(categories_cfg: dict) -> list[str]:
    """The category names of a loaded config/categories.yaml, in file order:
    every top-level key that is not in NON_CATEGORY_KEYS.

    A config that is not a mapping is a TypeError: an unreadable file must not
    read as "no categories"."""
    if not isinstance(categories_cfg, dict):
        raise TypeError("category_keys needs the loaded categories.yaml mapping")
    return [key for key in categories_cfg if key not in NON_CATEGORY_KEYS]
