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


# =========================================================================
# PART 2 — the runner, through run_tickets.run(deps)
# =========================================================================

import forward_action_items as forwarder  # noqa: E402
import pregate_tickets as pg  # noqa: E402
import run_tickets as rt  # noqa: E402
from tests.test_run_record import FakeClient  # noqa: E402 - the signature-checked fake
from ticket_pipeline import hubspot_api, llm, run_types  # noqa: E402

SEARCH_PATH = "/crm/v3/objects/tickets/search"
RUN_ID_FORM = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
H_OS_ID = "4a5b6c7d-8e9f-4a0b-9c1d-2e3f4a5b6c7d"  # the OS holds H; its list does not offer it
H_SIGNAL = {"kind": "client_reply", "id": "5b6c7d8e-9f0a-4b1c-8d2e-3f4a5b6c7d8e", "at": "2026-10-08T12:00:00.000Z"}
G_HS_ID = "9300000000777"  # a HubSpot candidate the mirror has not seen (404)
RERUN_ID = osm.RERUN_A
CLAIM = {"action_type": "file_carrier_claim", "severity": "normal", "description": f"File the claim {TEXT_MARKER}"}
AGENT_SUMMARY_ONE_FAILED = "failed_tickets=1; codes=image_error:1"

# The real factories, taken before any world replaces them.
REAL_BUILD_CLASSIFY = source_shadow._build_classify
REAL_BUILD_HUBSPOT_HYDRATE = source_shadow._build_hubspot_hydrate
REAL_BUILD_THREAD_FETCH = source_shadow._build_thread_fetch


def hit(ticket_id: str) -> dict:
    return {"id": ticket_id, "properties": {"hs_lastmodifieddate": "2026-10-08T10:00:00.500Z"}}


def os_listing(*indexes: int) -> dict:
    return {
        "contract_version": "1",
        "generated_at": "2026-10-09T12:00:00.000Z",
        "tickets": [payload(index) for index in indexes],
    }


def h_answer() -> dict:
    ticket = payload(2)
    ticket.update(id=H_OS_ID, hubspot_ticket_id=H_HS_ID, signal=dict(H_SIGNAL))
    return {"contract_version": "1", "ticket": ticket, "unprocessed": False}


def done(ref: str, draft_id: Any = "draft-1", items: tuple = (), **over: Any) -> dict:
    """A result entry the agent marks completed, as the hubspot skill writes it."""
    entry = {
        "ref": ref,
        "outcome": "completed",
        "classification": {
            "category": "wismo_tracking",
            "form_topic": None,
            "priority": "normal",
            "confidence": 0.9,
            "reason": f"asks where the order is {TEXT_MARKER}",
        },
        "action_items": [dict(item) for item in items],
    }
    if draft_id is not None:
        entry["draft_id"] = draft_id
    entry.update(over)
    return entry


def broke(ref: str, code: str = "image_error") -> dict:
    return {"ref": ref, "outcome": "failed", "failure_reason_code": code}


def agent_result(*entries: dict) -> dict:
    completed = [entry for entry in entries if entry["outcome"] == "completed"]
    return {
        "status": "success" if len(completed) == len(entries) else "partial",
        "tickets_processed": len(completed),
        "drafts_created": len([entry for entry in completed if entry.get("draft_id")]),
        "error_summary": None,
        "tickets": list(entries),
    }


class LoggedOsApi(osm.ScriptedOsApi):
    """The scripted OS fake, with its reads and the observation post on the shared call log."""

    def fetch_unprocessed(self, limit):
        self.timeline.append(("os_list", limit))
        return super().fetch_unprocessed(limit)

    def fetch_ticket(self, ticket_ref):
        self.timeline.append(("os_ticket", str(ticket_ref)))
        return super().fetch_ticket(ticket_ref)

    def post_source_shadow_observations(self, payload):
        self.timeline.append(("observation", payload.get("run_id") if isinstance(payload, dict) else None))
        return super().post_source_shadow_observations(payload)


class LoggedTransport(fakes.FakeHubSpotTransport):
    def __init__(self, timeline: list, responses: dict) -> None:
        super().__init__(responses)
        self.timeline = timeline

    def __call__(self, method: str, path: str, token: Any = None, body: Any = None) -> Any:
        if path.startswith(SEARCH_PATH):
            self.timeline.append(("hubspot_search",))
        return super().__call__(method, path, token, body)


class LoggedClassifier(Classifier):
    def __init__(self, timeline: list) -> None:
        super().__init__()
        self.timeline = timeline

    def __call__(self, context: dict) -> dict:
        self.timeline.append(("classify", context.get("ticket_id")))
        return super().__call__(context)


class ShadowAgent(fakes.FakeLauncher):
    """The agent, without an agent. It reads the run context the runner prepared,
    at launch time (the scratch directory is gone once the run is over)."""

    def __init__(self, timeline: list, exit_code: int = 0, result: Any = None) -> None:
        super().__init__(exit_code, result)
        self.timeline = timeline
        self.contexts: list[Any] = []

    def __call__(self, command, env):
        self.timeline.append(("agent",))
        self.contexts.append(json.loads(Path(env[run_types.RUN_CONTEXT_ENV]).read_text(encoding="utf-8")))
        return super().__call__(command, env)


class ShadowWorld:
    """One orchestrator with the source set to `shadow` and every collaborator
    replaced. The default is SPEC R9's acceptance fixture: HubSpot's search hits
    B and H, the OS lists B and O, the OS knows H but does not list it, and the
    agent completes B with a draft and reports H as failed."""

    def __init__(self, base: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "shadow") -> None:
        base.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.root = base / "root"
        self.root.mkdir()
        self.client = FakeClient()
        self.timeline: list[tuple] = []  # every outside call, in the order made
        self.os_api: Any = LoggedOsApi(
            self.timeline, unprocessed=os_listing(0, 1), tickets={B_HS_ID: ticket_answer(0), H_HS_ID: h_answer()}
        )
        self.transport = LoggedTransport(self.timeline, {SEARCH_PATH: {"results": [hit(B_HS_ID), hit(H_HS_ID)]}})
        self.hubspot = osm.HubSpotOpener({L_HS_ID: list(osm.T3_EMAILS), B_HS_ID: list(osm.T1_EMAILS)})
        monkeypatch.setattr(hubspot_api, "_default_opener", self.hubspot)
        self.ingest = osm.IngestRecorder(self.timeline)  # every action-item post lands here
        monkeypatch.setattr(poster, "_post", self.ingest)
        self.classifier = LoggedClassifier(self.timeline)
        self.hydrator = Hydrator({B_HS_ID: hubspot_context(B_HS_ID), H_HS_ID: hubspot_context(H_HS_ID, stage="3")})
        monkeypatch.setattr(source_shadow, "_build_classify", lambda settings: self.classifier)
        monkeypatch.setattr(source_shadow, "_build_hubspot_hydrate", lambda settings, read_token: self.hydrator)
        # The runner never writes a draft: any draft writer fails the test.
        self.draft_writes: list[str] = []
        for name in ("write_draft", "write_cron_draft", "replace_pending_draft", "mark_rerun_processed"):
            monkeypatch.setattr(client, name, self._draft_writer(name))
        self.sleeps: list[float] = []
        monkeypatch.setattr(os_pass, "_sleep", self.sleeps.append)
        self.agent: Any = ShadowAgent(self.timeline, 0, agent_result(done(B_HS_ID, "draft-B"), broke(H_HS_ID)))
        self.launch_attempts = 0
        self.settings: Any = dict(copy.deepcopy(SETTINGS), dry_run=False)
        self.token: Any = "test-token"
        self.token_reads = 0
        self.decide: Callable[..., Any] = pg.decide
        self.engine_envs: list[dict] = []
        self.engine_error: Optional[Exception] = None
        self.engine_artifacts: dict[str, dict] = {}
        self.state_path = fakes.temp_state_file(base, {"last_run_at": "2026-09-01T00:00:00Z", "ticket_fingerprints": {}})
        self.seen_path = base / "seen.json"
        self.environ = {"PATH": "kept-path", "PACKN_KEEP_ME": "1"}
        # an aware instant with microseconds at a non-UTC offset (checker WIRE-W3)
        self._tick = datetime(2026, 10, 9, 8, 0, 0, 120456, tzinfo=timezone(timedelta(hours=-4)))
        self.started: Optional[datetime] = None
        self.run_ids: list[str] = []
        rt.load_runners()

    # --- collaborators
    def _draft_writer(self, name: str) -> Callable[..., Any]:
        def record(*_args: Any, **_kwargs: Any) -> Any:
            self.draft_writes.append(name)
            raise AssertionError(f"the source-shadow runner never writes a draft, yet {name} was called")

        return record

    def _now(self) -> datetime:
        self._tick += timedelta(seconds=5)
        if self.started is None:
            self.started = self._tick
        return self._tick

    def _new_run_id(self) -> str:
        run_id = rt.new_run_id()
        self.run_ids.append(run_id)
        return run_id

    def _decide(self, mode: str, deps: Any) -> Any:
        return self.decide(mode, deps)

    def _load_settings(self) -> dict:
        if isinstance(self.settings, Exception):
            raise self.settings
        return self.settings

    def _read_token(self) -> Any:
        self.token_reads += 1
        return self.token

    def _engine(self, env: Any) -> None:
        self.timeline.append(("engine_shadow",))
        self.engine_envs.append(dict(env))
        if self.engine_artifacts:
            stamp = (self.started + timedelta(seconds=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
            directory = self.root / "outputs" / "shadow" / stamp
            directory.mkdir(parents=True, exist_ok=True)
            for ticket_id, content in self.engine_artifacts.items():
                (directory / f"{ticket_id}.json").write_text(json.dumps(content), encoding="utf-8")
        if self.engine_error is not None:
            raise self.engine_error

    def _launch(self, command: Any, env: Any) -> Any:
        self.launch_attempts += 1
        return self.agent(command, env)

    def deps(self) -> Any:
        return run_types.Deps(
            root=self.root,
            state_path=self.state_path,
            seen_path=self.seen_path,
            now=self._now,
            new_run_id=self._new_run_id,
            resolve_mode=lambda: self.mode,
            decide=self._decide,
            load_settings=self._load_settings,
            read_hubspot_token=self._read_token,
            engine_shadow_step=self._engine,
            launch_agent=self._launch,
            report_routine_schedule=self.client.report_routine_schedule,
            read_routine_enabled=self.client.read_routine_enabled,
            read_pending_rerun_requests=self.client.read_pending_rerun_requests,
            rollback_cycle_entries=self.client.rollback_cycle_entries,
            write_run_record=self.client.write_run_record,
            mark_manual_run_completed=self.client.mark_manual_run_completed,
            hubspot_transport=self.transport,
            environ=self.environ,
            os_api=self.os_api,
            read_os_origin=lambda: OS_ORIGIN,
        )

    # --- driving and reading
    def run(self) -> Any:
        return rt.run(self.deps())

    @property
    def record(self) -> dict:
        assert len(self.client.records) == 1, self.client.records
        return self.client.records[0]

    @property
    def observations(self) -> list[dict]:
        return [args[0] for args in self.os_api.calls_to("post_source_shadow_observations")]

    @property
    def writebacks(self) -> list[tuple]:
        return [(args[0], args[1]) for args in self.os_api.calls_to("post_classification")]

    def kinds(self) -> list[str]:
        return [entry[0] for entry in self.timeline]

    def first(self, kind: str) -> int:
        return self.kinds().index(kind)

    def last(self, kind: str) -> int:
        return len(self.timeline) - 1 - self.kinds()[::-1].index(kind)

    @property
    def cache_path(self) -> Path:
        return self.root / "outputs" / "runs" / "source_shadow_seen.json"


@pytest.fixture
def make_shadow_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., ShadowWorld]:
    def make(name: str = "w", mode: str = "shadow") -> ShadowWorld:
        return ShadowWorld(tmp_path / name, monkeypatch, mode)

    return make


@pytest.fixture
def shadow(make_shadow_world: Callable[..., ShadowWorld]) -> ShadowWorld:
    return make_shadow_world()


B_ENTRY = {"os_ticket_id": B_OS_ID, "cycle_key": osm.T1_SIGNAL_ID, "signal_at": "2026-10-08T10:00:00.123Z", "signal_id": osm.T1_SIGNAL_ID}
H_ENTRY = {"os_ticket_id": H_OS_ID, "cycle_key": H_SIGNAL["id"], "signal_at": H_SIGNAL["at"], "signal_id": H_SIGNAL["id"]}


# ----------------------------------------------------------------- the registry


def test_the_runner_is_registered_under_the_settings_value_by_a_lazy_spec() -> None:
    assert rt.RUNNER_SPECS["shadow"] == ("ticket_pipeline.source_shadow", "run_source_shadow")
    rt.load_runners()
    assert rt.MODE_RUNNERS["shadow"] is source_shadow.run_source_shadow
    assert list(inspect.signature(source_shadow.run_source_shadow).parameters) == ["ctx", "deps"]


def test_the_runner_takes_its_types_from_the_leaf_and_never_from_the_script() -> None:
    """Checker N-CRON-1: no import statement for the orchestrator script, and
    the run types come from ticket_pipeline.run_types."""
    module = _module_tree()
    imported: list[str] = []
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert "run_tickets" not in imported
    assert all(not name.startswith("scripts") for name in imported)
    assert any(name in ("ticket_pipeline.run_types", "ticket_pipeline") for name in imported)
    assert source_shadow.run_types is run_types
    # the orchestrator script itself still imports no project module at load
    script = ast.parse((PACKAGE_ROOT / "scripts" / "run_tickets.py").read_text(encoding="utf-8"))
    top_level = [n for n in script.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = [alias.name for n in top_level if isinstance(n, ast.Import) for alias in n.names]
    names += [n.module or "" for n in top_level if isinstance(n, ast.ImportFrom)]
    assert [name for name in names if name.split(".")[0] in ("ticket_pipeline", "packn_os_hubspot_client")] == []


# ------------------------------------------------- SPEC R9's acceptance, in one run


def test_the_acceptance_fixture_one_observation_one_draft_two_write_backs_and_nothing_for_the_os_only_ticket(
    shadow: ShadowWorld,
) -> None:
    state_before = shadow.state_path.read_bytes()
    assert shadow.run() == 0
    record = shadow.record

    # one observation: B and H on the HubSpot side, B and O on the OS side
    assert len(shadow.observations) == 1
    (body,) = shadow.observations
    assert body == {
        "contract_version": "1",
        "run_id": record["run_id"],
        "started_at": "2026-10-09T12:00:05.120Z",
        "per_run_cap": 25,
        "active_stages": ["1", "3"],
        "hubspot": {
            "page_full": False,
            "matched": [
                {"hubspot_ticket_id": B_HS_ID, "stage": "1", "deduped": False},
                {"hubspot_ticket_id": H_HS_ID, "stage": "3", "deduped": False},
            ],
            "selected": [
                {"hubspot_ticket_id": B_HS_ID, "os_ticket_id": B_OS_ID, "signal": signal_of(0), "category": "wismo_tracking"},
                {"hubspot_ticket_id": H_HS_ID, "os_ticket_id": H_OS_ID, "signal": H_SIGNAL, "category": "wismo_tracking"},
            ],
        },
        "os": {
            "selected": [
                {"os_ticket_id": B_OS_ID, "hubspot_ticket_id": B_HS_ID, "signal": signal_of(0), "category": "wismo_tracking"},
                {"os_ticket_id": O_OS_ID, "hubspot_ticket_id": None, "signal": signal_of(1), "category": "wismo_tracking"},
            ],
        },
    }

    # exactly one draft, and it is the HubSpot path's (the agent's own count)
    assert record["drafts_created"] == 1
    assert record["tickets_processed"] == 1
    assert record["status"] == "partial"  # the HubSpot path's own status: one of its two tickets failed
    assert record["error_summary"] == AGENT_SUMMARY_ONE_FAILED
    assert shadow.draft_writes == []  # the runner wrote none itself

    # one completed write-back for B and one failed one for H, both in the mode word `shadow`
    (b_id, b_body), (h_id, h_body) = shadow.writebacks
    assert (b_id, b_body["outcome"], b_body["source_mode"]) == (B_OS_ID, "completed", "shadow")
    assert b_body["signal"] == {"id": osm.T1_SIGNAL_ID, "at": "2026-10-08T10:00:00.123Z"}
    assert b_body["category"] == "wismo_tracking" and b_body["run_id"] == record["run_id"]
    assert (h_id, h_body["outcome"], h_body["source_mode"]) == (H_OS_ID, "failed", "shadow")
    assert h_body["failure_reason_code"] == "other"  # the agent's own code is not one of the OS's six
    assert h_body["signal"] == {"id": H_SIGNAL["id"], "at": H_SIGNAL["at"]}

    # nothing of any kind for O beyond its place in the OS list and the observation
    assert O_OS_ID not in [ticket_id for ticket_id, _body in shadow.writebacks]
    assert shadow.os_api.calls_to("fetch_ticket") == [(B_HS_ID,), (H_HS_ID,)]
    assert shadow.ingest.posts == []
    assert shadow.hubspot.requests == [] and shadow.hubspot.unexpected == []
    naming_o = [entry for entry in shadow.timeline if O_OS_ID in entry]
    assert naming_o == [("classify", O_OS_ID)]  # its one model call, and no write of any kind
    assert len(osm._CHILD_LEDGER) == 0  # no child process: no sender, no agent, no lookup

    # the run record
    assert record["source_mode"] == "shadow"
    assert record["hubspot_calls"] is None
    assert record["failure_reason_code"] is None
    assert len(shadow.client.records) == 1

    # the HubSpot path's state file is the agent's: the runner did not touch it
    assert shadow.state_path.read_bytes() == state_before
    assert shadow.token_reads == 1  # the pre-gate's own read; the comparison needed none


def test_both_hubspot_tickets_completing_is_two_drafts_and_still_nothing_from_the_os_half(shadow: ShadowWorld) -> None:
    shadow.agent.result = agent_result(done(B_HS_ID, "draft-B"), done(H_HS_ID, "draft-H"))
    shadow.run()
    record = shadow.record
    assert record["drafts_created"] == 2 and record["tickets_processed"] == 2
    assert record["status"] == "success"
    assert record["error_summary"] is None  # a clean run has no summary
    assert [(ticket_id, body["outcome"]) for ticket_id, body in shadow.writebacks] == [
        (B_OS_ID, "completed"),
        (H_OS_ID, "completed"),
    ]
    assert shadow.draft_writes == [] and shadow.ingest.posts == []
    assert O_OS_ID not in json.dumps([body for _id, body in shadow.writebacks])


def test_the_order_of_one_run_starts_with_the_os_list_and_ends_with_the_write_backs(shadow: ShadowWorld) -> None:
    shadow.run()
    assert shadow.timeline[0] == ("os_list", 100)  # the first outside call of the run (Pitfall 7)
    order = [
        shadow.first("os_list"),
        shadow.first("hubspot_search"),
        shadow.first("os_ticket"),
        shadow.last("os_ticket"),
        shadow.first("classify"),
        shadow.last("classify"),
        shadow.first("observation"),
        shadow.first("agent"),
        shadow.first("writeback"),
    ]
    assert order == sorted(order)
    assert len(set(order[:3])) == 3 and order[3] < order[4] and order[5] < order[6] < order[7] < order[8]
    assert shadow.first("engine_shadow") < shadow.first("agent")  # the engine shadow before the agent, as in hubspot mode
    assert shadow.kinds().count("os_list") == 1 and shadow.kinds().count("observation") == 1
    assert shadow.kinds().count("hubspot_search") == 1 and shadow.kinds().count("agent") == 1


def test_the_comparison_list_is_asked_with_limit_100_whatever_the_cap(shadow: ShadowWorld) -> None:
    shadow.settings["per_run_cap"] = 7
    shadow.run()
    assert shadow.os_api.calls_to("fetch_unprocessed") == [(100,)]
    assert source_shadow.COMPARISON_LIMIT == 100
    assert shadow.observations[0]["per_run_cap"] == 7


# ----------------------------------------------------- one run, one id, one instant


def test_the_observations_run_id_is_the_run_records_id(shadow: ShadowWorld) -> None:
    """Checker WIRE-W6: one id per run, so the OS can join the comparison to the run."""
    shadow.run()
    (body,) = shadow.observations
    (run_id,) = shadow.run_ids
    assert body["run_id"] == shadow.record["run_id"] == run_id
    assert RUN_ID_FORM.match(body["run_id"]) and RUN_ID_FORM.match(shadow.record["run_id"])
    assert {wb_body["run_id"] for _id, wb_body in shadow.writebacks} == {run_id}


def test_the_observations_start_is_the_runs_start_in_the_three_digit_z_form(shadow: ShadowWorld) -> None:
    shadow.run()
    assert shadow.started.utcoffset() == timedelta(hours=-4) and shadow.started.microsecond == 120456
    assert shadow.observations[0]["started_at"] == "2026-10-09T12:00:05.120Z"
    assert INSTANT_FORM.match(shadow.observations[0]["started_at"])


# ----------------------------------------------------------------- the run context


def test_the_agent_is_handed_the_os_ticket_and_the_signal_as_the_cycle_key(shadow: ShadowWorld) -> None:
    """So the agent's own draft step goes through the atomic replace, keyed by
    the client signal, exactly as an os-mode pass would key it."""
    shadow.run()
    assert shadow.agent.contexts == [{B_HS_ID: B_ENTRY, H_HS_ID: H_ENTRY}]


def test_a_candidate_the_mirror_has_not_seen_gets_no_cycle_key_and_no_write_back(shadow: ShadowWorld) -> None:
    shadow.transport.respond(SEARCH_PATH, {"results": [hit(B_HS_ID), hit(G_HS_ID)]})
    shadow.hydrator.contexts[G_HS_ID] = hubspot_context(G_HS_ID)
    shadow.agent.result = agent_result(done(B_HS_ID, "draft-B"), done(G_HS_ID, "draft-G", items=(CLAIM,)))
    shadow.run()
    assert shadow.os_api.calls_to("fetch_ticket") == [(B_HS_ID,), (G_HS_ID,)]  # the OS answered 404 for G
    assert shadow.agent.contexts == [{B_HS_ID: B_ENTRY}]  # G: no entry, so today's plain draft write
    assert [ticket_id for ticket_id, _body in shadow.writebacks] == [B_OS_ID]  # nothing written back for G
    assert shadow.ingest.posts == []  # G's action items are left to the forwarder, as today
    g_row = shadow.observations[0]["hubspot"]["selected"][1]
    assert g_row == {"hubspot_ticket_id": G_HS_ID, "os_ticket_id": None, "signal": None, "category": "wismo_tracking"}
    assert shadow.record["status"] == "success" and shadow.record["error_summary"] is None


def test_a_rollback_entry_replaces_the_draft_of_a_candidate_the_os_does_not_hold(shadow: ShadowWorld) -> None:
    """Checker CRON-B2: the context starts from the rollback entries, so a
    ticket with a live cycle-keyed draft is replaced, not doubled; a resolved
    candidate's signal wins over its rollback entry."""
    shadow.transport.respond(SEARCH_PATH, {"results": [hit(B_HS_ID), hit(G_HS_ID)]})
    shadow.hydrator.contexts[G_HS_ID] = hubspot_context(G_HS_ID)
    shadow.client.rollback_tickets = {G_HS_ID: None, B_HS_ID: B_OS_ID}
    shadow.agent.result = agent_result(done(B_HS_ID, "draft-B"), done(G_HS_ID, "draft-G"))
    shadow.run()
    (run_id,) = shadow.run_ids
    assert shadow.client.rollback_calls == [(run_id, "tickets-process")]
    (context,) = shadow.agent.contexts
    assert context[G_HS_ID] == {"os_ticket_id": None, "cycle_key": f"hubspot:{run_id}", "signal_at": None, "signal_id": None}
    assert context[B_HS_ID] == B_ENTRY  # the signal entry wins over the rollback entry
    assert [ticket_id for ticket_id, _body in shadow.writebacks] == [B_OS_ID]


def test_a_reruns_entry_wins_over_the_signal_and_the_rollback_entry(shadow: ShadowWorld) -> None:
    def decide(mode: str, deps: Any) -> dict:
        return {
            "decision": "run_agent",
            "reason": "candidates",
            "matched": [{"id": B_HS_ID, "hs_lastmodifieddate": None, "deduped": False}],
            "candidates": [B_HS_ID, H_HS_ID],
            "page_full": False,
            "reruns": [{"id": RERUN_ID, "ticket_id": B_HS_ID}],
        }

    shadow.decide = decide
    shadow.client.rollback_tickets = {B_HS_ID: B_OS_ID}
    shadow.agent.result = agent_result(done(B_HS_ID, "draft-B", items=(CLAIM,)), done(H_HS_ID, "draft-H"))
    shadow.run()
    (context,) = shadow.agent.contexts
    assert context[B_HS_ID] == {"os_ticket_id": B_OS_ID, "cycle_key": f"rerun:{RERUN_ID}", "signal_at": None, "signal_id": None}
    assert context[H_HS_ID] == H_ENTRY
    # the pass still consumed B's client signal, and its items carry the rerun's own pass id
    assert [ticket_id for ticket_id, _body in shadow.writebacks] == [B_OS_ID, H_OS_ID]
    (envelope,) = shadow.ingest.posts
    assert envelope["pass_id"] == f"rerun:{RERUN_ID}"


def test_an_unreadable_rollback_read_is_a_code_and_the_signal_entries_still_stand(shadow: ShadowWorld) -> None:
    shadow.client.rollback_error = RuntimeError(f"database {EXC_MARKER}")
    shadow.run()
    assert shadow.agent.contexts == [{B_HS_ID: B_ENTRY, H_HS_ID: H_ENTRY}]
    assert shadow.record["error_summary"] == f"{AGENT_SUMMARY_ONE_FAILED}; rollback_entries_unreadable"


def test_a_resolved_ticket_with_no_client_signal_gets_no_cycle_key_and_no_write_back(shadow: ShadowWorld) -> None:
    shadow.os_api.tickets[H_HS_ID]["ticket"]["signal"] = None
    shadow.agent.result = agent_result(done(B_HS_ID, "draft-B"), done(H_HS_ID, "draft-H", items=(CLAIM,)))
    shadow.run()
    assert shadow.agent.contexts == [{B_HS_ID: B_ENTRY}]
    assert [ticket_id for ticket_id, _body in shadow.writebacks] == [B_OS_ID]  # no signal to echo for H
    assert shadow.ingest.posts == []  # no stable pass id: H's items are left to the forwarder
    h_row = shadow.observations[0]["hubspot"]["selected"][1]
    assert h_row["os_ticket_id"] == H_OS_ID and h_row["signal"] is None


# ------------------------------------------------------------------- action items


def test_a_completed_ticket_posts_its_items_once_with_both_ids_and_the_signal_as_pass_id(shadow: ShadowWorld) -> None:
    second = dict(CLAIM, action_type="warehouse_investigation")
    shadow.agent.result = agent_result(done(B_HS_ID, "draft-B", items=(CLAIM, second)), done(H_HS_ID, "draft-H", items=(CLAIM,)))
    shadow.run()
    b_envelope, h_envelope = shadow.ingest.posts
    assert list(b_envelope) == ["type", "ticket_id", "os_ticket_id", "pass_id", "items"]
    assert (b_envelope["ticket_id"], b_envelope["os_ticket_id"], b_envelope["pass_id"]) == (B_HS_ID, B_OS_ID, osm.T1_SIGNAL_ID)
    assert [item["action_type"] for item in b_envelope["items"]] == ["file_carrier_claim", "warehouse_investigation"]
    assert (h_envelope["ticket_id"], h_envelope["os_ticket_id"], h_envelope["pass_id"]) == (H_HS_ID, H_OS_ID, H_SIGNAL["id"])
    assert shadow.ingest.dry_runs == [False, False]
    # confirmed: the keys join the forwarder's seen-set and no retry entry is left
    assert forwarder.load_seen(shadow.seen_path) == {
        f"{B_HS_ID}:file_carrier_claim",
        f"{B_HS_ID}:warehouse_investigation",
        f"{H_HS_ID}:file_carrier_claim",
    }
    assert forwarder.load_retry(shadow.seen_path) == {}
    assert shadow.record["error_summary"] is None
    # each ticket: its write-back, then its items
    kinds = [entry for entry in shadow.timeline if entry[0] in ("writeback", "action_items")]
    assert kinds == [
        ("writeback", B_OS_ID, "completed"),
        ("action_items", B_HS_ID),
        ("writeback", H_OS_ID, "completed"),
        ("action_items", H_HS_ID),
    ]


def test_the_forwarder_after_the_run_sends_no_second_set_of_action_items(shadow: ShadowWorld, tmp_path: Path) -> None:
    """In `shadow` mode the wrapper still runs the forwarder after the pass, over
    the queue the agent wrote. For a ticket the runner already posted, it sends
    nothing more; for a ticket the runner left alone, it sends today's envelope."""
    shadow.transport.respond(SEARCH_PATH, {"results": [hit(B_HS_ID), hit(G_HS_ID)]})
    shadow.hydrator.contexts[G_HS_ID] = hubspot_context(G_HS_ID)
    shadow.agent.result = agent_result(done(B_HS_ID, "draft-B", items=(CLAIM,)), done(G_HS_ID, "draft-G", items=(CLAIM,)))
    shadow.run()
    assert len(shadow.ingest.posts) == 1  # the runner's one envelope, for B

    queue = tmp_path / "pending_actions.json"
    queue.write_text(
        json.dumps(
            [
                {"ticket_id": B_HS_ID, "action_items": [dict(CLAIM)]},
                {"ticket_id": G_HS_ID, "action_items": [dict(CLAIM)]},
            ]
        ),
        encoding="utf-8",
    )
    assert forwarder.main(queue, shadow.seen_path) == 0
    assert len(shadow.ingest.posts) == 2  # one more: G's, and none for B
    assert [post.get("ticket_id") for post in shadow.ingest.posts] == [B_HS_ID, G_HS_ID]
    assert list(shadow.ingest.posts[1]) == ["type", "ticket_id", "items"]  # today's envelope, no OS id, no pass id
    assert forwarder.main(queue, shadow.seen_path) == 0
    assert len(shadow.ingest.posts) == 2  # and nothing on a later tick either


def test_an_unconfirmed_post_is_a_code_and_the_forwarder_resends_the_same_pass(shadow: ShadowWorld, tmp_path: Path) -> None:
    shadow.agent.result = agent_result(done(B_HS_ID, "draft-B", items=(CLAIM,)), done(H_HS_ID, "draft-H"))
    shadow.ingest.script = [{"ok": True, "mode": "sent", "status": 200, "response": {"ok": True, "emitted": 0, "errors": 1}}]
    shadow.run()
    assert shadow.record["status"] == "success"  # the HubSpot path's own status
    assert shadow.record["error_summary"] == "action_items_unconfirmed=1"
    assert forwarder.load_seen(shadow.seen_path) == set()
    assert forwarder.load_retry(shadow.seen_path) == {
        f"{B_HS_ID}:file_carrier_claim": {"os_ticket_id": B_OS_ID, "pass_id": osm.T1_SIGNAL_ID}
    }
    queue = tmp_path / "pending_actions.json"
    queue.write_text(json.dumps([{"ticket_id": B_HS_ID, "action_items": [dict(CLAIM)]}]), encoding="utf-8")
    forwarder.main(queue, shadow.seen_path)
    first, resent = shadow.ingest.posts
    assert resent == first  # the same envelope: the same OS id and the same pass id, so the OS creates nothing twice
    assert forwarder.load_retry(shadow.seen_path) == {}


# ------------------------------------------------------- what is written back, per entry


def test_a_completed_entry_with_no_draft_id_is_a_failed_write_back_never_a_completed_one(shadow: ShadowWorld) -> None:
    shadow.agent.result = agent_result(done(B_HS_ID, None, items=(CLAIM,)), done(H_HS_ID, "draft-H"))
    shadow.run()
    (b_id, b_body), (h_id, h_body) = shadow.writebacks
    assert (b_id, b_body["outcome"], b_body["failure_reason_code"]) == (B_OS_ID, "failed", "draft_write_failed")
    assert "category" not in b_body
    assert (h_id, h_body["outcome"]) == (H_OS_ID, "completed")
    assert shadow.ingest.posts == []  # no draft, so no action items from the runner


@pytest.mark.parametrize("draft_id", ["", "   ", 17, {"id": "x"}])
def test_a_draft_id_that_is_not_text_is_no_draft(shadow: ShadowWorld, draft_id: Any) -> None:
    shadow.agent.result = agent_result(done(B_HS_ID, draft_id), done(H_HS_ID, "draft-H"))
    shadow.run()
    assert shadow.writebacks[0][1]["outcome"] == "failed"
    assert shadow.writebacks[0][1]["failure_reason_code"] == "draft_write_failed"


def test_a_completed_entry_whose_classification_cannot_be_used_gets_no_write_back(shadow: ShadowWorld) -> None:
    """The draft is in place, so the ticket is not reported failed; and the
    OS cannot be told a category it would refuse. A code, and the items still go."""
    unusable = done(B_HS_ID, "draft-B", items=(CLAIM,), classification={"category": "Shipping question", "reason": TEXT_MARKER})
    shadow.agent.result = agent_result(unusable, done(H_HS_ID, "draft-H"))
    shadow.run()
    assert [ticket_id for ticket_id, _body in shadow.writebacks] == [H_OS_ID]
    assert shadow.record["error_summary"] == "classification_unusable=1"
    assert len(shadow.ingest.posts) == 1


def test_a_result_entry_for_a_ticket_the_run_never_resolved_is_left_alone(shadow: ShadowWorld) -> None:
    extra = done("9300000000321", "draft-X", items=(CLAIM,))
    twice = done(B_HS_ID, "draft-B-again")
    shadow.agent.result = agent_result(done(B_HS_ID, "draft-B"), extra, twice, broke(H_HS_ID))
    shadow.run()
    assert [(ticket_id, body["outcome"]) for ticket_id, body in shadow.writebacks] == [
        (B_OS_ID, "completed"),
        (H_OS_ID, "failed"),
    ]
    assert shadow.ingest.posts == []


def test_a_candidate_the_agent_wrote_no_entry_for_is_not_reported_failed(shadow: ShadowWorld) -> None:
    """The agent applies its own dedupe to what it fetches. A candidate it chose
    to skip is not a failed pass and must not count toward parking in the OS."""
    shadow.agent.result = agent_result(done(B_HS_ID, "draft-B"))
    shadow.run()
    assert [ticket_id for ticket_id, _body in shadow.writebacks] == [B_OS_ID]


def test_an_undelivered_write_back_is_a_code_and_the_status_is_still_the_hubspot_paths(shadow: ShadowWorld) -> None:
    shadow.os_api.script = [TicketsApiError("list_timeout"), TicketsApiError("list_timeout")]
    shadow.run()
    assert shadow.sleeps == [os_pass.WRITEBACK_RETRY_PAUSE_S]  # one retry, then the next ticket goes on
    assert [(ticket_id, body["outcome"]) for ticket_id, body in shadow.writebacks] == [
        (B_OS_ID, "completed"),
        (B_OS_ID, "completed"),
        (H_OS_ID, "failed"),
    ]
    assert shadow.record["status"] == "partial" and shadow.record["drafts_created"] == 1
    assert shadow.record["error_summary"] == f"{AGENT_SUMMARY_ONE_FAILED}; writeback_list_timeout=1"


def test_a_dry_run_posts_the_observation_and_writes_nothing_back(shadow: ShadowWorld) -> None:
    shadow.settings["dry_run"] = True
    shadow.agent.result = agent_result(done(B_HS_ID, "draft-B", items=(CLAIM,)), broke(H_HS_ID))
    shadow.run()
    assert len(shadow.observations) == 1  # a measurement, not a side effect on a ticket
    assert shadow.writebacks == []
    assert shadow.ingest.posts == []
    assert not shadow.seen_path.exists() and not forwarder.retry_path(shadow.seen_path).exists()
    assert shadow.record["error_summary"] == AGENT_SUMMARY_ONE_FAILED


def test_a_run_whose_agent_left_no_result_writes_nothing_back(shadow: ShadowWorld) -> None:
    shadow.agent = ShadowAgent(shadow.timeline, 0)  # exits 0 and leaves no result file
    shadow.run()
    assert shadow.record["status"] == "failure"
    assert shadow.record["failure_reason_code"] == "agent_exited_abnormally"
    assert shadow.writebacks == [] and shadow.ingest.posts == []
    assert len(shadow.observations) == 1  # it was posted before the agent was launched


def test_an_agent_that_cannot_be_started_is_the_hubspot_paths_failure_record(shadow: ShadowWorld) -> None:
    def cannot_start(command: Any, env: Any) -> int:
        raise FileNotFoundError(f"no such command {EXC_MARKER}")

    shadow.agent = cannot_start
    shadow.run()
    assert shadow.record["status"] == "failure"
    assert shadow.record["failure_reason_code"] == "agent_launch_failed"
    assert shadow.record["error_summary"] == "agent_launch_failed:FileNotFoundError"
    assert shadow.writebacks == []
    assert len(shadow.observations) == 1


# ------------------------------------ the comparison half can never affect the run


@pytest.mark.parametrize("code", ["signature_rejected", "secret_missing"])
def test_an_os_that_refuses_the_list_leaves_a_plain_hubspot_run_and_one_code(shadow: ShadowWorld, code: str) -> None:
    shadow.os_api.fail_next_list(code, 401 if code == "signature_rejected" else None)
    shadow.run()
    record = shadow.record
    assert (record["status"], record["tickets_processed"], record["drafts_created"]) == ("partial", 1, 1)
    assert record["failure_reason_code"] is None
    assert record["error_summary"] == f"{AGENT_SUMMARY_ONE_FAILED}; source_shadow_skipped:{code}"
    assert shadow.os_api.calls == [("fetch_unprocessed", (100,))]  # the OS is asked nothing more
    assert shadow.classifier.calls == [] and shadow.hydrator.calls == []
    assert shadow.agent.contexts == [{}]  # today's plain draft writes
    assert shadow.launch_attempts == 1 and len(shadow.engine_envs) == 1


def test_a_list_that_timed_out_skips_the_comparison_and_the_passes_are_still_written_back(shadow: ShadowWorld) -> None:
    """No snapshot, so no observation. The OS answers again a moment later, so
    the HubSpot path's passes are still reported: their tickets leave the OS's
    list instead of turning up as differences the code cannot explain."""
    shadow.os_api.fail_next_list("list_timeout")
    shadow.run()
    record = shadow.record
    assert (record["status"], record["tickets_processed"], record["drafts_created"]) == ("partial", 1, 1)
    assert record["error_summary"] == f"{AGENT_SUMMARY_ONE_FAILED}; source_shadow_skipped:list_timeout"
    assert shadow.observations == [] and shadow.classifier.calls == []
    assert shadow.agent.contexts == [{B_HS_ID: B_ENTRY, H_HS_ID: H_ENTRY}]
    assert [ticket_id for ticket_id, _body in shadow.writebacks] == [B_OS_ID, H_OS_ID]


def test_an_os_that_is_down_costs_one_more_refused_call_and_the_run_is_a_plain_hubspot_run(shadow: ShadowWorld) -> None:
    shadow.os_api.fail_next_list("list_timeout")
    shadow.os_api.fail_next("fetch_ticket", "list_timeout")
    shadow.run()
    assert [name for name, _args in shadow.os_api.calls] == ["fetch_unprocessed", "fetch_ticket"]
    assert shadow.agent.contexts == [{}] and shadow.writebacks == []
    assert shadow.record["error_summary"] == (
        f"{AGENT_SUMMARY_ONE_FAILED}; source_shadow_skipped:list_timeout; source_shadow_resolve_failed:list_timeout=1"
    )
    assert (shadow.record["status"], shadow.record["drafts_created"]) == ("partial", 1)


@pytest.mark.parametrize(
    "answer, detail",
    [
        ("not an object", "list_not_an_object"),
        ({"contract_version": "1", "tickets": "none"}, "tickets_not_a_list"),
        ({"contract_version": "2", "tickets": []}, "contract_version"),
        ({"contract_version": "1", "tickets": [{"subject": "no id"}]}, "ticket_without_an_id"),
    ],
)
def test_a_list_the_contract_does_not_describe_skips_the_comparison(shadow: ShadowWorld, answer: Any, detail: str) -> None:
    shadow.os_api.unprocessed = answer
    shadow.run()
    assert shadow.observations == []
    assert shadow.record["error_summary"] == f"{AGENT_SUMMARY_ONE_FAILED}; source_shadow_skipped:list_error:{detail}"
    assert shadow.launch_attempts == 1


def test_a_list_fetch_that_raises_something_else_is_its_class_name_only(
    shadow: ShadowWorld, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    def explode(limit: int) -> dict:
        raise RuntimeError(f"boom {EXC_MARKER}")

    shadow.os_api.fetch_unprocessed = explode
    shadow.run()
    assert shadow.record["error_summary"] == f"{AGENT_SUMMARY_ONE_FAILED}; source_shadow_skipped:list_error:RuntimeError"
    assert EXC_MARKER not in json.dumps(shadow.record)
    fakes.assert_marker_absent(EXC_MARKER, caplog, capfd)


@pytest.mark.parametrize(
    "verdict, code",
    [
        ({"status": 400, "error": "bad_payload", "replayed": None}, "source_shadow_observation_refused:bad_payload"),
        ({"status": 413, "error": "payload_too_large", "replayed": None}, "source_shadow_observation_refused:payload_too_large"),
        ({"status": 418, "error": f"text {TEXT_MARKER}", "replayed": None}, "source_shadow_observation_refused:418"),
        ("not a verdict", "source_shadow_observation_refused:unknown"),
    ],
)
def test_a_refused_observation_is_a_code_and_the_hubspot_path_is_unchanged(shadow: ShadowWorld, verdict: Any, code: str) -> None:
    shadow.os_api.source_shadow_result = verdict
    shadow.run()
    record = shadow.record
    assert (record["status"], record["tickets_processed"], record["drafts_created"]) == ("partial", 1, 1)
    assert record["error_summary"] == f"{AGENT_SUMMARY_ONE_FAILED}; {code}"
    assert TEXT_MARKER not in json.dumps(record)
    assert len(shadow.writebacks) == 2 and shadow.launch_attempts == 1


@pytest.mark.parametrize("code", ["list_timeout", "list_error", "signature_rejected"])
def test_an_observation_that_could_not_be_posted_is_a_code_and_the_hubspot_path_is_unchanged(
    shadow: ShadowWorld, code: str
) -> None:
    shadow.os_api.fail_next("post_source_shadow_observations", code)
    shadow.run()
    record = shadow.record
    assert (record["status"], record["tickets_processed"], record["drafts_created"]) == ("partial", 1, 1)
    assert record["error_summary"] == f"{AGENT_SUMMARY_ONE_FAILED}; source_shadow_observation_failed:{code}"
    assert len(shadow.writebacks) == 2


def test_a_replayed_observation_is_not_a_code(shadow: ShadowWorld) -> None:
    shadow.os_api.source_shadow_result = {"status": 200, "error": None, "replayed": True}
    shadow.run()
    assert shadow.record["error_summary"] == AGENT_SUMMARY_ONE_FAILED


def test_a_classifier_that_is_down_posts_no_observation_and_the_hubspot_path_is_unchanged(shadow: ShadowWorld) -> None:
    """The OS keeps the first category it is told for a selection, so a run of
    nulls would never be corrected. Such a run reports nothing."""
    shadow.classifier.failing = {"*"}
    shadow.run()
    record = shadow.record
    assert shadow.observations == []
    assert len(shadow.classifier.calls) == 3  # then the step was stopped
    assert (record["status"], record["tickets_processed"], record["drafts_created"]) == ("partial", 1, 1)
    assert record["error_summary"] == (
        f"{AGENT_SUMMARY_ONE_FAILED}; source_shadow_classify_failed=3; source_shadow_skipped:classify_unavailable"
    )
    assert len(shadow.writebacks) == 2  # the HubSpot path's passes are still reported
    assert not shadow.cache_path.exists()  # nothing was classified, so nothing was cached


def test_one_failed_classify_call_among_others_is_a_null_category_in_a_posted_observation(shadow: ShadowWorld) -> None:
    shadow.classifier.failing = {O_OS_ID}
    shadow.run()
    (body,) = shadow.observations
    assert body["os"]["selected"][1] == {
        "os_ticket_id": O_OS_ID, "hubspot_ticket_id": None, "signal": signal_of(1), "category": None,
    }
    assert shadow.record["error_summary"] == f"{AGENT_SUMMARY_ONE_FAILED}; source_shadow_classify_failed=1"


def test_an_exception_anywhere_in_the_comparison_is_a_code_and_never_its_message(
    shadow: ShadowWorld, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    def explode(*_args: Any, **_kwargs: Any) -> dict:
        raise RuntimeError(f"builder {EXC_MARKER}")

    monkeypatch.setattr(source_shadow, "build_observation", explode)
    shadow.run()
    record = shadow.record
    assert shadow.observations == []
    assert (record["status"], record["tickets_processed"], record["drafts_created"]) == ("partial", 1, 1)
    assert record["error_summary"] == f"{AGENT_SUMMARY_ONE_FAILED}; source_shadow_comparison_failed:RuntimeError"
    assert len(shadow.writebacks) == 2
    fakes.assert_marker_absent(EXC_MARKER, caplog, capfd)


def test_an_observation_the_builder_refuses_is_never_posted(shadow: ShadowWorld) -> None:
    shadow.settings["active_stages"] = ["1", "New ticket"]  # a display name is not a stage token
    shadow.run()
    assert shadow.observations == []
    assert shadow.record["error_summary"] == f"{AGENT_SUMMARY_ONE_FAILED}; source_shadow_comparison_failed:ValueError"
    assert shadow.launch_attempts == 1


def test_an_os_origin_that_cannot_be_read_skips_the_comparison(shadow: ShadowWorld) -> None:
    deps = shadow.deps()

    def unreadable() -> str:
        raise TicketsApiError("list_error")

    deps.read_os_origin = unreadable
    rt.run(deps)
    assert shadow.observations == []
    assert shadow.record["error_summary"] == f"{AGENT_SUMMARY_ONE_FAILED}; source_shadow_comparison_failed:TicketsApiError"
    assert len(shadow.writebacks) == 2


def test_a_finaliser_that_raises_is_a_code_and_the_agents_outcome_stands(
    shadow: ShadowWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> dict:
        raise RuntimeError(f"write-back {EXC_MARKER}")

    monkeypatch.setattr(os_pass, "post_ticket_writeback", explode)
    shadow.run()
    record = shadow.record
    assert (record["status"], record["tickets_processed"], record["drafts_created"]) == ("partial", 1, 1)
    assert record["error_summary"] == f"{AGENT_SUMMARY_ONE_FAILED}; source_shadow_finalise_ticket_error=2"
    assert EXC_MARKER not in json.dumps(record)


# -------------------------------------------------- the HubSpot side, when it is not known


def _no_token(world: ShadowWorld) -> None:
    world.token = None


def _settings_unreadable(world: ShadowWorld) -> None:
    world.settings = RuntimeError(f"yaml {EXC_MARKER}")


def _no_active_stages(world: ShadowWorld) -> None:
    world.settings["active_stages"] = []


def _search_failed(world: ShadowWorld) -> None:
    world.transport.respond(SEARCH_PATH, None)


def _pending_rerun(world: ShadowWorld) -> None:
    world.client.reruns = [{"id": RERUN_ID, "ticket_id": B_HS_ID}]


@pytest.mark.parametrize(
    "arrange, reason",
    [
        (_no_token, "token_missing"),
        (_settings_unreadable, "settings_unreadable"),
        (_no_active_stages, "active_stages_empty"),
        (_search_failed, "search_failed"),
        (_pending_rerun, "pending_reruns"),
    ],
)
def test_a_pregate_that_failed_open_launches_the_agent_and_reports_no_observation(
    shadow: ShadowWorld, arrange: Callable[[ShadowWorld], None], reason: str
) -> None:
    """Code does not know what the agent will select, so there is nothing to
    compare and nothing to write back: the run is a plain hubspot-mode run."""
    arrange(shadow)
    shadow.run()
    record = shadow.record
    assert shadow.launch_attempts == 1  # fail-open, exactly as hubspot mode
    assert shadow.observations == [] and shadow.classifier.calls == []
    assert shadow.os_api.calls_to("fetch_ticket") == [] and shadow.writebacks == []
    assert record["error_summary"] == f"{AGENT_SUMMARY_ONE_FAILED}; source_shadow_skipped:pregate_{reason}"
    assert (record["status"], record["drafts_created"]) == ("partial", 1)
    assert EXC_MARKER not in json.dumps(record)


def test_a_full_deduped_page_is_compared_with_nothing_selected(shadow: ShadowWorld) -> None:
    """The pre-gate launches the agent on a full page of deduped hits (fail-open),
    and what it knows is still a fact worth reporting: every hit, deduped."""
    shadow.settings["per_run_cap"] = 2
    state = {
        "last_run_at": "2026-09-01T00:00:00Z",
        "ticket_fingerprints": {
            ticket_id: {"processed_at": "2026-10-09T00:00:00Z", "hs_lastmodifieddate": "2026-10-08T10:00:00.500Z", "num_notes": 1}
            for ticket_id in (B_HS_ID, H_HS_ID)
        },
    }
    shadow.state_path.write_text(json.dumps(state), encoding="utf-8")
    shadow.agent.result = agent_result()
    shadow.run()
    (body,) = shadow.observations
    assert body["hubspot"] == {
        "page_full": True,
        "matched": [
            {"hubspot_ticket_id": B_HS_ID, "stage": None, "deduped": True},
            {"hubspot_ticket_id": H_HS_ID, "stage": None, "deduped": True},
        ],
        "selected": [],
    }
    assert [row["os_ticket_id"] for row in body["os"]["selected"]] == [B_OS_ID, O_OS_ID]
    assert shadow.launch_attempts == 1 and shadow.writebacks == []


def test_no_hubspot_candidate_and_an_os_list_is_an_observation_and_a_zero_ticket_success(shadow: ShadowWorld) -> None:
    shadow.transport.respond(SEARCH_PATH, {"results": []})
    shadow.run()
    record = shadow.record
    (body,) = shadow.observations
    assert body["hubspot"] == {"page_full": False, "matched": [], "selected": []}
    assert [row["os_ticket_id"] for row in body["os"]["selected"]] == [B_OS_ID, O_OS_ID]  # OS-only, for this run
    assert (record["status"], record["tickets_processed"], record["drafts_created"]) == ("success", 0, 0)
    assert record["error_summary"] is None and record["source_mode"] == "shadow"
    assert shadow.launch_attempts == 0 and shadow.engine_envs == []
    assert shadow.writebacks == [] and shadow.os_api.calls_to("fetch_ticket") == []
    # B is HubSpot-linked and only the OS selected it: its thread costs one fetch (two requests)
    assert [method for method, _path in shadow.hubspot.requests] == ["GET", "POST"]
    assert shadow.hubspot.unexpected == []
    assert shadow.token_reads == 2  # the pre-gate's, and one for the comparison's thread fetch


def test_nothing_on_either_side_is_still_one_observation(shadow: ShadowWorld) -> None:
    """A run that compared and found nothing is a run of that business day."""
    shadow.transport.respond(SEARCH_PATH, {"results": []})
    shadow.os_api.unprocessed = os_listing()
    shadow.run()
    (body,) = shadow.observations
    assert body["hubspot"]["selected"] == [] and body["os"]["selected"] == []
    assert shadow.classifier.calls == [] and shadow.record["status"] == "success"
    assert not shadow.cache_path.exists()


def test_a_paused_routine_is_one_skipped_record_and_asks_nothing(shadow: ShadowWorld) -> None:
    shadow.client.enabled = False
    shadow.run()
    assert shadow.record["status"] == "skipped" and shadow.record["error_summary"] == "routine_paused"
    assert shadow.record["source_mode"] == "shadow"
    assert shadow.timeline == [] and shadow.os_api.calls == []
    assert shadow.launch_attempts == 0 and shadow.token_reads == 0


# ----------------------------------------------- the HubSpot path is hubspot mode's


def test_with_an_os_that_knows_nothing_the_run_is_hubspot_modes_run(make_shadow_world: Callable[..., ShadowWorld]) -> None:
    """The proof that `shadow` adds to the HubSpot path and changes none of it:
    with the same search hits, state and agent, the agent is launched with the
    same command, the same environment (but for the run's own four variables)
    and the same run context, and the record has the same status and counts."""
    plain = make_shadow_world("plain", mode="hubspot")
    plain.run()
    plain_command, plain_env = plain.agent.calls[0]
    plain_context, plain_record = plain.agent.contexts[0], plain.record
    assert plain.os_api.calls == []  # hubspot mode asks the OS nothing

    world = make_shadow_world("compared", mode="shadow")
    world.os_api.tickets = {}  # the mirror has seen neither candidate
    world.os_api.unprocessed = os_listing()
    world.run()
    command, env = world.agent.calls[0]

    assert command == plain_command == rt.agent_command()  # no tool restriction, the pinned model
    own = set(run_types.RUN_ENV_NAMES)
    assert {k: v for k, v in env.items() if k not in own} == {k: v for k, v in plain_env.items() if k not in own}
    assert env[run_types.SOURCE_MODE_ENV] == "shadow" and plain_env[run_types.SOURCE_MODE_ENV] == "hubspot"
    assert world.agent.contexts[0] == plain_context == {}
    for key in ("status", "tickets_processed", "drafts_created", "error_summary", "failure_reason_code", "hubspot_calls"):
        assert world.record[key] == plain_record[key], key
    assert len(world.engine_envs) == len(plain.engine_envs) == 1
    assert world.writebacks == [] and world.ingest.posts == [] and world.draft_writes == []
    assert world.transport.bodies == plain.transport.bodies  # the same one search, byte for byte


def test_the_engine_shadow_step_failing_never_stops_the_run(shadow: ShadowWorld) -> None:
    shadow.engine_error = RuntimeError(f"engine {EXC_MARKER}")
    shadow.run()
    assert shadow.launch_attempts == 1 and len(shadow.observations) == 1
    assert shadow.record["error_summary"] == AGENT_SUMMARY_ONE_FAILED


def test_an_engine_artifact_of_this_run_saves_the_hubspot_sides_calls(shadow: ShadowWorld) -> None:
    shadow.engine_artifacts = {B_HS_ID: artifact("billing_invoice")}
    shadow.run()
    assert shadow.hydrator.calls == [H_HS_ID]  # B was not hydrated again
    assert shadow.classifier.ticket_ids() == [H_HS_ID, B_HS_ID, O_OS_ID]  # H; then the OS side's B and O
    (body,) = shadow.observations
    assert body["hubspot"]["selected"][0]["category"] == "billing_invoice"
    assert body["os"]["selected"][0]["category"] == "wismo_tracking"  # a disagreement, reported as it is
    written = sorted(p.name for p in (shadow.root / "outputs" / "shadow").rglob("*") if p.is_file())
    assert written == [f"{B_HS_ID}.json"]  # only what the engine shadow itself wrote


def test_the_runner_writes_nothing_under_the_engine_shadows_directory(shadow: ShadowWorld) -> None:
    shadow.run()
    assert not (shadow.root / "outputs" / "shadow").exists()
    assert sorted(p.name for p in (shadow.root / "outputs" / "runs").iterdir()) == ["os", "source_shadow_seen.json"]
    assert list((shadow.root / "outputs" / "runs" / "os").iterdir()) == []  # the scratch directory is gone


# ------------------------------------------------------------------------ the cache


def test_the_cache_is_kept_between_runs_so_a_ticket_that_stays_os_only_costs_one_call(shadow: ShadowWorld) -> None:
    shadow.run()
    stored = json.loads(shadow.cache_path.read_text(encoding="utf-8"))
    assert stored == {
        f"{B_OS_ID}:{osm.T1_SIGNAL_ID}": "wismo_tracking",
        f"{O_OS_ID}:{osm.T2_SIGNAL_ID}": "wismo_tracking",
    }
    first_calls = shadow.classifier.ticket_ids()
    assert first_calls == [B_HS_ID, H_HS_ID, B_HS_ID, O_OS_ID]

    shadow.run()  # the next tick: the OS still lists B and O
    assert shadow.classifier.ticket_ids()[len(first_calls):] == [B_HS_ID, H_HS_ID]  # the HubSpot side only
    second = shadow.observations[1]
    assert [row["category"] for row in second["os"]["selected"]] == ["wismo_tracking", "wismo_tracking"]
    assert second["run_id"] != shadow.observations[0]["run_id"]
    assert len(shadow.client.records) == 2


def test_a_cache_that_cannot_be_saved_is_a_code_and_the_observation_is_still_posted(
    shadow: ShadowWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(path: Any, cache: Any) -> None:
        raise PermissionError(f"disk {EXC_MARKER}")

    monkeypatch.setattr(source_shadow, "save_seen_cache", refuse)
    shadow.run()
    assert len(shadow.observations) == 1
    assert shadow.record["error_summary"] == f"{AGENT_SUMMARY_ONE_FAILED}; source_shadow_cache_unwritable"


# ------------------------------------------------------------- no text, end to end


def test_a_whole_run_puts_no_ticket_text_in_the_record_the_cache_or_a_log(
    shadow: ShadowWorld, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    shadow.os_api.unprocessed = {"contract_version": "1", "generated_at": "x", "tickets": [marked(0), marked(1)]}
    shadow.agent.result = agent_result(done(B_HS_ID, "draft-B", items=(CLAIM,)), broke(H_HS_ID))
    shadow.run()
    assert TEXT_MARKER in json.dumps(shadow.ingest.posts)  # the marker was in what the run handled
    assert TEXT_MARKER not in json.dumps(shadow.record)
    assert TEXT_MARKER not in json.dumps(shadow.observations)
    assert TEXT_MARKER not in shadow.cache_path.read_text(encoding="utf-8")
    assert TEXT_MARKER not in shadow.seen_path.read_text(encoding="utf-8")  # the seen-set holds keys only
    fakes.assert_marker_absent(TEXT_MARKER, caplog, capfd)


# ------------------------------------------------------------ the real collaborators


def test_the_real_classifier_is_the_engines_own_classify_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-05: `llm.classify`, the small model, with the real categories config."""
    seen: list = []

    def spy(ticket_context: dict, settings: dict, categories_cfg: dict, usages: list) -> dict:
        seen.append((ticket_context, settings, categories_cfg, usages))
        return {"category": "wismo_tracking", "reason": TEXT_MARKER}

    monkeypatch.setattr(llm, "classify", spy)
    classify = REAL_BUILD_CLASSIFY(SETTINGS)
    context = hubspot_context(B_HS_ID)
    other = hubspot_context(H_HS_ID)
    assert classify(context)["category"] == "wismo_tracking"
    classify(other)
    assert seen[0][0] is context and seen[1][0] is other
    assert seen[0][1] is SETTINGS
    assert "form_topic_mapping" in seen[0][2] and seen[0][2] is seen[1][2]  # the real file, read once
    assert seen[0][3] == [] and isinstance(seen[0][3], list)
    assert llm.CLASSIFY_MODEL != llm.DRAFT_MODEL  # the classify step's own, smaller model


def test_the_real_hubspot_hydration_is_one_single_ticket_search_and_hydrations_own_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = fakes.FakeHubSpotTransport({SEARCH_PATH: {"results": [hit(B_HS_ID)]}})
    monkeypatch.setattr(hubspot_api, "_request", transport)
    hydrated: list = []

    def spy(ticket: dict, settings: dict, token: str) -> dict:
        hydrated.append((ticket, settings, token))
        return hubspot_context(B_HS_ID)

    monkeypatch.setattr(hydrate, "hydrate_ticket", spy)
    settings = dict(SETTINGS, ticket_custom_properties=["order_number"])
    reads: list = []

    def read_token() -> str:
        reads.append(1)
        return "test-token"

    hydrate_one = REAL_BUILD_HUBSPOT_HYDRATE(settings, read_token)
    assert reads == []  # no token is read until a ticket needs hydrating
    assert hydrate_one(B_HS_ID) == hubspot_context(B_HS_ID)
    assert transport.calls == [("POST", SEARCH_PATH)]
    (body,) = transport.bodies
    assert body["filterGroups"] == [
        {"filters": [{"propertyName": "hs_object_id", "operator": "EQ", "value": B_HS_ID}]}
    ]
    assert body["limit"] == 1
    assert body["properties"] == hubspot_api.TICKET_BASE_PROPERTIES + ["order_number"]
    assert hydrated == [(hit(B_HS_ID), settings, "test-token")]
    assert hubspot_api.snapshot_calls()["kinds"]["search"] == 1


def test_the_real_hubspot_hydration_raises_when_there_is_no_token_or_no_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = fakes.FakeHubSpotTransport({SEARCH_PATH: {"results": []}})
    monkeypatch.setattr(hubspot_api, "_request", transport)
    with pytest.raises(LookupError):
        REAL_BUILD_HUBSPOT_HYDRATE(SETTINGS, lambda: None)(B_HS_ID)
    assert transport.calls == []  # nothing is asked of HubSpot without a token
    with pytest.raises(LookupError):
        REAL_BUILD_HUBSPOT_HYDRATE(SETTINGS, lambda: "test-token")(B_HS_ID)
    with pytest.raises(LookupError):
        REAL_BUILD_HUBSPOT_HYDRATE(SETTINGS, lambda: "test-token")("../not-digits")
    assert len(transport.calls) == 1


def test_the_real_thread_fetch_is_the_os_modes_own(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list = []

    def spy(hubspot_ticket_id: Any, settings: dict, token: Any) -> list:
        seen.append((hubspot_ticket_id, settings, token))
        return list(osm.T3_EMAILS)

    monkeypatch.setattr(os_bundle, "fetch_thread", spy)
    fetch = REAL_BUILD_THREAD_FETCH(SETTINGS, lambda: "test-token")
    assert fetch(L_HS_ID) == osm.T3_EMAILS
    assert seen == [(L_HS_ID, SETTINGS, "test-token")]


# ------------------------------------------------------------------ the rollback state


def test_the_runner_never_writes_the_hubspot_paths_state_file() -> None:
    """Plan 93-23: in `shadow` mode the AGENT keeps that file. A code write for
    a ticket the agent processed would replace its fingerprint with a
    null-count one, so this module holds no call of the state writer at all."""
    module = _module_tree()
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
    }
    assert "merge_" + "fingerprints" not in called and "fingerprint_entry" not in called
    attributes = {node.attr for node in ast.walk(module) if isinstance(node, ast.Attribute)}
    assert "state_path" not in attributes  # the path is never even named
    # and the os finaliser still holds exactly its one call (two tests of plan 93-23 pin it)
    os_pass_source = (PACKAGE_ROOT / "ticket_pipeline" / "os_pass.py").read_text(encoding="utf-8")
    assert os_pass_source.count("merge_" + "fingerprints(") == 1
