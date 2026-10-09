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

import builtins
import inspect
import json
import os
from pathlib import Path
from typing import Any, Iterator

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
from ticket_pipeline import source

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
