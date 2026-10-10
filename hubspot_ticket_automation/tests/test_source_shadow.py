"""The SOURCE shadow: the cron's half of the source comparison (Phase 93, plan 18).

`ticket_pipeline/source_shadow.py` runs when the ticket source setting is
`shadow`. HubSpot stays the source the cron works from; in the same run the
cron also asks Pack'N OS for its list, classifies both inputs with the same
classify step and reports what it saw to the OS, which does the matching.

This is NOT the engine shadow (`pipeline.shadow_enabled`, `outputs/shadow/`,
the `[pipeline.shadow]` log tag). That older comparison is direct-API engine
against agent; this one is ticket source against ticket source.

PART 1 (this file's first half): the comparison core. `compare_sources`,
`build_observation`, the seen cache, `resolve_hubspot_candidates` and the
engine-shadow artifact reader, each driven directly with fakes.

PART 2: the runner, `run_source_shadow`, driven through `run_tickets.run(deps)`.

No test here reaches a database, HubSpot, the Pack'N OS, the model API, a
secret file, the tracked config/ directory or the real agent:

    - the suite's network guard and no-database guard (tests/conftest.py) apply;
    - the autouse fixtures of tests/test_os_mode_run.py are taken by name: every
      child process and the ShipSidekick lookup are refused with a ledger, and
      the clients' file paths point at nothing;
    - the autouse fixtures of tests/test_rollback_state.py are taken by name:
      the tracked state file is never read, nothing under config/ is written,
      and the directory is compared before and after this file;
    - the classifier is always a fake.
"""
from __future__ import annotations

import ast
import copy
import inspect
import json
import logging
import os
import re
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # hubspot_ticket_automation/
sys.path.insert(0, str(PACKAGE_ROOT / "scripts"))  # scripts/ is imported flat

import post_action_items as poster  # noqa: E402
from packn_os_hubspot_client import client, tickets_api  # noqa: E402
from packn_os_hubspot_client.tickets_api import TicketsApiError  # noqa: E402
from tests import test_os_mode_run as osm  # noqa: E402
from tests.helpers import fakes  # noqa: E402
from tests.test_os_mode_run import (  # noqa: E402,F401 - autouse fixtures, used by name
    _no_child_process,
    _no_real_file_and_a_clean_counter,
)
from tests.test_rollback_state import (  # noqa: E402,F401 - autouse fixtures, used by name
    _no_tracked_state_file,
    _the_tracked_config_is_as_it_was,
)
from ticket_pipeline import hydrate, os_bundle, os_pass, source, source_shadow  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
OBSERVATION_FIXTURE = "tickets-api-source-shadow.v1.json"
MODULE_PATH = PACKAGE_ROOT / "ticket_pipeline" / "source_shadow.py"

OS_ORIGIN = osm.OS_ORIGIN
RUN_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"  # the contract fixture's
TEXT_MARKER = "ZZ-MARKER-93-18"
EXC_MARKER = "ZZ-EXC-93-18"

B_HS_ID = osm.T1_HS_ID  # selected by both sources
B_OS_ID = osm.T1_OS_ID
H_HS_ID = "9300000000099"  # selected by HubSpot only; the OS list does not hold it
O_OS_ID = osm.T2_OS_ID  # selected by the OS only; it has no HubSpot id
L_HS_ID = osm.T3_HS_ID  # HubSpot-linked, selected by the OS only
L_OS_ID = osm.T3_OS_ID

# A real instant with microseconds, at a non-UTC offset: 10:07:03.120456 -04:00.
STARTED_AT = datetime(2026, 10, 9, 10, 7, 3, 120456, tzinfo=timezone(timedelta(hours=-4)))
STARTED_AT_TEXT = "2026-10-09T14:07:03.120Z"

SETTINGS = {
    "per_run_cap": 25,
    "active_stages": ["1", "3"],
    "hubspot_portal_id": osm.PORTAL_ID,
    "conversation_email_limit": 10,
    "pipeline": {"shadow_enabled": False},
}

UUID_FORM = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
INSTANT_FORM = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

# The six fields of the observation that may be JSON null (93-07, checker WIRE-W3).
NULLABLE = {
    ("hubspot", "matched", "stage"),
    ("hubspot", "selected", "os_ticket_id"),
    ("hubspot", "selected", "signal"),
    ("hubspot", "selected", "category"),
    ("os", "selected", "hubspot_ticket_id"),
    ("os", "selected", "category"),
}


def observation_fixture() -> dict:
    return json.loads((FIXTURES / OBSERVATION_FIXTURE).read_bytes().decode("utf-8"))


def payload(index: int) -> dict:
    """List-fixture ticket `index` (0: linked, 1: no HubSpot id, 2: linked)."""
    return osm._payload(index)


def signal_of(index: int) -> dict:
    return dict(payload(index)["signal"])


def hubspot_context(hubspot_id: str, stage: Any = "1", last_message: str = "Any news from HubSpot?") -> dict:
    """A HubSpot-built ticket context, in the shape hydration returns."""
    return {
        "ticket_id": hubspot_id,
        "ticket_link": f"https://app.hubspot.com/contacts/{osm.PORTAL_ID}/ticket/{hubspot_id}",
        "subject": f"Where is my parcel {TEXT_MARKER}",
        "topic_of_ticket": "Shipping question",
        "source_type": "FORM",
        "form_fields": {"order_number": "FX-ORDER-1001", "inquiry_description": f"late {TEXT_MARKER}"},
        "has_attachments": False,
        "stage": stage,
        "priority_from_hubspot": "HIGH",
        "createdate": "2026-10-01T09:00:00.000Z",
        "hs_lastmodifieddate": "2026-10-08T10:00:00.500Z",
        "contact": {"name": "Pat Example", "email": "pat@example.test", "phone": None},
        "company": {"name": "Fixture Merchant Ltd", "domain": None},
        "thread": [
            {
                "from": "customer",
                "email": "pat@example.test",
                "date": "2026-10-01T09:00:00.000Z",
                "body_text": f"It was due on Tuesday. {TEXT_MARKER}",
            },
            {
                "from": "agent",
                "email": "care@example.test",
                "date": "2026-10-02T14:30:00.250Z",
                "body_text": "We are checking with the carrier.",
            },
            {
                "from": "customer",
                "email": "pat@example.test",
                "date": "2026-10-08T10:00:00.123Z",
                "body_text": last_message,
            },
        ],
        "latest_customer_message": last_message,
        "related_tickets": [],
    }


class Classifier:
    """The classify step, without a model: `classify(context) -> classification`.

    It answers what `llm.classify` answers (a dict with a category AND free
    text), so a test can see that only the category is ever kept."""

    def __init__(self, by_ticket: Optional[dict] = None, default: str = "wismo_tracking", failing: tuple = ()) -> None:
        self.by_ticket = dict(by_ticket or {})
        self.default = default
        self.failing = set(failing)
        self.calls: list[dict] = []

    def __call__(self, context: dict) -> dict:
        self.calls.append(context)
        ticket_id = context.get("ticket_id")
        if ticket_id in self.failing or "*" in self.failing:
            raise RuntimeError(f"the model refused {EXC_MARKER}")
        return {
            "category": self.by_ticket.get(ticket_id, self.default),
            "confidence": 0.9,
            "priority": "normal",
            "reason": f"the client wrote {TEXT_MARKER}",
        }

    def ticket_ids(self) -> list:
        return [context.get("ticket_id") for context in self.calls]


class Hydrator:
    """`hydrate_hubspot_context(hubspot ticket id) -> context`, from a table."""

    def __init__(self, contexts: Optional[dict] = None) -> None:
        self.contexts = dict(contexts or {})
        self.calls: list[str] = []

    def __call__(self, hubspot_ticket_id: str) -> dict:
        self.calls.append(hubspot_ticket_id)
        if hubspot_ticket_id not in self.contexts:
            raise LookupError(f"HubSpot holds no such ticket {EXC_MARKER}")
        return self.contexts[hubspot_ticket_id]


class ThreadFetcher:
    """`fetch_hubspot_thread(hubspot ticket id) -> email objects`, from a table."""

    def __init__(self, threads: Optional[dict] = None) -> None:
        self.threads = dict(threads or {})
        self.calls: list[str] = []

    def __call__(self, hubspot_ticket_id: str) -> list:
        self.calls.append(hubspot_ticket_id)
        return copy.deepcopy(self.threads.get(hubspot_ticket_id, []))


class EngineArtifacts:
    """`engine_classification(hubspot ticket id) -> {"category", "ticket_context"} | None`."""

    def __init__(self, artifacts: Optional[dict] = None) -> None:
        self.artifacts = dict(artifacts or {})
        self.calls: list[str] = []

    def __call__(self, hubspot_ticket_id: str) -> Optional[dict]:
        self.calls.append(hubspot_ticket_id)
        return self.artifacts.get(hubspot_ticket_id)


class Bench:
    """One comparison with every collaborator a recording fake."""

    def __init__(self) -> None:
        self.classifier = Classifier()
        self.hydrator = Hydrator({B_HS_ID: hubspot_context(B_HS_ID)})
        self.fetcher = ThreadFetcher({L_HS_ID: osm.T3_EMAILS, B_HS_ID: osm.T1_EMAILS})
        self.engine = EngineArtifacts()
        self.cache: dict = {}
        self.settings = copy.deepcopy(SETTINGS)

    def deps(self) -> Any:
        return source_shadow.ComparisonDeps(
            classify=self.classifier,
            hydrate_hubspot_context=self.hydrator,
            fetch_hubspot_thread=self.fetcher,
            engine_classification=self.engine,
            settings=self.settings,
            os_origin=OS_ORIGIN,
            cache=self.cache,
        )

    def compare(self, hubspot_side: dict, os_list: list) -> Any:
        return source_shadow.compare_sources(hubspot_side, os_list, self.deps())


@pytest.fixture
def bench() -> Bench:
    return Bench()


def side(
    candidates: tuple = (B_HS_ID, H_HS_ID),
    matched: Optional[list] = None,
    resolved: Optional[dict] = None,
    page_full: bool = False,
) -> dict:
    """The HubSpot side as the runner hands it over: the pre-gate's decision
    (`matched`, `candidates`, `page_full`) plus what the OS answered for each
    candidate (`resolved`; a candidate the OS answered 404 for is absent)."""
    if matched is None:
        matched = [
            {"id": ticket_id, "hs_lastmodifieddate": "2026-10-08T10:00:00.500Z", "deduped": False}
            for ticket_id in candidates
        ]
    if resolved is None:
        resolved = {B_HS_ID: {"os_ticket_id": B_OS_ID, "signal": signal_of(0)}}
    return {
        "decision": "run_agent",
        "reason": "candidates",
        "matched": matched,
        "candidates": list(candidates),
        "page_full": page_full,
        "resolved": resolved,
    }


def observation_of(result: Any, settings: Optional[dict] = None, run_id: str = RUN_ID) -> dict:
    return source_shadow.build_observation(
        run_id, STARTED_AT, settings if settings is not None else SETTINGS, result.hubspot_side, result.os_side
    )


def key_lists(body: Any, path: tuple = ()) -> dict:
    """Every object of a body, by where it sits: path -> the set of key lists
    seen there. A list's items share their list's path."""
    found: dict = {}
    if isinstance(body, dict):
        found.setdefault(path, set()).add(tuple(body))
        for key, value in body.items():
            for where, lists in key_lists(value, path + (key,)).items():
                found.setdefault(where, set()).update(lists)
    elif isinstance(body, list):
        for item in body:
            for where, lists in key_lists(item, path).items():
                found.setdefault(where, set()).update(lists)
    return found


def nulls(body: Any, path: tuple = ()) -> list:
    """The path of every JSON null in a body."""
    found: list = []
    if body is None:
        return [path]
    if isinstance(body, dict):
        for key, value in body.items():
            found.extend(nulls(value, path + (key,)))
    elif isinstance(body, list):
        for item in body:
            found.extend(nulls(item, path))
    return found


# =========================================================================
# PART 1 — the comparison core
# =========================================================================

# ------------------------------------------------- the contract's acceptance shape


def test_one_both_one_hubspot_only_and_one_os_only_build_the_contract_fixture(bench: Bench) -> None:
    """SPEC R9's fixture, end to end through the core: ticket B is selected by
    both sources, H by HubSpot only (the OS answers 404 for it and HubSpot
    cannot hydrate it), O by the OS only (its classify call fails). The body
    built from that comparison is the OS's contract fixture, key for key."""
    bench.classifier.failing = {O_OS_ID}
    result = bench.compare(side(), [payload(0), payload(1)])
    built = observation_of(result)
    expected = observation_fixture()
    assert built == expected
    assert json.dumps(built, indent=2, ensure_ascii=True) + "\n" == (
        (FIXTURES / OBSERVATION_FIXTURE).read_bytes().decode("utf-8").replace("\r\n", "\n")
    )


def test_the_observation_holds_both_sides_and_the_runs_four_facts(bench: Bench) -> None:
    bench.hydrator.contexts[H_HS_ID] = hubspot_context(H_HS_ID, stage="3")
    bench.classifier.by_ticket = {H_HS_ID: "billing_invoice", O_OS_ID: "other_unclassified"}
    matched = [
        {"id": B_HS_ID, "hs_lastmodifieddate": "2026-10-08T10:00:00.500Z", "deduped": False},
        {"id": H_HS_ID, "hs_lastmodifieddate": None, "deduped": False},
        {"id": "9300000000555", "hs_lastmodifieddate": "2026-10-01T00:00:00Z", "deduped": True},
    ]
    result = bench.compare(side(matched=matched, page_full=True), [payload(0), payload(1)])
    body = observation_of(result)
    assert list(body) == [
        "contract_version", "run_id", "started_at", "per_run_cap", "active_stages", "hubspot", "os",
    ]
    assert body["contract_version"] == "1"
    assert body["run_id"] == RUN_ID
    assert body["started_at"] == STARTED_AT_TEXT
    assert body["per_run_cap"] == 25
    assert body["active_stages"] == ["1", "3"]
    assert body["hubspot"]["page_full"] is True
    # every hit of the search, with its stage where the cron learned it, and its dedupe flag
    assert body["hubspot"]["matched"] == [
        {"hubspot_ticket_id": B_HS_ID, "stage": "1", "deduped": False},
        {"hubspot_ticket_id": H_HS_ID, "stage": "3", "deduped": False},
        {"hubspot_ticket_id": "9300000000555", "stage": None, "deduped": True},
    ]
    assert body["hubspot"]["selected"] == [
        {"hubspot_ticket_id": B_HS_ID, "os_ticket_id": B_OS_ID, "signal": signal_of(0), "category": "wismo_tracking"},
        {"hubspot_ticket_id": H_HS_ID, "os_ticket_id": None, "signal": None, "category": "billing_invoice"},
    ]
    assert body["os"]["selected"] == [
        {"os_ticket_id": B_OS_ID, "hubspot_ticket_id": B_HS_ID, "signal": signal_of(0), "category": "wismo_tracking"},
        {"os_ticket_id": O_OS_ID, "hubspot_ticket_id": None, "signal": signal_of(1), "category": "other_unclassified"},
    ]


def test_a_signal_is_passed_through_character_for_character(bench: Bench) -> None:
    listed = payload(1)
    listed["signal"] = {"kind": "opening", "id": "2d8e3f4a-5b6c-4d7e-8f9a-1b2c3d4e5f6a", "at": "2026-10-08T15:45:10.000Z"}
    result = bench.compare(side(candidates=(), resolved={}), [listed])
    body = observation_of(result)
    assert body["os"]["selected"][0]["signal"] == listed["signal"]
    assert list(body["os"]["selected"][0]["signal"]) == ["kind", "id", "at"]


# ------------------------------------------------------- absence (checker WIRE-W3)


def all_absent(bench: Bench) -> dict:
    """A comparison in which every nullable value is absent: a matched hit with
    no stage, a candidate the OS answered 404 for whose classify call failed, a
    listed ticket with no HubSpot id whose classify call failed."""
    bench.hydrator.contexts = {H_HS_ID: hubspot_context(H_HS_ID, stage=None)}
    bench.classifier.failing = {"*"}
    result = bench.compare(side(candidates=(H_HS_ID,), resolved={}), [payload(1)])
    return observation_of(result)


def test_every_nullable_field_is_present_and_null_and_no_key_is_missing(bench: Bench) -> None:
    built = all_absent(bench)
    fixture = observation_fixture()
    # the same key list at every level as the contract fixture: nothing dropped, nothing added
    built_keys, fixture_keys = key_lists(built), key_lists(fixture)
    for where, lists in built_keys.items():
        assert lists <= fixture_keys[where], where
        assert len(lists) == 1, where
    for where in fixture_keys:
        if where[-1:] != ("signal",):  # no signal object exists in this body: every one is null
            assert where in built_keys, where

    (matched,) = built["hubspot"]["matched"]
    (selected,) = built["hubspot"]["selected"]
    (listed,) = built["os"]["selected"]
    for record, key in (
        (matched, "stage"),
        (selected, "os_ticket_id"),
        (selected, "signal"),
        (selected, "category"),
        (listed, "hubspot_ticket_id"),
        (listed, "category"),
    ):
        assert key in record, key
        assert record[key] is None, key
    # six nulls, and they are exactly the six fields the contract lets be null
    found = nulls(built)
    assert len(found) == 6
    assert {(path[0], path[1], path[-1]) for path in found} == NULLABLE


def test_serialised_each_absent_value_is_the_json_word_null(bench: Bench) -> None:
    text = json.dumps(all_absent(bench), separators=(",", ":"))
    for fragment in (
        '"stage":null',
        '"os_ticket_id":null',
        '"signal":null',
        '"hubspot_ticket_id":null',
    ):
        assert fragment in text, fragment
    assert text.count('"category":null') == 2
    assert text.count("null") == 6
    assert "None" not in text


def good_sides() -> tuple[dict, dict]:
    fixture = observation_fixture()
    return copy.deepcopy(fixture["hubspot"]), copy.deepcopy(fixture["os"])


def build(hubspot_side: dict, os_side: dict, **over: Any) -> dict:
    return source_shadow.build_observation(
        over.get("run_id", RUN_ID),
        over.get("started_at", STARTED_AT),
        over.get("settings", SETTINGS),
        hubspot_side,
        os_side,
    )


def test_the_builder_reproduces_its_own_output_from_the_fixtures_two_sides() -> None:
    hubspot_side, os_side = good_sides()
    assert build(hubspot_side, os_side) == observation_fixture()


@pytest.mark.parametrize("field", ["signal", "os_ticket_id"])
@pytest.mark.parametrize("absent", [None, "", "missing"])
def test_the_builder_refuses_a_listed_os_ticket_with_no_signal_or_no_id(field: str, absent: Any) -> None:
    """Those two are not nullable in the contract: the builder raises rather
    than send a body the OS would refuse."""
    hubspot_side, os_side = good_sides()
    if absent == "missing":
        del os_side["selected"][0][field]
    else:
        os_side["selected"][0][field] = absent
    with pytest.raises((ValueError, TypeError)):
        build(hubspot_side, os_side)


@pytest.mark.parametrize(
    "edit",
    [
        lambda h, o: h["selected"][0].pop("category"),
        lambda h, o: h["selected"][1].pop("signal"),
        lambda h, o: h["selected"][1].pop("os_ticket_id"),
        lambda h, o: h["matched"][1].pop("stage"),
        lambda h, o: h["matched"][0].pop("deduped"),
        lambda h, o: o["selected"][1].pop("hubspot_ticket_id"),
        lambda h, o: o["selected"][1].pop("category"),
        lambda h, o: h.pop("page_full"),
        lambda h, o: h.pop("matched"),
        lambda h, o: o.pop("selected"),
    ],
)
def test_the_builder_never_fills_in_a_key_it_was_not_handed(edit: Callable[[dict, dict], Any]) -> None:
    """Absence is stated one way, by the caller: an input with a key MISSING is
    refused. The builder does not read "missing" as "null"."""
    hubspot_side, os_side = good_sides()
    edit(hubspot_side, os_side)
    with pytest.raises((ValueError, TypeError, KeyError)):
        build(hubspot_side, os_side)


@pytest.mark.parametrize(
    "edit",
    [
        lambda h, o: h["matched"][0].update(hubspot_ticket_id="93-not-digits"),
        lambda h, o: h["matched"][0].update(hubspot_ticket_id="9" * 21),
        lambda h, o: h["matched"][0].update(hubspot_ticket_id=9300000000001),
        lambda h, o: h["matched"][0].update(stage="New ticket"),  # a stage's display name
        lambda h, o: h["matched"][0].update(stage="x" * 65),
        lambda h, o: h["matched"][0].update(stage=""),
        lambda h, o: h["matched"][0].update(deduped="no"),
        lambda h, o: h.update(page_full=None),
        lambda h, o: h["selected"][0].update(os_ticket_id="not-a-uuid"),
        lambda h, o: h["selected"][0].update(os_ticket_id=B_OS_ID.replace("-", "")),
        lambda h, o: h["selected"][0].update(category="Where is my order"),
        lambda h, o: h["selected"][0].update(category=3),
        lambda h, o: h["selected"][0].update(signal={"kind": "client_reply", "id": "x"}),
        lambda h, o: h["selected"][0].update(signal="1c7d2e3f"),
        lambda h, o: h["selected"][0]["signal"].update(at="2026-10-08T10:00:00Z"),
        lambda h, o: h["selected"][0]["signal"].update(at="2026-10-08T10:00:00.123+00:00"),
        lambda h, o: o["selected"][0]["signal"].update(id=""),
        lambda h, o: o["selected"][0].update(category=f"client said {TEXT_MARKER}"),
        lambda h, o: o["selected"][1].update(hubspot_ticket_id="abc"),
        lambda h, o: o["selected"][0]["signal"].update(kind="internal_note"),
        lambda h, o: o["selected"][0]["signal"].update(id="1c7d2e3f"),
        lambda h, o: h.update(matched=h["matched"] * 101),  # 202 entries
        lambda h, o: h.update(selected=h["selected"] * 101),
        lambda h, o: o.update(selected=o["selected"] * 101),
        lambda h, o: o.update(selected="none"),
    ],
)
def test_the_builder_refuses_a_value_the_contract_refuses(edit: Callable[[dict, dict], Any]) -> None:
    hubspot_side, os_side = good_sides()
    edit(hubspot_side, os_side)
    with pytest.raises((ValueError, TypeError)):
        build(hubspot_side, os_side)


def test_a_full_list_of_200_is_accepted_and_201_is_not() -> None:
    hubspot_side, os_side = good_sides()
    hubspot_side["matched"] = [dict(hubspot_side["matched"][0]) for _ in range(200)]
    assert len(build(hubspot_side, os_side)["hubspot"]["matched"]) == 200
    hubspot_side["matched"].append(dict(hubspot_side["matched"][0]))
    with pytest.raises(ValueError):
        build(hubspot_side, os_side)


def test_the_builder_copies_no_key_it_does_not_know() -> None:
    """A caller cannot smuggle text in through an extra key: every object is
    assembled from its named keys only."""
    hubspot_side, os_side = good_sides()
    hubspot_side["selected"][0]["subject"] = TEXT_MARKER
    hubspot_side["matched"][0]["hs_lastmodifieddate"] = TEXT_MARKER
    hubspot_side["note"] = TEXT_MARKER
    os_side["selected"][0]["body"] = TEXT_MARKER
    os_side["selected"][0]["signal"]["body"] = TEXT_MARKER
    built = build(hubspot_side, os_side)
    assert built == observation_fixture()
    assert TEXT_MARKER not in json.dumps(built)


@pytest.mark.parametrize(
    "settings",
    [
        {"per_run_cap": 0, "active_stages": ["1"]},
        {"per_run_cap": 1001, "active_stages": ["1"]},
        {"per_run_cap": True, "active_stages": ["1"]},
        {"per_run_cap": "25", "active_stages": ["1"]},
        {"per_run_cap": 25.5, "active_stages": ["1"]},
        {"per_run_cap": 25, "active_stages": ["New ticket"]},
        {"per_run_cap": 25, "active_stages": [str(n) for n in range(51)]},
        {"per_run_cap": 25, "active_stages": "1"},
        None,
    ],
)
def test_the_builder_refuses_settings_the_contract_refuses(settings: Any) -> None:
    hubspot_side, os_side = good_sides()
    with pytest.raises((ValueError, TypeError)):
        build(hubspot_side, os_side, settings=settings)


def test_the_cap_defaults_to_25_and_a_numeric_stage_is_sent_as_text() -> None:
    """settings.yaml may hold the stages as numbers; the contract's are tokens."""
    hubspot_side, os_side = good_sides()
    body = build(hubspot_side, os_side, settings={"active_stages": [1, 3]})
    assert body["per_run_cap"] == 25
    assert body["active_stages"] == ["1", "3"]
    assert build(hubspot_side, os_side, settings={"per_run_cap": 1000, "active_stages": []})["active_stages"] == []


# -------------------------------------------------- started_at (checker WIRE-W3)


def test_started_at_is_written_from_a_real_aware_instant_in_the_three_digit_z_form() -> None:
    hubspot_side, os_side = good_sides()
    body = build(hubspot_side, os_side, started_at=STARTED_AT)
    assert isinstance(STARTED_AT, datetime) and STARTED_AT.utcoffset() == timedelta(hours=-4)
    assert STARTED_AT.microsecond == 120456
    assert body["started_at"] == "2026-10-09T14:07:03.120Z"
    assert body["started_at"] == observation_fixture()["started_at"]
    assert INSTANT_FORM.match(body["started_at"])
    assert body["started_at"] != STARTED_AT.isoformat()  # never isoformat's offset and six digits


@pytest.mark.parametrize(
    "instant, written",
    [
        (datetime(2026, 10, 9, 14, 7, 3, tzinfo=timezone.utc), "2026-10-09T14:07:03.000Z"),
        (datetime(2026, 10, 9, 14, 7, 3, 999999, tzinfo=timezone.utc), "2026-10-09T14:07:03.999Z"),
        (datetime(2026, 12, 31, 23, 30, 0, 1000, tzinfo=timezone(timedelta(hours=-5))), "2027-01-01T04:30:00.001Z"),
    ],
)
def test_started_at_is_cut_to_the_millisecond_in_utc(instant: datetime, written: str) -> None:
    hubspot_side, os_side = good_sides()
    assert build(hubspot_side, os_side, started_at=instant)["started_at"] == written


@pytest.mark.parametrize(
    "not_an_instant",
    [datetime(2026, 10, 9, 14, 7, 3, 120456), "2026-10-09T14:07:03.120Z", 1790000000, None],
)
def test_the_builder_refuses_a_naive_instant_and_anything_that_is_not_a_datetime(not_an_instant: Any) -> None:
    hubspot_side, os_side = good_sides()
    with pytest.raises((ValueError, TypeError)):
        build(hubspot_side, os_side, started_at=not_an_instant)


def test_started_at_is_formatted_by_the_clients_own_function(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []

    def spy(moment: datetime) -> str:
        seen.append(moment)
        return "2026-10-09T14:07:03.120Z"

    monkeypatch.setattr(tickets_api, "iso_millis_z", spy)
    hubspot_side, os_side = good_sides()
    build(hubspot_side, os_side)
    assert seen == [STARTED_AT]


# ------------------------------------------------------ run_id (checker WIRE-W6)


def test_the_run_id_is_the_string_it_was_given_unchanged() -> None:
    hubspot_side, os_side = good_sides()
    run_id = "0b6f6c1e-2d3a-4b5c-8d7e-9f0a1b2c3d4e"
    body = build(hubspot_side, os_side, run_id=run_id)
    assert body["run_id"] == run_id
    assert body["run_id"] is run_id


@pytest.mark.parametrize(
    "not_a_run_id",
    [
        "7C9E6679-7425-40DE-944B-E07FC1F90AE7",
        "7c9e6679742540de944be07fc1f90ae7",
        "7c9e6679-7425-40de-944b-e07fc1f90ae",
        " 7c9e6679-7425-40de-944b-e07fc1f90ae7",
        "7c9e6679-7425-40de-944b-e07fc1f90ae7\n",
        "",
        None,
        7,
    ],
)
def test_the_builder_refuses_a_run_id_that_is_not_a_lower_case_hyphenated_uuid(not_a_run_id: Any) -> None:
    hubspot_side, os_side = good_sides()
    with pytest.raises((ValueError, TypeError)):
        build(hubspot_side, os_side, run_id=not_a_run_id)


# ----------------------------------------------------- no ticket text, anywhere


def marked(index: int) -> dict:
    """A list ticket with the marker in its subject, its body and its thread."""
    ticket = payload(index)
    ticket["subject"] = f"{ticket['subject']} {TEXT_MARKER}"
    ticket["body"] = f"{ticket['body']} {TEXT_MARKER}"
    for item in ticket["thread"]:
        item["body"] = f"{item['body']} {TEXT_MARKER}"
    return ticket


def test_no_ticket_text_reaches_the_observation_the_cache_file_or_a_log(
    bench: Bench, tmp_path: Path, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    """The marker is in every text the comparison touches: the OS tickets'
    subject, body and thread, the HubSpot-built context, the classifier's own
    reason, and the messages of the two exceptions it raises."""
    caplog.set_level(logging.DEBUG)
    bench.classifier.failing = {L_HS_ID}
    cache_path = tmp_path / "source_shadow_seen.json"
    result = bench.compare(side(), [marked(0), marked(1), marked(2)])
    body = observation_of(result)
    source_shadow.save_seen_cache(cache_path, bench.cache)

    assert TEXT_MARKER in json.dumps(bench.classifier.calls, default=str)  # the marker did reach the classifier
    for text in (json.dumps(body), cache_path.read_text(encoding="utf-8"), repr(result.codes)):
        assert TEXT_MARKER not in text
        assert EXC_MARKER not in text
    assert bench.cache  # something was cached, so the file is not trivially empty
    captured = capfd.readouterr()
    logged = "\n".join([caplog.text, *(repr(vars(record)) for record in caplog.records)])
    for sink, text in (("logging", logged), ("stdout", captured.out), ("stderr", captured.err)):
        assert TEXT_MARKER not in text, sink
        assert EXC_MARKER not in text, sink
    assert source_shadow.LOG_TAG in captured.err  # the comparison did log, with its own tag


def test_the_marker_helper_agrees_no_text_reached_a_log(
    bench: Bench, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    """Checker CRON-W4: this repo's scripts log with print to stderr, so the
    shared helper reads captured logging, stdout and stderr."""
    caplog.set_level(logging.DEBUG)
    bench.classifier.failing = {H_HS_ID, O_OS_ID}
    bench.hydrator.contexts[H_HS_ID] = hubspot_context(H_HS_ID)
    bench.compare(side(), [marked(0), marked(1), marked(2)])
    fakes.assert_marker_absent(TEXT_MARKER, caplog, capfd)


def test_an_exceptions_message_is_never_logged_only_its_class(
    bench: Bench, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    bench.classifier.failing = {"*"}
    bench.hydrator.contexts = {}
    result = bench.compare(side(), [marked(0), marked(1)])
    assert result.codes  # both kinds of failure were counted
    fakes.assert_marker_absent(EXC_MARKER, caplog, capfd)


# ------------------------------------------------------------- the HubSpot side


def test_an_engine_artifact_of_this_run_is_reused_and_costs_no_call(bench: Bench) -> None:
    engine_context = hubspot_context(B_HS_ID)
    bench.engine.artifacts = {B_HS_ID: {"category": "billing_invoice", "ticket_context": engine_context}}
    result = bench.compare(side(candidates=(B_HS_ID,)), [])
    assert bench.engine.calls == [B_HS_ID]
    assert bench.hydrator.calls == []
    assert bench.classifier.calls == []
    assert result.hubspot_side["selected"][0]["category"] == "billing_invoice"
    assert result.hubspot_side["matched"][0]["stage"] == "1"  # read from the engine's own context
    assert result.classify_calls == 0


def test_without_an_engine_artifact_the_side_costs_one_hydrate_and_one_classify(bench: Bench) -> None:
    result = bench.compare(side(candidates=(B_HS_ID,)), [])
    assert bench.hydrator.calls == [B_HS_ID]
    assert len(bench.classifier.calls) == 1
    assert bench.classifier.calls[0] is bench.hydrator.contexts[B_HS_ID]  # the hydrated context itself
    assert result.hubspot_side["selected"][0]["category"] == "wismo_tracking"
    assert result.classify_calls == 1


def test_an_engine_category_outside_the_contract_is_not_reused(bench: Bench) -> None:
    bench.engine.artifacts = {B_HS_ID: {"category": "Shipping question", "ticket_context": hubspot_context(B_HS_ID)}}
    result = bench.compare(side(candidates=(B_HS_ID,)), [])
    assert bench.hydrator.calls == [B_HS_ID]
    assert len(bench.classifier.calls) == 1
    assert result.hubspot_side["selected"][0]["category"] == "wismo_tracking"


def test_a_candidate_named_twice_is_selected_once(bench: Bench) -> None:
    result = bench.compare(side(candidates=(B_HS_ID, B_HS_ID)), [])
    assert [row["hubspot_ticket_id"] for row in result.hubspot_side["selected"]] == [B_HS_ID]
    assert bench.hydrator.calls == [B_HS_ID]


# ------------------------------------------------------------------ the OS side


def test_the_os_side_costs_one_classify_call_per_listed_ticket_not_in_the_cache(bench: Bench) -> None:
    result = bench.compare(side(candidates=(), resolved={}), [payload(0), payload(1), payload(2)])
    assert bench.classifier.ticket_ids() == [B_HS_ID, O_OS_ID, L_HS_ID]
    assert result.classify_calls == 3
    assert bench.cache == {
        f"{B_OS_ID}:{osm.T1_SIGNAL_ID}": "wismo_tracking",
        f"{O_OS_ID}:{osm.T2_SIGNAL_ID}": "wismo_tracking",
        f"{L_OS_ID}:{osm.T3_SIGNAL_ID}": "wismo_tracking",
    }


def test_a_ticket_and_signal_already_in_the_cache_costs_no_call_and_reports_its_category(bench: Bench) -> None:
    bench.cache[f"{O_OS_ID}:{osm.T2_SIGNAL_ID}"] = "billing_invoice"
    result = bench.compare(side(candidates=(), resolved={}), [payload(1)])
    assert bench.classifier.calls == []
    assert bench.fetcher.calls == []
    assert result.os_side["selected"] == [
        {"os_ticket_id": O_OS_ID, "hubspot_ticket_id": None, "signal": signal_of(1), "category": "billing_invoice"}
    ]
    assert result.classify_calls == 0


def test_a_second_run_over_the_same_list_makes_no_call_at_all(bench: Bench) -> None:
    listed = [payload(0), payload(1), payload(2)]
    first = bench.compare(side(candidates=(), resolved={}), listed)
    calls_after_first = len(bench.classifier.calls)
    second = bench.compare(side(candidates=(), resolved={}), listed)
    assert calls_after_first == 3
    assert len(bench.classifier.calls) == 3  # a ticket that stays OS-only costs nothing on a later run
    assert second.os_side == first.os_side
    assert second.classify_calls == 0


def test_a_newer_signal_on_a_cached_ticket_is_classified_again(bench: Bench) -> None:
    bench.cache[f"{O_OS_ID}:{osm.T2_SIGNAL_ID}"] = "billing_invoice"
    listed = payload(1)
    listed["signal"] = {"kind": "client_reply", "id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee", "at": "2026-10-09T09:00:00.000Z"}
    result = bench.compare(side(candidates=(), resolved={}), [listed])
    assert len(bench.classifier.calls) == 1
    assert result.os_side["selected"][0]["category"] == "wismo_tracking"
    assert bench.cache[f"{O_OS_ID}:aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"] == "wismo_tracking"


def test_a_failed_classification_is_not_cached(bench: Bench) -> None:
    bench.classifier.failing = {O_OS_ID}
    result = bench.compare(side(candidates=(), resolved={}), [payload(1)])
    assert result.os_side["selected"][0]["category"] is None
    assert bench.cache == {}


def test_a_cached_value_that_is_not_a_contract_slug_is_not_trusted(bench: Bench) -> None:
    bench.cache[f"{O_OS_ID}:{osm.T2_SIGNAL_ID}"] = f"text {TEXT_MARKER}"
    result = bench.compare(side(candidates=(), resolved={}), [payload(1)])
    assert len(bench.classifier.calls) == 1
    assert result.os_side["selected"][0]["category"] == "wismo_tracking"


def test_a_listed_ticket_named_twice_is_reported_and_classified_once(bench: Bench) -> None:
    result = bench.compare(side(candidates=(), resolved={}), [payload(1), payload(1)])
    assert [row["os_ticket_id"] for row in result.os_side["selected"]] == [O_OS_ID]
    assert len(bench.classifier.calls) == 1


def test_a_listed_ticket_with_no_signal_is_left_out_with_a_code(bench: Bench) -> None:
    """The contract has no null signal on the OS side, so such a ticket cannot
    be reported. It is counted and the rest of the list is still compared."""
    broken = payload(2)
    broken["signal"] = None
    result = bench.compare(side(candidates=(), resolved={}), [broken, payload(1)])
    assert [row["os_ticket_id"] for row in result.os_side["selected"]] == [O_OS_ID]
    assert result.codes == {source_shadow.SOURCE_SHADOW_OS_TICKET_UNUSABLE: 1}
    assert bench.classifier.ticket_ids() == [O_OS_ID]
    observation_of(result)  # and what is left still builds


# --------------------------------------------------------- the thread, by ticket


def test_a_both_sides_ticket_reuses_the_hubspot_built_thread_with_no_second_fetch(bench: Bench) -> None:
    result = bench.compare(side(candidates=(B_HS_ID,)), [payload(0)])
    assert bench.fetcher.calls == []  # no second thread fetch
    hubspot_built, os_built = bench.classifier.calls
    assert hubspot_built is bench.hydrator.contexts[B_HS_ID]
    assert os_built["thread"] == hubspot_built["thread"]
    assert os_built["latest_customer_message"] == "Any news from HubSpot?"
    # every other key is the OS builder's own, from the OS payload
    expected = os_bundle.build_context(payload(0), None, bench.settings, OS_ORIGIN)
    expected["thread"] = hubspot_built["thread"]
    expected["latest_customer_message"] = "Any news from HubSpot?"
    assert os_built == expected
    assert list(os_built) == list(expected)
    assert result.classify_calls == 2


def test_the_reused_thread_is_a_copy_the_comparison_cannot_change(bench: Bench) -> None:
    before = copy.deepcopy(bench.hydrator.contexts[B_HS_ID])
    bench.compare(side(candidates=(B_HS_ID,)), [payload(0)])
    os_built = bench.classifier.calls[1]
    assert os_built["thread"] is not bench.hydrator.contexts[B_HS_ID]["thread"]
    assert bench.hydrator.contexts[B_HS_ID] == before


def test_a_both_sides_ticket_reuses_the_thread_of_a_reused_engine_context(bench: Bench) -> None:
    engine_context = hubspot_context(B_HS_ID, last_message="From the engine's own context")
    bench.engine.artifacts = {B_HS_ID: {"category": "billing_invoice", "ticket_context": engine_context}}
    bench.compare(side(candidates=(B_HS_ID,)), [payload(0)])
    assert bench.fetcher.calls == [] and bench.hydrator.calls == []
    (os_built,) = bench.classifier.calls
    assert os_built["latest_customer_message"] == "From the engine's own context"


def test_a_both_sides_ticket_whose_hubspot_context_holds_no_thread_keeps_the_os_thread(bench: Bench) -> None:
    """What `os` mode does when HubSpot holds no email for a linked ticket: the
    OS thread. An empty HubSpot thread is not laid over it."""
    bench.hydrator.contexts[B_HS_ID]["thread"] = []
    bench.hydrator.contexts[B_HS_ID]["latest_customer_message"] = ""
    bench.compare(side(candidates=(B_HS_ID,)), [payload(0)])
    os_built = bench.classifier.calls[1]
    assert os_built == os_bundle.build_context(payload(0), None, bench.settings, OS_ORIGIN)
    assert bench.fetcher.calls == []


def test_a_linked_ticket_only_the_os_selected_costs_one_thread_fetch(bench: Bench) -> None:
    bench.compare(side(candidates=(), resolved={}), [payload(2)])
    assert bench.fetcher.calls == [L_HS_ID]
    (os_built,) = bench.classifier.calls
    assert os_built == os_bundle.build_context(payload(2), osm.T3_EMAILS, bench.settings, OS_ORIGIN)


def test_a_ticket_with_no_hubspot_id_costs_no_thread_fetch(bench: Bench) -> None:
    bench.compare(side(candidates=(), resolved={}), [payload(1)])
    assert bench.fetcher.calls == []
    (os_built,) = bench.classifier.calls
    assert os_built == os_bundle.build_context(payload(1), None, bench.settings, OS_ORIGIN)


def test_a_both_sides_ticket_whose_hubspot_context_could_not_be_had_fetches_its_thread(bench: Bench) -> None:
    bench.hydrator.contexts = {}  # hydration fails for B
    result = bench.compare(side(candidates=(B_HS_ID,)), [payload(0)])
    assert bench.fetcher.calls == [B_HS_ID]
    assert result.hubspot_side["selected"][0]["category"] is None
    assert result.os_side["selected"][0]["category"] == "wismo_tracking"


def test_the_os_context_is_built_by_the_os_modes_own_builder(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []
    real = os_bundle.build_context

    def spy(listed: dict, thread: Any, settings: dict, os_origin: str) -> dict:
        seen.append((listed["id"], thread, os_origin))
        return real(listed, thread, settings, os_origin)

    monkeypatch.setattr(os_bundle, "build_context", spy)
    bench.compare(side(candidates=(B_HS_ID,)), [payload(0), payload(1), payload(2)])
    assert [(os_id, origin) for os_id, _thread, origin in seen] == [
        (B_OS_ID, OS_ORIGIN),
        (O_OS_ID, OS_ORIGIN),
        (L_OS_ID, OS_ORIGIN),
    ]
    assert seen[0][1] is None and seen[1][1] is None
    assert seen[2][1] == osm.T3_EMAILS


def test_one_classify_function_object_serves_both_sides(bench: Bench) -> None:
    """D-05: a disagreement can only come from the input, so both inputs go to
    the SAME callable: the hydrated HubSpot context and the OS-built one."""
    calls: list = []

    def classify(context: dict) -> dict:
        calls.append(context)
        return {"category": "wismo_tracking"}

    deps = bench.deps()
    deps.classify = classify
    source_shadow.compare_sources(side(candidates=(B_HS_ID,)), [payload(0), payload(1)], deps)
    assert calls[0] is bench.hydrator.contexts[B_HS_ID]
    assert calls[1]["ticket_id"] == B_HS_ID and calls[1] is not calls[0]
    assert calls[2] == os_bundle.build_context(payload(1), None, bench.settings, OS_ORIGIN)
    assert len(calls) == 3
    fields = [f.name for f in source_shadow.ComparisonDeps.__dataclass_fields__.values()]
    assert fields.count("classify") == 1
    assert [name for name in fields if "classif" in name and name != "engine_classification"] == ["classify"]


@pytest.mark.parametrize(
    "answer, category",
    [
        ({"category": "billing_invoice", "reason": TEXT_MARKER}, "billing_invoice"),
        ("billing_invoice", "billing_invoice"),
        ({"category": "Billing"}, None),
        ({"reason": "no category"}, None),
        (None, None),
        (["billing_invoice"], None),
    ],
)
def test_only_a_contract_slug_is_kept_of_what_the_classifier_answers(bench: Bench, answer: Any, category: Any) -> None:
    deps = bench.deps()
    deps.classify = lambda context: answer
    result = source_shadow.compare_sources(side(candidates=(), resolved={}), [payload(1)], deps)
    assert result.os_side["selected"][0]["category"] == category
    assert TEXT_MARKER not in json.dumps(result.os_side)


# ------------------------------------------------------------ one ticket's error


def test_a_classify_call_that_raises_leaves_that_category_null_and_the_rest_classified(bench: Bench) -> None:
    bench.hydrator.contexts[H_HS_ID] = hubspot_context(H_HS_ID)
    bench.classifier.failing = {H_HS_ID}
    result = bench.compare(side(), [payload(0), payload(1), payload(2)])
    assert [row["category"] for row in result.hubspot_side["selected"]] == ["wismo_tracking", None]
    assert [row["category"] for row in result.os_side["selected"]] == ["wismo_tracking"] * 3
    assert result.codes == {source_shadow.SOURCE_SHADOW_CLASSIFY_FAILED: 1}
    assert result.classify_calls == 5 and result.classify_failures == 1
    assert result.classifier_unavailable is False


def test_a_hydration_that_raises_leaves_that_category_null_and_the_rest_classified(bench: Bench) -> None:
    result = bench.compare(side(), [payload(1)])  # the hydrator holds no context for H
    assert [row["category"] for row in result.hubspot_side["selected"]] == ["wismo_tracking", None]
    assert result.os_side["selected"][0]["category"] == "wismo_tracking"
    assert result.codes == {source_shadow.SOURCE_SHADOW_HYDRATE_FAILED: 1}
    assert bench.classifier.ticket_ids() == [B_HS_ID, O_OS_ID]


def test_a_thread_fetch_that_raises_is_one_code_and_the_ticket_is_built_from_the_os_thread(bench: Bench) -> None:
    def refuse(hubspot_ticket_id: str) -> list:
        raise ConnectionError(f"HubSpot refused {EXC_MARKER}")

    deps = bench.deps()
    deps.fetch_hubspot_thread = refuse
    result = source_shadow.compare_sources(side(candidates=(), resolved={}), [payload(2)], deps)
    assert result.os_side["selected"][0]["category"] == "wismo_tracking"
    assert result.codes == {source_shadow.SOURCE_SHADOW_THREAD_FETCH_FAILED: 1}
    assert bench.classifier.calls[0] == os_bundle.build_context(payload(2), None, bench.settings, OS_ORIGIN)


def test_an_engine_reader_that_raises_falls_back_to_hydrate_and_classify(bench: Bench) -> None:
    def explode(hubspot_ticket_id: str) -> dict:
        raise OSError(f"disk {EXC_MARKER}")

    deps = bench.deps()
    deps.engine_classification = explode
    result = source_shadow.compare_sources(side(candidates=(B_HS_ID,)), [], deps)
    assert bench.hydrator.calls == [B_HS_ID]
    assert result.hubspot_side["selected"][0]["category"] == "wismo_tracking"


def test_three_classify_failures_in_a_row_stop_the_step_for_the_rest_of_the_comparison(bench: Bench) -> None:
    """A model API that is down (an expired key, no credit) must not be asked
    once per listed ticket. After three failures in a row nothing more is asked."""
    bench.classifier.failing = {"*"}
    listed = []
    for other_id in (O_OS_ID, "5b1f0c2a-9d3e-4f4a-8b6c-7d8e9f0a1b2c", "6c2a1d3b-0e4f-4a5b-9c7d-8e9f0a1b2c3d"):
        ticket = payload(1)
        ticket["id"] = other_id
        listed.append(ticket)
    listed.append(payload(2))  # HubSpot-linked: its thread would be fetched before its classify call
    result = bench.compare(side(candidates=(), resolved={}), listed)
    assert source_shadow.COMPARISON_CLASSIFY_FAILURE_LIMIT == 3
    assert len(bench.classifier.calls) == 3
    assert [row["category"] for row in result.os_side["selected"]] == [None] * 4
    assert result.classifier_unavailable is True
    assert result.classify_calls == 3 and result.classify_failures == 3
    assert bench.fetcher.calls == []  # and no thread is fetched for a ticket that will not be classified


def test_a_success_between_failures_keeps_the_step_going(bench: Bench) -> None:
    """Three failures, but never three IN A ROW: fail, fail, answer, fail, answer."""
    failing_twin = payload(1)
    failing_twin["id"] = "5b1f0c2a-9d3e-4f4a-8b6c-7d8e9f0a1b2c"
    answered_twin = payload(1)
    answered_twin["id"] = "6c2a1d3b-0e4f-4a5b-9c7d-8e9f0a1b2c3d"
    bench.classifier.failing = {B_HS_ID, failing_twin["id"], L_HS_ID}
    listed = [payload(0), failing_twin, payload(1), payload(2), answered_twin]
    result = bench.compare(side(candidates=(), resolved={}), listed)
    assert len(bench.classifier.calls) == 5  # the step was never stopped
    assert result.classify_failures == 3
    assert result.classifier_unavailable is False
    assert [row["category"] for row in result.os_side["selected"]] == [
        None,
        None,
        "wismo_tracking",
        None,
        "wismo_tracking",
    ]


def test_every_call_failing_is_reported_as_an_unavailable_classifier(bench: Bench) -> None:
    """One ticket, one failed call: nothing this comparison asked was answered."""
    bench.classifier.failing = {"*"}
    result = bench.compare(side(candidates=(), resolved={}), [payload(1)])
    assert result.classify_calls == 1 and result.classify_failures == 1
    assert result.classifier_unavailable is True


def test_a_comparison_that_asked_the_classifier_nothing_is_not_unavailable(bench: Bench) -> None:
    result = bench.compare(side(candidates=(), resolved={}, matched=[]), [])
    assert result.classify_calls == 0
    assert result.classifier_unavailable is False
    assert result.hubspot_side == {"page_full": False, "matched": [], "selected": []}
    assert result.os_side == {"selected": []}


# ------------------------------------------------------------- zero side effects


def test_the_comparison_writes_no_draft_action_item_write_back_or_email(
    bench: Bench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Threat T-93-56: the measuring half must not change what it measures."""
    counters = {"drafts": 0, "action_items": 0, "write_backs": 0, "emails": 0}

    def counting(name: str) -> Callable[..., Any]:
        def record(*_args: Any, **_kwargs: Any) -> Any:
            counters[name] += 1
            raise AssertionError(f"the comparison must never do this: {name}")

        return record

    for writer in ("write_draft", "write_cron_draft", "replace_pending_draft", "mark_rerun_processed"):
        monkeypatch.setattr(client, writer, counting("drafts"))
    monkeypatch.setattr(poster, "_post", counting("action_items"))
    monkeypatch.setattr(poster, "post_action_items", counting("action_items"))
    monkeypatch.setattr(tickets_api, "post_classification", counting("write_backs"))
    monkeypatch.setattr(tickets_api, "post_source_shadow_observations", counting("write_backs"))
    monkeypatch.setattr(smtplib, "SMTP", counting("emails"))
    monkeypatch.setattr(smtplib, "SMTP_SSL", counting("emails"))

    bench.hydrator.contexts[H_HS_ID] = hubspot_context(H_HS_ID)
    result = bench.compare(side(), [payload(0), payload(1), payload(2)])
    observation_of(result)

    assert counters == {"drafts": 0, "action_items": 0, "write_backs": 0, "emails": 0}
    assert len(osm._CHILD_LEDGER) == 0  # and no child process: no sender script, no agent
    assert result.classify_calls == 5  # the comparison did run


def test_the_comparisons_dependencies_hold_no_writer() -> None:
    """`compare_sources` is handed a classifier and three readers. No draft
    writer, poster, write-back client or sender is among its dependencies."""
    fields = set(source_shadow.ComparisonDeps.__dataclass_fields__)
    assert fields == {
        "classify",
        "hydrate_hubspot_context",
        "fetch_hubspot_thread",
        "engine_classification",
        "settings",
        "os_origin",
        "cache",
    }
    assert list(inspect.signature(source_shadow.compare_sources).parameters) == ["hubspot_side", "os_list", "deps"]


def test_the_module_names_no_sender_and_no_draft_writer() -> None:
    text = MODULE_PATH.read_text(encoding="utf-8")
    for name in (
        "send_customer_" + "reply",
        "send_" + "draft",
        "send_digest_" + "email",
        "smtp" + "lib",
        "google" + "apiclient",
        "write_" + "draft",
        "write_cron_" + "draft",
        "replace_pending_" + "draft",
        "merge_" + "fingerprints",
        "state" + ".json",
    ):
        assert name not in text, name


# ---------------------------------------------------------------------- the cache

KEY_FORM = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}:[A-Za-z0-9:_-]{1,64}$")


def test_the_cache_path_is_under_outputs_runs_and_named_for_the_source_shadow() -> None:
    assert source_shadow.SEEN_CACHE_PATH == PACKAGE_ROOT / "outputs" / "runs" / "source_shadow_seen.json"
    assert source_shadow.seen_cache_path(Path("somewhere")) == Path("somewhere") / "outputs" / "runs" / "source_shadow_seen.json"
    assert source_shadow.COMPARISON_LIMIT == 100
    assert source_shadow.LOG_TAG == "[source_shadow]"


def test_the_cache_is_saved_through_a_temp_file_beside_it_and_a_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "runs" / "source_shadow_seen.json"
    target.parent.mkdir()
    target.write_text("{}", encoding="utf-8")
    renames: list = []
    real_replace = os.replace

    def spy(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        assert Path(dst).read_text(encoding="utf-8") == "{}"  # the target is untouched until the rename
        renames.append((Path(src), Path(dst), json.loads(Path(src).read_text(encoding="utf-8"))))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy)
    cache = {f"{O_OS_ID}:{osm.T2_SIGNAL_ID}": "billing_invoice"}
    source_shadow.save_seen_cache(target, cache)

    assert len(renames) == 1
    temp, destination, written = renames[0]
    assert destination == target and temp != target
    assert temp.parent == target.parent  # beside the target: the rename never crosses a filesystem
    assert written == cache
    assert json.loads(target.read_text(encoding="utf-8")) == cache
    assert sorted(p.name for p in target.parent.iterdir()) == ["source_shadow_seen.json"]  # no temp file left


def test_a_failed_rename_leaves_the_old_cache_and_no_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "source_shadow_seen.json"
    before = json.dumps({f"{B_OS_ID}:{osm.T1_SIGNAL_ID}": "billing_invoice"})
    target.write_text(before, encoding="utf-8")

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError("the file is held open")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        source_shadow.save_seen_cache(target, {f"{O_OS_ID}:{osm.T2_SIGNAL_ID}": "billing_invoice"})
    assert target.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["source_shadow_seen.json"]


def test_the_cache_file_holds_only_id_keys_and_category_slugs(tmp_path: Path) -> None:
    target = tmp_path / "source_shadow_seen.json"
    dirty = {
        f"{O_OS_ID}:{osm.T2_SIGNAL_ID}": "billing_invoice",
        f"{B_OS_ID}:{osm.T1_SIGNAL_ID}": f"the client said {TEXT_MARKER}",  # not a slug
        f"Subject {TEXT_MARKER}": "billing_invoice",  # not an id key
        f"{L_OS_ID}:{osm.T3_SIGNAL_ID}": None,
        O_OS_ID: "billing_invoice",  # no signal id
        7: "billing_invoice",
        f"{L_OS_ID}:rerun:{osm.RERUN_A}": "wismo_tracking",
    }
    source_shadow.save_seen_cache(target, dirty)
    text = target.read_text(encoding="utf-8")
    stored = json.loads(text)
    assert stored == {
        f"{O_OS_ID}:{osm.T2_SIGNAL_ID}": "billing_invoice",
        f"{L_OS_ID}:rerun:{osm.RERUN_A}": "wismo_tracking",
    }
    assert TEXT_MARKER not in text
    assert all(KEY_FORM.match(key) for key in stored)
    assert set(stored.values()) <= set(os_pass.CATEGORY_SLUGS)
    assert text.endswith("\n") and "\r" not in text


def test_the_cache_is_capped_at_its_newest_5000_entries(tmp_path: Path) -> None:
    target = tmp_path / "source_shadow_seen.json"
    assert source_shadow.SEEN_CACHE_MAX_ENTRIES == 5000
    cache = {f"{O_OS_ID}:signal-{n:05d}": "billing_invoice" for n in range(5003)}
    source_shadow.save_seen_cache(target, cache)
    stored = json.loads(target.read_text(encoding="utf-8"))
    assert len(stored) == 5000
    assert list(stored)[0] == f"{O_OS_ID}:signal-00003"  # the three oldest are gone
    assert list(stored)[-1] == f"{O_OS_ID}:signal-05002"
    assert len(cache) == 5003  # the caller's dict is not changed


def test_a_saved_cache_is_what_a_later_run_loads(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "runs" / "source_shadow_seen.json"
    cache = {f"{O_OS_ID}:{osm.T2_SIGNAL_ID}": "billing_invoice", f"{B_OS_ID}:{osm.T1_SIGNAL_ID}": "wismo_tracking"}
    source_shadow.save_seen_cache(target, cache)  # the directory is created
    loaded = source_shadow.load_seen_cache(target)
    assert loaded == cache and list(loaded) == list(cache)
    assert source_shadow.seen_cache_key(O_OS_ID, osm.T2_SIGNAL_ID) == f"{O_OS_ID}:{osm.T2_SIGNAL_ID}"


@pytest.mark.parametrize(
    "content",
    [None, b"", b"{", b"[]", b'"billing_invoice"', b"\xff\xfe\x00", b"null", b'{"a": {"b": 1}}'],
)
def test_a_cache_file_that_is_missing_or_unusable_loads_as_empty(tmp_path: Path, content: Any) -> None:
    target = tmp_path / "source_shadow_seen.json"
    if content is not None:
        target.write_bytes(content)
    assert source_shadow.load_seen_cache(target) == {}


def test_loading_drops_an_entry_that_is_not_an_id_key_and_a_slug(tmp_path: Path) -> None:
    target = tmp_path / "source_shadow_seen.json"
    target.write_text(
        json.dumps(
            {
                f"{O_OS_ID}:{osm.T2_SIGNAL_ID}": "billing_invoice",
                f"{B_OS_ID}:{osm.T1_SIGNAL_ID}": f"text {TEXT_MARKER}",
                f"text {TEXT_MARKER}": "billing_invoice",
            }
        ),
        encoding="utf-8",
    )
    assert source_shadow.load_seen_cache(target) == {f"{O_OS_ID}:{osm.T2_SIGNAL_ID}": "billing_invoice"}


# ------------------------------------------- the engine shadow's artifacts (read only)


def engine_dir(root: Path, stamp: str) -> Path:
    directory = root / "outputs" / "shadow" / stamp
    directory.mkdir(parents=True)
    return directory


def artifact(category: Any = "billing_invoice", context: Any = "default") -> dict:
    return {
        "ticket_id": B_HS_ID,
        "classification": {"category": category, "confidence": 0.9, "reason": f"because {TEXT_MARKER}"},
        "draft_body": f"Dear client {TEXT_MARKER}",
        "ticket_context": hubspot_context(B_HS_ID) if context == "default" else context,
    }


SINCE = datetime(2026, 10, 9, 14, 7, 3, 120456, tzinfo=timezone.utc)


def tree(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*"))}


def test_an_engine_artifact_written_during_this_run_gives_its_category_and_context(tmp_path: Path) -> None:
    directory = engine_dir(tmp_path, "2026-10-09T14-07-05Z")
    (directory / f"{B_HS_ID}.json").write_text(json.dumps(artifact()), encoding="utf-8")
    found = source_shadow._engine_classification(tmp_path, B_HS_ID, SINCE)
    assert found == {"category": "billing_invoice", "ticket_context": hubspot_context(B_HS_ID)}
    assert set(found) == {"category", "ticket_context"}  # never the draft body, never the reason


def test_an_engine_artifact_of_the_runs_own_second_counts_as_this_runs(tmp_path: Path) -> None:
    directory = engine_dir(tmp_path, "2026-10-09T14-07-03Z")  # the directory's name has no fraction
    (directory / f"{B_HS_ID}.json").write_text(json.dumps(artifact()), encoding="utf-8")
    assert source_shadow._engine_classification(tmp_path, B_HS_ID, SINCE) is not None


def test_an_engine_artifact_of_an_earlier_run_is_not_reused(tmp_path: Path) -> None:
    directory = engine_dir(tmp_path, "2026-10-09T13-37-00Z")
    (directory / f"{B_HS_ID}.json").write_text(json.dumps(artifact()), encoding="utf-8")
    assert source_shadow._engine_classification(tmp_path, B_HS_ID, SINCE) is None


def test_the_newest_engine_run_wins(tmp_path: Path) -> None:
    for stamp, category in (("2026-10-09T14-07-05Z", "billing_invoice"), ("2026-10-09T14-09-00Z", "returns_rma")):
        (engine_dir(tmp_path, stamp) / f"{B_HS_ID}.json").write_text(json.dumps(artifact(category)), encoding="utf-8")
    assert "returns_rma" in os_pass.CATEGORY_SLUGS
    assert source_shadow._engine_classification(tmp_path, B_HS_ID, SINCE)["category"] == "returns_rma"


@pytest.mark.parametrize(
    "content",
    [
        b"{",
        b"[]",
        json.dumps({"classification": {"category": "Shipping question"}}).encode(),
        json.dumps({"classification": "billing_invoice"}).encode(),
        json.dumps({"ticket_context": {}}).encode(),
        b"\xff\xfe",
    ],
)
def test_an_engine_artifact_that_cannot_be_used_is_no_artifact(tmp_path: Path, content: bytes) -> None:
    directory = engine_dir(tmp_path, "2026-10-09T14-07-05Z")
    (directory / f"{B_HS_ID}.json").write_bytes(content)
    assert source_shadow._engine_classification(tmp_path, B_HS_ID, SINCE) is None


@pytest.mark.parametrize("ticket_id", ["../../escape", "93/1", "", None, "9300000000001/../x", "a" * 80])
def test_the_artifact_reader_only_looks_for_a_numeric_ticket_id(tmp_path: Path, ticket_id: Any) -> None:
    directory = engine_dir(tmp_path, "2026-10-09T14-07-05Z")
    (directory / f"{B_HS_ID}.json").write_text(json.dumps(artifact()), encoding="utf-8")
    # a usable artifact exactly where each traversal would land, were the id put in a path
    (tmp_path / "outputs" / "escape.json").write_text(json.dumps(artifact()), encoding="utf-8")
    (directory / "x.json").write_text(json.dumps(artifact()), encoding="utf-8")
    (directory / "93").mkdir()
    (directory / "93" / "1.json").write_text(json.dumps(artifact()), encoding="utf-8")
    (directory / ".json").write_text(json.dumps(artifact()), encoding="utf-8")
    (directory / ("a" * 80 + ".json")).write_text(json.dumps(artifact()), encoding="utf-8")
    assert source_shadow._engine_classification(tmp_path, ticket_id, SINCE) is None


def test_with_no_engine_directory_there_is_no_artifact_and_none_is_created(tmp_path: Path) -> None:
    assert source_shadow._engine_classification(tmp_path, B_HS_ID, SINCE) is None
    assert list(tmp_path.iterdir()) == []


def test_reading_an_engine_artifact_changes_nothing_under_the_engines_directory(tmp_path: Path) -> None:
    directory = engine_dir(tmp_path, "2026-10-09T14-07-05Z")
    (directory / f"{B_HS_ID}.json").write_text(json.dumps(artifact()), encoding="utf-8")
    (directory / "_summary.json").write_text("{}", encoding="utf-8")
    before = tree(tmp_path)
    source_shadow._engine_classification(tmp_path, B_HS_ID, SINCE)
    source_shadow._engine_classification(tmp_path, H_HS_ID, SINCE)
    assert tree(tmp_path) == before


# --------------------------------------------- D-08: this is the SOURCE shadow


def _module_tree() -> ast.Module:
    return ast.parse(MODULE_PATH.read_text(encoding="utf-8"))


def _docstrings(module: ast.Module) -> set:
    found = set()
    for node in ast.walk(module):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def _names_the_engine_directory(value: Any) -> bool:
    return isinstance(value, str) and (value == "shadow" or "outputs/shadow" in value or "outputs\\shadow" in value)


def test_the_engine_shadows_directory_is_named_only_in_the_artifact_reader_and_never_written() -> None:
    module = _module_tree()
    docstrings = _docstrings(module)
    (reader,) = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "_engine_classification"]
    inside = {id(node) for node in ast.walk(reader)}
    naming = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Constant) and id(node) not in docstrings and _names_the_engine_directory(node.value)
    ]
    assert naming, "the artifact reader names the engine shadow's directory somewhere"
    assert [node.lineno for node in naming if id(node) not in inside] == []

    # the reader reads: no call in it can create, write, move or remove anything
    writers = {"write_text", "write_bytes", "mkdir", "touch", "unlink", "rmdir", "rename", "replace", "rmtree", "mkstemp", "dump"}
    called = set()
    for node in ast.walk(reader):
        if isinstance(node, ast.Call):
            func = node.func
            called.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
            if getattr(func, "id", "") == "open" or getattr(func, "attr", "") == "open":
                modes = [a.value for a in node.args[1:2] if isinstance(a, ast.Constant)]
                modes += [k.value.value for k in node.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)]
                assert all(not any(flag in str(mode) for flag in "wax+") for mode in modes)
    assert called & writers == set()


def test_the_comparison_writes_nothing_under_the_engine_shadows_directory(bench: Bench, tmp_path: Path) -> None:
    directory = engine_dir(tmp_path, "2026-10-09T14-07-05Z")
    (directory / f"{B_HS_ID}.json").write_text(json.dumps(artifact()), encoding="utf-8")
    before = tree(tmp_path / "outputs" / "shadow")
    deps = bench.deps()
    deps.engine_classification = lambda hid: source_shadow._engine_classification(tmp_path, hid, SINCE)
    result = source_shadow.compare_sources(side(), [payload(0), payload(1)], deps)
    source_shadow.save_seen_cache(source_shadow.seen_cache_path(tmp_path), bench.cache)
    assert result.hubspot_side["selected"][0]["category"] == "billing_invoice"  # the artifact was used
    assert tree(tmp_path / "outputs" / "shadow") == before
    assert sorted(p.name for p in (tmp_path / "outputs").iterdir()) == ["runs", "shadow"]


def _defined_names(module: ast.Module) -> list:
    names: list = []
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names.extend(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


def test_every_public_name_says_what_it_belongs_to() -> None:
    """D-08: nothing this module defines can be mistaken for the engine shadow's."""
    tokens = ("source_shadow", "observation", "compar", "cache", "resolve")
    public = [name for name in _defined_names(_module_tree()) if not name.startswith("_")]
    assert {"compare_sources", "build_observation", "load_seen_cache", "save_seen_cache",
            "resolve_hubspot_candidates", "run_source_shadow", "ComparisonResult", "ComparisonDeps",
            "SEEN_CACHE_PATH", "LOG_TAG", "COMPARISON_LIMIT"} <= set(public)
    for name in public:
        assert name in ("LOG_TAG", "COMPARISON_LIMIT") or any(token in name.lower() for token in tokens), name


def test_no_identifier_is_a_bare_shadow() -> None:
    """Every identifier that says `shadow` says WHICH: the source shadow, or
    the engine shadow it reads from. The one exception is the setting's value,
    `source.MODE_SHADOW`, which D-01 locks."""
    identifiers: set = set()
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            identifiers.add(node.name)
        elif isinstance(node, ast.arg):
            identifiers.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            identifiers.add(node.arg)
    saying = sorted(name for name in identifiers if "shadow" in name.lower())
    assert saying, "the module does name the source shadow"
    for name in saying:
        lowered = name.lower()
        assert lowered != "shadow", name
        assert "source_shadow" in lowered or "engine_shadow" in lowered or name == "MODE_SHADOW", name


def test_the_log_tag_and_the_engine_shadows_names_are_each_their_own() -> None:
    """Outside its docstrings (which say what this module is NOT), the module
    holds neither the engine shadow's log tag nor the name of its setting: it
    does not log as the engine shadow and does not read or change its switch."""
    module = _module_tree()
    docstrings = _docstrings(module)
    code_strings = [
        node.value
        for node in ast.walk(module)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]
    assert "[source_shadow]" in code_strings
    assert [text for text in code_strings if "pipeline.shadow" in text or "shadow_enabled" in text] == []
    attributes = {node.attr for node in ast.walk(module) if isinstance(node, ast.Attribute)}
    assert "shadow_enabled" not in attributes and "SHADOW_DIR" not in attributes
    # the engine shadow's own module is untouched by this plan's naming
    from ticket_pipeline import run as engine

    assert engine.SHADOW_DIR == PACKAGE_ROOT / "outputs" / "shadow"
    assert source.MODES == ("hubspot", "shadow", "os")  # the setting's VALUE is locked by D-01


# ----------------------------------------------------- learning the OS ticket


def ticket_answer(index: int, **over: Any) -> dict:
    ticket = payload(index)
    ticket.update(over)
    return {"contract_version": "1", "ticket": ticket, "unprocessed": True}


def test_each_candidate_is_asked_of_the_os_once_and_a_404_is_nothing() -> None:
    api = fakes.FakeOsApi(tickets={B_HS_ID: ticket_answer(0)})
    resolved = source_shadow.resolve_hubspot_candidates([B_HS_ID, H_HS_ID, B_HS_ID], SimpleNamespace(os_api=api))
    assert api.calls_to("fetch_ticket") == [(B_HS_ID,), (H_HS_ID,)]  # by HubSpot id, once each
    assert resolved == {B_HS_ID: {"os_ticket_id": B_OS_ID, "signal": signal_of(0)}}
    assert H_HS_ID not in resolved  # the mirror has not seen it: nothing, not a null entry
    assert api.calls_to("post_classification") == [] and api.calls_to("fetch_unprocessed") == []


def test_a_resolved_ticket_with_no_client_signal_keeps_its_os_id_and_a_null_signal() -> None:
    api = fakes.FakeOsApi(tickets={B_HS_ID: ticket_answer(0, signal=None)})
    resolved = source_shadow.resolve_hubspot_candidates([B_HS_ID], SimpleNamespace(os_api=api))
    assert resolved == {B_HS_ID: {"os_ticket_id": B_OS_ID, "signal": None}}


@pytest.mark.parametrize(
    "answer",
    [
        {"contract_version": "1", "ticket": None},
        {"contract_version": "1", "ticket": {"id": "not-a-uuid", "hubspot_ticket_id": B_HS_ID}},
        {"contract_version": "1", "ticket": {"id": B_OS_ID, "hubspot_ticket_id": H_HS_ID}},  # another ticket
        "a string",
        [],
    ],
)
def test_an_answer_that_does_not_name_this_ticket_resolves_nothing(answer: Any) -> None:
    api = fakes.FakeOsApi(tickets={B_HS_ID: answer})
    codes: dict = {}
    resolved = source_shadow.resolve_hubspot_candidates([B_HS_ID], SimpleNamespace(os_api=api), codes)
    assert resolved == {}
    assert codes == {source_shadow.SOURCE_SHADOW_RESOLVE_INVALID: 1}


@pytest.mark.parametrize("code", ["list_timeout", "signature_rejected", "secret_missing", "list_error"])
def test_an_os_that_cannot_be_asked_stops_the_lookups_after_the_first(code: str) -> None:
    """An OS that is down costs one timeout, not one per candidate."""
    api = fakes.FakeOsApi(tickets={B_HS_ID: ticket_answer(0), L_HS_ID: ticket_answer(2)})
    api.fail_next("fetch_ticket", code)
    codes: dict = {}
    resolved = source_shadow.resolve_hubspot_candidates([H_HS_ID, B_HS_ID, L_HS_ID], SimpleNamespace(os_api=api), codes)
    assert resolved == {}
    assert api.calls_to("fetch_ticket") == [(H_HS_ID,)]
    assert codes == {f"{source_shadow.SOURCE_SHADOW_RESOLVE_FAILED}:{code}": 1}


def test_a_lookup_that_raises_something_else_is_list_error_and_its_class_only(
    caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    class Api:
        def fetch_ticket(self, ref: str) -> Any:
            raise RuntimeError(f"boom {EXC_MARKER}")

    codes: dict = {}
    assert source_shadow.resolve_hubspot_candidates([B_HS_ID], SimpleNamespace(os_api=Api()), codes) == {}
    assert codes == {f"{source_shadow.SOURCE_SHADOW_RESOLVE_FAILED}:list_error": 1}
    fakes.assert_marker_absent(EXC_MARKER, caplog, capfd)


def test_a_candidate_id_that_is_not_digits_is_never_put_in_a_request_path() -> None:
    api = fakes.FakeOsApi()
    resolved = source_shadow.resolve_hubspot_candidates(["../unprocessed", "", None, B_HS_ID], SimpleNamespace(os_api=api))
    assert api.calls_to("fetch_ticket") == [(B_HS_ID,)]
    assert resolved == {}
    assert isinstance(TicketsApiError("list_error"), Exception)


def test_the_observation_limits_are_the_os_routes() -> None:
    """93-07-SUMMARY, "The observation body": 200 tickets a list, 50 stages, a cap of 1 to 1000."""
    assert source_shadow.OBSERVATION_CONTRACT_VERSION == "1"
    assert source_shadow.OBSERVATION_MAX_TICKETS_PER_LIST == 200
    assert source_shadow.OBSERVATION_MAX_ACTIVE_STAGES == 50
    assert source_shadow.OBSERVATION_MAX_PER_RUN_CAP == 1000
    assert source_shadow.COMPARISON_LIMIT <= tickets_api.MAX_LIST_LIMIT
    assert hydrate.latest_customer_message([]) == ""
