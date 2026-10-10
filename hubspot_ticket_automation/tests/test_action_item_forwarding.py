"""Action items carry the OS ticket id and the pass id; the number backfill by code
(Phase 93, plan 17).

Three pieces, each tested here:

    scripts/post_action_items.py     `build_envelope`: today's three keys, plus
                                     `os_ticket_id` and `pass_id` only when given;
                                     the HubSpot id key is omitted, never null. The
                                     dry-run path reports a type, a count and a
                                     size, never the envelope.
    scripts/forward_action_items.py  the seen-set and the retry file beside it:
                                     a retry of an unconfirmed post carries the OS
                                     ticket id and the pass id; a record with no
                                     retry entry is sent exactly as before.
    ticket_pipeline/os_pass.py       the finalizer posts a completed ticket's
                                     action items with both ids and the cycle key
                                     as `pass_id`, writes the retry entry BEFORE
                                     the post, and fills an empty order or tracking
                                     number in HubSpot from the form block.

The envelope fixture is a byte copy of Pack'N OS's
docs/contracts/fixtures/action-items-envelope.v1.json (its checksum is pinned in
tests/test_contract_fixtures.py). Every file a test writes is under the pytest
temp directory; nothing is sent, and nothing under config/ is opened.
"""
from __future__ import annotations

import copy
import inspect
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

import pytest

from tests import test_os_mode_run as osm
from tests.helpers import fakes
from tests.test_os_finalize import (
    CLASSIFICATION,
    OK,
    OTHER_RUN_ID,
    completed,
    failed,
    finalize,
    make_entry,
    posts,
    result_of,
)
from tests.test_os_mode_run import (  # noqa: F401 - fixtures, used by name
    OsWorld,
    _no_child_process,
    _no_real_file_and_a_clean_counter,
    make_world,
    world,
)
from ticket_pipeline import hubspot_api, os_pass

import forward_action_items as fwd  # scripts/, put on the path by test_os_mode_run
import post_action_items as poster

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REAL_POST = poster._post  # taken at import, before any world replaces it
MARKER = "ZZ-MARKER-93-17-ITEMS"
FAKE_SECRET = "test-action-ingest-secret-not-a-real-one"

ITEM_A = {
    "action_type": "warehouse_investigation",
    "description": "Check the pick record.",
    "owner_hint": "warehouse",
    "severity": "normal",
    "needs_hubspot_reply": False,
}
ITEM_B = {
    "action_type": "file_carrier_claim",
    "description": "File a lost-parcel claim.",
    "owner_hint": "carrier",
    "severity": "urgent",
    "needs_hubspot_reply": False,
}

# Captured from the functions BEFORE this plan's edit (post_action_items for
# ticket "123", and the forwarder for a queue record keyed "9300000000001", each
# with [ITEM_A]): the exact bytes that were signed and sent.
TODAY_POSTER_BYTES = (
    b'{"type":"action_items","ticket_id":"123","items":[{"action_type":"warehouse_investigation",'
    b'"description":"Check the pick record.","owner_hint":"warehouse","severity":"normal",'
    b'"needs_hubspot_reply":false}]}'
)
TODAY_FORWARDER_BYTES = (
    b'{"type":"action_items","ticket_id":"9300000000001","items":[{"action_type":"warehouse_investigation",'
    b'"description":"Check the pick record.","owner_hint":"warehouse","severity":"normal",'
    b'"needs_hubspot_reply":false}]}'
)
T1_KEY_A = f"{osm.T1_HS_ID}:warehouse_investigation"
T1_KEY_B = f"{osm.T1_HS_ID}:file_carrier_claim"
T2_KEY_A = f"{osm.T2_OS_ID}:warehouse_investigation"
UNCONFIRMED = {"ok": True, "mode": "sent", "status": 200, "response": {"ok": True, "emitted": 2, "errors": 1}}
POST_FAILED = {"ok": False, "mode": "error", "status": 503, "error": "unavailable"}


def sent_bytes(envelope: dict) -> bytes:
    """The bytes `_post` signs and sends for an envelope (its one serialisation)."""
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")


def envelope_fixture() -> dict:
    return json.loads((FIXTURES / "action-items-envelope.v1.json").read_bytes().decode("utf-8"))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_queue(world: OsWorld, *records: dict) -> Path:
    path = world.base / "pending_actions.json"
    path.write_text(json.dumps(list(records)), encoding="utf-8")
    return path


def run_forwarder(world: OsWorld, queue: Path) -> int:
    return fwd.main(queue_path=queue, seen_path=world.seen_path)


def retry_file(world: OsWorld) -> Path:
    return world.seen_path.with_name(".action_items_retry.json")


def seen_of(world: OsWorld) -> list:
    return read_json(world.seen_path) if world.seen_path.exists() else []


def retry_of(world: OsWorld) -> dict:
    return read_json(retry_file(world)) if retry_file(world).exists() else {}


# ------------------------------------------------------------- build_envelope


@pytest.mark.parametrize(
    ("body", "ticket_id"),
    [
        ("hubspot_id_only", "9300000000001"),
        ("both_ids_with_pass_id", "9300000000001"),
        ("os_id_only_with_pass_id", None),
    ],
)
def test_build_envelope_reproduces_each_body_of_the_os_fixture(body: str, ticket_id: Optional[str]) -> None:
    expected = envelope_fixture()[body]
    built = poster.build_envelope(
        ticket_id,
        copy.deepcopy(expected["items"]),
        os_ticket_id=expected.get("os_ticket_id"),
        pass_id=expected.get("pass_id"),
    )
    assert built == expected
    assert list(built) == list(expected)  # the key order of the fixture, too
    assert sent_bytes(built) == sent_bytes(expected)


def test_the_envelope_with_the_hubspot_id_alone_is_todays_three_keys_byte_for_byte() -> None:
    built = poster.build_envelope("123", [dict(ITEM_A)])
    assert list(built) == ["type", "ticket_id", "items"]
    assert sent_bytes(built) == TODAY_POSTER_BYTES


def test_the_envelope_with_both_ids_and_a_pass_id_carries_all_three() -> None:
    built = poster.build_envelope("123", [dict(ITEM_A)], os_ticket_id=osm.T1_OS_ID, pass_id=osm.T1_SIGNAL_ID)
    assert list(built) == ["type", "ticket_id", "os_ticket_id", "pass_id", "items"]
    assert (built["ticket_id"], built["os_ticket_id"], built["pass_id"]) == (
        "123",
        osm.T1_OS_ID,
        osm.T1_SIGNAL_ID,
    )


@pytest.mark.parametrize("absent", [None, "", "   "])
def test_an_envelope_with_no_hubspot_id_has_no_ticket_id_key_at_all(absent: Any) -> None:
    built = poster.build_envelope(absent, [dict(ITEM_A)], os_ticket_id=osm.T2_OS_ID, pass_id=osm.T2_SIGNAL_ID)
    assert "ticket_id" not in built  # omitted, never null: the OS refuses null
    assert list(built) == ["type", "os_ticket_id", "pass_id", "items"]
    assert "null" not in sent_bytes(built).decode("utf-8")


def test_an_envelope_with_no_pass_id_or_no_os_id_omits_that_key() -> None:
    assert "pass_id" not in poster.build_envelope("123", [dict(ITEM_A)], os_ticket_id=osm.T1_OS_ID)
    assert "os_ticket_id" not in poster.build_envelope("123", [dict(ITEM_A)], pass_id="rerun:abc")
    assert "pass_id" not in poster.build_envelope("123", [dict(ITEM_A)], os_ticket_id=osm.T1_OS_ID, pass_id="")


def test_post_action_items_hands_both_ids_and_the_pass_id_to_the_one_serialisation(world: OsWorld) -> None:
    result = poster.post_action_items(
        "123", [dict(ITEM_A)], False, os_ticket_id=osm.T1_OS_ID, pass_id=osm.T1_SIGNAL_ID
    )
    assert result["ok"] is True
    (envelope,) = world.ingest.posts
    assert envelope == poster.build_envelope(
        "123", [dict(ITEM_A)], os_ticket_id=osm.T1_OS_ID, pass_id=osm.T1_SIGNAL_ID
    )
    # the three-argument call of before this plan still works and sends the same bytes
    poster.post_action_items("123", [dict(ITEM_A)], False)
    assert sent_bytes(world.ingest.posts[-1]) == TODAY_POSTER_BYTES


def test_post_action_items_sends_nothing_with_no_items_or_no_ticket_id(world: OsWorld) -> None:
    assert poster.post_action_items("123", [], False)["mode"] == "noop"
    skipped = poster.post_action_items(None, [dict(ITEM_A)], False)
    assert (skipped["ok"], skipped["mode"]) == (False, "skipped")
    assert world.ingest.posts == []


# --------------------------------------------------- the dry-run path prints no text


def _real_dry_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """One dry-run post through the REAL `_post`, with a fake secret under tmp."""
    secret = tmp_path / "fake_action_ingest_secret.txt"
    secret.write_text(FAKE_SECRET, encoding="utf-8")
    monkeypatch.setattr(poster, "SECRET_PATH", secret)
    monkeypatch.setattr(poster, "_post", REAL_POST)
    items = [dict(ITEM_A, description=f"Call the client about {MARKER}"), dict(ITEM_B)]
    return poster.post_action_items("123", items, True, os_ticket_id=osm.T1_OS_ID, pass_id=osm.T1_SIGNAL_ID)


def test_a_dry_run_post_puts_no_envelope_text_in_a_log_stdout_or_stderr(
    world: OsWorld,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    result = _real_dry_run(monkeypatch, tmp_path)
    assert (result["ok"], result["mode"]) == (True, "dry-run")
    # the CLI prints this result, so it holds no envelope either
    assert MARKER not in json.dumps(result, default=str)
    assert "payload" not in result
    fakes.assert_marker_absent(MARKER, caplog, capfd)


def test_the_dry_run_line_reports_the_type_the_item_count_and_the_byte_length(
    world: OsWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture
) -> None:
    result = _real_dry_run(monkeypatch, tmp_path)
    _out, err = capfd.readouterr()
    lines = [line for line in err.splitlines() if "DRY-RUN" in line]
    assert len(lines) == 1
    assert "type=action_items" in lines[0] and "items=2" in lines[0]
    assert f"bytes={result['bytes']}" in lines[0] and result["items"] == 2
    assert "Call the client" not in err and "description" not in err


# ----------------------------------------------- the seen-set and the retry file


def test_the_retry_file_sits_beside_the_seen_set_and_follows_its_path(tmp_path: Path) -> None:
    assert fwd.retry_path(tmp_path / "a" / "seen.json") == tmp_path / "a" / ".action_items_retry.json"
    assert fwd.RETRY_FILE_NAME == ".action_items_retry.json"
    assert fwd.retry_path().parent == fwd.SEEN_PATH.parent  # the module's own seen-set, by default


def test_mark_forwarded_merges_into_the_seen_set(tmp_path: Path) -> None:
    seen = tmp_path / "seen.json"
    seen.write_text(json.dumps(["1:a", "2:b"]), encoding="utf-8")
    assert fwd.mark_forwarded(["2:b", "3:c"], seen_path=seen) is True
    assert read_json(seen) == ["1:a", "2:b", "3:c"]
    assert fwd.load_seen(seen) == {"1:a", "2:b", "3:c"}
    assert fwd.mark_forwarded(["0:z"], seen_path=tmp_path / "new" / "seen.json") is False  # no folder: not raised


def test_the_retry_helpers_merge_and_forget(tmp_path: Path) -> None:
    seen = tmp_path / "seen.json"
    first = {"1:a": {"os_ticket_id": osm.T1_OS_ID, "pass_id": osm.T1_SIGNAL_ID}}
    second = {"2:b": {"os_ticket_id": osm.T2_OS_ID, "pass_id": "rerun:" + osm.RERUN_A}}
    assert fwd.load_retry(seen) == {}
    assert fwd.remember_retry(first, seen_path=seen) is True
    assert fwd.remember_retry(second, seen_path=seen) is True
    assert fwd.load_retry(seen) == {**first, **second}
    assert read_json(tmp_path / ".action_items_retry.json") == {**first, **second}
    assert fwd.forget_retry(["1:a", "9:not-there"], seen_path=seen) is True
    assert fwd.load_retry(seen) == second
    assert not seen.exists()  # the retry helpers never touch the seen-set


@pytest.mark.parametrize("content", [None, "", "{not json", "[1, 2]", '"text"', '{"1:a": "not an object"}'])
def test_a_missing_or_unparseable_retry_file_is_empty(tmp_path: Path, content: Optional[str]) -> None:
    seen = tmp_path / "seen.json"
    if content is not None:
        (tmp_path / ".action_items_retry.json").write_text(content, encoding="utf-8")
    assert fwd.load_retry(seen) == {}
    entry = {"2:b": {"os_ticket_id": osm.T2_OS_ID, "pass_id": osm.T2_SIGNAL_ID}}
    assert fwd.remember_retry(entry, seen_path=seen) is True
    assert fwd.load_retry(seen) == entry


@pytest.mark.parametrize("helper", ["mark_forwarded", "remember_retry", "forget_retry"])
def test_each_writer_goes_through_a_temp_file_and_a_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helper: str
) -> None:
    seen = tmp_path / "seen.json"
    fwd.remember_retry({"1:a": {"os_ticket_id": osm.T1_OS_ID}}, seen_path=seen)
    renames: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(source: Any, target: Any) -> None:
        assert Path(source).exists() and Path(source) != Path(target)
        renames.append((Path(source).name, Path(target).name))
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", spy)
    argument: Any = {"1:a": {"os_ticket_id": osm.T1_OS_ID}} if helper == "remember_retry" else ["1:a"]
    assert getattr(fwd, helper)(argument, seen_path=seen) is True

    target = "seen.json" if helper == "mark_forwarded" else ".action_items_retry.json"
    assert renames == [(target + ".tmp", target)]
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")) == []


def test_the_pass_id_rule_is_the_os_fixtures_matched_whole() -> None:
    rule = envelope_fixture()["pass_id_rule"]
    assert "^" + fwd.PASS_ID_RE.pattern + "$" == rule["pattern"]
    for example in rule["examples"]:
        assert fwd.valid_pass_id(example) is True
    assert fwd.valid_pass_id("a" * rule["max_chars"]) is True
    assert fwd.valid_pass_id("a" * (rule["max_chars"] + 1)) is False
    for refused in ("", None, 7, "v3.3.0#" + osm.T1_SIGNAL_ID, osm.T1_SIGNAL_ID + chr(10), "a b"):
        assert fwd.valid_pass_id(refused) is False  # a trailing line feed included
    assert fwd.is_os_ticket_id(osm.T2_OS_ID) is True and fwd.is_os_ticket_id(osm.T2_OS_ID.upper()) is True
    for refused in (osm.T1_HS_ID, "", None, osm.T2_OS_ID + chr(10), "rerun:" + osm.T2_OS_ID):
        assert fwd.is_os_ticket_id(refused) is False


def test_a_post_is_confirmed_only_by_ok_and_zero_body_errors() -> None:
    sent = {"ok": True, "mode": "sent", "status": 200, "response": {"emitted": 2, "errors": 0}}
    assert fwd.confirmed(sent) is True
    assert fwd.confirmed({"ok": True, "mode": "sent", "status": 200, "response": {}}) is True
    assert fwd.confirmed(UNCONFIRMED) is False  # HTTP 200 is not proof
    assert fwd.confirmed(POST_FAILED) is False
    assert fwd.confirmed({"ok": False, "mode": "skipped", "reason": "secret_unprovisioned"}) is False
    assert fwd.confirmed(None) is False


# ------------------------------------------------------------- the forwarder loop


def test_the_forwarder_sends_a_record_with_no_retry_entry_exactly_as_before(world: OsWorld) -> None:
    queue = write_queue(world, {"ticket_id": osm.T1_HS_ID, "action_items": [dict(ITEM_A)]})
    assert run_forwarder(world, queue) == 0

    (envelope,) = world.ingest.posts
    assert sent_bytes(envelope) == TODAY_FORWARDER_BYTES
    assert world.ingest.dry_runs == [False]
    assert seen_of(world) == [T1_KEY_A]


def test_the_forwarder_sends_a_uuid_shaped_key_as_the_os_ticket_id_only(world: OsWorld) -> None:
    queue = write_queue(world, {"ticket_id": osm.T2_OS_ID, "action_items": [dict(ITEM_A)]})
    run_forwarder(world, queue)

    (envelope,) = world.ingest.posts
    assert list(envelope) == ["type", "os_ticket_id", "items"]
    assert envelope["os_ticket_id"] == osm.T2_OS_ID
    assert seen_of(world) == [T2_KEY_A]


def test_a_forwarder_retry_carries_the_os_id_and_the_pass_id_of_the_retry_file(world: OsWorld) -> None:
    held = {"os_ticket_id": osm.T1_OS_ID, "pass_id": osm.T1_SIGNAL_ID}
    fwd.remember_retry({T1_KEY_A: dict(held), T1_KEY_B: dict(held)}, seen_path=world.seen_path)
    queue = write_queue(world, {"ticket_id": osm.T1_HS_ID, "action_items": [dict(ITEM_A), dict(ITEM_B)]})
    run_forwarder(world, queue)

    (envelope,) = world.ingest.posts
    assert list(envelope) == ["type", "ticket_id", "os_ticket_id", "pass_id", "items"]
    assert envelope["ticket_id"] == osm.T1_HS_ID  # the record's digit key
    assert (envelope["os_ticket_id"], envelope["pass_id"]) == (held["os_ticket_id"], held["pass_id"])
    assert [item["action_type"] for item in envelope["items"]] == ["warehouse_investigation", "file_carrier_claim"]
    assert seen_of(world) == sorted([T1_KEY_A, T1_KEY_B])
    assert retry_of(world) == {}  # confirmed: the entries are dropped


def test_a_forwarder_retry_for_a_ticket_with_no_hubspot_id_has_no_hubspot_key(world: OsWorld) -> None:
    fwd.remember_retry(
        {T2_KEY_A: {"os_ticket_id": osm.T2_OS_ID, "pass_id": osm.T2_SIGNAL_ID}}, seen_path=world.seen_path
    )
    queue = write_queue(world, {"ticket_id": osm.T2_OS_ID, "action_items": [dict(ITEM_A)]})
    run_forwarder(world, queue)

    (envelope,) = world.ingest.posts
    assert "ticket_id" not in envelope
    assert (envelope["os_ticket_id"], envelope["pass_id"]) == (osm.T2_OS_ID, osm.T2_SIGNAL_ID)
    assert seen_of(world) == [T2_KEY_A] and retry_of(world) == {}


@pytest.mark.parametrize("answer", [UNCONFIRMED, POST_FAILED], ids=["200-with-errors", "failed-post"])
def test_an_unconfirmed_forward_marks_nothing_seen_and_keeps_its_retry_entry(
    world: OsWorld, answer: dict
) -> None:
    held = {T1_KEY_A: {"os_ticket_id": osm.T1_OS_ID, "pass_id": osm.T1_SIGNAL_ID}}
    fwd.remember_retry(held, seen_path=world.seen_path)
    world.ingest.script = [answer]
    queue = write_queue(world, {"ticket_id": osm.T1_HS_ID, "action_items": [dict(ITEM_A)]})
    assert run_forwarder(world, queue) == 0

    assert seen_of(world) == [] and retry_of(world) == held
    run_forwarder(world, queue)  # the next run sends the same envelope, with the same pass id
    assert world.ingest.posts[0] == world.ingest.posts[1]
    assert seen_of(world) == [T1_KEY_A] and retry_of(world) == {}


def test_a_key_held_in_the_retry_file_is_sent_again_even_when_an_earlier_pass_marked_it_seen(
    world: OsWorld,
) -> None:
    """A later pass re-created the item and its post was not confirmed: the
    seen-set still holds the key from the first pass, and the retry must not be
    swallowed by it."""
    fwd.mark_forwarded([T1_KEY_A, T1_KEY_B], seen_path=world.seen_path)
    fwd.remember_retry(
        {T1_KEY_A: {"os_ticket_id": osm.T1_OS_ID, "pass_id": "rerun:" + osm.RERUN_A}}, seen_path=world.seen_path
    )
    queue = write_queue(world, {"ticket_id": osm.T1_HS_ID, "action_items": [dict(ITEM_A), dict(ITEM_B)]})
    run_forwarder(world, queue)

    (envelope,) = world.ingest.posts
    assert [item["action_type"] for item in envelope["items"]] == ["warehouse_investigation"]
    assert envelope["pass_id"] == "rerun:" + osm.RERUN_A
    assert retry_of(world) == {} and seen_of(world) == sorted([T1_KEY_A, T1_KEY_B])


def test_items_of_one_record_held_under_different_passes_go_in_separate_envelopes(world: OsWorld) -> None:
    fwd.remember_retry(
        {T1_KEY_A: {"os_ticket_id": osm.T1_OS_ID, "pass_id": osm.T1_SIGNAL_ID}}, seen_path=world.seen_path
    )
    queue = write_queue(world, {"ticket_id": osm.T1_HS_ID, "action_items": [dict(ITEM_A), dict(ITEM_B)]})
    run_forwarder(world, queue)

    assert len(world.ingest.posts) == 2
    by_type = {post["items"][0]["action_type"]: post for post in world.ingest.posts}
    assert by_type["warehouse_investigation"]["pass_id"] == osm.T1_SIGNAL_ID
    assert list(by_type["file_carrier_claim"]) == ["type", "ticket_id", "items"]  # as before this plan


def test_a_retry_entry_the_os_would_refuse_is_not_sent_as_it_is(world: OsWorld) -> None:
    retry_file(world).write_text(
        json.dumps({T1_KEY_A: {"os_ticket_id": "not-a-uuid", "pass_id": "v3.3.0#" + osm.T1_SIGNAL_ID}}),
        encoding="utf-8",
    )
    queue = write_queue(world, {"ticket_id": osm.T1_HS_ID, "action_items": [dict(ITEM_A)]})
    run_forwarder(world, queue)

    (envelope,) = world.ingest.posts
    assert sent_bytes(envelope) == TODAY_FORWARDER_BYTES  # neither bad value is sent


def test_the_forwarder_never_raises_and_prints_its_one_summary_line(
    world: OsWorld, capfd: pytest.CaptureFixture
) -> None:
    world.seen_path.write_text(json.dumps([T1_KEY_A]), encoding="utf-8")
    queue = write_queue(
        world,
        {"ticket_id": osm.T1_HS_ID, "action_items": [dict(ITEM_A), dict(ITEM_B, description=f"about {MARKER}")]},
        "not a record",
        {"ticket_id": "", "action_items": [dict(ITEM_A)]},
    )
    assert run_forwarder(world, queue) == 0
    _out, err = capfd.readouterr()
    assert "done: forwarded=1 already_delivered=1 errors=0 records=3" in err
    assert MARKER not in err
    assert fwd.main(queue_path=world.base / "no-such-queue.json", seen_path=world.seen_path) == 0
    assert list(inspect.signature(fwd.main).parameters) == ["queue_path", "seen_path"]


# -------------------------------------------- the finalizer: a ticket's action items


def with_items(entry: dict, *items: dict) -> dict:
    return completed(entry, action_items=[dict(item) for item in items])


def test_a_completed_linked_ticket_posts_its_items_once_with_both_ids_and_the_cycle_key(
    world: OsWorld,
) -> None:
    entry = make_entry(0)
    # the seen-set already holds both keys (an earlier pass): it is NOT consulted
    fwd.mark_forwarded([T1_KEY_A, T1_KEY_B], seen_path=world.seen_path)
    summary = finalize(world, [entry], result_of(with_items(entry, ITEM_A, ITEM_B)))

    (envelope,) = world.ingest.posts
    assert envelope == poster.build_envelope(
        osm.T1_HS_ID, [ITEM_A, ITEM_B], os_ticket_id=osm.T1_OS_ID, pass_id=osm.T1_SIGNAL_ID
    )
    assert envelope["pass_id"] == entry["cycle_key"]
    assert world.ingest.dry_runs == [False]
    assert seen_of(world) == sorted([T1_KEY_A, T1_KEY_B])
    assert retry_of(world) == {}
    assert summary.tickets[0]["action_items"] == "confirmed" and summary.codes == {}


def test_the_retry_entries_are_on_disk_before_the_post_is_attempted(world: OsWorld) -> None:
    entry = make_entry(0)
    at_post: list[dict] = []
    world.ingest.before = lambda _envelope: at_post.append(retry_of(world))
    finalize(world, [entry], result_of(with_items(entry, ITEM_A, ITEM_B)))

    held = {"os_ticket_id": osm.T1_OS_ID, "pass_id": osm.T1_SIGNAL_ID}
    assert at_post == [{T1_KEY_A: held, T1_KEY_B: held}]
    assert retry_of(world) == {}  # and gone once the post was confirmed


@pytest.mark.parametrize("answer", [UNCONFIRMED, POST_FAILED], ids=["200-with-errors", "failed-post"])
def test_an_unconfirmed_post_adds_nothing_to_the_seen_set_and_leaves_its_retry_entries(
    world: OsWorld, answer: dict
) -> None:
    entry = make_entry(0)
    world.ingest.script = [answer]
    summary = finalize(world, [entry], result_of(with_items(entry, ITEM_A, ITEM_B)))

    held = {"os_ticket_id": osm.T1_OS_ID, "pass_id": osm.T1_SIGNAL_ID}
    assert seen_of(world) == []
    assert retry_of(world) == {T1_KEY_A: held, T1_KEY_B: held}
    assert summary.codes == {"action_items_unconfirmed": 1}
    assert summary.tickets[0]["action_items"] == "unconfirmed"
    assert summary.tickets_processed == 1 and summary.partial is False  # the draft and the write-back stand
    assert len(world.ingest.posts) == 1  # the finalizer does not resend; the forwarder does


def test_the_forwarder_retries_an_unconfirmed_finaliser_post_with_the_os_id_and_the_pass_id(
    world: OsWorld,
) -> None:
    entry = make_entry(0)
    world.ingest.script = [UNCONFIRMED]
    finalize(world, [entry], result_of(with_items(entry, ITEM_A, ITEM_B)))
    # what the agent queued for the digest: keyed by the bundle entry's ref
    queue = write_queue(world, {"ticket_id": entry["ref"], "action_items": [dict(ITEM_A), dict(ITEM_B)]})
    run_forwarder(world, queue)

    first, retried = world.ingest.posts
    assert retried == first  # the same envelope, the same pass id
    assert (retried["ticket_id"], retried["os_ticket_id"], retried["pass_id"]) == (
        osm.T1_HS_ID,
        osm.T1_OS_ID,
        osm.T1_SIGNAL_ID,
    )
    assert seen_of(world) == sorted([T1_KEY_A, T1_KEY_B]) and retry_of(world) == {}


# ---------------------------------------- a retry is resent from its own pass (review WR-46)
#
# The queue keeps one record per pass of a ticket, oldest first, and a queue
# record carries no pass id. A retry entry names the pass whose post is owed.
# The forwarder used to attach that pass id to the FIRST record of the ticket
# that held the action type: the stale text of an earlier pass went out under
# the new pass id, the entry was dropped, and the current item was never sent.

SECOND_SIGNAL = {"kind": "client_reply", "id": "6a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d", "at": "2026-10-09T09:15:00.250Z"}
D1 = dict(ITEM_A, description="D1 (pass 1, stale)")
D2 = dict(ITEM_A, description="D2 (pass 2, current)")
D3 = dict(ITEM_A, description="D3 (pass 3)")


def second_pass_entry() -> dict:
    """Ticket 1 again, after a newer client message: the same ref, a new cycle key."""
    entry = make_entry(0, payload_edit=lambda payload: payload.update(signal=copy.deepcopy(SECOND_SIGNAL)))
    assert entry["cycle_key"] == SECOND_SIGNAL["id"] != osm.T1_SIGNAL_ID
    return entry


def two_passes_the_second_unconfirmed(world: OsWorld) -> tuple[dict, dict]:
    """Pass 1 posts D1 and is confirmed. Pass 2 posts D2 and is NOT confirmed."""
    first, second = make_entry(0), second_pass_entry()
    assert first["ref"] == second["ref"] == osm.T1_HS_ID
    finalize(world, [first], result_of(with_items(first, D1)))
    world.ingest.script = [UNCONFIRMED]
    finalize(world, [second], result_of(with_items(second, D2)), run_id=OTHER_RUN_ID)
    assert [(post["pass_id"], post["items"]) for post in world.ingest.posts] == [
        (osm.T1_SIGNAL_ID, [D1]),
        (SECOND_SIGNAL["id"], [D2]),
    ]
    assert seen_of(world) == [T1_KEY_A]  # pass 1's delivery
    assert retry_of(world) == {T1_KEY_A: {"os_ticket_id": osm.T1_OS_ID, "pass_id": SECOND_SIGNAL["id"]}}
    return first, second


def test_an_unconfirmed_second_pass_is_resent_from_its_own_queue_record_not_the_first_passes(
    world: OsWorld,
) -> None:
    """The review's probe. Before the fix the forwarder made one post: D1, the
    stale text, under pass 2's id; D2 was never sent."""
    first, _second = two_passes_the_second_unconfirmed(world)
    # what the agent queued: one record per pass, oldest first, keyed by the ref
    queue = write_queue(
        world,
        {"ticket_id": first["ref"], "action_items": [dict(D1)]},
        {"ticket_id": first["ref"], "action_items": [dict(D2)]},
    )
    run_forwarder(world, queue)

    resent = world.ingest.posts[2:]
    assert [(post["pass_id"], post["items"]) for post in resent] == [(SECOND_SIGNAL["id"], [D2])]
    assert resent[0] == world.ingest.posts[1]  # the same envelope as the unconfirmed post
    stale_under_the_new_pass = [
        post for post in world.ingest.posts if post.get("pass_id") == SECOND_SIGNAL["id"] and D1 in post["items"]
    ]
    assert stale_under_the_new_pass == []
    assert retry_of(world) == {} and seen_of(world) == [T1_KEY_A]

    run_forwarder(world, queue)  # and nothing is left owed
    assert len(world.ingest.posts) == 3


def test_a_resend_that_fails_again_still_never_sends_the_earlier_passes_item(world: OsWorld) -> None:
    first, _second = two_passes_the_second_unconfirmed(world)
    queue = write_queue(
        world,
        {"ticket_id": first["ref"], "action_items": [dict(D1)]},
        {"ticket_id": first["ref"], "action_items": [dict(D2)]},
    )
    world.ingest.script = [POST_FAILED]
    run_forwarder(world, queue)

    assert [post["items"] for post in world.ingest.posts[2:]] == [[D2]]
    assert retry_of(world) == {T1_KEY_A: {"os_ticket_id": osm.T1_OS_ID, "pass_id": SECOND_SIGNAL["id"]}}
    run_forwarder(world, queue)  # the next run: the same item, the same pass id, confirmed
    assert [(post["pass_id"], post["items"]) for post in world.ingest.posts[2:]] == [
        (SECOND_SIGNAL["id"], [D2]),
        (SECOND_SIGNAL["id"], [D2]),
    ]
    assert retry_of(world) == {}


def test_a_later_record_of_the_ticket_without_that_action_type_does_not_hide_the_owed_item(
    world: OsWorld,
) -> None:
    first, _second = two_passes_the_second_unconfirmed(world)
    fwd.mark_forwarded([T1_KEY_B], seen_path=world.seen_path)  # the third record's item is delivered already
    queue = write_queue(
        world,
        {"ticket_id": first["ref"], "action_items": [dict(D1)]},
        {"ticket_id": first["ref"], "action_items": [dict(D2)]},
        {"ticket_id": first["ref"], "action_items": [dict(ITEM_B)]},
        {"ticket_id": osm.T3_HS_ID, "action_items": [dict(D3)]},  # another ticket's record, later still
    )
    run_forwarder(world, queue)

    resent = world.ingest.posts[2:]
    assert [(post["ticket_id"], post.get("pass_id"), post["items"]) for post in resent] == [
        (osm.T1_HS_ID, SECOND_SIGNAL["id"], [D2]),
        (osm.T3_HS_ID, None, [D3]),  # no retry entry: exactly as before
    ]


def test_a_retry_entry_with_no_pass_id_is_matched_as_it_always_was(world: OsWorld) -> None:
    """An entry that names no pass (the cycle key was not one the OS accepts)
    keeps the earlier rule, unchanged by the fix: the first record of the
    ticket that holds the action type is sent, with the OS ticket id and no
    pass id, and its confirmation drops the entry."""
    fwd.mark_forwarded([T1_KEY_A], seen_path=world.seen_path)
    fwd.remember_retry({T1_KEY_A: {"os_ticket_id": osm.T1_OS_ID}}, seen_path=world.seen_path)
    queue = write_queue(
        world,
        {"ticket_id": osm.T1_HS_ID, "action_items": [dict(D1)]},
        {"ticket_id": osm.T1_HS_ID, "action_items": [dict(D2)]},
    )
    run_forwarder(world, queue)

    assert [(post["os_ticket_id"], "pass_id" in post, post["items"]) for post in world.ingest.posts] == [
        (osm.T1_OS_ID, False, [D1]),
    ]
    assert retry_of(world) == {}


def test_a_confirmed_resend_does_not_drop_an_entry_a_newer_pass_wrote_meanwhile(world: OsWorld) -> None:
    """The pass is part of a retry entry's identity. If, while the forwarder is
    posting pass 2's item, another run records pass 3 as owed under the same
    key, the confirmation of pass 2 must not erase pass 3's entry."""
    first, _second = two_passes_the_second_unconfirmed(world)
    queue = write_queue(
        world,
        {"ticket_id": first["ref"], "action_items": [dict(D1)]},
        {"ticket_id": first["ref"], "action_items": [dict(D2)]},
    )
    third = {"os_ticket_id": osm.T1_OS_ID, "pass_id": "rerun:" + osm.RERUN_A}
    world.ingest.before = lambda _envelope: fwd.remember_retry({T1_KEY_A: dict(third)}, seen_path=world.seen_path)
    run_forwarder(world, queue)

    assert world.ingest.posts[2]["pass_id"] == SECOND_SIGNAL["id"]
    assert retry_of(world) == {T1_KEY_A: third}  # pass 3 is still owed


def test_the_finaliser_drops_only_the_entry_of_the_pass_it_confirmed(world: OsWorld) -> None:
    entry = make_entry(0)
    newer = {"os_ticket_id": osm.T1_OS_ID, "pass_id": SECOND_SIGNAL["id"]}
    # at the moment of pass 1's post another run records a newer pass under the same key
    world.ingest.before = lambda _envelope: fwd.remember_retry({T1_KEY_A: dict(newer)}, seen_path=world.seen_path)
    summary = finalize(world, [entry], result_of(with_items(entry, D1)))

    assert summary.tickets[0]["action_items"] == "confirmed"
    assert retry_of(world) == {T1_KEY_A: newer}


def test_forget_retry_with_passes_drops_an_entry_only_when_it_still_names_that_pass(tmp_path: Path) -> None:
    seen = tmp_path / "seen.json"
    held = {
        "1:a": {"os_ticket_id": osm.T1_OS_ID, "pass_id": "p1"},
        "2:b": {"os_ticket_id": osm.T2_OS_ID, "pass_id": "p9"},
        "3:c": {"os_ticket_id": osm.T3_OS_ID},  # held with no pass id
    }
    fwd.remember_retry(held, seen_path=seen)
    assert fwd.forget_retry(["1:a", "2:b", "3:c"], seen_path=seen, passes={"1:a": "p1", "2:b": "p2", "3:c": None})
    assert fwd.load_retry(seen) == {"2:b": held["2:b"]}  # p9 is not the pass that was confirmed
    # with no `passes` the keys are dropped whatever they name, as before
    assert fwd.forget_retry(["2:b"], seen_path=seen) is True
    assert fwd.load_retry(seen) == {}


def test_the_forwarder_forwards_nothing_the_finaliser_confirmed(
    world: OsWorld, capfd: pytest.CaptureFixture
) -> None:
    entry = make_entry(0)
    finalize(world, [entry], result_of(with_items(entry, ITEM_A, ITEM_B)))
    queue = write_queue(world, {"ticket_id": entry["ref"], "action_items": [dict(ITEM_A), dict(ITEM_B)]})
    capfd.readouterr()
    run_forwarder(world, queue)

    assert len(world.ingest.posts) == 1  # the finalizer's; the forwarder sent none
    assert "forwarded=0 already_delivered=2 errors=0" in capfd.readouterr().err


def test_a_completed_ticket_with_no_action_items_posts_nothing_and_remembers_nothing(world: OsWorld) -> None:
    entry = make_entry(0)
    summary = finalize(world, [entry], result_of(completed(entry)))
    assert world.ingest.posts == [] and not retry_file(world).exists() and not world.seen_path.exists()
    assert summary.tickets[0]["action_items"] is None


def test_a_ticket_with_no_hubspot_id_posts_the_os_id_only_and_keys_by_it(world: OsWorld) -> None:
    entry = make_entry(1)
    finalize(world, [entry], result_of(with_items(entry, ITEM_A)))

    (envelope,) = world.ingest.posts
    assert list(envelope) == ["type", "os_ticket_id", "pass_id", "items"]
    assert (envelope["os_ticket_id"], envelope["pass_id"]) == (osm.T2_OS_ID, osm.T2_SIGNAL_ID)
    assert seen_of(world) == [T2_KEY_A]
    assert world.hubspot.requests == []


def test_a_rerun_passes_its_rerun_cycle_key_as_the_pass_id(world: OsWorld) -> None:
    entry = make_entry(0, requests=(osm.RERUN_A,))
    finalize(world, [entry], result_of(with_items(entry, ITEM_A)))
    assert world.ingest.posts[0]["pass_id"] == f"rerun:{osm.RERUN_A}"


def test_a_failed_ticket_and_an_undrafted_one_post_no_action_items(world: OsWorld) -> None:
    first, second = make_entry(0), make_entry(1)
    world.draft_errors[second["ref"]] = RuntimeError("no")
    result = result_of(dict(failed(first), action_items=[dict(ITEM_A)]), with_items(second, ITEM_A))
    finalize(world, [first, second], result)
    assert world.ingest.posts == [] and not retry_file(world).exists()


def test_a_dry_run_takes_the_posters_dry_run_path_and_writes_no_retry_entry(world: OsWorld) -> None:
    world.settings["dry_run"] = True
    entry = make_entry(0)
    summary = finalize(world, [entry], result_of(with_items(entry, ITEM_A, ITEM_B)))

    assert world.ingest.dry_runs == [True]  # the poster's own dry-run branch: nothing is sent
    assert not retry_file(world).exists() and not world.seen_path.exists()
    assert summary.would["action_item_posts"] == 1


def test_items_that_are_not_action_items_are_not_sent(world: OsWorld) -> None:
    entry = make_entry(0)
    odd = completed(entry, action_items=["text", {"description": "no type"}, dict(ITEM_A), None])
    finalize(world, [entry], result_of(odd))
    (envelope,) = world.ingest.posts
    assert envelope["items"] == [ITEM_A]
    world.ingest.posts.clear()
    finalize(world, [entry], result_of(completed(entry, action_items="not a list")), run_id=OTHER_RUN_ID)
    assert world.ingest.posts == []


def test_the_order_per_ticket_is_draft_write_back_action_items_backfill(world: OsWorld) -> None:
    entry = make_entry(2)  # ticket 3: linked, and its numbers are only in the form block
    finalize(world, [entry], result_of(with_items(entry, ITEM_A)))
    assert world.timeline == [
        ("draft", osm.T3_HS_ID),
        ("writeback", osm.T3_OS_ID, "completed"),
        ("action_items", osm.T3_HS_ID),
        ("backfill", osm.T3_HS_ID),
    ]


def test_no_action_item_text_reaches_a_log_stdout_or_stderr_live_or_dry(
    world: OsWorld, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    entry = make_entry(0)
    marked = with_items(entry, dict(ITEM_A, description=f"Tell the dock {MARKER}"), ITEM_B)
    world.ingest.script = [UNCONFIRMED]
    finalize(world, [copy.deepcopy(entry)], result_of(marked))
    queue = write_queue(world, {"ticket_id": entry["ref"], "action_items": marked["action_items"]})
    run_forwarder(world, queue)
    world.settings["dry_run"] = True
    finalize(world, [copy.deepcopy(entry)], result_of(marked), run_id=OTHER_RUN_ID)

    assert MARKER in world.ingest.posts[0]["items"][0]["description"]  # the text did reach the poster
    assert MARKER not in json.dumps(retry_of(world)) + json.dumps(seen_of(world))  # ids only on disk
    fakes.assert_marker_absent(MARKER, caplog, capfd)


# ------------------------------------------------------------ the number backfill


def source(payload: tuple, form_block: tuple) -> dict:
    return {
        "payload": {"order_number": payload[0], "tracking_number": payload[1]},
        "form_block": {"order_number": form_block[0], "tracking_number": form_block[1]},
    }


@pytest.mark.parametrize(
    ("backfill_source", "expected"),
    [
        (source((None, None), ("FX-ORDER-9", None)), {"order_number": "FX-ORDER-9"}),
        (source((None, "FX-TRACK-1"), ("FX-ORDER-9", "FX-TRACK-OTHER")), {"order_number": "FX-ORDER-9"}),
        (
            source((None, None), ("FX-ORDER-9", "FX-TRACK-9")),
            {"order_number": "FX-ORDER-9", "tracking_number": "FX-TRACK-9"},
        ),
        (source(("FX-ORDER-1", None), ("FX-ORDER-OTHER", None)), {}),  # the payload already holds it
        (source(("FX-ORDER-1", "FX-TRACK-1"), ("FX-ORDER-1", "FX-TRACK-1")), {}),
        (source((None, None), (None, None)), {}),
        (source(("", "  "), ("FX-ORDER-9", "  ")), {"order_number": "FX-ORDER-9"}),  # blank is empty
    ],
)
def test_backfill_properties_are_the_numbers_empty_in_the_payload_and_held_by_the_form_block(
    backfill_source: dict, expected: dict
) -> None:
    entry = {"hubspot_ticket_id": osm.T1_HS_ID, "backfill_source": backfill_source}
    assert os_pass.backfill_properties(entry) == expected


@pytest.mark.parametrize("hubspot_id", [None, "", "  "])
def test_a_ticket_with_no_hubspot_id_has_nothing_to_backfill(hubspot_id: Any) -> None:
    entry = {"hubspot_ticket_id": hubspot_id, "backfill_source": source((None, None), ("FX-ORDER-9", "FX-TRACK-9"))}
    assert os_pass.backfill_properties(entry) == {}


@pytest.mark.parametrize("broken", [None, {}, {"payload": None, "form_block": None}, {"payload": {}}, "text"])
def test_an_entry_with_no_usable_backfill_source_has_nothing_to_backfill(broken: Any) -> None:
    assert os_pass.backfill_properties({"hubspot_ticket_id": osm.T1_HS_ID, "backfill_source": broken}) == {}


def test_the_backfill_reads_the_unmerged_source_never_the_contexts_merged_form_fields() -> None:
    """The trap: the context's form fields hold a number whether it came from the
    payload or from the form block, so they cannot say what was empty."""
    merged_from_payload = make_entry(0)  # ticket 1: the payload holds both numbers
    assert merged_from_payload["ticket_context"]["form_fields"]["order_number"] == "FX-ORDER-1001"
    assert merged_from_payload["backfill_source"]["payload"]["order_number"] == "FX-ORDER-1001"
    assert os_pass.backfill_properties(merged_from_payload) == {}

    from_the_form_block_only = make_entry(2)  # ticket 3: only its form block holds them
    assert from_the_form_block_only["ticket_context"]["form_fields"]["order_number"] == "FX-ORDER-3003"
    assert from_the_form_block_only["backfill_source"]["payload"]["order_number"] is None
    assert os_pass.backfill_properties(from_the_form_block_only) == {
        "order_number": "FX-ORDER-3003",
        "tracking_number": "FX-TRACK-3003",
    }

    # a context that lies cannot move the decision either way
    merged_from_payload["ticket_context"]["form_fields"]["order_number"] = None
    from_the_form_block_only["ticket_context"]["form_fields"] = {}
    assert os_pass.backfill_properties(merged_from_payload) == {}
    assert len(os_pass.backfill_properties(from_the_form_block_only)) == 2


def test_a_backfill_is_one_property_update_and_one_counted_call(world: OsWorld) -> None:
    entry = make_entry(2)
    summary = finalize(world, [entry], result_of(completed(entry)))

    assert world.hubspot.patches == [
        (osm.T3_HS_ID, {"order_number": "FX-ORDER-3003", "tracking_number": "FX-TRACK-3003"})
    ]
    assert world.hubspot.requests == [("PATCH", f"/crm/v3/objects/tickets/{osm.T3_HS_ID}")]
    assert world.hubspot.unexpected == []
    assert hubspot_api.snapshot_calls() == {
        "kinds": dict(osm.ZERO_KINDS, number_backfill=1),
        "raw_requests": 1,
    }
    assert world.token_reads == 1  # through the injected reader, once
    assert summary.tickets[0]["backfilled"] == ["order_number", "tracking_number"]
    assert summary.codes == {}


def test_only_the_empty_property_is_written(world: OsWorld) -> None:
    entry = make_entry(2)
    entry["backfill_source"]["payload"]["tracking_number"] = "FX-TRACK-ALREADY"
    finalize(world, [entry], result_of(completed(entry)))
    assert world.hubspot.patches == [(osm.T3_HS_ID, {"order_number": "FX-ORDER-3003"})]


def test_a_second_pass_whose_payload_now_carries_the_numbers_writes_nothing(world: OsWorld) -> None:
    first = make_entry(2)
    finalize(world, [first], result_of(completed(first)))
    assert len(world.hubspot.patches) == 1

    def now_mirrored(payload: dict) -> None:
        payload["order_number"], payload["tracking_number"] = "FX-ORDER-3003", "FX-TRACK-3003"

    second = make_entry(2, payload_edit=now_mirrored)
    finalize(world, [second], result_of(completed(second)), run_id=OTHER_RUN_ID)
    assert len(world.hubspot.patches) == 1  # zero updates on the reprocess
    assert hubspot_api.snapshot_calls()["kinds"]["number_backfill"] == 1


def test_a_ticket_with_no_hubspot_id_and_a_dry_run_update_nothing(world: OsWorld) -> None:
    unlinked = make_entry(1)
    unlinked["backfill_source"] = source((None, None), ("FX-ORDER-9", "FX-TRACK-9"))
    finalize(world, [unlinked], result_of(completed(unlinked)))
    assert world.hubspot.requests == [] and world.token_reads == 0

    world.settings["dry_run"] = True
    entry = make_entry(2)
    summary = finalize(world, [entry], result_of(completed(entry)), run_id=OTHER_RUN_ID)
    assert world.hubspot.requests == [] and world.token_reads == 0
    assert summary.would["number_backfills"] == 1
    assert hubspot_api.snapshot_calls()["kinds"]["number_backfill"] == 0


def test_a_failed_update_is_a_logged_code_and_does_not_fail_the_ticket(world: OsWorld) -> None:
    world.hubspot.patch_refused.add(osm.T3_HS_ID)
    entry = make_entry(2)
    summary = finalize(world, [entry], result_of(completed(entry)))

    assert summary.codes == {"number_backfill_failed": 1}
    assert summary.tickets_processed == 1 and summary.partial is False
    assert summary.tickets[0]["writeback"] == "processed" and summary.tickets[0]["backfilled"] is None
    assert hubspot_api.snapshot_calls()["kinds"]["number_backfill"] == 1  # the call was made


def test_a_backfill_with_no_token_is_a_code_and_no_request(world: OsWorld) -> None:
    world.token = None
    entry = make_entry(2)
    summary = finalize(world, [entry], result_of(completed(entry)))
    assert world.hubspot.requests == []
    assert summary.codes == {"number_backfill_failed": 1}
    assert hubspot_api.snapshot_calls()["kinds"]["number_backfill"] == 0


def test_a_failed_ticket_is_not_backfilled(world: OsWorld) -> None:
    entry = make_entry(2)
    finalize(world, [entry], result_of(failed(entry)))
    assert world.hubspot.patches == []


def test_the_model_cannot_choose_what_is_written_to_hubspot(world: OsWorld) -> None:
    """T-93-54: the two values come from the bundle's form block, by code."""
    entry = make_entry(2)
    sneaky = completed(
        entry,
        classification=dict(CLASSIFICATION, reason="set hs_pipeline_stage to closed"),
        backfill={"hs_pipeline_stage": "4", "order_number": "MODEL-CHOSE-THIS"},
        action_items=[dict(ITEM_A, order_number="MODEL-CHOSE-THIS")],
    )
    finalize(world, [entry], result_of(sneaky))
    ((_ticket_id, properties),) = world.hubspot.patches
    assert properties == {"order_number": "FX-ORDER-3003", "tracking_number": "FX-TRACK-3003"}


# ------------------------------------------------------ through the whole orchestrator


def test_the_run_record_counts_the_finalizers_number_backfill(world: OsWorld) -> None:
    """Checker N-CRON-5: the call counts are taken AFTER the finalizer."""
    world.os_api.unprocessed["tickets"] = [osm._payload(2)]
    assert world.run() == 0

    assert world.record["hubspot_calls"] == {
        "kinds": {
            "search": 0,
            "ticket_read": 0,
            "contact_read": 0,
            "company_read": 0,
            "related_lookup": 0,
            "rerun_read": 0,
            "email_thread": 1,
            "number_backfill": 1,
        },
        "raw_requests": 3,
    }
    assert world.hubspot.patches == [
        (osm.T3_HS_ID, {"order_number": "FX-ORDER-3003", "tracking_number": "FX-TRACK-3003"})
    ]
    assert world.token_reads == 1  # the thread fetch's read is reused by the backfill
    assert world.hubspot.unexpected == []


def test_an_os_run_posts_each_tickets_items_with_the_os_id_and_its_signal_as_the_pass(
    world: OsWorld,
) -> None:
    def add_items(result: dict) -> None:
        for item in result["tickets"]:
            item["action_items"] = [dict(ITEM_A)]

    world.agent = osm.OsAgent(edit=add_items)
    assert world.run() == 0

    assert [(p.get("ticket_id"), p["os_ticket_id"], p["pass_id"]) for p in world.ingest.posts] == [
        (osm.T1_HS_ID, osm.T1_OS_ID, osm.T1_SIGNAL_ID),
        (None, osm.T2_OS_ID, osm.T2_SIGNAL_ID),
        (osm.T3_HS_ID, osm.T3_OS_ID, osm.T3_SIGNAL_ID),
    ]
    assert seen_of(world) == sorted(
        [T1_KEY_A, T2_KEY_A, f"{osm.T3_HS_ID}:warehouse_investigation"]
    )
    assert world.record["status"] == "success"


def test_two_overlapping_runs_over_one_ticket_send_the_same_pass_id_and_write_one_draft(
    make_world: Any,
) -> None:
    """The second run's draft is a same-cycle replay and its envelope names the
    same pass, so the OS absorbs it (93-06: a used pass id creates nothing)."""
    world = make_world()
    entry = make_entry(0)
    world.os_api.script = [OK, {"status": 200, "error": None, "replayed": True}]
    first = finalize(world, [copy.deepcopy(entry)], result_of(with_items(entry, ITEM_A)))
    second = finalize(world, [copy.deepcopy(entry)], result_of(with_items(entry, ITEM_A)), run_id=OTHER_RUN_ID)

    assert (first.drafts_created, second.drafts_created) == (1, 0)
    one, two = world.ingest.posts
    assert one == two and one["pass_id"] == osm.T1_SIGNAL_ID
    assert len(posts(world)) == 2
    assert not re.search("hubspot:", json.dumps(world.ingest.posts))  # never a run-scoped pass id


def test_nothing_under_config_is_written_by_a_finalize(world: OsWorld) -> None:
    entry = make_entry(0)
    finalize(world, [entry], result_of(with_items(entry, ITEM_A)))
    written = {p.name for p in world.base.iterdir() if p.is_file()}
    assert {"seen.json"} <= written
    assert world.seen_path.parent == world.base and retry_file(world).parent == world.base
    package_config = Path(os_pass.__file__).resolve().parent.parent / "config"
    assert not (package_config / ".action_items_retry.json").exists()
