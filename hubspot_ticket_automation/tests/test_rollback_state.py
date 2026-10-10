"""Rollback is a setting: `os` mode keeps the HubSpot path's state current (Phase 93, plan 23).

The HubSpot source path decides what is new work from a local state file: a
search for tickets modified after `last_run_at`, then a dedupe against
`ticket_fingerprints`. In `os` mode that file plays no part in selection, but a
flip of the owner's switch back to `hubspot` reads it at once. So code keeps it
current:

    source.merge_fingerprints   the one writer: read, merge, replace through a
                                temp file and a rename; never a numeric note count
    os_pass.finalize_os_run     calls it once, at its end, for every HubSpot-linked
                                ticket the pass completed

Every test here uses a state file under the pytest temp directory. The tracked
config/state.json is never opened by the code under test: an autouse guard
refuses it (and any write under config/) with a ledger, and a module fixture
compares the tracked file's bytes before the first test and after the last.

The safety fixtures of the runner's tests apply here too: no child process, no
ShipSidekick lookup, no real token or secret path; and the suite's network and
no-database guards (tests/conftest.py).
"""
from __future__ import annotations

import ast
import builtins
import copy
import inspect
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from tests import test_os_mode_run as osm
from tests.test_os_mode_run import (  # noqa: F401 - fixtures, used by name
    OsWorld,
    _no_child_process,
    _no_real_file_and_a_clean_counter,
    make_world,
    world,
)
from tests.helpers import fakes
from tests.test_os_finalize import (
    CONFLICT,
    E,
    completed,
    failed,
    finalize,
    make_entry,
    posts,
    result_of,
    run_context,
)
from ticket_pipeline import os_pass, source

REAL_STATE_PATH = osm.REAL_STATE_PATH.resolve()
REAL_CONFIG_DIR = REAL_STATE_PATH.parent
REAL_FILE_REFUSED = "a test in test_rollback_state.py reached the tracked config directory"
_REAL_FILE_LEDGER: list[str] = []

STAMP = "2026-10-09T12:00:05.000Z"
EARLIER = "2026-10-09T11:00:00.000Z"
LATER = "2026-10-09T13:00:00.000Z"
UPDATED = "2026-10-08T10:00:00.500Z"

BASE = {
    "last_run_at": "2026-10-01T00:00:00.000Z",
    "processed_ticket_ids": ["700", "701"],
    "run_count": 41,
    "last_run_mode": "live",
    "notes_posted_by_run": {"2026-10-01": 3},
    "auto_sends_by_run": {},
    "ticket_fingerprints": {
        "700": {
            "num_notes": 4,
            "processed_at": "2026-10-01T09:00:00.000Z",
            "hs_lastmodifieddate": "2026-10-01T08:59:00.000Z",
            "note": "kept as it is",
        },
        "701": {
            "num_notes": 1,
            "processed_at": "2026-10-02T09:00:00.000Z",
            "hs_lastmodifieddate": "2026-10-02T08:59:00.000Z",
        },
    },
}


def base() -> dict:
    return json.loads(json.dumps(BASE))


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


# ----------------------------------------------------------------- test safety


def _config_snapshot() -> dict:
    """The tracked state file's bytes, and the name, size and change time of
    every plain file directly under config/. Sub-directories are not entered."""
    listing = {
        p.name: (p.stat().st_size, p.stat().st_mtime_ns) for p in REAL_CONFIG_DIR.iterdir() if p.is_file()
    }
    return {"state_bytes": REAL_STATE_PATH.read_bytes(), "files": listing}


@pytest.fixture(scope="module", autouse=True)
def _the_tracked_config_is_as_it_was() -> Iterator[None]:
    before = _config_snapshot()
    yield
    after = _config_snapshot()
    assert after["state_bytes"] == before["state_bytes"], "config/state.json changed while this file ran"
    assert after["files"] == before["files"], "a file under config/ changed while this file ran"


def _inside_real_config(target: Any) -> bool:
    try:
        resolved = Path(target).resolve()
    except (TypeError, ValueError, OSError):
        return False
    return resolved == REAL_STATE_PATH or REAL_CONFIG_DIR in resolved.parents


@pytest.fixture(autouse=True)
def _no_tracked_state_file(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The tracked state file is never read, and nothing under config/ is ever
    written, by a test of this file. The finalizer turns an exception into a
    code, so a refusal is recorded as well as raised and fails the test at
    teardown."""
    _REAL_FILE_LEDGER.clear()
    real_open, real_os_open, real_replace = builtins.open, os.open, os.replace
    real_read_text, real_read_bytes = Path.read_text, Path.read_bytes
    real_write_text, real_write_bytes = Path.write_text, Path.write_bytes

    def refuse(what: str, target: Any) -> None:
        _REAL_FILE_LEDGER.append(f"{what}:{Path(target).name}")
        raise AssertionError(REAL_FILE_REFUSED)

    def reading(what: str, target: Any) -> None:
        try:
            hit = Path(target).resolve() == REAL_STATE_PATH
        except (TypeError, ValueError, OSError):
            hit = False
        if hit:
            refuse(what, target)

    def writing(what: str, target: Any) -> None:
        if _inside_real_config(target):
            refuse(what, target)

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if not isinstance(file, int):
            reading("open", file)
            if any(flag in str(mode) for flag in "wax+"):
                writing("open-for-write", file)
        return real_open(file, mode, *args, **kwargs)

    def guarded_os_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> Any:
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            writing("os.open", path)
        return real_os_open(path, flags, *args, **kwargs)

    def guarded_replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        writing("os.replace", dst)
        return real_replace(src, dst, *args, **kwargs)

    def guarded_read_text(self: Path, *args: Any, **kwargs: Any) -> Any:
        reading("read_text", self)
        return real_read_text(self, *args, **kwargs)

    def guarded_read_bytes(self: Path, *args: Any, **kwargs: Any) -> Any:
        reading("read_bytes", self)
        return real_read_bytes(self, *args, **kwargs)

    def guarded_write_text(self: Path, *args: Any, **kwargs: Any) -> Any:
        writing("write_text", self)
        return real_write_text(self, *args, **kwargs)

    def guarded_write_bytes(self: Path, *args: Any, **kwargs: Any) -> Any:
        writing("write_bytes", self)
        return real_write_bytes(self, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    monkeypatch.setattr(os, "replace", guarded_replace)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    monkeypatch.setattr(Path, "write_text", guarded_write_text)
    monkeypatch.setattr(Path, "write_bytes", guarded_write_bytes)
    yield
    leftover = list(_REAL_FILE_LEDGER)
    _REAL_FILE_LEDGER.clear()
    if leftover:
        pytest.fail(f"{REAL_FILE_REFUSED}: {leftover}", pytrace=False)


def test_the_tracked_state_file_and_a_write_under_config_are_refused_in_this_file() -> None:
    with pytest.raises(AssertionError, match="tracked config directory"):
        osm.pg._load_state()  # the HubSpot path's own loader, at its default path
    with pytest.raises(AssertionError, match="tracked config directory"):
        REAL_STATE_PATH.read_bytes()
    with pytest.raises(AssertionError, match="tracked config directory"):
        open(REAL_STATE_PATH, "a", encoding="utf-8")
    with pytest.raises(AssertionError, match="tracked config directory"):
        (REAL_CONFIG_DIR / "state.json.new").write_text("{}", encoding="utf-8")
    with pytest.raises(AssertionError, match="tracked config directory"):
        os.replace(REAL_CONFIG_DIR / "does-not-exist.tmp", REAL_STATE_PATH)
    assert len(_REAL_FILE_LEDGER) == 5
    _REAL_FILE_LEDGER.clear()  # provoked on purpose
    assert not (REAL_CONFIG_DIR / "state.json.new").exists()


def test_the_helper_pointed_at_the_tracked_file_is_refused_before_it_reads_it() -> None:
    refused: list[str] = []
    try:
        with pytest.raises(AssertionError, match="tracked config directory"):
            source.merge_fingerprints(REAL_STATE_PATH, {"1": {"processed_at": STAMP}})
    finally:
        refused.extend(_REAL_FILE_LEDGER)
        _REAL_FILE_LEDGER.clear()  # provoked on purpose
    assert refused == ["read_text:state.json"]


# ------------------------------------------------------- fingerprint_entry


def test_a_fingerprint_entry_has_exactly_the_three_keys_and_no_note_count() -> None:
    entry = source.fingerprint_entry(STAMP, UPDATED)
    assert entry == {"num_notes": None, "processed_at": STAMP, "hs_lastmodifieddate": UPDATED}
    assert list(entry) == ["num_notes", "processed_at", "hs_lastmodifieddate"]


@pytest.mark.parametrize("unknown", [None, "", "   ", "yesterday", 1760000000000, {"at": UPDATED}])
def test_a_hubspot_time_the_candidate_rule_cannot_read_is_written_as_null(unknown: Any) -> None:
    assert source.fingerprint_entry(STAMP, unknown) == {
        "num_notes": None,
        "processed_at": STAMP,
        "hs_lastmodifieddate": None,
    }


@pytest.mark.parametrize("bad", [None, "", "not a time", 5])
def test_a_fingerprint_entry_needs_a_readable_processed_at(bad: Any) -> None:
    with pytest.raises(ValueError):
        source.fingerprint_entry(bad, UPDATED)


# ------------------------------------------------------- merge_fingerprints


def test_merge_adds_a_fingerprint_and_preserves_every_other_key_and_fingerprint(tmp_path: Path) -> None:
    path = fakes.temp_state_file(tmp_path, base())

    answer = source.merge_fingerprints(path, {"H1": source.fingerprint_entry(STAMP, UPDATED)})

    after = read(path)
    expected = base()
    expected["ticket_fingerprints"]["H1"] = {
        "num_notes": None,
        "processed_at": STAMP,
        "hs_lastmodifieddate": UPDATED,
    }
    assert after == expected
    assert list(after) == list(BASE)  # the top-level keys, in their order
    assert list(after["ticket_fingerprints"]) == ["700", "701", "H1"]
    assert answer == {"written": True, "fingerprints": 1, "last_run_at_moved": False}


def test_an_agent_written_fingerprint_is_replaced_by_the_code_written_one(tmp_path: Path) -> None:
    path = fakes.temp_state_file(tmp_path, base())

    source.merge_fingerprints(path, {"700": source.fingerprint_entry(STAMP, UPDATED)})

    # the newer processed_at wins: the agent's numeric note count and its note are gone
    assert read(path)["ticket_fingerprints"]["700"] == {
        "num_notes": None,
        "processed_at": STAMP,
        "hs_lastmodifieddate": UPDATED,
    }
    assert read(path)["ticket_fingerprints"]["701"] == BASE["ticket_fingerprints"]["701"]


def test_a_stored_fingerprint_with_a_newer_processed_at_is_kept(tmp_path: Path) -> None:
    state = base()
    state["ticket_fingerprints"]["700"]["processed_at"] = LATER
    path = fakes.temp_state_file(tmp_path, state)
    before = path.read_bytes()

    answer = source.merge_fingerprints(path, {"700": source.fingerprint_entry(STAMP, UPDATED)})

    assert read(path)["ticket_fingerprints"]["700"] == state["ticket_fingerprints"]["700"]
    assert path.read_bytes() == before  # nothing to write, so nothing written
    assert answer == {"written": False, "fingerprints": 0, "last_run_at_moved": False}


def test_a_caller_cannot_get_a_numeric_note_count_written(tmp_path: Path) -> None:
    path = fakes.temp_state_file(tmp_path, base())
    sentinel = {"num_notes": 999999, "processed_at": STAMP, "hs_lastmodifieddate": UPDATED, "extra": "x"}

    source.merge_fingerprints(path, {"H1": sentinel})

    assert read(path)["ticket_fingerprints"]["H1"] == {
        "num_notes": None,
        "processed_at": STAMP,
        "hs_lastmodifieddate": UPDATED,
    }


@pytest.mark.parametrize(
    "key, entry",
    [
        ("", {"processed_at": STAMP}),
        (700, {"processed_at": STAMP}),
        ("H1", {"processed_at": "not a time"}),
        ("H1", {"hs_lastmodifieddate": UPDATED}),
        ("H1", "2026-10-09T12:00:05.000Z"),
        ("H1", None),
    ],
)
def test_an_entry_that_is_not_a_fingerprint_is_not_written(tmp_path: Path, key: Any, entry: Any) -> None:
    path = fakes.temp_state_file(tmp_path, base())
    before = path.read_bytes()

    answer = source.merge_fingerprints(path, {key: entry})

    assert path.read_bytes() == before
    assert answer["written"] is False and answer["fingerprints"] == 0


def test_the_write_goes_to_a_temp_file_beside_the_target_and_is_renamed_over_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = fakes.temp_state_file(tmp_path, base())
    renames: list[tuple[Path, Path, dict, dict]] = []
    replace = os.replace  # the guarded one: it passes a temp path through

    def spy(src: Any, dst: Any) -> None:
        # at the moment of the rename the temp file is complete and the target is the old one
        renames.append((Path(src), Path(dst), read(Path(src)), read(Path(dst))))
        replace(src, dst)

    monkeypatch.setattr(source.os, "replace", spy)
    source.merge_fingerprints(path, {"H1": source.fingerprint_entry(STAMP, UPDATED)})

    assert len(renames) == 1
    src, dst, new_content, old_content = renames[0]
    assert dst == path and src != path and src.parent == path.parent
    assert old_content == BASE and "H1" in new_content["ticket_fingerprints"]
    assert names(tmp_path) == ["state.json"]  # the temp file is gone
    assert read(path) == new_content


def test_a_failed_rename_leaves_the_original_bytes_and_the_helper_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = fakes.temp_state_file(tmp_path, base())
    before = path.read_bytes()

    def refuse(src: Any, dst: Any) -> None:
        raise PermissionError("the target is held open")

    monkeypatch.setattr(source.os, "replace", refuse)
    with pytest.raises(PermissionError):
        source.merge_fingerprints(path, {"H1": source.fingerprint_entry(STAMP, UPDATED)})

    assert path.read_bytes() == before
    assert names(tmp_path) == ["state.json"]  # no half-written file is left beside it


def test_a_missing_state_file_is_treated_as_empty_and_created_valid(tmp_path: Path) -> None:
    path = tmp_path / "state.json"

    answer = source.merge_fingerprints(path, {"H1": source.fingerprint_entry(STAMP, UPDATED)}, last_run_at=UPDATED)

    assert read(path) == {
        "ticket_fingerprints": {
            "H1": {"num_notes": None, "processed_at": STAMP, "hs_lastmodifieddate": UPDATED}
        },
        "last_run_at": UPDATED,
    }
    assert answer == {"written": True, "fingerprints": 1, "last_run_at_moved": True}


@pytest.mark.parametrize(
    "content",
    [b"{ this is not json", b"", b"   \n", b'["a", "list"]', b'"a string"', b"null", b"\xff\xfe\x00bad"],
)
def test_an_unparseable_state_file_is_replaced_by_a_valid_one(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(content)

    source.merge_fingerprints(path, {"H1": source.fingerprint_entry(STAMP, UPDATED)})

    assert read(path) == {
        "ticket_fingerprints": {
            "H1": {"num_notes": None, "processed_at": STAMP, "hs_lastmodifieddate": UPDATED}
        }
    }


def test_fingerprints_that_are_not_an_object_are_replaced_and_the_other_keys_kept(tmp_path: Path) -> None:
    state = base()
    state["ticket_fingerprints"] = ["700", "701"]
    path = fakes.temp_state_file(tmp_path, state)

    source.merge_fingerprints(path, {"H1": source.fingerprint_entry(STAMP, UPDATED)})

    after = read(path)
    assert after["ticket_fingerprints"] == {
        "H1": {"num_notes": None, "processed_at": STAMP, "hs_lastmodifieddate": UPDATED}
    }
    assert {k: v for k, v in after.items() if k != "ticket_fingerprints"} == {
        k: v for k, v in BASE.items() if k != "ticket_fingerprints"
    }


def test_a_state_file_that_is_there_and_cannot_be_read_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A file that cannot be read is not an empty one: replacing it would throw
    # away every fingerprint it holds. The helper raises and writes nothing.
    path = fakes.temp_state_file(tmp_path, base())
    before = path.read_bytes()
    real_read_text = Path.read_text

    def locked(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == path:
            raise PermissionError("held by another process")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", locked)
    with pytest.raises(PermissionError):
        source.merge_fingerprints(path, {"H1": source.fingerprint_entry(STAMP, UPDATED)})
    monkeypatch.setattr(Path, "read_text", real_read_text)

    assert path.read_bytes() == before
    assert names(tmp_path) == ["state.json"]


@pytest.mark.parametrize(
    "stored, given, expected",
    [
        ("2026-10-01T00:00:00.000Z", "2026-10-08T10:00:00.500Z", "2026-10-08T10:00:00.500Z"),  # later: moves
        ("2026-10-08T10:00:00.500Z", "2026-10-01T00:00:00.000Z", "2026-10-08T10:00:00.500Z"),  # earlier: stays
        ("2026-10-08T10:00:00.500Z", "2026-10-08T10:00:00.500Z", "2026-10-08T10:00:00.500Z"),  # equal: stays
        ("2026-10-08T10:00:00Z", "2026-10-08T12:00:00+02:00", "2026-10-08T10:00:00Z"),  # the same instant
        ("2026-10-01T00:00:00.000Z", None, "2026-10-01T00:00:00.000Z"),  # none given: stays
        ("2026-10-01T00:00:00.000Z", "soon", "2026-10-01T00:00:00.000Z"),  # unreadable: stays
        ("", "2026-10-08T10:00:00.500Z", "2026-10-08T10:00:00.500Z"),  # nothing stored: set
        ("garbage", "2026-10-08T10:00:00.500Z", "2026-10-08T10:00:00.500Z"),
    ],
)
def test_last_run_at_only_moves_forward(tmp_path: Path, stored: str, given: Any, expected: str) -> None:
    state = base()
    state["last_run_at"] = stored
    path = fakes.temp_state_file(tmp_path, state)

    answer = source.merge_fingerprints(path, {"H1": source.fingerprint_entry(STAMP, UPDATED)}, last_run_at=given)

    assert read(path)["last_run_at"] == expected
    assert answer["last_run_at_moved"] is (expected != stored)


def test_last_run_at_can_move_with_no_fingerprint_to_write(tmp_path: Path) -> None:
    path = fakes.temp_state_file(tmp_path, base())

    answer = source.merge_fingerprints(path, {}, last_run_at=UPDATED)

    expected = base()
    expected["last_run_at"] = UPDATED
    assert read(path) == expected
    assert answer == {"written": True, "fingerprints": 0, "last_run_at_moved": True}


def test_nothing_to_merge_does_not_rewrite_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = fakes.temp_state_file(tmp_path, base())
    before = path.read_bytes()
    missing = tmp_path / "absent" / "state.json"
    writes: list[Any] = []
    monkeypatch.setattr(source.os, "replace", lambda *args: writes.append(args))

    assert source.merge_fingerprints(path, {}) == {
        "written": False,
        "fingerprints": 0,
        "last_run_at_moved": False,
    }
    assert source.merge_fingerprints(path, {}, last_run_at=None)["written"] is False
    assert source.merge_fingerprints(missing, {})["written"] is False  # and no file is created

    assert writes == [] and path.read_bytes() == before
    assert names(tmp_path) == ["state.json"]

    # and the file is not even read: a locked or unreadable file cannot matter
    def unreadable(_path: Any) -> dict:
        raise PermissionError("held by another process")

    monkeypatch.setattr(source, "_read_state", unreadable)
    assert source.merge_fingerprints(path, {})["written"] is False
    assert source.merge_fingerprints(path, None)["written"] is False


def test_merging_what_the_file_already_holds_does_not_rewrite_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = fakes.temp_state_file(tmp_path, base())
    entries = {"H1": source.fingerprint_entry(STAMP, UPDATED)}
    assert source.merge_fingerprints(path, entries, last_run_at=UPDATED)["written"] is True
    before = path.read_bytes()
    writes: list[Any] = []
    monkeypatch.setattr(source.os, "replace", lambda *args: writes.append(args))

    assert source.merge_fingerprints(path, entries, last_run_at=UPDATED) == {
        "written": False,
        "fingerprints": 0,
        "last_run_at_moved": False,
    }
    assert writes == [] and path.read_bytes() == before


def test_the_file_is_plain_json_the_hubspot_paths_own_loader_reads(tmp_path: Path) -> None:
    state = base()
    state["ticket_fingerprints"]["700"]["note"] = "café — reviewed"
    path = fakes.temp_state_file(tmp_path, state)

    source.merge_fingerprints(path, {"H1": source.fingerprint_entry(STAMP, UPDATED)})

    raw = path.read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw
    loaded = osm.pg._load_state(path)  # the loader the pre-gate uses
    assert loaded["ticket_fingerprints"]["700"]["note"] == "café — reviewed"
    assert loaded["ticket_fingerprints"]["H1"]["num_notes"] is None


def test_two_interleaved_merges_lose_the_first_runs_fingerprint_and_nothing_else(tmp_path: Path) -> None:
    """The accepted residual (runbook tag STATE-FILE-RACE, checker CRON-I5),
    pinned to its extent. The read-merge-replace is not locked across
    processes. By hand: run A reads, run B reads, A writes its fingerprint, B
    writes its own. B's replace is built on the state B read, so A's
    fingerprint is gone. Nothing else is: every earlier key and fingerprint is
    there and the file is valid JSON."""
    path = fakes.temp_state_file(tmp_path, base())
    a = {"A1": source.fingerprint_entry(STAMP, UPDATED)}
    b = {"B1": source.fingerprint_entry(LATER, UPDATED)}

    read_by_a = source._read_state(path)
    read_by_b = source._read_state(path)
    source._replace_state(path, source._merge_state(read_by_a, a, UPDATED))
    assert "A1" in read(path)["ticket_fingerprints"]  # A's write did land
    source._replace_state(path, source._merge_state(read_by_b, b, None))

    after = read(path)  # valid JSON
    assert "A1" not in after["ticket_fingerprints"]
    assert after["ticket_fingerprints"]["B1"] == b["B1"]
    assert {k: after["ticket_fingerprints"][k] for k in ("700", "701")} == BASE["ticket_fingerprints"]
    assert list(after["ticket_fingerprints"]) == ["700", "701", "B1"]
    # the same loss reaches A's last_run_at, and no other key
    assert after == {**base(), "ticket_fingerprints": {**BASE["ticket_fingerprints"], **b}}
    assert names(tmp_path) == ["state.json"]

    # and what two runs that do NOT overlap leave: both fingerprints
    source.merge_fingerprints(path, a, last_run_at=UPDATED)
    assert set(read(path)["ticket_fingerprints"]) == {"700", "701", "A1", "B1"}


def test_merge_does_not_change_the_state_it_was_handed() -> None:
    state = base()
    merged = source._merge_state(state, {"700": source.fingerprint_entry(STAMP, UPDATED)}, UPDATED)
    assert state == BASE
    assert merged["ticket_fingerprints"]["700"]["num_notes"] is None and merged["last_run_at"] == UPDATED


def test_merge_fingerprints_has_no_default_for_its_path() -> None:
    parameters = inspect.signature(source.merge_fingerprints).parameters
    assert list(parameters) == ["state_path", "entries", "last_run_at"]
    assert parameters["state_path"].default is inspect.Parameter.empty
    assert parameters["entries"].default is inspect.Parameter.empty
    assert parameters["last_run_at"].default is None
    with pytest.raises(TypeError):
        source.merge_fingerprints()  # type: ignore[call-arg]


def test_the_helpers_docstring_says_the_merge_is_not_a_lock() -> None:
    doc = " ".join((source.merge_fingerprints.__doc__ or "").split())
    assert "not locked across processes" in doc
    assert "STATE-FILE-RACE" in doc


def test_the_state_helper_keeps_the_module_standard_library_only() -> None:
    text = Path(source.__file__).read_text(encoding="utf-8")
    top_level = [
        line.split()[1].split(".")[0]
        for line in text.splitlines()
        if line.startswith(("import ", "from ")) and not line.startswith("from __future__")
    ]
    assert set(top_level) <= {"json", "os", "shutil", "sys", "tempfile", "time", "datetime", "pathlib", "typing"}


# ===========================================================================
# THE `os` FINALISER KEEPS THE STATE CURRENT (task 2)
#
# Fixture tickets: 1 and 3 have a HubSpot id, 2 has none. Ticket 3's numbers
# are only in its form block, so its pass ends with the number backfill, a
# HubSpot write of the pass itself.
# ===========================================================================

T1_UPDATED = "2026-10-08T10:00:00.500Z"  # the list fixture's hubspot_updated_at
T3_UPDATED = "2026-10-09T07:20:30.456Z"
STORED_LAST_RUN = "2026-09-01T00:00:00Z"
SEARCH_PATH = "/crm/v3/objects/tickets/search"
HUBSPOT_SETTINGS = {"active_stages": ["1", "2"], "per_run_cap": 25, "require_last_message_from_visitor": True}
UNWRITABLE = "rollback_state_unwritable"
EXC_MARKER = "ZZ-EXC-93-23"
KEYS = {"num_notes", "processed_at", "hs_lastmodifieddate"}


def code_written(processed_at: str, updated: Any) -> dict:
    return {"num_notes": None, "processed_at": processed_at, "hs_lastmodifieddate": updated}


def plus_ms(iso: str, ms: int) -> str:
    moment = datetime.fromisoformat(iso.replace("Z", "+00:00")) + timedelta(milliseconds=ms)
    return os_pass._iso_ms(moment)


def empty_state(world: OsWorld, last_run_at: str = STORED_LAST_RUN) -> None:
    """A state file that holds no fingerprint yet."""
    world.state_path.write_text(
        json.dumps({"last_run_at": last_run_at, "run_count": 7, "ticket_fingerprints": {}}), encoding="utf-8"
    )


def all_completed(*entries: dict) -> dict:
    return result_of(*(completed(entry) for entry in entries))


def flip_to_hubspot(world: OsWorld, hits: list) -> tuple:
    """What the HubSpot path selects after the switch: its own loader over the
    file the finaliser wrote, its own candidate rule, a fake search answering
    `hits` as (ticket id, hs_lastmodifieddate). -> (decision, the search window's start in ms)"""
    transport = fakes.FakeHubSpotTransport(
        {
            SEARCH_PATH: {
                "results": [
                    {"id": ticket_id, "properties": {"hs_lastmodifieddate": modified}}
                    for ticket_id, modified in hits
                ]
            }
        }
    )
    state = osm.pg._load_state(world.state_path)
    decision = osm.pg.hubspot_candidates("hubspot", HUBSPOT_SETTINGS, state, "test-token", transport=transport)
    assert transport.calls == [("POST", SEARCH_PATH)]
    window = transport.bodies[0]["filterGroups"][0]["filters"][0]
    assert window["propertyName"] == "hs_lastmodifieddate" and window["operator"] == "GT"
    return decision, window["value"]


def test_a_finalised_run_fingerprints_each_completed_hubspot_linked_ticket(world: OsWorld) -> None:
    entries = [make_entry(0), make_entry(1), make_entry(2)]

    summary = finalize(world, entries, all_completed(*entries))

    assert summary.tickets_processed == 3 and summary.codes == {}
    state = read(world.state_path)
    # ticket 1's agent-written fingerprint (a numeric note count) is replaced;
    # ticket 2 has no HubSpot id, so HubSpot can never select it: nothing is recorded
    assert state["ticket_fingerprints"] == {
        osm.T1_HS_ID: code_written(STAMP, T1_UPDATED),
        osm.T3_HS_ID: code_written(STAMP, T3_UPDATED),
    }
    assert osm.T2_OS_ID not in json.dumps(state)
    assert state["last_run_at"] == T3_UPDATED  # the latest among the completed, later than the stored one


def test_processed_at_is_read_once_after_the_last_step_of_the_last_ticket(world: OsWorld) -> None:
    ticking = world._now

    def clock() -> Any:
        world.timeline.append(("clock",))
        return ticking()

    world._now = clock  # type: ignore[method-assign]
    entries = [make_entry(0), make_entry(2)]
    finalize(world, entries, all_completed(*entries))

    assert world.timeline.count(("clock",)) == 1 and world.timeline[-1] == ("clock",)
    assert ("backfill", osm.T3_HS_ID) in world.timeline  # the pass's own HubSpot write came first
    assert read(world.state_path)["ticket_fingerprints"][osm.T3_HS_ID]["processed_at"] == STAMP


def test_a_failed_ticket_gets_no_fingerprint_and_stays_inside_the_search_window(world: OsWorld) -> None:
    empty_state(world)
    entries = [make_entry(0), make_entry(2)]

    finalize(world, entries, result_of(failed(entries[0]), completed(entries[1])))

    state = read(world.state_path)
    assert state["ticket_fingerprints"] == {osm.T3_HS_ID: code_written(STAMP, T3_UPDATED)}
    # ticket 3 was modified later than the failed ticket 1: moving the window's start
    # to it would hide ticket 1 from the HubSpot search for good. It does not move.
    assert state["last_run_at"] == STORED_LAST_RUN
    decision, window_start = flip_to_hubspot(world, [(osm.T1_HS_ID, T1_UPDATED), (osm.T3_HS_ID, T3_UPDATED)])
    assert window_start < source.iso_to_ms(T1_UPDATED)
    assert decision["candidates"] == [osm.T1_HS_ID]


def test_last_run_at_stops_short_of_a_linked_ticket_the_run_did_not_complete(world: OsWorld) -> None:
    empty_state(world)
    entries = [make_entry(0), make_entry(2)]

    finalize(world, entries, result_of(completed(entries[0]), failed(entries[1])))

    # ticket 1 (older) completed, ticket 3 (newer) failed: the start may move to
    # ticket 1's time, which is still before ticket 3's
    state = read(world.state_path)
    assert state["last_run_at"] == T1_UPDATED
    assert set(state["ticket_fingerprints"]) == {osm.T1_HS_ID}
    decision, window_start = flip_to_hubspot(world, [(osm.T3_HS_ID, T3_UPDATED)])
    assert window_start < source.iso_to_ms(T3_UPDATED) and decision["candidates"] == [osm.T3_HS_ID]


def test_last_run_at_does_not_move_onto_the_time_of_an_uncompleted_ticket(world: OsWorld) -> None:
    # The HubSpot search asks for tickets modified AFTER the start: a start exactly
    # at an uncompleted ticket's time would hide it as surely as one past it.
    empty_state(world)
    same_time = make_entry(0, payload_edit=lambda payload: payload.update(hubspot_updated_at=T3_UPDATED))
    entries = [same_time, make_entry(2)]

    finalize(world, entries, result_of(completed(same_time), failed(entries[1])))

    state = read(world.state_path)
    assert state["ticket_fingerprints"] == {osm.T1_HS_ID: code_written(STAMP, T3_UPDATED)}
    assert state["last_run_at"] == STORED_LAST_RUN
    decision, window_start = flip_to_hubspot(world, [(osm.T3_HS_ID, T3_UPDATED)])
    assert window_start < source.iso_to_ms(T3_UPDATED) and decision["candidates"] == [osm.T3_HS_ID]


def test_last_run_at_does_not_move_back_or_without_a_readable_time(world: OsWorld) -> None:
    empty_state(world, last_run_at="2026-10-09T09:00:00Z")  # later than both tickets' times
    entries = [make_entry(0), make_entry(2)]
    finalize(world, entries, all_completed(*entries))
    assert read(world.state_path)["last_run_at"] == "2026-10-09T09:00:00Z"


def test_an_uncompleted_linked_ticket_with_no_known_time_holds_last_run_at(world: OsWorld) -> None:
    empty_state(world)
    unknown = make_entry(2, payload_edit=lambda payload: payload.update(hubspot_updated_at=None))
    entries = [make_entry(0), unknown]

    finalize(world, entries, result_of(completed(entries[0]), failed(unknown)))

    state = read(world.state_path)
    assert set(state["ticket_fingerprints"]) == {osm.T1_HS_ID}
    assert state["last_run_at"] == STORED_LAST_RUN


def test_a_ticket_no_bundle_entry_could_be_built_for_holds_last_run_at(world: OsWorld) -> None:
    empty_state(world)
    entry = make_entry(0)

    finalize(world, [entry], all_completed(entry), unbundled=[{"os_ticket_id": osm.T3_OS_ID, "signal": None}])

    state = read(world.state_path)
    assert set(state["ticket_fingerprints"]) == {osm.T1_HS_ID}
    assert state["last_run_at"] == STORED_LAST_RUN  # nothing is known of that ticket's HubSpot time


def test_a_ticket_whose_write_back_answered_409_is_fingerprinted(world: OsWorld) -> None:
    empty_state(world)
    entry = make_entry(0)
    world.os_api.script = [CONFLICT]  # an earlier pass already holds this signal

    summary = finalize(world, [entry], all_completed(entry))

    assert summary.tickets[0]["writeback"] == os_pass.WB_PROCESSED
    assert read(world.state_path)["ticket_fingerprints"] == {osm.T1_HS_ID: code_written(STAMP, T1_UPDATED)}


def test_a_drafted_ticket_the_os_did_not_record_gets_no_fingerprint(world: OsWorld) -> None:
    empty_state(world)
    before = world.state_path.read_bytes()
    entry = make_entry(0)
    world.os_api.script = [E("list_timeout"), E("list_timeout")]  # the write-back and its one retry

    summary = finalize(world, [entry], all_completed(entry))

    assert summary.tickets[0]["drafted"] is True and summary.tickets[0]["writeback"] == os_pass.WB_UNRECORDED
    assert world.state_path.read_bytes() == before  # no ticket ended processed: the file is not touched


def test_an_os_only_ticket_and_a_run_with_no_result_leave_the_file_untouched(world: OsWorld) -> None:
    before = world.state_path.read_bytes()
    os_only = make_entry(1)
    assert os_only["hubspot_ticket_id"] is None

    finalize(world, [os_only], all_completed(os_only))
    finalize(world, [make_entry(0)], None)  # the agent left no result at all

    assert world.state_path.read_bytes() == before
    assert names(world.state_path.parent).count("state.json") == 1


def test_a_dry_run_leaves_the_state_file_untouched(world: OsWorld) -> None:
    before = world.state_path.read_bytes()
    world.settings["dry_run"] = True
    entries = [make_entry(0), make_entry(2)]

    summary = finalize(world, entries, all_completed(*entries))

    assert summary.dry_run is True and summary.tickets_processed == 2
    assert world.state_path.read_bytes() == before


def test_the_state_step_itself_refuses_a_dry_run(world: OsWorld) -> None:
    # A dry run never marks a ticket drafted, so the test above cannot tell the
    # step's own dry-run rule from that. Here the step is handed a dry-run
    # summary that (wrongly) claims a processed ticket: still nothing is written.
    before = world.state_path.read_bytes()
    entry = make_entry(0)
    claimed = {"ref": entry["ref"], "drafted": True, "writeback": os_pass.WB_PROCESSED}

    def step(dry_run: bool) -> None:
        summary = os_pass.FinalizeSummary(dry_run=dry_run, tickets=[dict(claimed)])
        run = os_pass._Pass(run_context(world), world.deps(), world.os_api, summary, world.settings, {}, None)
        os_pass._keep_rollback_state(run, [entry], ())
        assert summary.codes == {}

    step(dry_run=True)
    assert world.state_path.read_bytes() == before
    step(dry_run=False)  # the same call, live, does write: the dry-run rule is what held it
    assert read(world.state_path)["ticket_fingerprints"][osm.T1_HS_ID]["num_notes"] is None


def test_a_state_write_that_fails_is_a_code_and_changes_nothing_else(
    make_world: Callable[..., OsWorld],
) -> None:
    # One world at a time: a world's fakes are module-level patches, so the
    # second world is built only after the first has run.
    working = make_world("working")
    assert working.run() == 0

    broken = make_world("broken")
    broken.state_path = broken.base / "no-such-directory" / "state.json"  # the temp file cannot be made
    assert broken.run() == 0

    same = ("status", "tickets_processed", "drafts_created", "failure_reason_code", "hubspot_calls")
    assert {k: broken.record.get(k) for k in same} == {k: working.record.get(k) for k in same}
    assert working.record["status"] == "success" and working.record["tickets_processed"] == 3
    assert working.record["drafts_created"] == 3
    assert working.record["error_summary"] is None
    assert broken.record["error_summary"] == f"{UNWRITABLE}=1"
    # every ticket was still finalised: the same drafts, write-backs and HubSpot write
    assert [d["draft_key"] for d in broken.drafts] == [d["draft_key"] for d in working.drafts]
    assert len(broken.drafts) == 3 and len(posts(broken)) == len(posts(working)) == 3
    assert broken.hubspot.patches == working.hubspot.patches and len(broken.hubspot.patches) == 1
    assert not broken.state_path.parent.exists()
    assert set(read(working.state_path)["ticket_fingerprints"]) == {osm.T1_HS_ID, osm.T3_HS_ID}


def test_a_failed_state_write_logs_a_class_name_and_never_the_message(
    world: OsWorld, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    before = world.state_path.read_bytes()

    def refuse(_path: Any, _state: dict) -> None:
        raise OSError(f"cannot replace {EXC_MARKER}")

    monkeypatch.setattr(source, "_replace_state", refuse)
    entries = [make_entry(0), make_entry(1), make_entry(2)]
    summary = finalize(world, entries, all_completed(*entries))

    assert summary.codes == {UNWRITABLE: 1}
    assert summary.tickets_processed == 3 and summary.partial is False
    assert [ticket["writeback"] for ticket in summary.tickets] == [os_pass.WB_PROCESSED] * 3
    assert world.state_path.read_bytes() == before
    fakes.assert_marker_absent(EXC_MARKER, caplog, capfd)


def test_after_three_os_runs_a_flip_to_hubspot_selects_none_of_the_completed_tickets(world: OsWorld) -> None:
    """SPEC R15's acceptance. Three `os`-mode runs through the orchestrator,
    each completing a different fixture ticket, over one temp state file. Then
    the HubSpot path's own candidate rule over that file."""
    listing = osm._list()
    for index in (0, 1, 2):
        world.os_api.unprocessed = {**listing, "tickets": [copy.deepcopy(listing["tickets"][index])]}
        assert world.run() == 0
    assert [(r["status"], r["tickets_processed"]) for r in world.client.records] == [("success", 1)] * 3
    assert [draft["draft_key"] for draft in world.drafts] == [osm.T1_HS_ID, osm.T2_OS_ID, osm.T3_HS_ID]
    assert world.hubspot.patches and world.hubspot.patches[0][0] == osm.T3_HS_ID  # run 3 wrote to HubSpot

    state = osm.pg._load_state(world.state_path)
    written = state["ticket_fingerprints"]
    assert set(written) == {osm.T1_HS_ID, osm.T3_HS_ID}
    # a fingerprint written by code never carries a note count: every one the three runs wrote
    assert all(set(fp) == KEYS and fp["num_notes"] is None for fp in written.values())
    assert [fp["hs_lastmodifieddate"] for fp in written.values()] == [T1_UPDATED, T3_UPDATED]
    done_1, done_3 = (written[key]["processed_at"] for key in (osm.T1_HS_ID, osm.T3_HS_ID))
    assert source.iso_to_ms(done_1) < source.iso_to_ms(done_3)  # each run's own finishing instant
    assert state["last_run_at"] == T3_UPDATED

    # 1. HubSpot reports every completed ticket, unchanged since the mirror saw it: none is a candidate
    decision, window_start = flip_to_hubspot(world, [(osm.T1_HS_ID, T1_UPDATED), (osm.T3_HS_ID, T3_UPDATED)])
    assert decision["candidates"] == [] and decision["decision"] == "no_work"
    assert [hit["deduped"] for hit in decision["matched"]] == [True, True]
    assert window_start == source.iso_to_ms(T3_UPDATED)

    # 2. modified up to the very instant each pass finished (ticket 3's own
    #    backfill write is such a change): still none
    decision, _ = flip_to_hubspot(world, [(osm.T1_HS_ID, done_1), (osm.T3_HS_ID, done_3)])
    assert decision["candidates"] == [] and decision["decision"] == "no_work"

    # 3. ticket 1 got a new message after its pass finished: it IS a candidate, ticket 3 is not
    new_message = plus_ms(done_1, 1)
    decision, window_start = flip_to_hubspot(world, [(osm.T1_HS_ID, new_message), (osm.T3_HS_ID, done_3)])
    assert decision["candidates"] == [osm.T1_HS_ID] and decision["decision"] == "run_agent"
    assert window_start < source.iso_to_ms(new_message)  # and the search would return it

    # the three runs and the flip never opened the tracked state file
    assert _REAL_FILE_LEDGER == []
    assert world.state_path.resolve() != REAL_STATE_PATH


def test_a_ticket_the_os_holds_no_hubspot_time_for_is_judged_by_processed_at_both_ways(world: OsWorld) -> None:
    empty_state(world)
    entry = make_entry(0, payload_edit=lambda payload: payload.update(hubspot_updated_at=None))

    finalize(world, [entry], all_completed(entry))

    state = read(world.state_path)
    assert state["ticket_fingerprints"] == {osm.T1_HS_ID: code_written(STAMP, None)}
    assert state["last_run_at"] == STORED_LAST_RUN  # no readable time to move it to
    not_again, _ = flip_to_hubspot(world, [(osm.T1_HS_ID, STAMP)])
    assert not_again["candidates"] == []  # completed in `os` mode: not processed again
    picked_up, _ = flip_to_hubspot(world, [(osm.T1_HS_ID, plus_ms(STAMP, 1))])
    assert picked_up["candidates"] == [osm.T1_HS_ID]  # a change after the pass: picked up


def test_the_stated_limit_a_hubspot_change_made_while_the_pass_ran_is_not_a_candidate(world: OsWorld) -> None:
    """The limit of `processed_at`, pinned so it is not mistaken for more.

    The pass's own number backfill changes the ticket in HubSpot before the
    pass finishes, so "finished" is the earliest instant that covers the
    pass's own echo. HubSpot keeps ONE modified time per ticket: a client
    message that reached HubSpot after the mirror's copy and before the pass
    finished looks exactly like that echo and is deduped with it. This is rule
    (b) of the candidate rule, as the HubSpot path has always applied it to its
    own passes. In `os` mode the OS's marker still holds that message as
    unprocessed, so it is lost only if the switch back happens before the next
    `os` run."""
    empty_state(world)
    entry = make_entry(2)
    finalize(world, [entry], all_completed(entry))
    assert world.hubspot.patches  # the echo exists

    during_the_pass = plus_ms(STAMP, -4000)
    assert source.iso_to_ms(T3_UPDATED) < source.iso_to_ms(during_the_pass) < source.iso_to_ms(STAMP)
    decision, _ = flip_to_hubspot(world, [(osm.T3_HS_ID, during_the_pass)])
    assert decision["candidates"] == []


def test_a_fingerprint_lost_to_an_overlap_costs_one_candidate_after_the_flip(world: OsWorld) -> None:
    """The bound of STATE-FILE-RACE, at the candidate rule: the ticket whose
    fingerprint an overlapping run dropped is selected once more after a switch
    back (its draft then goes through the replace of the rollback path, so no
    second pending draft). The other run's ticket is not selected."""
    empty_state(world)
    path = world.state_path
    read_by_a, read_by_b = source._read_state(path), source._read_state(path)
    source._replace_state(
        path, source._merge_state(read_by_a, {osm.T1_HS_ID: source.fingerprint_entry(STAMP, T1_UPDATED)})
    )
    source._replace_state(
        path, source._merge_state(read_by_b, {osm.T3_HS_ID: source.fingerprint_entry(STAMP, T3_UPDATED)})
    )

    decision, _ = flip_to_hubspot(world, [(osm.T1_HS_ID, T1_UPDATED), (osm.T3_HS_ID, T3_UPDATED)])
    assert decision["candidates"] == [osm.T1_HS_ID]


def test_the_finaliser_hands_the_state_path_to_the_one_writer_and_never_reads_it() -> None:
    text = Path(os_pass.__file__).read_text(encoding="utf-8")
    tree = ast.parse(text)
    writer_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", ""))
        == "merge_fingerprints"
    ]
    assert len(writer_calls) == 1  # one call per run
    path_uses = [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "state_path"]
    assert len(path_uses) == 1 and writer_calls[0].args[0] is path_uses[0]
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert not called & {"_read_state", "_load_state", "_merge_state", "_replace_state", "unprocessed_candidates"}
    assert "state" + ".json" not in text


# ===========================================================================
# ROLLBACK REACHES EVERY TICKET (review CR-41)
#
# The HubSpot path selects `hs_lastmodifieddate > last_run_at`, then dedupes by
# fingerprint. An `os` pass only knows the tickets it LISTED. A ticket it never
# listed (parked after two failed passes, beyond the per-run cap, not mirrored
# yet) has no fingerprint, so a search window moved past its modified time
# would hide it from the HubSpot path for good. The rule: an `os` pass writes
# fingerprints only. `last_run_at` belongs to the HubSpot path alone.
#
# These tests drive the real candidate rule (`pregate_tickets.hubspot_candidates`)
# over the state the finalizer wrote, against a search that APPLIES the window
# filter: a fake that answers the same hits whatever the window says cannot see
# this defect.
# ===========================================================================

NOON = "2026-10-09T12:00:00.000Z"
ELEVEN = "2026-10-09T11:00:00.000Z"
UNLISTED_HS_ID = "9300000000777"  # a HubSpot ticket no `os` pass ever listed


class FilteringSearch:
    """HubSpot's tickets/search, as far as the candidate rule relies on it:
    only tickets modified AFTER the body's `hs_lastmodifieddate GT` value,
    oldest first, cut to the body's `limit`. Called like `hubspot_api._request`."""

    def __init__(self, tickets: dict) -> None:
        self.tickets = dict(tickets)  # HubSpot id -> hs_lastmodifieddate
        self.windows: list[int] = []

    def __call__(self, method: str, path: str, token: Any = None, body: Any = None) -> dict:
        assert (method, path) == ("POST", SEARCH_PATH)
        window = body["filterGroups"][0]["filters"][0]
        assert window["propertyName"] == "hs_lastmodifieddate" and window["operator"] == "GT"
        self.windows.append(window["value"])
        hits = sorted(
            (source.iso_to_ms(modified), ticket_id, modified)
            for ticket_id, modified in self.tickets.items()
            if source.iso_to_ms(modified) > window["value"]
        )
        return {
            "results": [
                {"id": ticket_id, "properties": {"hs_lastmodifieddate": modified}}
                for _ms, ticket_id, modified in hits[: body["limit"]]
            ]
        }


def after_the_flip(world: OsWorld, tickets: dict) -> dict:
    """The HubSpot path's decision after a switch back to `hubspot`: its own
    loader over the file the finalizer wrote, its own candidate rule, and a
    HubSpot that holds `tickets` and honours the search window."""
    state = osm.pg._load_state(world.state_path)
    search = FilteringSearch(tickets)
    decision = osm.pg.hubspot_candidates("hubspot", HUBSPOT_SETTINGS, state, "test-token", transport=search)
    assert len(search.windows) == 1
    return decision


def test_the_filtering_search_honours_the_window_it_is_asked_with(world: OsWorld) -> None:
    # the fake's own rule, so a green result below cannot come from a fake that returns everything
    world.state_path.write_text(json.dumps({"last_run_at": NOON, "ticket_fingerprints": {}}), encoding="utf-8")
    decision = after_the_flip(world, {"1": ELEVEN, "2": NOON, "3": plus_ms(NOON, 1)})
    assert decision["candidates"] == ["3"]  # GT: neither the earlier ticket nor the one AT the window's start


def test_a_flip_back_selects_the_ticket_the_os_pass_never_listed_and_not_the_completed_one(world: OsWorld) -> None:
    """The review's probe, as the acceptance case. One completed ticket modified
    at 12:00, one ticket the pass never listed modified at 11:00."""
    empty_state(world)
    done = make_entry(0, payload_edit=lambda payload: payload.update(hubspot_updated_at=NOON))

    summary = finalize(world, [done], all_completed(done))

    assert summary.tickets[0]["writeback"] == os_pass.WB_PROCESSED
    state = read(world.state_path)
    assert state["ticket_fingerprints"] == {osm.T1_HS_ID: code_written(STAMP, NOON)}

    decision = after_the_flip(world, {osm.T1_HS_ID: NOON, UNLISTED_HS_ID: ELEVEN})
    assert decision["candidates"] == [UNLISTED_HS_ID]  # the unlisted ticket IS selected
    assert decision["matched"] == [
        {"id": UNLISTED_HS_ID, "hs_lastmodifieddate": ELEVEN, "deduped": False},
        {"id": osm.T1_HS_ID, "hs_lastmodifieddate": NOON, "deduped": True},  # completed: not processed again
    ]
    assert decision["decision"] == "run_agent"

    # ... and a completed ticket HubSpot reports a NEWER modified time for is picked up again
    changed = plus_ms(STAMP, 1)
    decision = after_the_flip(world, {osm.T1_HS_ID: changed, UNLISTED_HS_ID: ELEVEN})
    assert decision["candidates"] == [UNLISTED_HS_ID, osm.T1_HS_ID]
    assert state["last_run_at"] == STORED_LAST_RUN  # the pass wrote a fingerprint and moved nothing


def test_a_flip_back_selects_a_ticket_that_was_parked_before_a_later_pass_completed_another(world: OsWorld) -> None:
    """A parked ticket is exactly what a rollback is for. Ticket 1 fails its
    pass and is then no longer listed (the OS parks it); a later pass completes
    ticket 3, which HubSpot modified AFTER ticket 1. Ticket 1 must still be
    inside the HubSpot search after the switch back."""
    empty_state(world)
    parked, later = make_entry(0), make_entry(2)
    assert source.iso_to_ms(T1_UPDATED) < source.iso_to_ms(T3_UPDATED)

    finalize(world, [parked], result_of(failed(parked)))  # the pass that failed it
    finalize(world, [later], all_completed(later))  # a pass that no longer lists it

    state = read(world.state_path)
    assert set(state["ticket_fingerprints"]) == {osm.T3_HS_ID}
    decision = after_the_flip(world, {osm.T1_HS_ID: T1_UPDATED, osm.T3_HS_ID: T3_UPDATED})
    assert decision["candidates"] == [osm.T1_HS_ID]
    assert state["last_run_at"] == STORED_LAST_RUN


@pytest.mark.parametrize("stored", ["", "garbage", None, "2026-09-01T00:00:00Z"])
def test_an_os_pass_leaves_last_run_at_exactly_as_it_found_it(world: OsWorld, stored: Any) -> None:
    """Readable or not, present or not: the key is the HubSpot path's. With an
    unreadable stored value the old rule wrote the newest completed ticket's
    time outright, the same jump with no stored window to hold it back."""
    before = {"run_count": 7, "ticket_fingerprints": {}}
    if stored is not None:
        before["last_run_at"] = stored
    world.state_path.write_text(json.dumps(before), encoding="utf-8")
    entries = [make_entry(0), make_entry(2)]

    finalize(world, entries, all_completed(*entries))

    after = read(world.state_path)
    assert set(after["ticket_fingerprints"]) == {osm.T1_HS_ID, osm.T3_HS_ID}
    assert {key: value for key, value in after.items() if key != "ticket_fingerprints"} == {
        key: value for key, value in before.items() if key != "ticket_fingerprints"
    }


def test_no_code_hands_the_state_writer_a_search_window() -> None:
    """Structural: every call of `merge_fingerprints` in the package passes the
    path and the entries and nothing else, and the rule that computed a window
    is gone, so a later edit cannot quietly start moving `last_run_at` again."""
    package = Path(os_pass.__file__).resolve().parent.parent
    calls = []
    for folder in ("ticket_pipeline", "scripts", "packn_os_hubspot_client"):
        for module in sorted((package / folder).glob("*.py")):
            for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
                if name == "merge_fingerprints":
                    calls.append((module.name, len(node.args), [keyword.arg for keyword in node.keywords]))
    assert calls == [("os_pass.py", 2, [])]
    assert not hasattr(os_pass, "_window_start")
