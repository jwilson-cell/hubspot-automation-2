"""The read contract, on the cron's side (Phase 93, plan 22).

Three files under tests/fixtures/ are byte copies of Pack'N OS's
docs/contracts/fixtures/ (written by its plan 93-04):

    tickets-api-list.v1.json        GET /api/tickets/unprocessed
    tickets-api-ticket.v1.json      GET /api/tickets/{id}
    ticket-category-slugs.v1.json   the fifteen category slugs

Each is pinned by the checksum the OS pins for its own copy, so a change on one
side only turns a test red on that side. The rule is the OS's: read the file as
UTF-8 text, read every CRLF as LF, take the SHA-256 of the result.

The second half pins the cron's category list to the contract's: the classifier's
own set, and the category keys of config/categories.yaml as picked by
`os_bundle.category_keys` — an exclude-list, so the comparison is a real check.

A fourth file is the WRITE contract (plan 17), copied from the OS's plan 93-05:

    tickets-api-classification.v1.json   POST /api/tickets/{id}/classification

It is pinned the same way, and the third half ties the cron's write-back to it:
the six failure codes, the two text limits, the prompt version and the model
are named constants of `os_pass` that equal the fixture's values, and the
request builder reproduces both fixture bodies key for key.

No network, no database, no model call.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pytest
import yaml

from ticket_pipeline import llm, os_bundle, os_pass

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # hubspot_ticket_automation/
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CATEGORIES_PATH = PACKAGE_ROOT / "config" / "categories.yaml"

LIST_FIXTURE = "tickets-api-list.v1.json"
TICKET_FIXTURE = "tickets-api-ticket.v1.json"
SLUGS_FIXTURE = "ticket-category-slugs.v1.json"
CLASSIFICATION_FIXTURE = "tickets-api-classification.v1.json"

# The literals of 93-04-SUMMARY ("Fixtures"). Pack'N OS asserts the same three in
# tests/unit/tickets/cron-contract.test.ts.
PINNED_CHECKSUMS = {
    LIST_FIXTURE: "8205533cee0f4195f5a97d44c76f30adf4f65467a3f423bbfbbc43694e3580ce",
    TICKET_FIXTURE: "3a7b120917d024b9ae596732215ff53ad3cd2f6a795502f088d311881ad6b396",
    SLUGS_FIXTURE: "fadc6c90ab14c62f1360ca8992afdb4d43305a4ca581254b0129cdb021107e85",
    # 93-05-SUMMARY ("Fixture"): 1303 bytes.
    CLASSIFICATION_FIXTURE: "f585b19aef732d6e3bb07d93383e3b5dcc395948e242671cd96b341edbdf2aa1",
}

# The four top-level keys of config/categories.yaml that are not categories.
FOUR_NON_CATEGORY_KEYS = {
    "form_topic_mapping",
    "auto_send_form_topics",
    "category_owners",
    "suppress_reply_reminder_categories",
}

# CR and LF built from their codes, so no tool that rewrites escapes can change
# what the checksum rule compares.
CR = chr(13)
LF = chr(10)


def fixture_checksum(text: str) -> str:
    """The OS's `fixtureChecksum`: CRLF read as LF, then SHA-256 of the UTF-8 bytes."""
    return hashlib.sha256(text.replace(CR + LF, LF).encode("utf-8")).hexdigest()


def read_fixture_text(name: str) -> str:
    """The file as UTF-8 text with its line endings untouched (no newline translation)."""
    return (FIXTURES / name).read_bytes().decode("utf-8")


def load_fixture(name: str) -> dict:
    return json.loads(read_fixture_text(name))


def real_categories_config() -> dict:
    with CATEGORIES_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


# ------------------------------------------------------------------ the three copies


@pytest.mark.parametrize("name", sorted(PINNED_CHECKSUMS))
def test_each_fixture_has_the_checksum_the_os_pins(name: str) -> None:
    assert fixture_checksum(read_fixture_text(name)) == PINNED_CHECKSUMS[name]


def test_the_checksum_rule_reads_crlf_as_lf_and_nothing_else() -> None:
    lf_text = "{" + LF + '  "a": 1' + LF + "}" + LF
    crlf_text = lf_text.replace(LF, CR + LF)
    assert crlf_text != lf_text
    assert fixture_checksum(crlf_text) == fixture_checksum(lf_text)
    # A lone CR, a trailing space and a missing final newline are all differences.
    assert fixture_checksum(lf_text + CR) != fixture_checksum(lf_text)
    assert fixture_checksum(lf_text + " ") != fixture_checksum(lf_text)
    assert fixture_checksum(lf_text[:-1]) != fixture_checksum(lf_text)


@pytest.mark.parametrize("name", sorted(PINNED_CHECKSUMS))
def test_a_checkout_with_crlf_endings_has_the_same_checksum(name: str) -> None:
    """A Windows checkout may hand the file over with CRLF endings; the pin holds."""
    as_lf = read_fixture_text(name).replace(CR + LF, LF)
    as_crlf = as_lf.replace(LF, CR + LF)
    assert fixture_checksum(as_crlf) == PINNED_CHECKSUMS[name]


@pytest.mark.parametrize("name", sorted(PINNED_CHECKSUMS))
def test_each_fixture_is_ascii_with_one_trailing_newline(name: str) -> None:
    text = read_fixture_text(name).replace(CR + LF, LF)
    assert CR not in text
    assert all(code == 10 or 32 <= code <= 126 for code in text.encode("utf-8"))
    assert text.endswith(LF) and not text.endswith(LF + LF)


@pytest.mark.parametrize("name", sorted(PINNED_CHECKSUMS))
def test_each_fixture_is_the_stated_serialisation_of_its_own_value(name: str) -> None:
    """Either repo can regenerate a file from its value: indent 2, ASCII escapes, one LF."""
    text = read_fixture_text(name).replace(CR + LF, LF)
    assert json.dumps(json.loads(text), indent=2, ensure_ascii=True) + LF == text


def test_a_changed_character_changes_the_checksum() -> None:
    text = read_fixture_text(LIST_FIXTURE)
    assert "Any news?" in text
    assert fixture_checksum(text.replace("Any news?", "Any newz?")) != PINNED_CHECKSUMS[LIST_FIXTURE]


# ------------------------------------------------------------------ what the copies hold


def test_the_list_fixture_is_the_list_response_with_three_tickets() -> None:
    body = load_fixture(LIST_FIXTURE)
    assert list(body) == ["contract_version", "generated_at", "tickets"]
    assert body["contract_version"] == "1"
    assert [ticket["hubspot_ticket_id"] for ticket in body["tickets"]] == [
        "9300000000001",
        None,
        "9300000000003",
    ]


def test_every_ticket_of_the_list_fixture_carries_the_24_keys_in_order() -> None:
    expected = [
        "id",
        "hubspot_ticket_id",
        "subject",
        "body",
        "state",
        "priority",
        "channel",
        "source_type",
        "topic_of_ticket",
        "hs_pipeline_stage",
        "created_at",
        "hubspot_updated_at",
        "signal",
        "category",
        "form_answers",
        "order_number",
        "tracking_number",
        "requester",
        "company_name",
        "merchant",
        "thread",
        "thread_truncated",
        "has_attachments",
        "related",
    ]
    for ticket in load_fixture(LIST_FIXTURE)["tickets"]:
        assert list(ticket) == expected


def test_the_single_ticket_fixture_is_ticket_one_of_the_list() -> None:
    body = load_fixture(TICKET_FIXTURE)
    assert list(body) == ["contract_version", "ticket", "unprocessed"]
    assert body["contract_version"] == "1"
    assert body["unprocessed"] is False
    assert body["ticket"] == load_fixture(LIST_FIXTURE)["tickets"][0]


def test_no_fixture_holds_a_real_address() -> None:
    for name in (LIST_FIXTURE, TICKET_FIXTURE):
        text = read_fixture_text(name)
        addresses = [word.strip('",') for word in text.split() if "@" in word]
        assert addresses and all(address.endswith("@example.test") for address in addresses)


# ------------------------------------------------------------------ the category list


def test_the_slug_fixture_is_the_contract_version_and_fifteen_slugs() -> None:
    body = load_fixture(SLUGS_FIXTURE)
    assert list(body) == ["contract_version", "slugs"]
    assert body["contract_version"] == "1"
    assert len(body["slugs"]) == 15
    assert len(set(body["slugs"])) == 15
    assert all(isinstance(slug, str) and slug for slug in body["slugs"])


def test_the_classifiers_categories_are_the_contracts_slugs() -> None:
    assert set(load_fixture(SLUGS_FIXTURE)["slugs"]) == llm.VALID_CATEGORIES


def test_non_category_keys_are_exactly_the_four() -> None:
    assert set(os_bundle.NON_CATEGORY_KEYS) == FOUR_NON_CATEGORY_KEYS
    assert len(os_bundle.NON_CATEGORY_KEYS) == 4


def test_the_four_non_category_keys_are_all_in_the_real_file() -> None:
    """A name in the constant that the file no longer holds is a stale exclusion."""
    assert FOUR_NON_CATEGORY_KEYS <= set(real_categories_config())


def test_the_category_keys_of_the_real_file_are_the_contracts_slugs() -> None:
    keys = os_bundle.category_keys(real_categories_config())
    assert isinstance(keys, list)
    assert len(keys) == 15
    assert set(keys) == set(load_fixture(SLUGS_FIXTURE)["slugs"])


def test_category_keys_are_every_top_level_key_but_the_four_in_file_order() -> None:
    config = {
        "form_topic_mapping": {"Billing": "billing_invoice"},
        "zeta_category": {"kb_file": "kb/z.md"},
        "auto_send_form_topics": [],
        "alpha_category": {"kb_file": "kb/a.md"},
        "category_owners": {},
        "suppress_reply_reminder_categories": [],
        "middle_category": None,
    }
    assert os_bundle.category_keys(config) == ["zeta_category", "alpha_category", "middle_category"]


def test_a_fifth_non_category_key_is_returned_so_the_equality_fails() -> None:
    """The drift alarm: a new top-level key that is not a category must be added to
    NON_CATEGORY_KEYS on purpose. Until it is, the rule returns it and the
    comparison with the contract's slugs is red."""
    slugs = load_fixture(SLUGS_FIXTURE)["slugs"]
    config = dict(real_categories_config())
    config["category_review_cadence"] = {"days": 7}

    keys = os_bundle.category_keys(config)

    assert len(keys) == 16
    assert "category_review_cadence" in keys
    assert set(keys) != set(slugs)
    assert set(keys) - set(slugs) == {"category_review_cadence"}


def test_category_keys_does_not_know_the_category_list() -> None:
    """An exclude-list: names the classifier has never heard of come straight back."""
    assert os_bundle.category_keys({"not_a_known_category": {}, "another": {}}) == [
        "not_a_known_category",
        "another",
    ]
    assert os_bundle.category_keys({}) == []


@pytest.mark.parametrize("not_a_config", [None, [], "wismo_tracking", 3])
def test_category_keys_refuses_a_config_that_is_not_a_mapping(not_a_config) -> None:
    with pytest.raises(TypeError):
        os_bundle.category_keys(not_a_config)


# ------------------------------------------------------------------ the write-back contract (plan 17)

# The run id of both fixture bodies. The ticket is ticket 1 of the list fixture.
FIXTURE_RUN_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def _fixture_entry() -> dict:
    """What the write-back builder reads of a bundle entry, for list ticket 1."""
    ticket = load_fixture(LIST_FIXTURE)["tickets"][0]
    return {"os_ticket_id": ticket["id"], "signal": dict(ticket["signal"])}


def test_the_classification_fixture_is_two_bodies_the_codes_and_the_limits() -> None:
    body = load_fixture(CLASSIFICATION_FIXTURE)
    assert list(body) == ["contract_version", "completed", "failed", "failure_reason_codes", "limits"]
    assert len(read_fixture_text(CLASSIFICATION_FIXTURE).replace(CR + LF, LF).encode("utf-8")) == 1303
    assert body["completed"]["signal"] == body["failed"]["signal"]
    ticket = load_fixture(LIST_FIXTURE)["tickets"][0]
    assert body["completed"]["signal"] == {"id": ticket["signal"]["id"], "at": ticket["signal"]["at"]}


def test_the_pass_failure_codes_are_the_fixtures_six_in_order() -> None:
    assert list(os_pass.PASS_FAILURE_CODES) == load_fixture(CLASSIFICATION_FIXTURE)["failure_reason_codes"]
    assert len(set(os_pass.PASS_FAILURE_CODES)) == 6


def test_the_two_text_limits_are_the_fixtures() -> None:
    limits = load_fixture(CLASSIFICATION_FIXTURE)["limits"]
    assert os_pass.REASON_MAX_CHARS == limits["reason_max_chars"] == 1000
    assert os_pass.FORM_TOPIC_MAX_CHARS == limits["form_topic_max_chars"] == 200


def test_the_prompt_version_and_the_model_are_the_fixtures_literals() -> None:
    """Checker WIRE-W4: the write-back reports the real prompt version and the
    agent's model, and both are named constants the builder cannot be argued out of."""
    body = load_fixture(CLASSIFICATION_FIXTURE)
    for shape in ("completed", "failed"):
        assert os_pass.BASE_PROMPT_VERSION == body[shape]["prompt_version"] == "v3.3.0"
        assert os_pass.AGENT_MODEL == body[shape]["model"]
    assert os_pass.AGENT_MODEL == llm.DRAFT_MODEL
    assert os_pass.AGENT_MODEL != llm.CLASSIFY_MODEL
    limits = body["limits"]
    assert 1 <= len(os_pass.BASE_PROMPT_VERSION) <= limits["prompt_version_max_chars"]
    assert 1 <= len(os_pass.AGENT_MODEL) <= limits["model_max_chars"]
    assert "#" not in os_pass.BASE_PROMPT_VERSION  # never the cycle-suffixed draft version


def test_the_write_backs_category_list_is_the_contracts_and_the_classifiers() -> None:
    slugs = load_fixture(SLUGS_FIXTURE)["slugs"]
    assert list(os_pass.CATEGORY_SLUGS) == slugs
    assert set(os_pass.CATEGORY_SLUGS) == llm.VALID_CATEGORIES


def test_the_builder_takes_the_entry_the_ticket_result_the_run_id_and_the_mode_only() -> None:
    assert list(inspect.signature(os_pass.build_classification_payload).parameters) == [
        "entry",
        "ticket_result",
        "run_id",
        "source_mode",
    ]


def test_the_completed_builder_reproduces_the_fixture_body_key_for_key() -> None:
    expected = load_fixture(CLASSIFICATION_FIXTURE)["completed"]
    ticket_result = {
        "outcome": "completed",
        "classification": {
            "category": expected["category"],
            "confidence": expected["confidence"],
            "priority": expected["priority"],
            "reason": expected["reason"],
            "form_topic": expected["form_topic"],
        },
    }
    built = os_pass.build_classification_payload(_fixture_entry(), ticket_result, FIXTURE_RUN_ID, "os")
    assert built == expected
    assert list(built) == list(expected)
    assert list(built["signal"]) == ["id", "at"]


def test_the_failed_builder_reproduces_the_fixture_body_key_for_key() -> None:
    expected = load_fixture(CLASSIFICATION_FIXTURE)["failed"]
    ticket_result = {"outcome": "failed", "failure_reason_code": expected["failure_reason_code"]}
    built = os_pass.build_classification_payload(_fixture_entry(), ticket_result, FIXTURE_RUN_ID, "os")
    assert built == expected
    assert list(built) == list(expected)
