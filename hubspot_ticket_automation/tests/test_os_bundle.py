"""An OS ticket payload becomes the ticket context HubSpot hydration builds
(Phase 93, plan 22).

`ticket_pipeline/os_bundle.py` is the one module that reshapes an OS payload
(tests/fixtures/tickets-api-list.v1.json) into the `ticket_context` the
classifier and the drafter already read. Three things are pinned here:

  the shape     the OS-built context has the keys, the key order and the nesting
                of the one `hydrate.hydrate_ticket` builds from HubSpot
  the values    every row of the plan's mapping table, asserted by value for
                fixture ticket 1 (HubSpot-linked) and ticket 2 (no HubSpot id)
  the calls     a linked ticket costs at most one thread fetch: two requests,
                ONE `email_thread`; a ticket with no HubSpot id costs nothing

The second half pins `hydrate_ticket` itself: it returns and sends exactly what
it did before this plan (literals written and run green against the untouched
function), and it now records one logical call per lookup, each request counted
under exactly one kind.

No network (the suite guard), no database, no model call, no ShipSidekick
lookup: an autouse fixture refuses the lookup and every child process.
"""
from __future__ import annotations

import ast
import builtins
import copy
import inspect
import io
import json
import logging
import os
import re
import subprocess
import time
import tokenize
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.helpers import fakes
from ticket_pipeline import hubspot_api, hydrate, os_bundle

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BASE = "https://api.hubapi.com"
TOKEN = "test-token"
OS_ORIGIN = "https://os.example.test"
PORTAL_ID = 50830922
MARKER = "ZZ-MARKER-93-22"

HS_ID = "9300000000001"  # fixture ticket 1's HubSpot id
RELATED_HS_ID = "9300000000009"
T1_OS_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
T2_OS_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
T3_OS_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
RELATED_OS_ID = "9b2e4c1a-5d3f-4a7b-8c6d-1e2f3a4b5c6d"

SETTINGS = {
    "hubspot_portal_id": PORTAL_ID,
    "conversation_email_limit": 10,
    "active_stages": ["1", "3"],
    "ticket_custom_properties": [
        "topic_of_ticket",
        "company_name",
        "order_number",
        "tracking_number",
        "inquiry_description",
        "supporting_information",
        "hs_all_associated_contact_emails",
        "hs_last_message_from_visitor",
    ],
    "cross_ticket_lookup": {
        "by_order_number": {
            "enabled": True,
            "lookback_days": 30,
            "max_results": 3,
            "include_closed_stages": True,
        }
    },
}

EIGHT_KINDS = (
    "search",
    "ticket_read",
    "contact_read",
    "company_read",
    "related_lookup",
    "rerun_read",
    "email_thread",
    "number_backfill",
)

# The context keys `hydrate_ticket` builds, in its order (`ssk_state` aside: it is
# optional there too).
CONTEXT_KEYS = [
    "ticket_id",
    "ticket_link",
    "subject",
    "topic_of_ticket",
    "source_type",
    "form_fields",
    "has_attachments",
    "stage",
    "priority_from_hubspot",
    "createdate",
    "hs_lastmodifieddate",
    "contact",
    "company",
    "thread",
    "latest_customer_message",
    "related_tickets",
]

# ------------------------------------------------------------------ HubSpot fixtures

FORM_BLOCK_HTML = (
    "<blockquote><p>Topic of Ticket: Shipping question</p>"
    "<p>Order Number: FX-ORDER-0000</p>"
    "<p>Inquiry Description: It was due on Tuesday.</p></blockquote>"
)
FORM_BLOCK_TEXT = (
    "Topic of Ticket: Shipping question\n"
    "Order Number: FX-ORDER-0000\n"
    "Inquiry Description: It was due on Tuesday."
)


def _email(email_id: str, text: str, html, direction: str, sender: str, created: str) -> dict:
    return {
        "id": email_id,
        "properties": {
            "hs_email_subject": "Where is my parcel",
            "hs_email_text": text,
            "hs_email_html": html,
            "hs_email_direction": direction,
            "hs_email_from_email": sender,
            "hs_email_to_email": "care@example.test",
            "hs_createdate": created,
        },
    }


EMAIL_OPENING = _email(
    "501", "", FORM_BLOCK_HTML, "INCOMING_EMAIL", "pat@example.test", "2026-10-01T09:00:00.000Z"
)
EMAIL_AGENT = _email(
    "502",
    "We are checking with the carrier.",
    None,
    "EMAIL",
    "care@example.test",
    "2026-10-02T14:30:00.250Z",
)
EMAIL_FOLLOW_UP = _email(
    "503", "Any news?", None, "INCOMING_EMAIL", "pat@example.test", "2026-10-08T10:00:00.123Z"
)
EMAILS_OLDEST_FIRST = [EMAIL_OPENING, EMAIL_AGENT, EMAIL_FOLLOW_UP]

# What `hydrate.build_thread` makes of those three emails.
HUBSPOT_THREAD = [
    {
        "from": "customer",
        "email": "pat@example.test",
        "date": "2026-10-01T09:00:00.000Z",
        "body_text": FORM_BLOCK_TEXT,
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
        "body_text": "Any news?",
    },
]

# A HubSpot search hit for the same ticket, as `hydrate_ticket` receives it.
HS_TICKET = {
    "id": HS_ID,
    "properties": {
        "subject": "Where is my parcel",
        "content": "It was due on Tuesday.",
        "hs_pipeline_stage": "1",
        "hs_ticket_priority": "HIGH",
        "hs_lastmodifieddate": "2026-10-08T10:00:00.500Z",
        "createdate": "2026-10-01T09:00:00.000Z",
        "source_type": "FORM",
        "hs_last_message_from_visitor": "true",
        "topic_of_ticket": "Shipping question",
        "company_name": "Fixture Merchant Ltd",
        "order_number": "FX-ORDER-1001",
        "tracking_number": "FX-TRACK-1001",
        "hs_all_associated_contact_emails": "pat@example.test",
    },
}
HS_CONTACT = {
    "id": "701",
    "properties": {
        "firstname": "Pat",
        "lastname": "Example",
        "email": "pat@example.test",
        "phone": "555-0100",
    },
}
HS_COMPANY = {"id": "801", "properties": {"name": "Fixture Merchant Ltd", "domain": "example.test"}}
HS_RELATED_HIT = {
    "id": RELATED_HS_ID,
    "properties": {
        "subject": "Earlier question about the same order",
        "hs_pipeline_stage": "4",
        "createdate": "2026-09-20T08:15:00.000Z",
        "hs_lastmodifieddate": "2026-09-21T00:00:00.000Z",
        "topic_of_ticket": "Shipping question",
    },
}
HS_RELATED_EMAIL = _email(
    "601",
    "The parcel had not moved for four days.",
    None,
    "INCOMING_EMAIL",
    "pat@example.test",
    "2026-09-20T08:15:00.000Z",
)

FIXED_NOW_S = 1_790_000_000.0
SINCE_MS = 1_787_408_000_000  # FIXED_NOW_S in ms, less 30 days

# PINNED: the nine requests one hydration sends today, in order.
PINNED_HYDRATION_REQUESTS = [
    ("GET", f"{BASE}/crm/v4/objects/tickets/{HS_ID}/associations/emails?limit=10"),
    ("POST", f"{BASE}/crm/v3/objects/emails/batch/read"),
    ("GET", f"{BASE}/crm/v4/objects/tickets/{HS_ID}/associations/contacts?limit=1"),
    ("POST", f"{BASE}/crm/v3/objects/contacts/batch/read"),
    ("GET", f"{BASE}/crm/v4/objects/tickets/{HS_ID}/associations/companies?limit=1"),
    ("POST", f"{BASE}/crm/v3/objects/companies/batch/read"),
    ("POST", f"{BASE}/crm/v3/objects/tickets/search"),
    ("GET", f"{BASE}/crm/v4/objects/tickets/{RELATED_HS_ID}/associations/emails?limit=1"),
    ("POST", f"{BASE}/crm/v3/objects/emails/batch/read"),
]

# PINNED: the related-ticket search body it sends today.
PINNED_RELATED_SEARCH_BODY = {
    "filterGroups": [
        {
            "filters": [
                {"propertyName": "order_number", "operator": "EQ", "value": "FX-ORDER-1001"},
                {"propertyName": "hs_lastmodifieddate", "operator": "GT", "value": SINCE_MS},
                {"propertyName": "hs_object_id", "operator": "NEQ", "value": HS_ID},
            ]
        }
    ],
    "properties": [
        "subject",
        "hs_pipeline_stage",
        "createdate",
        "hs_lastmodifieddate",
        "topic_of_ticket",
    ],
    "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "DESCENDING"}],
    "limit": 3,
}

# PINNED: the context that hydration returns today.
PINNED_HYDRATED_CONTEXT = {
    "ticket_id": HS_ID,
    "ticket_link": f"https://app.hubspot.com/contacts/{PORTAL_ID}/ticket/{HS_ID}",
    "subject": "Where is my parcel",
    "topic_of_ticket": "Shipping question",
    "source_type": "FORM",
    "form_fields": {
        "topic_of_ticket": "Shipping question",
        "order_number": "FX-ORDER-1001",
        "inquiry_description": "It was due on Tuesday.",
        "company_name": "Fixture Merchant Ltd",
        "tracking_number": "FX-TRACK-1001",
        "hs_all_associated_contact_emails": "pat@example.test",
        "hs_last_message_from_visitor": "true",
    },
    "has_attachments": False,
    "stage": "1",
    "priority_from_hubspot": "HIGH",
    "createdate": "2026-10-01T09:00:00.000Z",
    "hs_lastmodifieddate": "2026-10-08T10:00:00.500Z",
    "contact": {"name": "Pat Example", "email": "pat@example.test", "phone": "555-0100"},
    "company": {"name": "Fixture Merchant Ltd", "domain": "example.test"},
    "thread": HUBSPOT_THREAD,
    "latest_customer_message": "Any news?",
    "related_tickets": [
        {
            "ticket_id": RELATED_HS_ID,
            "ticket_link": f"https://app.hubspot.com/contacts/{PORTAL_ID}/ticket/{RELATED_HS_ID}",
            "subject": "Earlier question about the same order",
            "stage": "4",
            "createdate": "2026-09-20T08:15:00.000Z",
            "hs_lastmodifieddate": "2026-09-21T00:00:00.000Z",
            "topic_of_ticket": "Shipping question",
            "snippet": "The parcel had not moved for four days.",
        }
    ],
}

# ------------------------------------------------------------------ the harness


def _assoc(*ids: str) -> dict:
    return {"results": [{"toObjectId": object_id} for object_id in ids]}


def _batch(*objects: dict) -> dict:
    return {"results": [copy.deepcopy(item) for item in objects]}


def _zero(**overrides: int) -> dict:
    kinds = {kind: 0 for kind in EIGHT_KINDS}
    raw = overrides.pop("raw_requests", 0)
    kinds.update(overrides)
    return {"kinds": kinds, "raw_requests": raw}


class _Response:
    def __init__(self, payload) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def read(self) -> bytes:
        return self._raw


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(BASE, code, "error", hdrs=None, fp=io.BytesIO(b""))


class QueueOpener:
    """Stands in for urlopen under the real request function: `opener(request, timeout=...)`.

    Answers from a queue, in order. An item is a JSON-able payload (a 200) or an
    exception to raise. A request with nothing queued fails the test, so a test
    states every request it allows.
    """

    def __init__(self, *answers) -> None:
        self._answers = list(answers)
        self.requests: list[urllib.request.Request] = []
        self.on_request = None

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if self.on_request is not None:
            self.on_request(request)
        if not self._answers:
            raise AssertionError(
                f"unexpected HubSpot request: {request.get_method()} {request.full_url}"
            )
        answer = self._answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return _Response(answer)

    def sent(self) -> list[tuple[str, str]]:
        return [(request.get_method(), request.full_url) for request in self.requests]

    def body(self, index: int):
        data = self.requests[index].data
        return None if data is None else json.loads(data.decode("utf-8"))

    @property
    def unanswered(self) -> int:
        return len(self._answers)


def _open_with(monkeypatch: pytest.MonkeyPatch, *answers) -> QueueOpener:
    opener = QueueOpener(*answers)
    monkeypatch.setattr(hubspot_api, "_default_opener", opener)
    return opener


def _thread_answers() -> list:
    """The two answers of one thread fetch; the association order is not the date order."""
    return [_assoc("503", "501", "502"), _batch(EMAIL_FOLLOW_UP, EMAIL_OPENING, EMAIL_AGENT)]


def _hydration_answers() -> list:
    """The nine answers of one full hydration, in the order the requests are sent."""
    return [
        *_thread_answers(),
        _assoc("701"),
        _batch(HS_CONTACT),
        _assoc("801"),
        _batch(HS_COMPANY),
        {"results": [copy.deepcopy(HS_RELATED_HIT)]},
        _assoc("601"),
        _batch(HS_RELATED_EMAIL),
    ]


def _hydrate(monkeypatch: pytest.MonkeyPatch, ticket=None, settings=None, answers=None):
    """Run the real `hydrate_ticket` over queued answers, with the clock fixed."""
    opener = _open_with(monkeypatch, *(_hydration_answers() if answers is None else answers))
    with monkeypatch.context() as patch:
        patch.setattr(time, "time", lambda: FIXED_NOW_S)
        context = hydrate.hydrate_ticket(
            copy.deepcopy(HS_TICKET if ticket is None else ticket),
            copy.deepcopy(SETTINGS if settings is None else settings),
            TOKEN,
        )
    return context, opener


def _tickets() -> list[dict]:
    text = (FIXTURES / "tickets-api-list.v1.json").read_bytes().decode("utf-8")
    return json.loads(text)["tickets"]


@pytest.fixture
def t1() -> dict:
    """Fixture ticket 1: HubSpot-linked, every nullable key set."""
    return _tickets()[0]


@pytest.fixture
def t2() -> dict:
    """Fixture ticket 2: no HubSpot id, no requester, no company, no numbers."""
    return _tickets()[1]


@pytest.fixture
def t3() -> dict:
    """Fixture ticket 3: HubSpot-linked, numbers empty in the payload and present in the form block."""
    return _tickets()[2]


@pytest.fixture(autouse=True)
def _quiet_chokepoint(monkeypatch: pytest.MonkeyPatch):
    """No rate-limiter lookup, no retry sleep, and a counter at zero before and after."""
    monkeypatch.setattr(hubspot_api, "_acquire_rate_token", lambda: None)
    monkeypatch.setattr(hubspot_api, "RETRY_SLEEP_S", 0)
    monkeypatch.delenv("REDIS_URL", raising=False)
    hubspot_api.reset_calls()
    yield
    hubspot_api.reset_calls()


@pytest.fixture(autouse=True)
def _no_ssk_and_no_child_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ShipSidekick lookup is a child process that reaches a live API: refused here.

    A test that wants a lookup result installs its own fake over `hydrate._ssk_lookup`.
    """

    def refuse_lookup(*_args, **_kwargs):
        raise AssertionError("a test in test_os_bundle.py ran the ShipSidekick lookup")

    def refuse_process(*_args, **_kwargs):
        raise AssertionError("a test in test_os_bundle.py started a child process")

    monkeypatch.setattr(hydrate, "_ssk_lookup", refuse_lookup)
    monkeypatch.setattr(subprocess, "run", refuse_process)


def _source(module) -> str:
    return inspect.getsource(module)


# ====================================================================================
# hydrate_ticket — pinned before any line of it changed
# ====================================================================================


def test_pin_hydrate_ticket_returns_todays_context(monkeypatch: pytest.MonkeyPatch) -> None:
    context, opener = _hydrate(monkeypatch)
    assert context == PINNED_HYDRATED_CONTEXT
    assert list(context) == CONTEXT_KEYS
    assert opener.unanswered == 0


def test_pin_hydrate_ticket_sends_todays_nine_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    _context, opener = _hydrate(monkeypatch)
    assert opener.sent() == PINNED_HYDRATION_REQUESTS
    assert opener.body(1) == {
        "properties": hubspot_api.EMAIL_PROPERTIES,
        "inputs": [{"id": "503"}, {"id": "501"}, {"id": "502"}],
    }
    assert opener.body(3) == {"properties": ["firstname", "lastname", "email", "phone"], "inputs": [{"id": "701"}]}
    assert opener.body(5) == {"properties": ["name", "domain"], "inputs": [{"id": "801"}]}
    assert opener.body(6) == PINNED_RELATED_SEARCH_BODY
    assert opener.body(8) == {"properties": hubspot_api.EMAIL_PROPERTIES, "inputs": [{"id": "601"}]}


def test_pin_hydrate_ticket_with_no_contact_company_or_order_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = copy.deepcopy(HS_TICKET)
    for name in ("order_number", "tracking_number", "hs_all_associated_contact_emails"):
        del ticket["properties"][name]
    answers = [_assoc("503"), _batch(EMAIL_FOLLOW_UP), _assoc(), _assoc()]

    context, opener = _hydrate(monkeypatch, ticket=ticket, answers=answers)

    assert opener.sent() == [
        ("GET", f"{BASE}/crm/v4/objects/tickets/{HS_ID}/associations/emails?limit=10"),
        ("POST", f"{BASE}/crm/v3/objects/emails/batch/read"),
        ("GET", f"{BASE}/crm/v4/objects/tickets/{HS_ID}/associations/contacts?limit=1"),
        ("GET", f"{BASE}/crm/v4/objects/tickets/{HS_ID}/associations/companies?limit=1"),
    ]
    assert context["contact"] == {}
    assert context["company"] == {}
    assert context["related_tickets"] == []
    assert context["thread"] == [HUBSPOT_THREAD[2]]
    assert list(context) == CONTEXT_KEYS


# ====================================================================================
# hydrate_ticket — one logical call per lookup, each request under exactly one kind
# ====================================================================================


def test_hydrate_ticket_records_one_logical_call_per_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    _hydrate(monkeypatch)
    assert hubspot_api.snapshot_calls() == _zero(
        email_thread=1, contact_read=1, company_read=1, related_lookup=1, raw_requests=9
    )


def test_the_related_lookup_is_counted_once_and_never_also_as_a_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _hydrate(monkeypatch)
    kinds = hubspot_api.snapshot_calls()["kinds"]
    assert kinds["related_lookup"] == 1
    assert kinds["search"] == 0
    assert sum(kinds.values()) == 4  # four lookups, four logical calls, no lookup twice


def test_every_request_of_a_hydration_belongs_to_exactly_one_recorded_kind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The once-only rule, request by request.

    A logical call may be more than one request (a thread fetch is two: decision
    Q7), so the raw total is not the number of kinds recorded. What must hold:
    every request is sent under exactly one recorded kind, and the requests of
    the kinds add up to the raw total with none left over.
    """
    recorded: list[str] = []
    real_record = hubspot_api.record_call

    def tracking_record(kind: str) -> None:
        recorded.append(kind)
        real_record(kind)

    monkeypatch.setattr(hubspot_api, "record_call", tracking_record)
    opener = _open_with(monkeypatch, *_hydration_answers())
    owner_of_request: list = []
    opener.on_request = lambda _request: owner_of_request.append(recorded[-1] if recorded else None)

    with monkeypatch.context() as patch:
        patch.setattr(time, "time", lambda: FIXED_NOW_S)
        hydrate.hydrate_ticket(copy.deepcopy(HS_TICKET), copy.deepcopy(SETTINGS), TOKEN)

    assert recorded == ["email_thread", "contact_read", "company_read", "related_lookup"]
    assert None not in owner_of_request  # no request before a kind was recorded
    requests_per_kind = {kind: owner_of_request.count(kind) for kind in recorded}
    assert requests_per_kind == {
        "email_thread": 2,  # association list, batch read
        "contact_read": 2,
        "company_read": 2,
        "related_lookup": 3,  # the search, then one related ticket's snippet (2)
    }
    snapshot = hubspot_api.snapshot_calls()
    assert snapshot["raw_requests"] == sum(requests_per_kind.values()) == 9
    assert snapshot["kinds"] == _zero(
        email_thread=1, contact_read=1, company_read=1, related_lookup=1
    )["kinds"]


def test_a_hydration_with_no_order_number_records_no_related_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = copy.deepcopy(HS_TICKET)
    del ticket["properties"]["order_number"]
    answers = [*_thread_answers(), _assoc("701"), _batch(HS_CONTACT), _assoc("801"), _batch(HS_COMPANY)]

    _hydrate(monkeypatch, ticket=ticket, answers=answers)

    assert hubspot_api.snapshot_calls() == _zero(
        email_thread=1, contact_read=1, company_read=1, raw_requests=6
    )


def test_a_contact_or_company_lookup_that_finds_nothing_is_still_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = copy.deepcopy(HS_TICKET)
    del ticket["properties"]["order_number"]

    _hydrate(monkeypatch, ticket=ticket, answers=[_assoc(), _assoc(), _assoc()])

    assert hubspot_api.snapshot_calls() == _zero(
        email_thread=1, contact_read=1, company_read=1, raw_requests=3
    )


def test_hydrate_names_each_kind_once_at_its_call_site() -> None:
    source = _source(hydrate)
    assert source.count("hs.record_call(") == 3
    for kind in ("email_thread", "contact_read", "company_read"):
        assert source.count(f'hs.record_call("{kind}")') == 1
    # The related lookup already goes through search_tickets, which records its
    # own kind: it is NAMED there, never recorded a second time beside it.
    assert source.count('kind="related_lookup"') == 1
    assert 'record_call("related_lookup")' not in source


# ====================================================================================
# The shape: the same keys from either source
# ====================================================================================


def _shape(context: dict) -> dict:
    """The key structure of a context: its keys in order, and each nested key set."""
    return {
        "keys": [key for key in context if key != "ssk_state"],
        "contact": sorted(context["contact"]),
        "company": sorted(context["company"]),
        "thread": sorted({key for entry in context["thread"] for key in entry}),
        "related_tickets": sorted({key for item in context["related_tickets"] for key in item}),
    }


def test_key_set_of_a_linked_ticket_equals_hydrations(monkeypatch: pytest.MonkeyPatch, t1) -> None:
    hydrated, _opener = _hydrate(monkeypatch)

    built = os_bundle.build_context(t1, copy.deepcopy(EMAILS_OLDEST_FIRST), SETTINGS, OS_ORIGIN)

    assert _shape(built) == _shape(hydrated)
    assert list(built) == CONTEXT_KEYS


def test_key_set_of_a_ticket_with_no_hubspot_id_equals_hydrations(
    monkeypatch: pytest.MonkeyPatch, t2
) -> None:
    hydrated, _opener = _hydrate(monkeypatch)

    built = os_bundle.build_context(t2, None, SETTINGS, OS_ORIGIN)

    assert list(built) == [key for key in hydrated if key != "ssk_state"] == CONTEXT_KEYS
    # No requester, no company, no related ticket: the nested values are hydration's
    # own "nothing" values, and every thread entry has hydration's four keys.
    assert built["contact"] == {} and built["company"] == {} and built["related_tickets"] == []
    assert _shape(built)["thread"] == _shape(hydrated)["thread"]


def test_key_set_of_a_linked_ticket_built_from_the_os_thread_equals_hydrations(
    monkeypatch: pytest.MonkeyPatch, t1
) -> None:
    hydrated, _opener = _hydrate(monkeypatch)
    assert _shape(os_bundle.build_context(t1, [], SETTINGS, OS_ORIGIN)) == _shape(hydrated)


# ====================================================================================
# The values: every row of the mapping table
# ====================================================================================


def test_mapped_values_of_a_linked_ticket_with_a_fetched_thread(t1) -> None:
    built = os_bundle.build_context(t1, copy.deepcopy(EMAILS_OLDEST_FIRST), SETTINGS, OS_ORIGIN)

    assert built == {
        "ticket_id": HS_ID,
        "ticket_link": f"https://app.hubspot.com/contacts/{PORTAL_ID}/ticket/{HS_ID}",
        "subject": "Where is my parcel",
        "topic_of_ticket": "Shipping question",
        "source_type": "FORM",
        "form_fields": {
            # the first email's form block …
            "inquiry_description": "It was due on Tuesday.",
            # … the payload's form answers over it …
            "reason": "late",
            "boxes": 2,
            "contact_preference": "email",
            # … and the payload's own four on top (the block said FX-ORDER-0000)
            "order_number": "FX-ORDER-1001",
            "tracking_number": "FX-TRACK-1001",
            "topic_of_ticket": "Shipping question",
            "company_name": "Fixture Merchant Ltd",
        },
        "has_attachments": True,
        "stage": "1",
        "priority_from_hubspot": None,
        "createdate": "2026-10-01T09:00:00.000Z",
        "hs_lastmodifieddate": "2026-10-08T10:00:00.500Z",
        "contact": {"name": "Pat Example", "email": "pat@example.test", "phone": None},
        "company": {"name": "Fixture Merchant Ltd", "domain": None},
        "thread": HUBSPOT_THREAD,
        "latest_customer_message": "Any news?",
        "related_tickets": [
            {
                "ticket_id": RELATED_OS_ID,
                "ticket_link": f"{OS_ORIGIN}/tickets/{RELATED_OS_ID}",
                "subject": "Earlier question about the same order",
                "stage": "closed",
                "createdate": "2026-09-20T08:15:00.000Z",
                "hs_lastmodifieddate": None,
                "topic_of_ticket": "shipping_delay",
                "snippet": "The parcel had not moved for four days.",
            }
        ],
    }


def test_mapped_values_of_a_ticket_with_no_hubspot_id(t2) -> None:
    built = os_bundle.build_context(t2, None, SETTINGS, OS_ORIGIN)

    assert built == {
        "ticket_id": T2_OS_ID,
        "ticket_link": f"{OS_ORIGIN}/tickets/{T2_OS_ID}",
        "subject": "Question about an invoice line",
        "topic_of_ticket": None,
        "source_type": None,
        "form_fields": {"invoice_number": "FX-INV-2002", "charge": "storage"},
        "has_attachments": False,
        "stage": None,
        "priority_from_hubspot": None,
        "createdate": "2026-10-08T15:45:10.000Z",
        "hs_lastmodifieddate": None,
        "contact": {},
        "company": {},
        "thread": [
            {
                "from": "customer",
                "email": None,
                "date": "2026-10-08T15:45:10.000Z",
                "body_text": "Could you explain the storage charge on the September invoice?",
            }
        ],
        "latest_customer_message": "Could you explain the storage charge on the September invoice?",
        "related_tickets": [],
    }


def test_mapped_values_of_a_linked_ticket_with_no_fetched_thread(t1) -> None:
    """THREAD-FALLBACK: a linked ticket whose fetch returned nothing gets the OS thread."""
    opening = "It was due on Tuesday. Clienté asked — “where is it?” \U0001f4e6"
    for nothing in (None, []):
        built = os_bundle.build_context(copy.deepcopy(t1), nothing, SETTINGS, OS_ORIGIN)

        assert built["ticket_id"] == HS_ID
        assert built["ticket_link"] == f"https://app.hubspot.com/contacts/{PORTAL_ID}/ticket/{HS_ID}"
        assert built["thread"] == [
            {
                "from": "customer",
                "email": "pat@example.test",
                "date": "2026-10-01T09:00:00.000Z",
                "body_text": opening,
            },
            {
                "from": "agent",
                "email": None,
                "date": "2026-10-02T14:30:00.250Z",
                "body_text": "We are checking with the carrier.",
            },
            {
                "from": "customer",
                "email": "pat@example.test",
                "date": "2026-10-08T10:00:00.123Z",
                "body_text": "Any news?",
            },
        ]
        assert built["latest_customer_message"] == "Any news?"
        # The opening body holds no form block, so the fields are the payload's alone.
        assert built["form_fields"] == {
            "reason": "late",
            "boxes": 2,
            "contact_preference": "email",
            "order_number": "FX-ORDER-1001",
            "tracking_number": "FX-TRACK-1001",
            "topic_of_ticket": "Shipping question",
            "company_name": "Fixture Merchant Ltd",
        }


def test_the_ticket_id_of_a_context_is_its_draft_key(t1, t2, t3) -> None:
    for payload in (t1, t2, t3):
        built = os_bundle.build_context(payload, None, SETTINGS, OS_ORIGIN)
        assert built["ticket_id"] == os_bundle.draft_key(payload)


def test_no_requester_and_no_company_are_each_an_empty_dict(t2) -> None:
    built = os_bundle.build_context(t2, None, SETTINGS, OS_ORIGIN)
    assert built["contact"] == {} and isinstance(built["contact"], dict)
    assert built["company"] == {} and isinstance(built["company"], dict)


def test_the_requester_name_is_one_string_used_as_is(t1) -> None:
    t1["requester"] = {"name": "  Pat  van der Example-Smith III ", "email": "pat@example.test"}
    built = os_bundle.build_context(t1, None, SETTINGS, OS_ORIGIN)
    assert built["contact"] == {
        "name": "  Pat  van der Example-Smith III ",
        "email": "pat@example.test",
        "phone": None,
    }


def test_a_requester_with_no_name_keeps_its_email(t3) -> None:
    built = os_bundle.build_context(t3, None, SETTINGS, OS_ORIGIN)
    assert built["contact"] == {"name": None, "email": "sam@example.test", "phone": None}
    assert built["company"] == {"name": "Fixture Merchant Ltd", "domain": None}


def test_the_os_priority_is_not_passed_off_as_hubspots(t1) -> None:
    assert t1["priority"] == "high"
    built = os_bundle.build_context(t1, None, SETTINGS, OS_ORIGIN)
    assert built["priority_from_hubspot"] is None
    assert "priority" not in built


def test_has_attachments_is_the_payloads_boolean_and_nothing_else(t1, t2) -> None:
    t1["has_attachments"] = False
    t1["body"] = "See the attached screenshot and photos."
    t1["thread"][-1]["body"] = "Screenshot attached."
    assert os_bundle.build_context(t1, None, SETTINGS, OS_ORIGIN)["has_attachments"] is False

    t2["has_attachments"] = True
    assert os_bundle.build_context(t2, None, SETTINGS, OS_ORIGIN)["has_attachments"] is True


def test_the_topic_is_the_payloads_else_the_form_blocks(t1, t3) -> None:
    assert os_bundle.build_context(t1, None, SETTINGS, OS_ORIGIN)["topic_of_ticket"] == "Shipping question"
    # Ticket 3: null topic in the payload, "Topic of Ticket: …" in the opening form block.
    assert t3["topic_of_ticket"] is None
    built = os_bundle.build_context(t3, None, SETTINGS, OS_ORIGIN)
    assert built["topic_of_ticket"] == "Shipping question"
    assert built["form_fields"]["topic_of_ticket"] == "Shipping question"


def test_related_tickets_carry_their_os_state_and_category(t1) -> None:
    t1["related"].append(
        {
            "id": "11111111-2222-4333-8444-555555555555",
            "subject": None,
            "state": "waiting_on_client",
            "created_at": "2026-09-12T00:00:00.000Z",
            "category": None,
            "snippet": None,
        }
    )
    related = os_bundle.build_context(t1, None, SETTINGS, OS_ORIGIN)["related_tickets"]
    assert [item["stage"] for item in related] == ["closed", "waiting_on_client"]  # payload order
    assert related[1] == {
        "ticket_id": "11111111-2222-4333-8444-555555555555",
        "ticket_link": f"{OS_ORIGIN}/tickets/11111111-2222-4333-8444-555555555555",
        "subject": None,
        "stage": "waiting_on_client",
        "createdate": "2026-09-12T00:00:00.000Z",
        "hs_lastmodifieddate": None,
        "topic_of_ticket": None,
        "snippet": None,
    }


def test_a_trailing_slash_on_the_os_origin_is_not_doubled(t2) -> None:
    built = os_bundle.build_context(t2, None, SETTINGS, OS_ORIGIN + "/")
    assert built["ticket_link"] == f"{OS_ORIGIN}/tickets/{T2_OS_ID}"


@pytest.mark.parametrize("origin", ["", None, "os.example.test", "ftp://os.example.test", 3])
def test_an_os_origin_that_is_not_an_http_origin_is_refused(t2, origin) -> None:
    with pytest.raises(ValueError):
        os_bundle.build_context(t2, None, SETTINGS, origin)


def test_a_payload_that_is_not_an_object_or_has_no_id_is_refused(t2) -> None:
    with pytest.raises(TypeError):
        os_bundle.build_context(None, None, SETTINGS, OS_ORIGIN)
    with pytest.raises(TypeError):
        os_bundle.build_context([t2], None, SETTINGS, OS_ORIGIN)
    t2["id"] = None
    with pytest.raises(ValueError):
        os_bundle.build_context(t2, None, SETTINGS, OS_ORIGIN)


# ------------------------------------------------------------------ the thread from the OS


def test_os_thread_maps_each_kind_by_the_table(t1) -> None:
    thread = os_bundle.os_thread(t1)
    assert [(entry["from"], entry["email"]) for entry in thread] == [
        ("customer", "pat@example.test"),  # opening
        ("agent", None),  # outbound
        ("customer", "pat@example.test"),  # client_reply
    ]
    assert [entry["date"] for entry in thread] == [
        "2026-10-01T09:00:00.000Z",
        "2026-10-02T14:30:00.250Z",
        "2026-10-08T10:00:00.123Z",
    ]
    assert all(sorted(entry) == ["body_text", "date", "email", "from"] for entry in thread)


def test_os_thread_keeps_the_date_string_unchanged(t1) -> None:
    t1["thread"][0]["at"] = "2026-10-01T09:00:00.007Z"
    assert os_bundle.os_thread(t1)[0]["date"] == "2026-10-01T09:00:00.007Z"


def test_os_thread_cuts_a_body_at_the_thread_builders_own_cap(t1) -> None:
    long_body = "a" * 3999 + "bcdef" + "z" * 996
    assert len(long_body) == 5000
    t1["thread"][0]["body"] = long_body

    cut = os_bundle.os_thread(t1)[0]["body_text"]

    assert len(cut) == 4000 == os_bundle.BODY_TEXT_CAP
    assert cut == long_body[:4000] and cut.endswith("ab")
    # The same cap as hydration's builder: the two sources cut at the same character.
    built = hydrate.build_thread([_email("1", long_body, None, "INCOMING_EMAIL", "a@example.test", "x")])
    assert built[0]["body_text"] == cut


def test_os_thread_keeps_the_payloads_order(t1) -> None:
    """The OS sends the thread oldest first; the order is taken, never re-sorted."""
    t1["thread"] = [
        {"kind": "client_reply", "at": "2026-10-08T10:00:00.123Z", "body": "third by date, first here"},
        {"kind": "opening", "at": "2026-10-01T09:00:00.000Z", "body": "first by date, second here"},
        {"kind": "outbound", "at": "2026-10-02T14:30:00.250Z", "body": "second by date, third here"},
    ]
    assert [entry["body_text"] for entry in os_bundle.os_thread(t1)] == [
        "third by date, first here",
        "first by date, second here",
        "second by date, third here",
    ]


def test_os_thread_with_no_requester_gives_a_customer_item_no_email(t2) -> None:
    assert os_bundle.os_thread(t2) == [
        {
            "from": "customer",
            "email": None,
            "date": "2026-10-08T15:45:10.000Z",
            "body_text": "Could you explain the storage charge on the September invoice?",
        }
    ]


def test_os_thread_leaves_out_an_item_of_a_kind_the_contract_does_not_name(t1, capfd) -> None:
    """Anything that is not one of the three kinds (an internal note, were one ever
    sent) never becomes customer or agent text for the model."""
    t1["thread"].insert(1, {"kind": "internal_note", "at": "2026-10-02T00:00:00.000Z", "body": MARKER})
    t1["thread"].append({"at": "2026-10-09T00:00:00.000Z", "body": MARKER})
    t1["thread"].append("not an item")

    thread = os_bundle.os_thread(t1)

    assert [entry["from"] for entry in thread] == ["customer", "agent", "customer"]
    assert MARKER not in json.dumps(thread)
    err = capfd.readouterr().err
    assert "count=3" in err and MARKER not in err


def test_os_thread_of_a_payload_with_no_thread_is_empty(t2) -> None:
    for nothing in (None, [], "text"):
        t2["thread"] = nothing
        assert os_bundle.os_thread(t2) == []


def test_os_thread_reads_a_missing_body_as_empty_text(t1) -> None:
    t1["thread"][1]["body"] = None
    assert os_bundle.os_thread(t1)[1]["body_text"] == ""


# ------------------------------------------------------------------ the form fields


def test_form_fields_structured_values_win_over_the_form_block(t1) -> None:
    """A form block saying one order number and a payload saying another → the payload's."""
    assert "Order Number: FX-ORDER-0000" in FORM_BLOCK_TEXT
    built = os_bundle.build_context(t1, copy.deepcopy(EMAILS_OLDEST_FIRST), SETTINGS, OS_ORIGIN)
    assert built["form_fields"]["order_number"] == "FX-ORDER-1001"


def test_form_fields_merge_in_three_layers(t1) -> None:
    t1["body"] = "Order Number: BLOCK-ORDER\nTracking Number: BLOCK-TRACK\nCarrier Issue: lost\nReason: block reason"
    t1["form_answers"] = {"reason": "answers reason", "tracking_number": "ANSWERS-TRACK", "blank": " ", "none": None}
    t1["order_number"] = "PAYLOAD-ORDER"
    t1["tracking_number"] = None
    t1["topic_of_ticket"] = None
    t1["company_name"] = "   "

    fields = os_bundle.build_context(t1, None, SETTINGS, OS_ORIGIN)["form_fields"]

    assert fields == {
        "order_number": "PAYLOAD-ORDER",  # the payload's own key beats the block
        "tracking_number": "ANSWERS-TRACK",  # the answers beat the block; the payload's is null
        "carrier_issue": "lost",  # only the block has it
        "reason": "answers reason",  # the answers beat the block
    }
    # A blank or null structured value never erases a parsed one, and adds no key.
    assert "blank" not in fields and "none" not in fields and "company_name" not in fields


def test_form_fields_ignore_form_answers_that_are_not_an_object(t1) -> None:
    for not_an_object in (None, ["a"], "text", 3):
        payload = copy.deepcopy(t1)
        payload["form_answers"] = not_an_object
        fields = os_bundle.build_context(payload, None, SETTINGS, OS_ORIGIN)["form_fields"]
        assert fields == {
            "order_number": "FX-ORDER-1001",
            "tracking_number": "FX-TRACK-1001",
            "topic_of_ticket": "Shipping question",
            "company_name": "Fixture Merchant Ltd",
        }


def test_the_form_block_of_ticket_three_is_read_from_its_opening_body(t3) -> None:
    fields = os_bundle.build_context(t3, None, SETTINGS, OS_ORIGIN)["form_fields"]
    assert fields["order_number"] == "FX-ORDER-3003"
    assert fields["tracking_number"] == "FX-TRACK-3003"
    assert fields["topic_of_ticket"] == "Shipping question"
    assert fields["company_name"] == "Fixture Merchant Ltd"  # the payload's


# ------------------------------------------------------------------ the latest customer message


def test_latest_customer_message_falls_back_to_the_body_then_the_form_block(t2) -> None:
    t2["thread"] = []
    assert os_bundle.build_context(t2, None, SETTINGS, OS_ORIGIN)["latest_customer_message"] == (
        "Could you explain the storage charge on the September invoice?"
    )

    t2["body"] = None
    t2["form_answers"] = {"inquiry_description": "From the form answers."}
    assert os_bundle.build_context(t2, None, SETTINGS, OS_ORIGIN)["latest_customer_message"] == (
        "From the form answers."
    )

    t2["form_answers"] = None
    assert os_bundle.build_context(t2, None, SETTINGS, OS_ORIGIN)["latest_customer_message"] == ""


def test_latest_customer_message_is_hydrations_pick_over_the_thread(t1) -> None:
    t1["thread"].append({"kind": "outbound", "at": "2026-10-09T08:00:00.000Z", "body": "An agent wrote last."})
    built = os_bundle.build_context(t1, None, SETTINGS, OS_ORIGIN)
    assert built["latest_customer_message"] == "Any news?"
    assert built["latest_customer_message"] == hydrate.latest_customer_message(built["thread"])


# ====================================================================================
# The one HubSpot read that stays: the thread fetch
# ====================================================================================


def test_fetch_thread_is_two_requests_and_one_email_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _open_with(monkeypatch, *_thread_answers())

    emails = os_bundle.fetch_thread(HS_ID, SETTINGS, TOKEN)

    assert opener.sent() == [
        ("GET", f"{BASE}/crm/v4/objects/tickets/{HS_ID}/associations/emails?limit=10"),
        ("POST", f"{BASE}/crm/v3/objects/emails/batch/read"),
    ]
    assert opener.body(1) == {
        "properties": hubspot_api.EMAIL_PROPERTIES,
        "inputs": [{"id": "503"}, {"id": "501"}, {"id": "502"}],
    }
    assert hubspot_api.snapshot_calls() == _zero(email_thread=1, raw_requests=2)
    assert [email["id"] for email in emails] == ["501", "502", "503"]  # oldest first
    assert emails == EMAILS_OLDEST_FIRST


def test_the_context_thread_of_a_fetched_thread_is_build_thread_of_those_emails(
    monkeypatch: pytest.MonkeyPatch, t1
) -> None:
    _open_with(monkeypatch, *_thread_answers())
    emails = os_bundle.fetch_thread(HS_ID, SETTINGS, TOKEN)

    built = os_bundle.build_context(t1, emails, SETTINGS, OS_ORIGIN)

    assert built["thread"] == hydrate.build_thread(copy.deepcopy(emails)) == HUBSPOT_THREAD


def test_build_context_orders_fetched_emails_itself(t1) -> None:
    shuffled = [copy.deepcopy(EMAIL_FOLLOW_UP), copy.deepcopy(EMAIL_OPENING), copy.deepcopy(EMAIL_AGENT)]
    built = os_bundle.build_context(t1, shuffled, SETTINGS, OS_ORIGIN)
    assert built["thread"] == HUBSPOT_THREAD
    # The form block is the OLDEST email's, whatever order the list came in.
    assert built["form_fields"]["inquiry_description"] == "It was due on Tuesday."


def test_fetch_thread_is_capped_by_the_settings_email_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _open_with(monkeypatch, _assoc("501", "502", "503", "504", "505"), _batch(EMAIL_OPENING))
    os_bundle.fetch_thread(HS_ID, {"conversation_email_limit": 2}, TOKEN)
    assert opener.sent()[0] == (
        "GET",
        f"{BASE}/crm/v4/objects/tickets/{HS_ID}/associations/emails?limit=2",
    )
    assert opener.body(1)["inputs"] == [{"id": "501"}, {"id": "502"}]


def test_fetch_thread_defaults_to_ten_emails(monkeypatch: pytest.MonkeyPatch) -> None:
    opener = _open_with(monkeypatch, _assoc())
    os_bundle.fetch_thread(HS_ID, {}, TOKEN)
    assert opener.sent() == [
        ("GET", f"{BASE}/crm/v4/objects/tickets/{HS_ID}/associations/emails?limit=10")
    ]


def test_fetch_thread_with_no_associated_email_is_one_request_and_one_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open_with(monkeypatch, _assoc())
    assert os_bundle.fetch_thread(HS_ID, SETTINGS, TOKEN) == []
    assert hubspot_api.snapshot_calls() == _zero(email_thread=1, raw_requests=1)


def test_fetch_thread_that_hubspot_refuses_returns_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _open_with(monkeypatch, _http_error(404))
    assert os_bundle.fetch_thread(HS_ID, SETTINGS, TOKEN) == []
    assert hubspot_api.snapshot_calls() == _zero(email_thread=1, raw_requests=1)


@pytest.mark.parametrize("no_id", [None, "", "   "])
def test_fetch_thread_for_a_ticket_with_no_hubspot_id_makes_no_request(
    monkeypatch: pytest.MonkeyPatch, no_id
) -> None:
    opener = _open_with(monkeypatch)
    assert os_bundle.fetch_thread(no_id, SETTINGS, TOKEN) == []
    assert opener.sent() == []
    assert hubspot_api.snapshot_calls() == _zero()


@pytest.mark.parametrize("bad_id", ["93/../7", "pytest-0a1b2c", "93 00", "٩٣", 9300000000001])
def test_fetch_thread_never_puts_a_non_numeric_id_in_a_path(
    monkeypatch: pytest.MonkeyPatch, capfd, bad_id
) -> None:
    opener = _open_with(monkeypatch)
    assert os_bundle.fetch_thread(bad_id, SETTINGS, TOKEN) == []
    assert opener.sent() == []
    assert hubspot_api.snapshot_calls() == _zero()
    if isinstance(bad_id, str):
        assert bad_id not in capfd.readouterr().err


@pytest.mark.parametrize("no_token", [None, ""])
def test_fetch_thread_without_a_token_makes_no_request(
    monkeypatch: pytest.MonkeyPatch, no_token
) -> None:
    opener = _open_with(monkeypatch)
    assert os_bundle.fetch_thread(HS_ID, SETTINGS, no_token) == []
    assert opener.sent() == []
    assert hubspot_api.snapshot_calls() == _zero()


def test_a_linked_ticket_costs_exactly_two_transport_calls(monkeypatch: pytest.MonkeyPatch, t1) -> None:
    transport = fakes.FakeHubSpotTransport(
        {
            f"/crm/v4/objects/tickets/{HS_ID}/associations/emails": _assoc("503", "501", "502"),
            "/crm/v3/objects/emails/batch/read": _batch(EMAIL_FOLLOW_UP, EMAIL_OPENING, EMAIL_AGENT),
        }
    )
    monkeypatch.setattr(hubspot_api, "_request", transport)

    emails = os_bundle.fetch_thread(t1["hubspot_ticket_id"], SETTINGS, TOKEN)
    built = os_bundle.build_context(t1, emails, SETTINGS, OS_ORIGIN)

    assert transport.calls == [
        ("GET", f"/crm/v4/objects/tickets/{HS_ID}/associations/emails?limit=10"),
        ("POST", "/crm/v3/objects/emails/batch/read"),
    ]
    assert built["thread"] == HUBSPOT_THREAD
    assert hubspot_api.snapshot_calls()["kinds"] == _zero(email_thread=1)["kinds"]


def test_a_ticket_with_no_hubspot_id_costs_no_hubspot_call_of_any_kind(
    monkeypatch: pytest.MonkeyPatch, t2
) -> None:
    transport = fakes.FakeHubSpotTransport()
    monkeypatch.setattr(hubspot_api, "_request", transport)
    opener = _open_with(monkeypatch)

    emails = os_bundle.fetch_thread(t2["hubspot_ticket_id"], SETTINGS, TOKEN)
    built = os_bundle.build_context(t2, emails, SETTINGS, OS_ORIGIN)

    assert transport.calls == []
    assert opener.sent() == []
    assert hubspot_api.snapshot_calls() == _zero()
    assert built["ticket_id"] == T2_OS_ID
    assert built["ticket_link"] == f"{OS_ORIGIN}/tickets/{T2_OS_ID}"
    assert built["thread"] == os_bundle.os_thread(t2) and len(built["thread"]) == 1


def test_a_linked_ticket_whose_fetch_returns_nothing_uses_the_os_thread(
    monkeypatch: pytest.MonkeyPatch, t1
) -> None:
    _open_with(monkeypatch, _assoc())
    emails = os_bundle.fetch_thread(HS_ID, SETTINGS, TOKEN)
    assert emails == []

    built = os_bundle.build_context(t1, emails, SETTINGS, OS_ORIGIN)

    assert built["thread"] == os_bundle.os_thread(t1)
    assert os_bundle.thread_source(t1, emails) == "os_fallback"


def test_thread_source_names_where_the_thread_came_from(t1, t2) -> None:
    assert os_bundle.thread_source(t1, copy.deepcopy(EMAILS_OLDEST_FIRST)) == "hubspot"
    assert os_bundle.thread_source(t1, []) == "os_fallback"
    assert os_bundle.thread_source(t1, None) == "os_fallback"
    assert os_bundle.thread_source(t2, None) == "os"
    assert os_bundle.thread_source(t2, []) == "os"
    assert (os_bundle.THREAD_HUBSPOT, os_bundle.THREAD_OS, os_bundle.THREAD_OS_FALLBACK) == (
        "hubspot",
        "os",
        "os_fallback",
    )


# ====================================================================================
# The two keys
# ====================================================================================


def test_draft_key_is_the_hubspot_id_when_the_ticket_has_one_else_the_os_id(t1, t2, t3) -> None:
    assert os_bundle.draft_key(t1) == HS_ID
    assert os_bundle.draft_key(t2) == T2_OS_ID
    assert os_bundle.draft_key(t3) == "9300000000003"
    t1["hubspot_ticket_id"] = "  "
    assert os_bundle.draft_key(t1) == T1_OS_ID


def test_draft_keys_of_the_three_fixture_tickets_are_pairwise_different(t1, t2, t3) -> None:
    assert len({os_bundle.draft_key(payload) for payload in (t1, t2, t3)}) == 3


def test_cycle_key_is_the_payloads_signal_id(t1, t2, t3) -> None:
    assert os_bundle.cycle_key(t1) == "1c7d2e3f-4a5b-4c6d-9e8f-0a1b2c3d4e5f"
    assert os_bundle.cycle_key(t2) == "2d8e3f4a-5b6c-4d7e-8f9a-1b2c3d4e5f6a"
    assert os_bundle.cycle_key(t3) == "4f0a5b6c-7d8e-4f9a-8b1c-3d4e5f6a7b8c"


def test_cycle_key_with_a_rerun_request_id_is_the_rerun_key(t1) -> None:
    assert os_bundle.cycle_key(t1, "5a6b7c8d-0000-4000-8000-000000000001") == (
        "rerun:5a6b7c8d-0000-4000-8000-000000000001"
    )
    assert os_bundle.cycle_key(t1, rerun_request_id="r-2") == "rerun:r-2"
    # A rerun of a ticket with no client signal (a single fetch can return one) still has a key.
    t1["signal"] = None
    assert os_bundle.cycle_key(t1, "r-3") == "rerun:r-3"


def test_cycle_key_with_no_signal_and_no_rerun_is_refused(t1) -> None:
    t1["signal"] = None
    with pytest.raises(ValueError):
        os_bundle.cycle_key(t1)
    t1["signal"] = {"kind": "opening", "id": "", "at": "2026-10-01T09:00:00.000Z"}
    with pytest.raises(ValueError):
        os_bundle.cycle_key(t1)
    with pytest.raises(ValueError):
        os_bundle.cycle_key(t1, "")


# ====================================================================================
# The number backfill's source: the two origins, never merged
# ====================================================================================


def test_backfill_source_ticket_three_numbers_only_in_the_form_block(t3) -> None:
    assert t3["order_number"] is None and t3["tracking_number"] is None
    assert os_bundle.backfill_source(t3, os_bundle.first_email_html(t3, None)) == {
        "payload": {"order_number": None, "tracking_number": None},
        "form_block": {"order_number": "FX-ORDER-3003", "tracking_number": "FX-TRACK-3003"},
    }


def test_backfill_source_ticket_one_reports_the_payloads_numbers_whatever_the_block_says(t1) -> None:
    assert os_bundle.backfill_source(t1, FORM_BLOCK_HTML) == {
        "payload": {"order_number": "FX-ORDER-1001", "tracking_number": "FX-TRACK-1001"},
        "form_block": {"order_number": "FX-ORDER-0000", "tracking_number": None},
    }


def test_backfill_source_reports_a_blank_payload_string_as_none(t1) -> None:
    t1["order_number"] = ""
    t1["tracking_number"] = "   "
    assert os_bundle.backfill_source(t1, FORM_BLOCK_HTML)["payload"] == {
        "order_number": None,
        "tracking_number": None,
    }


def test_backfill_source_with_no_first_email_has_an_empty_form_block(t1) -> None:
    for nothing in (None, ""):
        assert os_bundle.backfill_source(t1, nothing) == {
            "payload": {"order_number": "FX-ORDER-1001", "tracking_number": "FX-TRACK-1001"},
            "form_block": {"order_number": None, "tracking_number": None},
        }


def test_backfill_source_is_not_the_merged_form_fields(t1) -> None:
    """The trap it closes: once merged, nothing can say a number was empty in the payload."""
    t1["order_number"] = None
    merged = os_bundle.build_context(t1, copy.deepcopy(EMAILS_OLDEST_FIRST), SETTINGS, OS_ORIGIN)
    assert merged["form_fields"]["order_number"] == "FX-ORDER-0000"  # from the block, origin lost

    source = os_bundle.backfill_source(t1, os_bundle.first_email_html(t1, EMAILS_OLDEST_FIRST))
    assert source["payload"]["order_number"] is None
    assert source["form_block"]["order_number"] == "FX-ORDER-0000"


def test_first_email_html_is_the_oldest_fetched_emails_html_else_the_opening_body(t1, t2) -> None:
    shuffled = [copy.deepcopy(EMAIL_FOLLOW_UP), copy.deepcopy(EMAIL_OPENING)]
    assert os_bundle.first_email_html(t1, shuffled) == FORM_BLOCK_HTML
    assert os_bundle.first_email_html(t1, None) == t1["body"]
    assert os_bundle.first_email_html(t1, []) == t1["body"]
    t2["body"] = None
    assert os_bundle.first_email_html(t2, None) is None


# ====================================================================================
# ShipSidekick state: the existing lookup, not a HubSpot call
# ====================================================================================


def _ssk_settings(enabled: bool = True) -> dict:
    return {**SETTINGS, "shipsidekick": {"enabled": enabled}}


def test_ssk_state_is_added_through_the_existing_lookup(monkeypatch: pytest.MonkeyPatch, t1) -> None:
    seen: list = []

    def lookup(order_number, tracking_number, merchant=""):
        seen.append((order_number, tracking_number, merchant))
        return {"found": True, "status": "in_transit"}

    monkeypatch.setattr(hydrate, "_ssk_lookup", lookup)

    built = os_bundle.build_context(t1, None, _ssk_settings(), OS_ORIGIN)

    assert seen == [("FX-ORDER-1001", "FX-TRACK-1001", "Fixture Merchant Ltd")]
    assert built["ssk_state"] == {"found": True, "status": "in_transit"}
    assert list(built) == [*CONTEXT_KEYS, "ssk_state"]
    assert hubspot_api.snapshot_calls() == _zero()


def test_ssk_lookup_is_not_run_when_the_settings_do_not_enable_it(t1) -> None:
    # The autouse fixture refuses the lookup, so reaching it would fail the test.
    for settings in (SETTINGS, _ssk_settings(False), {**SETTINGS, "shipsidekick": None}):
        assert "ssk_state" not in os_bundle.build_context(copy.deepcopy(t1), None, settings, OS_ORIGIN)


def test_ssk_lookup_is_not_run_without_an_order_or_tracking_number(t2) -> None:
    assert "ssk_state" not in os_bundle.build_context(t2, None, _ssk_settings(), OS_ORIGIN)


def test_an_ssk_lookup_that_finds_nothing_adds_no_key(monkeypatch: pytest.MonkeyPatch, t1) -> None:
    monkeypatch.setattr(hydrate, "_ssk_lookup", lambda *_a, **_k: None)
    assert "ssk_state" not in os_bundle.build_context(t1, None, _ssk_settings(), OS_ORIGIN)


def test_an_ssk_lookup_that_raises_still_yields_a_context(
    monkeypatch: pytest.MonkeyPatch, t1, capfd
) -> None:
    def lookup(*_args, **_kwargs):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(hydrate, "_ssk_lookup", lookup)

    built = os_bundle.build_context(t1, None, _ssk_settings(), OS_ORIGIN)

    assert list(built) == CONTEXT_KEYS
    err = capfd.readouterr().err
    assert "RuntimeError" in err and MARKER not in err


# ====================================================================================
# Prohibitions
# ====================================================================================


def test_build_context_reads_no_file_no_url_and_starts_no_process(
    monkeypatch: pytest.MonkeyPatch, t2
) -> None:
    """has_attachments is the payload's boolean: nothing is opened to find out."""

    def refuse(*_args, **_kwargs):
        raise AssertionError("build_context opened a file, a URL or a process")

    t2["has_attachments"] = True
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", refuse)
        patch.setattr(io, "open", refuse)
        patch.setattr(os, "open", refuse)
        patch.setattr(urllib.request, "urlopen", refuse)
        patch.setattr(hubspot_api, "_request", refuse)
        patch.setattr(hubspot_api, "_default_opener", refuse)
        patch.setattr(subprocess, "Popen", refuse)
        built = os_bundle.build_context(t2, None, _ssk_settings(), OS_ORIGIN)

    assert built["has_attachments"] is True
    assert built["ticket_id"] == T2_OS_ID
    assert hubspot_api.snapshot_calls() == _zero()


def test_no_ticket_text_reaches_a_log_stdout_or_stderr(
    monkeypatch: pytest.MonkeyPatch, caplog, capfd, t1
) -> None:
    """Checker CRON-W4, over every path that logs: a fetch that works, a fetch
    HubSpot refuses, a fetch that is skipped, an unknown thread kind and a failed
    ShipSidekick lookup."""
    caplog.set_level(logging.DEBUG)
    t1["subject"] = f"{MARKER} subject"
    t1["body"] = f"Order Number: {MARKER}-ORDER\nbody {MARKER}"
    t1["requester"] = {"name": f"{MARKER} name", "email": f"{MARKER}@example.test"}
    t1["company_name"] = f"{MARKER} company"
    t1["form_answers"] = {"reason": MARKER}
    for item in t1["thread"]:
        item["body"] = f"{MARKER} thread"
    t1["thread"].append({"kind": "internal_note", "at": "2026-10-09T00:00:00.000Z", "body": MARKER})
    t1["related"][0]["subject"] = MARKER
    t1["related"][0]["snippet"] = MARKER
    marked_emails = [
        _email("501", f"{MARKER} text", f"<p>Order Number: {MARKER}</p>", "INCOMING_EMAIL", f"{MARKER}@example.test", "1"),
    ]

    def failing_lookup(*_args, **_kwargs):
        raise RuntimeError(f"lookup echoed {MARKER}")

    monkeypatch.setattr(hydrate, "_ssk_lookup", failing_lookup)
    _open_with(monkeypatch, _assoc("501"), _batch(*marked_emails), _http_error(404))

    fetched = os_bundle.fetch_thread(HS_ID, SETTINGS, TOKEN)
    refused = os_bundle.fetch_thread(HS_ID, SETTINGS, TOKEN)
    skipped = os_bundle.fetch_thread(f"{MARKER}", SETTINGS, TOKEN)
    from_hubspot = os_bundle.build_context(t1, fetched, _ssk_settings(), OS_ORIGIN)
    from_os = os_bundle.build_context(t1, refused, _ssk_settings(), OS_ORIGIN)
    os_bundle.backfill_source(t1, os_bundle.first_email_html(t1, fetched))
    os_bundle.thread_source(t1, fetched)
    os_bundle.cycle_key(t1)
    os_bundle.draft_key(t1)

    # The text really was in play: it is in both contexts, and only there.
    assert skipped == []
    assert MARKER in json.dumps(from_hubspot) and MARKER in json.dumps(from_os)
    fakes.assert_marker_absent(MARKER, caplog, capfd)


def test_the_modules_log_lines_hold_ids_codes_and_counts(
    monkeypatch: pytest.MonkeyPatch, capfd, t1
) -> None:
    _open_with(monkeypatch, *_thread_answers())
    os_bundle.fetch_thread(HS_ID, SETTINGS, TOKEN)
    os_bundle.fetch_thread(HS_ID, SETTINGS, None)
    os_bundle.fetch_thread("not-digits", SETTINGS, TOKEN)

    lines = [line for line in capfd.readouterr().err.splitlines() if line.startswith("[pipeline.os_bundle]")]

    assert lines == [
        f"[pipeline.os_bundle] thread fetched: ticket={HS_ID} emails=3",
        f"[pipeline.os_bundle] thread fetch skipped: code=no_hubspot_token ticket={HS_ID}",
        "[pipeline.os_bundle] thread fetch skipped: code=hubspot_id_not_numeric",
    ]


def test_hydrations_helpers_are_imported_never_copied() -> None:
    tree = ast.parse(_source(os_bundle))
    defined = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    helpers = {
        "html_to_text",
        "parse_form_fields",
        "build_thread",
        "latest_customer_message",
        "resolve_merchant",
        "detect_attachments",
        "_ssk_lookup",
    }
    assert defined & helpers == set()
    used = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "hydrate"
    }
    assert {"parse_form_fields", "build_thread", "latest_customer_message", "resolve_merchant", "_ssk_lookup"} <= used


def test_build_context_uses_hydrations_own_functions(monkeypatch: pytest.MonkeyPatch, t1) -> None:
    monkeypatch.setattr(hydrate, "parse_form_fields", lambda _html: {"from_the_patched_parser": "yes"})
    monkeypatch.setattr(hydrate, "build_thread", lambda _emails: [{"from": "customer", "email": None, "date": "d", "body_text": "patched builder"}])
    monkeypatch.setattr(hydrate, "latest_customer_message", lambda _thread: "patched picker")

    built = os_bundle.build_context(t1, copy.deepcopy(EMAILS_OLDEST_FIRST), SETTINGS, OS_ORIGIN)

    assert built["form_fields"]["from_the_patched_parser"] == "yes"
    assert built["thread"][0]["body_text"] == "patched builder"
    assert built["latest_customer_message"] == "patched picker"


def test_the_module_never_hydrates_an_attachment() -> None:
    """No attachment property, no file association, no attachment detection by text."""
    source = _source(os_bundle)
    for forbidden in ("hs_file_upload", "hs_attachment_ids", '"files"', "detect_attachments", "urlopen"):
        assert forbidden not in source
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "json", "os", "sys", "datetime", "pathlib", "typing", "urllib"}
    assert "urllib.request" not in source and "subprocess" not in source and "socket" not in source


def test_no_new_identifier_says_a_bare_shadow() -> None:
    """D-08: `shadow` alone already names the engine shadow."""
    names = {
        token.string
        for token in tokenize.generate_tokens(io.StringIO(_source(os_bundle)).readline)
        if token.type == tokenize.NAME
    }
    assert not [name for name in names if "shadow" in name.lower()]


# ====================================================================================
# The bundle file
# ====================================================================================


def test_write_bundle_writes_the_bundle_as_utf8_json_keeping_non_ascii(tmp_path, t1) -> None:
    target = tmp_path / "outputs" / "runs" / "os" / "run-1" / "bundle.json"
    entry = {"ref": os_bundle.draft_key(t1), "ticket_context": os_bundle.build_context(t1, None, SETTINGS, OS_ORIGIN)}

    written = os_bundle.write_bundle(target, "run-1", [entry], generated_at="2026-10-09T12:00:00.000Z")

    assert written == target and target.is_file()
    raw = target.read_bytes()
    assert "Clienté asked — “where is it?” \U0001f4e6".encode("utf-8") in raw
    assert b"\\u00e9" not in raw and b"\\ud83d" not in raw
    assert json.loads(raw.decode("utf-8")) == {
        "run_id": "run-1",
        "mode": "os",
        "generated_at": "2026-10-09T12:00:00.000Z",
        "tickets": [entry],
    }
    assert list(json.loads(raw.decode("utf-8"))) == ["run_id", "mode", "generated_at", "tickets"]
    assert [path.name for path in target.parent.iterdir()] == ["bundle.json"]  # no temp file left


def test_write_bundle_stamps_the_time_when_none_is_given(tmp_path) -> None:
    target = os_bundle.write_bundle(tmp_path / "bundle.json", "run-2", [])
    body = json.loads(target.read_text(encoding="utf-8"))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", body["generated_at"])
    assert body["tickets"] == [] and body["run_id"] == "run-2"


def test_write_bundle_replaces_an_earlier_bundle(tmp_path) -> None:
    target = tmp_path / "bundle.json"
    os_bundle.write_bundle(target, "run-3", [{"ref": "a"}, {"ref": "b"}])
    os_bundle.write_bundle(target, "run-3", [{"ref": "c"}])
    assert [entry["ref"] for entry in json.loads(target.read_text(encoding="utf-8"))["tickets"]] == ["c"]


def test_write_bundle_refuses_entries_that_are_not_a_list_and_writes_nothing(tmp_path) -> None:
    target = tmp_path / "run" / "bundle.json"
    with pytest.raises(TypeError):
        os_bundle.write_bundle(target, "run-4", {"ref": "a"})
    with pytest.raises(TypeError):
        os_bundle.write_bundle(target, "run-4", [{"ref": object()}])
    assert not target.exists()
    assert not target.parent.exists() or list(target.parent.iterdir()) == []


def test_write_bundle_survives_text_that_is_not_valid_unicode(tmp_path) -> None:
    target = os_bundle.write_bundle(tmp_path / "bundle.json", "run-5", [{"ref": "a", "text": "half \ud83d pair"}])
    assert json.loads(target.read_bytes().decode("utf-8"))["tickets"][0]["ref"] == "a"
