"""`os` mode, second half: code persists what the agent produced (Phase 93, plan 17).

`os_pass.finalize_os_run` runs after the agent's result is reconciled. For each
bundle ticket, in order:

    the draft        through the atomic replace, keyed by the entry's draft key,
                     OS ticket id and cycle key — BEFORE anything tells the OS the
                     ticket was processed
    the write-back   `completed`, echoing the consumed signal exactly as the list
                     supplied it; or `failed` with a code of the OS's closed list
    every answer     200, 400, 401, 404, 409, 413, 422, both kinds of 503, another
                     5xx and a transport error each have one defined action; each
                     row of the plan's two status tables is a test here

Most tests call the finalizer directly with entries built from the list fixture
by the same `os_bundle` functions the runner uses; a few drive the whole
orchestrator. The fakes are `OsWorld`'s (tests/test_os_mode_run.py): a recording
draft writer with the real one's keyword-only signature, a scripted OS API and a
recording pause, so no test waits and none reaches a database, HubSpot, the
Pack'N OS, a real secret file or the agent.
"""
from __future__ import annotations

import copy
import inspect
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from packn_os_hubspot_client import client
from packn_os_hubspot_client.tickets_api import TicketsApiError
from tests import test_os_mode_run as osm

# The safety fixtures of the runner's tests apply to every test here too: no
# child process, no ShipSidekick lookup, no real token or secret path.
from tests.test_os_mode_run import (  # noqa: F401 - fixtures, used by name
    OsWorld,
    _no_child_process,
    _no_real_file_and_a_clean_counter,
    make_world,
    world,
)
from tests.helpers import fakes
from ticket_pipeline import os_bundle, os_pass, run_types

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RUN_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
OTHER_RUN_ID = "5b1f0c2a-9d3e-4f4a-8b6c-7d8e9f0a1b2c"
MARKER = "ZZ-MARKER-93-17"
EXC_MARKER = "ZZ-EXC-93-17"
REREAD_SIGNAL = {"kind": "client_reply", "id": "8a7b6c5d-4e3f-4a2b-9c1d-0e9f8a7b6c5d", "at": "2026-10-08T11:30:00.250Z"}
THREADS = {osm.T1_HS_ID: osm.T1_EMAILS, osm.T3_HS_ID: osm.T3_EMAILS}

CLASSIFICATION = {
    "category": "wismo_tracking",
    "confidence": 0.9,
    "priority": "normal",
    "reason": "asks where the order is",
    "form_topic": "Shipping question",
}


def V(status: int, error: Optional[str] = None, replayed: Optional[bool] = None) -> dict:
    """One write-back verdict, in the shape the client returns."""
    return {"status": status, "error": error, "replayed": replayed}


OK = V(200, None, False)
REPLAYED = V(200, None, True)
CONFLICT = V(409, "conflict")
BAD_PAYLOAD = V(400, "bad_payload")
TOO_LARGE = V(413, "payload_too_large")
SIGNAL_AHEAD = V(422, "signal_ahead")
NOT_FOUND = V(404, "not_found")


def E(code: str, http_status: Optional[int] = None) -> TicketsApiError:
    return TicketsApiError(code, http_status)


# ----------------------------------------------------------------- building blocks


def make_entry(index: int, requests: tuple = (), payload_edit: Optional[Callable[[dict], None]] = None) -> dict:
    """The bundle entry of list-fixture ticket `index`, built as the runner builds it."""
    payload = osm._payload(index)
    if payload_edit is not None:
        payload_edit(payload)
    hubspot_id = payload.get("hubspot_ticket_id")
    emails = THREADS.get(hubspot_id, [])
    return {
        "ref": os_bundle.draft_key(payload),
        "os_ticket_id": payload["id"],
        "hubspot_ticket_id": hubspot_id,
        "cycle_key": os_bundle.cycle_key(payload, requests[0] if requests else None),
        "is_rerun": bool(requests),
        "rerun_request_ids": list(requests),
        "signal": os_pass._signal(payload),
        "thread_source": os_bundle.thread_source(payload, emails),
        "backfill_source": os_bundle.backfill_source(payload, os_bundle.first_email_html(payload, emails)),
        "ticket_context": os_bundle.build_context(payload, emails, osm.SETTINGS, osm.OS_ORIGIN),
    }


def completed(entry: dict, **over: Any) -> dict:
    item = {
        "ref": entry["ref"],
        "outcome": "completed",
        "classification": dict(CLASSIFICATION),
        "draft_body": "Thanks for reaching out. Let me confirm that with the team.",
        "action_items": [],
    }
    item.update(over)
    return item


def failed(entry: dict, code: Any = "agent_ticket_error") -> dict:
    return {"ref": entry["ref"], "outcome": "failed", "failure_reason_code": code}


def result_of(*items: dict) -> dict:
    return {
        "run_id": RUN_ID,
        "status": "success",
        "tickets_processed": len(items),
        "drafts_created": len(items),
        "error_summary": None,
        "tickets": list(items),
    }


def run_context(world: OsWorld, run_id: str = RUN_ID) -> run_types.RunContext:
    scratch = world.root / "outputs" / "runs" / "os" / run_id
    return run_types.RunContext(
        run_id=run_id,
        mode="os",
        started_at=datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc),
        scratch_dir=scratch,
        result_path=scratch / "result.json",
        context_path=scratch / "context.json",
    )


def finalize(world: OsWorld, entries: list, result: Any, run_id: str = RUN_ID, **kwargs: Any) -> Any:
    kwargs.setdefault("settings", world.settings)
    return os_pass.finalize_os_run(run_context(world, run_id), world.deps(), entries, result, **kwargs)


def posts(world: OsWorld) -> list[tuple]:
    """Every write-back made, in order, as (OS ticket id, payload)."""
    return world.os_api.calls_to("post_classification")


def one_completed(world: OsWorld, script: list, index: int = 0) -> tuple[Any, dict]:
    """Finalize one completed ticket against a scripted OS. (summary, the entry)."""
    entry = make_entry(index)
    world.os_api.script = list(script)
    return finalize(world, [entry], result_of(completed(entry))), entry


def one_failed(world: OsWorld, script: list) -> tuple[Any, dict]:
    entry = make_entry(0)
    world.os_api.script = list(script)
    return finalize(world, [entry], result_of(failed(entry))), entry


def reread_answers(world: OsWorld, entry: dict, signal: Any = REREAD_SIGNAL) -> None:
    """Make the single-ticket route answer for the entry's ticket, with `signal`."""
    payload = osm._payload(0)
    payload["id"] = entry["os_ticket_id"]
    payload["signal"] = copy.deepcopy(signal)
    world.os_api.tickets[entry["os_ticket_id"]] = {
        "contract_version": "1",
        "ticket": payload,
        "unprocessed": True,
    }


# ------------------------------------------------------------------ the fakes


def test_the_draft_fake_has_the_real_writers_parameters() -> None:
    real = inspect.signature(client.replace_pending_draft).parameters
    fake = inspect.signature(OsWorld._replace_pending_draft).parameters
    assert [name for name in fake if name != "self"] == list(real)
    assert all(real[name].kind is inspect.Parameter.KEYWORD_ONLY for name in real)
    assert list(inspect.signature(OsWorld._mark_rerun_processed).parameters)[1:] == list(
        inspect.signature(client.mark_rerun_processed).parameters
    )


def test_the_scripted_os_api_plays_its_script_then_its_canned_result(world: OsWorld) -> None:
    world.os_api.script = [CONFLICT, E("list_timeout")]
    assert world.os_api.post_classification(osm.T1_OS_ID, {"outcome": "completed"}) == CONFLICT
    with pytest.raises(TicketsApiError):
        world.os_api.post_classification(osm.T1_OS_ID, {"outcome": "completed"})
    assert world.os_api.post_classification(osm.T1_OS_ID, {"outcome": "failed"})["status"] == 200
    assert world.timeline == [
        ("writeback", osm.T1_OS_ID, "completed"),
        ("writeback", osm.T1_OS_ID, "completed"),
        ("writeback", osm.T1_OS_ID, "failed"),
    ]


# -------------------------------------------------- normalise_classification


def test_a_valid_classification_is_returned_unchanged() -> None:
    assert os_pass.normalise_classification(dict(CLASSIFICATION)) == CLASSIFICATION
    nulls = dict(CLASSIFICATION, form_topic=None, reason="", confidence=0, priority="urgent")
    assert os_pass.normalise_classification(dict(nulls)) == nulls


def test_a_1400_character_reason_is_cut_to_1000() -> None:
    cut = os_pass.normalise_classification(dict(CLASSIFICATION, reason="r" * 1400))
    assert cut["reason"] == "r" * 1000


def test_the_reason_is_cut_by_code_point_as_the_os_counts_it() -> None:
    astral = chr(0x1F4E6)  # one code point, two UTF-16 units, four UTF-8 bytes
    cut = os_pass.normalise_classification(dict(CLASSIFICATION, reason=astral * 1400))
    assert cut["reason"] == astral * 1000 and len(cut["reason"]) == 1000


def test_a_260_character_form_topic_is_cut_to_200() -> None:
    cut = os_pass.normalise_classification(dict(CLASSIFICATION, form_topic="t" * 260))
    assert cut["form_topic"] == "t" * 200


@pytest.mark.parametrize("empty", ["", "   ", None])
def test_an_empty_form_topic_is_null(empty: Any) -> None:
    assert os_pass.normalise_classification(dict(CLASSIFICATION, form_topic=empty))["form_topic"] is None


def test_a_classification_with_no_form_topic_key_has_a_null_one() -> None:
    raw = {key: value for key, value in CLASSIFICATION.items() if key != "form_topic"}
    assert os_pass.normalise_classification(raw)["form_topic"] is None


@pytest.mark.parametrize(("given", "clamped"), [(1.4, 1), (-0.2, 0), (1, 1), (0, 0), (0.5, 0.5), (7, 1)])
def test_the_confidence_is_clamped_into_0_to_1(given: Any, clamped: Any) -> None:
    assert os_pass.normalise_classification(dict(CLASSIFICATION, confidence=given))["confidence"] == clamped


@pytest.mark.parametrize(("given", "word"), [("URGENT", "urgent"), (" Normal ", "normal"), ("urgent", "urgent")])
def test_the_priority_is_lowered_to_one_of_the_two_words(given: str, word: str) -> None:
    assert os_pass.normalise_classification(dict(CLASSIFICATION, priority=given))["priority"] == word


@pytest.mark.parametrize(
    "unusable",
    [
        dict(CLASSIFICATION, confidence="0.9"),
        dict(CLASSIFICATION, confidence=None),
        dict(CLASSIFICATION, confidence=True),
        dict(CLASSIFICATION, confidence=math.nan),
        dict(CLASSIFICATION, confidence=math.inf),
        dict(CLASSIFICATION, category="not_a_category"),
        dict(CLASSIFICATION, category="WISMO_TRACKING"),
        dict(CLASSIFICATION, category=None),
        {key: value for key, value in CLASSIFICATION.items() if key != "category"},
        dict(CLASSIFICATION, priority="high"),
        dict(CLASSIFICATION, priority=None),
        dict(CLASSIFICATION, reason=["a", "list"]),
        None,
        [],
        "wismo_tracking",
    ],
)
def test_a_classification_that_cannot_be_made_valid_is_none(unusable: Any) -> None:
    assert os_pass.normalise_classification(unusable) is None


def test_a_missing_reason_is_the_empty_string_and_text_the_os_cannot_store_is_removed() -> None:
    assert os_pass.normalise_classification(dict(CLASSIFICATION, reason=None))["reason"] == ""
    odd = "before" + chr(0) + "mid" + chr(0xD83D) + "after"  # a NUL and a lone surrogate
    cleaned = os_pass.normalise_classification(dict(CLASSIFICATION, reason=odd, form_topic=odd))
    for text in (cleaned["reason"], cleaned["form_topic"]):
        assert chr(0) not in text
        text.encode("utf-8")  # no lone surrogate is left: this does not raise
        assert text.startswith("before") and text.endswith("after")


def test_normalising_twice_changes_nothing_and_does_not_touch_its_input() -> None:
    raw = dict(CLASSIFICATION, reason="r" * 1400, priority="URGENT", confidence=1.4, extra="ignored")
    before = copy.deepcopy(raw)
    once = os_pass.normalise_classification(raw)
    assert raw == before
    assert list(once) == ["category", "confidence", "priority", "reason", "form_topic"]
    assert os_pass.normalise_classification(once) == once


# ------------------------------------------------------------- map_failure_code


@pytest.mark.parametrize("code", list(os_pass.PASS_FAILURE_CODES))
def test_each_of_the_six_codes_maps_to_itself(code: str) -> None:
    assert os_pass.map_failure_code(code) == code


@pytest.mark.parametrize("code", ["image_processing_error", "", None, 7, "AGENT_NO_RESULT", ["other"]])
def test_a_code_outside_the_six_becomes_other(code: Any) -> None:
    assert os_pass.map_failure_code(code) == "other"


# ------------------------------------------------- build_classification_payload


def test_the_payloads_signal_is_the_exact_string_the_list_supplied() -> None:
    entry = make_entry(0)
    entry["signal"] = {"kind": "client_reply", "id": osm.T1_SIGNAL_ID, "at": "2026-10-08T10:00:22.123Z"}
    payload = os_pass.build_classification_payload(
        entry, {"outcome": "completed", "classification": dict(CLASSIFICATION)}, RUN_ID, "os"
    )
    assert payload["signal"] == {"id": osm.T1_SIGNAL_ID, "at": "2026-10-08T10:00:22.123Z"}
    assert "kind" not in payload["signal"]
    failed_payload = os_pass.build_classification_payload(
        entry, {"outcome": "failed", "failure_reason_code": "weird"}, RUN_ID, "os"
    )
    assert failed_payload["signal"]["at"] == "2026-10-08T10:00:22.123Z"
    assert failed_payload["failure_reason_code"] == "other"
    assert "category" not in failed_payload


def test_the_builder_normalises_what_it_is_handed_and_refuses_what_it_cannot() -> None:
    entry = make_entry(0)
    payload = os_pass.build_classification_payload(
        entry, {"outcome": "completed", "classification": dict(CLASSIFICATION, reason="r" * 1400)}, RUN_ID, "os"
    )
    assert len(payload["reason"]) == 1000
    with pytest.raises(ValueError):
        os_pass.build_classification_payload(
            entry, {"outcome": "completed", "classification": {"category": "nope"}}, RUN_ID, "os"
        )
    with pytest.raises(ValueError):
        os_pass.build_classification_payload(dict(entry, signal=None), failed(entry), RUN_ID, "os")


EDGE_CLASSIFICATIONS = [
    dict(CLASSIFICATION),
    dict(CLASSIFICATION, reason="r" * 1400),
    dict(CLASSIFICATION, reason=chr(0x1F4E6) * 5000),
    dict(CLASSIFICATION, reason='"\\' * 4000),  # every character is escaped in JSON
    dict(CLASSIFICATION, reason=chr(0x7) * 3000),  # each is a six-character escape
    dict(CLASSIFICATION, form_topic=chr(0x1F4E6) * 260, reason=chr(0xFFFF) * 2000),
    dict(CLASSIFICATION, confidence=1.4),
    dict(CLASSIFICATION, confidence=-0.2),
    dict(CLASSIFICATION, confidence=1e308),
    dict(CLASSIFICATION, priority="URGENT", form_topic=""),
    dict(CLASSIFICATION, category="other_unclassified", reason=None, form_topic=None),
]


@pytest.mark.parametrize("raw", EDGE_CLASSIFICATIONS)
def test_no_normalised_classification_builds_a_body_the_contract_refuses(raw: dict) -> None:
    """The 400 and 413 rows are unreachable by content: whatever normalise
    returns, the body is inside every limit of the OS's fixture."""
    fixture = json.loads((FIXTURES / "tickets-api-classification.v1.json").read_text(encoding="utf-8"))
    limits = fixture["limits"]
    normalised = os_pass.normalise_classification(raw)
    assert normalised is not None
    body = os_pass.build_classification_payload(
        make_entry(0), {"outcome": "completed", "classification": normalised}, RUN_ID, "os"
    )
    assert set(body) == set(fixture["completed"])
    # the bytes the client sends (tickets_api._post's serialisation)
    sent = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert len(sent) <= limits["max_body_bytes"]
    assert len(json.dumps(body).encode("utf-8")) <= limits["max_body_bytes"]  # escaped, too
    assert isinstance(body["reason"], str) and len(body["reason"]) <= limits["reason_max_chars"]
    assert body["form_topic"] is None or 1 <= len(body["form_topic"]) <= limits["form_topic_max_chars"]
    assert not isinstance(body["confidence"], bool) and 0 <= body["confidence"] <= 1
    assert body["priority"] in ("urgent", "normal")
    assert body["category"] in os_pass.CATEGORY_SLUGS
    assert body["prompt_version"] == "v3.3.0" and body["model"] == os_pass.AGENT_MODEL


# ------------------------------------------------- a completed ticket, persisted


def test_a_completed_hubspot_linked_ticket_is_one_draft_then_one_write_back(world: OsWorld) -> None:
    summary, entry = one_completed(world, [OK])

    (draft,) = world.drafts
    assert draft["draft_key"] == osm.T1_HS_ID  # the HubSpot id stays the text key
    assert draft["os_ticket_id"] == osm.T1_OS_ID  # the OS id is set at insert
    assert draft["routine_name"] == "tickets-process"
    assert draft["base_prompt_version"] == "v3.3.0"
    assert draft["cycle_key"] == osm.T1_SIGNAL_ID
    assert draft["model"] == os_pass.AGENT_MODEL
    assert draft["draft_body"] == "Thanks for reaching out. Let me confirm that with the team."
    snapshot = draft["hubspot_ticket_snapshot"]
    assert snapshot["category"] == "wismo_tracking"
    assert snapshot["captured_at"] == "2026-10-09T12:00:00.000Z"
    assert snapshot["subject"] == entry["ticket_context"]["subject"]
    assert snapshot["body"] == entry["ticket_context"]["latest_customer_message"]
    assert snapshot["contact"] == {"name": "Pat Example", "email": "pat@example.test"}

    ((ticket_id, payload),) = posts(world)
    assert ticket_id == osm.T1_OS_ID
    assert payload["outcome"] == "completed" and payload["source_mode"] == "os"
    assert payload["prompt_version"] == "v3.3.0"  # no cycle suffix
    assert payload["run_id"] == RUN_ID
    assert payload["signal"] == {"id": osm.T1_SIGNAL_ID, "at": "2026-10-08T10:00:00.123Z"}
    assert (summary.tickets_processed, summary.drafts_created) == (1, 1)
    assert summary.codes == {} and summary.partial is False


def test_a_completed_ticket_with_no_hubspot_id_is_keyed_by_the_os_id_and_asks_hubspot_nothing(
    world: OsWorld,
) -> None:
    summary, entry = one_completed(world, [OK], index=1)

    (draft,) = world.drafts
    assert draft["draft_key"] == osm.T2_OS_ID == entry["ref"]
    assert draft["os_ticket_id"] == osm.T2_OS_ID
    assert draft["cycle_key"] == osm.T2_SIGNAL_ID
    ((ticket_id, payload),) = posts(world)
    assert ticket_id == osm.T2_OS_ID and payload["outcome"] == "completed"
    assert world.hubspot.requests == [] and world.token_reads == 0
    assert (summary.tickets_processed, summary.drafts_created) == (1, 1)


def test_the_draft_call_precedes_the_write_back_for_every_ticket(world: OsWorld) -> None:
    entries = [make_entry(0), make_entry(1), make_entry(2)]
    finalize(world, entries, result_of(*[completed(entry) for entry in entries]))

    # ticket 3's number backfill (a later step of the same pass) is in the log too
    assert [step[0] for step in world.timeline if step[0] != "backfill"] == ["draft", "writeback"] * 3
    for entry in entries:
        drafted = world.timeline.index(("draft", entry["ref"]))
        written_back = world.timeline.index(("writeback", entry["os_ticket_id"], "completed"))
        assert drafted < written_back


def test_an_unusable_classification_is_a_failed_ticket_with_no_draft(world: OsWorld) -> None:
    entry = make_entry(0)
    bad = completed(entry, classification=dict(CLASSIFICATION, category="made_up_category"))
    summary = finalize(world, [entry], result_of(bad))

    assert world.drafts == []
    ((ticket_id, payload),) = posts(world)
    assert ticket_id == osm.T1_OS_ID
    assert (payload["outcome"], payload["failure_reason_code"]) == ("failed", "agent_ticket_error")
    assert summary.tickets_processed == 0 and summary.drafts_created == 0
    assert summary.codes == {"classification_unusable": 1}


@pytest.mark.parametrize("body", ["", "   ", None, 7])
def test_a_completed_entry_with_no_draft_text_is_a_failed_ticket_never_a_processed_one(
    world: OsWorld, body: Any
) -> None:
    entry = make_entry(0)
    summary = finalize(world, [entry], result_of(completed(entry, draft_body=body)))

    assert world.drafts == []
    ((_ticket_id, payload),) = posts(world)
    assert (payload["outcome"], payload["failure_reason_code"]) == ("failed", "agent_ticket_error")
    assert summary.tickets_processed == 0


def test_a_draft_write_that_raises_is_a_failed_write_back_and_the_next_ticket_goes_on(
    world: OsWorld,
) -> None:
    first, second = make_entry(0), make_entry(1)
    world.draft_errors[first["ref"]] = RuntimeError(EXC_MARKER)
    summary = finalize(world, [first, second], result_of(completed(first), completed(second)))

    assert [draft["draft_key"] for draft in world.drafts] == [first["ref"], second["ref"]]
    (one, two) = posts(world)
    assert one[0] == osm.T1_OS_ID
    assert (one[1]["outcome"], one[1]["failure_reason_code"]) == ("failed", "draft_write_failed")
    assert two[0] == osm.T2_OS_ID and two[1]["outcome"] == "completed"
    assert not [p for p in posts(world) if p[0] == osm.T1_OS_ID and p[1]["outcome"] == "completed"]
    assert (summary.tickets_processed, summary.drafts_created) == (1, 1)
    assert summary.codes == {"draft_write_failed": 1}


@pytest.mark.parametrize(
    ("agent_code", "sent"),
    [
        ("agent_ticket_error", "agent_ticket_error"),
        ("other", "other"),
        ("image_processing_error", "other"),
        (None, "other"),
    ],
)
def test_a_ticket_the_result_marks_failed_is_one_failed_write_back_and_no_draft(
    world: OsWorld, agent_code: Any, sent: str
) -> None:
    entry = make_entry(0)
    summary = finalize(world, [entry], result_of(failed(entry, agent_code)))

    assert world.drafts == []
    ((ticket_id, payload),) = posts(world)
    assert ticket_id == osm.T1_OS_ID
    assert (payload["outcome"], payload["failure_reason_code"]) == ("failed", sent)
    assert payload["signal"] == {"id": osm.T1_SIGNAL_ID, "at": "2026-10-08T10:00:00.123Z"}
    assert summary.tickets_processed == 0 and summary.partial is False


def test_a_ticket_with_no_result_entry_is_one_failed_write_back_with_agent_no_result(
    world: OsWorld,
) -> None:
    first, second = make_entry(0), make_entry(1)
    summary = finalize(world, [first, second], result_of(completed(first)))

    assert [draft["draft_key"] for draft in world.drafts] == [first["ref"]]
    (one, two) = posts(world)
    assert one[1]["outcome"] == "completed"
    assert two[0] == osm.T2_OS_ID
    assert (two[1]["outcome"], two[1]["failure_reason_code"]) == ("failed", "agent_no_result")
    assert summary.tickets_processed == 1


def test_a_run_whose_agent_left_no_result_at_all_writes_nothing_back(world: OsWorld) -> None:
    """A run-level failure is not counted against each listed ticket: two agent
    outages would otherwise park the whole list (RUN-FAILURE-NOT-COUNTED)."""
    entries = [make_entry(0), make_entry(1)]
    summary = finalize(world, entries, None)

    assert world.drafts == [] and posts(world) == [] and world.timeline == []
    assert summary.tickets_processed == 0 and summary.codes == {}


def test_a_ticket_no_bundle_entry_could_be_built_for_is_written_back_as_bundle_build_failed(
    world: OsWorld,
) -> None:
    entry = make_entry(0)
    unbundled = [
        {"os_ticket_id": osm.T3_OS_ID, "signal": {"kind": "stage_reopen", "id": osm.T3_SIGNAL_ID, "at": "2026-10-09T07:20:30.456Z"}},
        {"os_ticket_id": osm.T2_OS_ID, "signal": None},  # nothing to echo: skipped
    ]
    finalize(world, [entry], None, unbundled=unbundled)

    ((ticket_id, payload),) = posts(world)
    assert ticket_id == osm.T3_OS_ID
    assert (payload["outcome"], payload["failure_reason_code"]) == ("failed", "bundle_build_failed")
    assert payload["signal"] == {"id": osm.T3_SIGNAL_ID, "at": "2026-10-09T07:20:30.456Z"}
    assert world.drafts == []


# ------------------------------------------ the status table: a `completed` write-back


def test_completed_200_is_processed_and_nothing_more(world: OsWorld) -> None:
    summary, _entry = one_completed(world, [OK])
    assert len(posts(world)) == 1 and world.sleeps == []
    assert summary.tickets[0]["writeback"] == "processed"
    assert summary.codes == {} and summary.partial is False


def test_completed_200_replayed_is_processed_and_nothing_more(world: OsWorld) -> None:
    summary, _entry = one_completed(world, [REPLAYED])
    assert len(posts(world)) == 1
    assert summary.tickets[0]["writeback"] == "processed" and summary.codes == {}


def test_completed_409_is_a_logged_code_and_not_a_run_failure(world: OsWorld) -> None:
    summary, _entry = one_completed(world, [CONFLICT])
    assert len(posts(world)) == 1 and world.sleeps == []
    assert summary.tickets[0]["writeback"] == "processed"
    assert summary.codes == {"writeback_conflict": 1}
    assert summary.partial is False and summary.tickets_processed == 1


@pytest.mark.parametrize("refusal", [BAD_PAYLOAD, TOO_LARGE], ids=["400", "413"])
def test_completed_400_or_413_is_followed_by_exactly_one_failed_write_back_with_other(
    world: OsWorld, refusal: dict
) -> None:
    summary, _entry = one_completed(world, [refusal, OK])

    (first, second) = posts(world)
    assert first[1]["outcome"] == "completed"
    assert second[0] == osm.T1_OS_ID
    assert (second[1]["outcome"], second[1]["failure_reason_code"]) == ("failed", "other")
    assert second[1]["signal"] == first[1]["signal"]
    assert summary.tickets[0]["writeback"] == "counted"
    assert summary.codes == {"writeback_rejected": 1}
    assert len(world.drafts) == 1  # the draft stays; nothing supersedes it


def test_completed_422_rereads_the_ticket_and_resends_once_with_that_signal(world: OsWorld) -> None:
    entry = make_entry(0)
    reread_answers(world, entry)
    world.os_api.script = [SIGNAL_AHEAD, OK]
    summary = finalize(world, [entry], result_of(completed(entry)))

    assert world.os_api.calls_to("fetch_ticket") == [(osm.T1_OS_ID,)]
    (first, second) = posts(world)
    assert first[1]["signal"]["id"] == osm.T1_SIGNAL_ID
    assert second[1]["outcome"] == "completed"
    assert second[1]["signal"] == {"id": REREAD_SIGNAL["id"], "at": REREAD_SIGNAL["at"]}
    assert {key: value for key, value in second[1].items() if key != "signal"} == {
        key: value for key, value in first[1].items() if key != "signal"
    }
    assert summary.tickets[0]["writeback"] == "processed" and summary.partial is False


def test_completed_422_then_409_on_the_resend_ends_it(world: OsWorld) -> None:
    entry = make_entry(0)
    reread_answers(world, entry)
    world.os_api.script = [SIGNAL_AHEAD, CONFLICT]
    summary = finalize(world, [entry], result_of(completed(entry)))

    assert len(posts(world)) == 2
    assert summary.tickets[0]["writeback"] == "processed"
    assert summary.codes == {"writeback_conflict": 1}


def test_completed_422_twice_is_one_failed_write_back_with_other_echoing_the_reread_signal(
    world: OsWorld,
) -> None:
    entry = make_entry(0)
    reread_answers(world, entry)
    world.os_api.script = [SIGNAL_AHEAD, SIGNAL_AHEAD, OK]
    summary = finalize(world, [entry], result_of(completed(entry)))

    assert world.os_api.calls_to("fetch_ticket") == [(osm.T1_OS_ID,)]  # one re-read, not two
    (_first, _second, third) = posts(world)
    assert (third[1]["outcome"], third[1]["failure_reason_code"]) == ("failed", "other")
    assert third[1]["signal"] == {"id": REREAD_SIGNAL["id"], "at": REREAD_SIGNAL["at"]}
    assert summary.tickets[0]["writeback"] == "counted"
    assert summary.codes == {"writeback_rejected": 1}


def test_completed_422_with_a_reread_that_finds_no_ticket_does_nothing_more(world: OsWorld) -> None:
    summary, _entry = one_completed(world, [SIGNAL_AHEAD])  # the single-ticket route answers 404

    assert world.os_api.calls_to("fetch_ticket") == [(osm.T1_OS_ID,)]
    assert len(posts(world)) == 1
    assert summary.tickets[0]["writeback"] == "gone"
    assert summary.codes == {"writeback_signal_gone": 1} and summary.partial is False


def test_completed_422_with_a_reread_that_finds_no_signal_does_nothing_more(world: OsWorld) -> None:
    entry = make_entry(0)
    reread_answers(world, entry, signal=None)
    world.os_api.script = [SIGNAL_AHEAD]
    summary = finalize(world, [entry], result_of(completed(entry)))

    assert len(posts(world)) == 1
    assert summary.tickets[0]["writeback"] == "gone"


def test_completed_404_is_a_logged_code_and_nothing_more(world: OsWorld) -> None:
    summary, _entry = one_completed(world, [NOT_FOUND])
    assert len(posts(world)) == 1 and world.os_api.calls_to("fetch_ticket") == []
    assert summary.tickets[0]["writeback"] == "gone"
    assert summary.codes == {"writeback_ticket_gone": 1} and summary.partial is False


def test_completed_401_is_collected_with_no_retry_and_the_run_is_partial(world: OsWorld) -> None:
    summary, _entry = one_completed(world, [E("signature_rejected", 401)])
    assert len(posts(world)) == 1 and world.sleeps == []
    assert summary.tickets[0]["writeback"] == "unrecorded"
    assert summary.codes == {"writeback_signature_rejected": 1} and summary.partial is True


def test_completed_503_with_the_secret_error_is_collected_with_no_retry_and_the_run_is_partial(
    world: OsWorld,
) -> None:
    summary, _entry = one_completed(world, [E("secret_missing", 503)])
    assert len(posts(world)) == 1 and world.sleeps == []
    assert summary.tickets[0]["writeback"] == "unrecorded"
    assert summary.codes == {"writeback_secret_missing": 1} and summary.partial is True


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (lambda: E("list_error", 500), "writeback_list_error"),
        (lambda: E("list_error", 503), "writeback_list_error"),
        (lambda: E("list_timeout"), "writeback_list_timeout"),
    ],
    ids=["500", "gateway-503", "timeout"],
)
def test_completed_5xx_or_a_transport_error_is_retried_exactly_once_then_collected(
    world: OsWorld, failure: Callable[[], Exception], code: str
) -> None:
    summary, _entry = one_completed(world, [failure(), failure(), OK])

    assert len(posts(world)) == 2  # the first and exactly one retry
    assert posts(world)[0] == posts(world)[1]
    assert world.sleeps == [os_pass.WRITEBACK_RETRY_PAUSE_S] == [2.0]
    assert summary.tickets[0]["writeback"] == "unrecorded"
    assert summary.codes == {code: 1} and summary.partial is True


def test_completed_timeout_whose_retry_answers_200_is_processed_and_the_status_is_unaffected(
    world: OsWorld,
) -> None:
    summary, _entry = one_completed(world, [E("list_timeout"), OK])
    assert len(posts(world)) == 2 and world.sleeps == [os_pass.WRITEBACK_RETRY_PAUSE_S]
    assert summary.tickets[0]["writeback"] == "processed"
    assert summary.codes == {} and summary.partial is False and summary.tickets_processed == 1


def test_a_write_back_that_cannot_be_delivered_does_not_undo_the_draft(world: OsWorld) -> None:
    summary, entry = one_completed(world, [E("list_timeout"), E("list_timeout")])

    assert [draft["draft_key"] for draft in world.drafts] == [entry["ref"]]  # one call, never a second
    assert [step for step in world.timeline if step[0] == "draft"] == [("draft", entry["ref"])]
    assert summary.tickets[0]["drafted"] is True and summary.tickets[0]["draft_id"] == "draft-1"
    assert (summary.tickets_processed, summary.drafts_created) == (1, 1)  # the draft is in place
    assert summary.partial is True


def test_an_exception_that_is_not_the_clients_is_retried_once_and_collected_as_list_error(
    world: OsWorld,
) -> None:
    summary, _entry = one_completed(world, [RuntimeError(EXC_MARKER), RuntimeError(EXC_MARKER)])
    assert len(posts(world)) == 2 and world.sleeps == [2.0]
    assert summary.codes == {"writeback_list_error": 1} and summary.partial is True


# --------------------------------------------- the status table: a `failed` write-back


def test_failed_200_is_counted_and_nothing_more(world: OsWorld) -> None:
    summary, _entry = one_failed(world, [OK])
    assert len(posts(world)) == 1 and world.sleeps == []
    assert summary.tickets[0]["writeback"] == "counted"
    assert summary.codes == {} and summary.partial is False


def test_failed_422_rereads_the_signal_and_resends_once(world: OsWorld) -> None:
    entry = make_entry(0)
    reread_answers(world, entry)
    world.os_api.script = [SIGNAL_AHEAD, OK]
    summary = finalize(world, [entry], result_of(failed(entry)))

    assert world.os_api.calls_to("fetch_ticket") == [(osm.T1_OS_ID,)]
    (_first, second) = posts(world)
    assert (second[1]["outcome"], second[1]["failure_reason_code"]) == ("failed", "agent_ticket_error")
    assert second[1]["signal"] == {"id": REREAD_SIGNAL["id"], "at": REREAD_SIGNAL["at"]}
    assert summary.tickets[0]["writeback"] == "counted"


def test_failed_422_twice_is_logged_and_not_sent_a_third_time(world: OsWorld) -> None:
    entry = make_entry(0)
    reread_answers(world, entry)
    world.os_api.script = [SIGNAL_AHEAD, SIGNAL_AHEAD, OK]
    summary = finalize(world, [entry], result_of(failed(entry)))

    assert len(posts(world)) == 2
    assert summary.tickets[0]["writeback"] == "rejected"
    assert summary.codes == {"writeback_rejected": 1}


@pytest.mark.parametrize("refusal", [BAD_PAYLOAD, CONFLICT, TOO_LARGE], ids=["400", "409", "413"])
def test_failed_400_409_or_413_is_logged_as_rejected_and_not_resent(world: OsWorld, refusal: dict) -> None:
    summary, _entry = one_failed(world, [refusal, OK])
    assert len(posts(world)) == 1
    assert summary.tickets[0]["writeback"] == "rejected"
    assert summary.codes == {"writeback_rejected": 1} and summary.partial is False


def test_failed_404_is_a_logged_code(world: OsWorld) -> None:
    summary, _entry = one_failed(world, [NOT_FOUND])
    assert len(posts(world)) == 1
    assert summary.tickets[0]["writeback"] == "gone"
    assert summary.codes == {"writeback_ticket_gone": 1}


def test_failed_timeout_is_retried_exactly_once(world: OsWorld) -> None:
    summary, _entry = one_failed(world, [E("list_timeout"), E("list_timeout"), OK])
    assert len(posts(world)) == 2 and world.sleeps == [os_pass.WRITEBACK_RETRY_PAUSE_S]
    assert summary.tickets[0]["writeback"] == "unrecorded"
    assert summary.codes == {"writeback_list_timeout": 1} and summary.partial is True


def test_failed_401_is_collected_with_no_retry(world: OsWorld) -> None:
    summary, _entry = one_failed(world, [E("signature_rejected", 401)])
    assert len(posts(world)) == 1 and world.sleeps == []
    assert summary.codes == {"writeback_signature_rejected": 1} and summary.partial is True


# ---------------------------------------------------------------- reruns


def test_a_rerun_entry_uses_the_rerun_cycle_key_and_marks_each_request_with_the_draft_id(
    world: OsWorld,
) -> None:
    entry = make_entry(0, requests=(osm.RERUN_A, osm.RERUN_B))
    assert entry["cycle_key"] == f"rerun:{osm.RERUN_A}"
    finalize(world, [entry], result_of(completed(entry)))

    (draft,) = world.drafts
    assert draft["cycle_key"] == f"rerun:{osm.RERUN_A}"
    assert world.rerun_marks == [(osm.RERUN_A, "draft-1"), (osm.RERUN_B, "draft-1")]
    assert world.timeline.index(("draft", entry["ref"])) < world.timeline.index(("rerun_mark", osm.RERUN_A))
    ((_ticket_id, payload),) = posts(world)
    assert payload["signal"]["id"] == osm.T1_SIGNAL_ID  # the signal its payload carried


def test_a_rerun_entry_with_no_client_signal_is_drafted_and_not_written_back(world: OsWorld) -> None:
    def no_signal(payload: dict) -> None:
        payload["signal"] = None

    entry = make_entry(0, requests=(osm.RERUN_A,), payload_edit=no_signal)
    assert entry["signal"] is None
    summary = finalize(world, [entry], result_of(completed(entry)))

    assert len(world.drafts) == 1 and posts(world) == []
    assert world.rerun_marks == [(osm.RERUN_A, "draft-1")]
    assert summary.tickets[0]["writeback"] == "no_signal"
    assert (summary.tickets_processed, summary.drafts_created) == (1, 1) and summary.partial is False


def test_a_rerun_mark_that_fails_is_a_code_and_the_ticket_is_still_written_back(world: OsWorld) -> None:
    world.rerun_mark_error = RuntimeError(EXC_MARKER)
    entry = make_entry(0, requests=(osm.RERUN_A,))
    summary = finalize(world, [entry], result_of(completed(entry)))

    assert len(posts(world)) == 1 and posts(world)[0][1]["outcome"] == "completed"
    assert summary.codes == {"rerun_mark_failed": 1} and summary.partial is False


def test_a_failed_rerun_ticket_marks_no_request(world: OsWorld) -> None:
    entry = make_entry(0, requests=(osm.RERUN_A,))
    finalize(world, [entry], result_of(failed(entry)))
    assert world.rerun_marks == [] and world.drafts == []


# ------------------------------------------------- overlap, dry run, the summary


def test_the_same_bundle_finalised_twice_is_one_draft_and_a_replay(world: OsWorld) -> None:
    entry = make_entry(0)
    world.os_api.script = [OK, REPLAYED]
    first = finalize(world, [entry], result_of(completed(entry)), run_id=RUN_ID)
    second = finalize(world, [copy.deepcopy(entry)], result_of(completed(entry)), run_id=OTHER_RUN_ID)

    assert [draft["cycle_key"] for draft in world.drafts] == [osm.T1_SIGNAL_ID, osm.T1_SIGNAL_ID]
    assert (first.drafts_created, second.drafts_created) == (1, 0)  # new rows only
    assert (first.tickets_processed, second.tickets_processed) == (1, 1)
    assert second.tickets[0]["created"] is False and second.tickets[0]["draft_id"] == "draft-1"
    (one, two) = posts(world)  # the write-back is idempotent on the OS's side
    assert {k: v for k, v in one[1].items() if k != "run_id"} == {k: v for k, v in two[1].items() if k != "run_id"}
    assert (one[1]["run_id"], two[1]["run_id"]) == (RUN_ID, OTHER_RUN_ID)
    assert world.rerun_marks == []


def test_a_dry_run_writes_no_draft_and_no_write_back_and_counts_what_it_would_do(world: OsWorld) -> None:
    world.settings["dry_run"] = True
    entries = [make_entry(0), make_entry(1), make_entry(0, requests=(osm.RERUN_A,))]
    entries[2]["ref"] = "9300000000009"
    result = result_of(completed(entries[0]), failed(entries[1]), completed(entries[2]))
    summary = finalize(world, entries, result)

    assert world.drafts == [] and posts(world) == [] and world.rerun_marks == []
    assert world.timeline == [] and world.hubspot.requests == []
    assert summary.dry_run is True
    assert summary.would["drafts"] == 2 and summary.would["writebacks"] == 3
    assert (summary.tickets_processed, summary.drafts_created) == (2, 0)


def test_settings_the_finalizer_cannot_read_persist_nothing(world: OsWorld) -> None:
    world.settings = RuntimeError(EXC_MARKER)
    entry = make_entry(0)
    summary = finalize(world, [entry], result_of(completed(entry)), settings=None)

    assert world.drafts == [] and posts(world) == []
    assert summary.tickets_processed == 0
    assert summary.codes == {"finalize_settings_unreadable": 1}


def test_one_tickets_unexpected_error_does_not_stop_the_next(world: OsWorld) -> None:
    first, second = make_entry(0), make_entry(1)
    first["ticket_context"] = None  # the snapshot builder cannot read this
    summary = finalize(world, [first, second], result_of(completed(first), completed(second)))

    assert [draft["draft_key"] for draft in world.drafts] == [second["ref"]]
    assert [(p[0], p[1]["outcome"]) for p in posts(world)] == [
        (osm.T1_OS_ID, "failed"),
        (osm.T2_OS_ID, "completed"),
    ]
    assert posts(world)[0][1]["failure_reason_code"] == "draft_write_failed"
    assert summary.tickets_processed == 1


def test_the_summary_counts_and_no_ticket_text_reaches_a_log_stdout_or_stderr(
    world: OsWorld, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    entries = [make_entry(0), make_entry(1), make_entry(2)]
    marked = [
        completed(
            entry,
            draft_body=f"Dear client, {MARKER} your parcel is on its way.",
            classification=dict(CLASSIFICATION, reason=f"the client wrote {MARKER}", form_topic=f"topic {MARKER}"),
        )
        for entry in entries
    ]
    for entry in entries:
        entry["ticket_context"]["latest_customer_message"] = f"hello {MARKER}"
        entry["ticket_context"]["subject"] = f"subject {MARKER}"
    world.draft_errors[entries[1]["ref"]] = RuntimeError(f"db said {MARKER}")
    world.os_api.script = [CONFLICT, OK, E("list_timeout"), E("list_timeout")]

    live = finalize(world, copy.deepcopy(entries), result_of(*marked))
    world.settings["dry_run"] = True
    dry = finalize(world, copy.deepcopy(entries), result_of(*marked), run_id=OTHER_RUN_ID)

    assert MARKER in world.drafts[0]["draft_body"]  # the text did reach the draft writer
    assert MARKER in posts(world)[0][1]["reason"]  # and the write-back
    assert (live.tickets_processed, live.drafts_created) == (2, 2)
    assert live.codes == {"writeback_conflict": 1, "draft_write_failed": 1, "writeback_list_timeout": 1}
    assert live.partial is True
    assert [ticket["ref"] for ticket in live.tickets] == [entry["ref"] for entry in entries]
    assert MARKER not in repr(live) and MARKER not in repr(dry)
    assert MARKER not in (os_pass.summary_text(live) or "") + (os_pass.summary_text(dry) or "")
    assert dry.dry_run is True and dry.would["drafts"] == 3
    fakes.assert_marker_absent(MARKER, caplog, capfd)


# -------------------------------------------------- through the whole orchestrator


def test_an_os_run_ends_with_one_draft_and_one_completed_write_back_per_ticket(world: OsWorld) -> None:
    assert world.run() == 0

    record = world.record
    assert (record["status"], record["tickets_processed"], record["drafts_created"]) == ("success", 3, 3)
    assert [draft["draft_key"] for draft in world.drafts] == [osm.T1_HS_ID, osm.T2_OS_ID, osm.T3_HS_ID]
    assert [draft["os_ticket_id"] for draft in world.drafts] == [osm.T1_OS_ID, osm.T2_OS_ID, osm.T3_OS_ID]
    assert [(p[0], p[1]["outcome"], p[1]["source_mode"]) for p in posts(world)] == [
        (osm.T1_OS_ID, "completed", "os"),
        (osm.T2_OS_ID, "completed", "os"),
        (osm.T3_OS_ID, "completed", "os"),
    ]
    assert {p[1]["run_id"] for p in posts(world)} == {record["run_id"]}
    assert [p[1]["signal"]["id"] for p in posts(world)] == [osm.T1_SIGNAL_ID, osm.T2_SIGNAL_ID, osm.T3_SIGNAL_ID]
    assert world.timeline.index(("draft", osm.T1_HS_ID)) < world.timeline.index(
        ("writeback", osm.T1_OS_ID, "completed")
    )
    assert world.order == ["agent"]  # the finalizer ran after the agent, in the same run
    assert world.draft_writes == []  # never a plain writer


def test_an_undeliverable_write_back_makes_the_runs_one_record_partial_with_its_code(
    world: OsWorld,
) -> None:
    world.os_api.script = [E("list_timeout"), E("list_timeout")]
    assert world.run() == 0

    record = world.record
    assert record["status"] == "partial"
    assert (record["tickets_processed"], record["drafts_created"]) == (3, 3)
    assert "writeback_list_timeout=1" in record["error_summary"]
    assert world.sleeps == [os_pass.WRITEBACK_RETRY_PAUSE_S]
    assert len(world.drafts) == 3


def test_a_409_leaves_the_run_a_success_with_the_code_in_its_summary(world: OsWorld) -> None:
    world.os_api.script = [CONFLICT]
    world.run()
    assert world.record["status"] == "success"
    assert "writeback_conflict=1" in world.record["error_summary"]


def test_a_ticket_whose_draft_cannot_be_written_makes_the_run_partial(world: OsWorld) -> None:
    world.draft_errors[osm.T2_OS_ID] = RuntimeError(EXC_MARKER)
    world.run()

    record = world.record
    assert (record["status"], record["tickets_processed"], record["drafts_created"]) == ("partial", 2, 2)
    assert "draft_write_failed=1" in record["error_summary"]
    assert EXC_MARKER not in json.dumps(record, default=str)


def test_an_agent_that_left_no_result_persists_nothing_and_writes_nothing_back(world: OsWorld) -> None:
    world.agent = osm.OsAgent(exit_code=1, write=False)
    world.run()

    record = world.record
    assert (record["status"], record["failure_reason_code"]) == ("failure", "agent_launch_failed")
    assert (record["tickets_processed"], record["drafts_created"]) == (0, 0)
    assert world.drafts == [] and posts(world) == []


def test_a_dry_run_through_the_orchestrator_persists_nothing(world: OsWorld) -> None:
    world.settings["dry_run"] = True
    world.run()

    record = world.record
    assert world.drafts == [] and posts(world) == []
    assert (record["status"], record["drafts_created"]) == ("success", 0)
    assert "dry_run" in record["error_summary"] and "would_drafts=3" in record["error_summary"]


def test_a_finalizer_that_raises_is_a_code_on_the_one_record_and_never_a_crash(
    world: OsWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(EXC_MARKER)

    monkeypatch.setattr(os_pass, "finalize_os_run", explode)
    assert world.run() == 0

    record = world.record
    assert record["status"] == "partial"
    assert "finalize_failed:RuntimeError" in record["error_summary"]
    assert EXC_MARKER not in json.dumps(record, default=str)
    assert record["hubspot_calls"]["kinds"]["email_thread"] == 2  # the counts are still taken


def test_a_fail_closed_run_never_reaches_the_finalizer(world: OsWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    real = os_pass.finalize_os_run
    monkeypatch.setattr(os_pass, "finalize_os_run", lambda *a, **k: calls.append(a) or real(*a, **k))
    world.os_api.fail_next_list("list_timeout")
    world.run()

    assert calls == [] and world.drafts == [] and posts(world) == []
    assert world.record["failure_reason_code"] == "list_timeout"
