"""The retired close envelope has no sender in the cron (Phase 93, plan 19; SPEC R13).

Pack'N OS used to accept a second envelope type on its action-item ingest route,
one that told it a HubSpot ticket had closed. The cron held a sender for it and a
command-line mode to call that sender, and nothing ever called either. Pack'N OS
now answers that envelope with 400 (plan 93-06); this file proves the cron side
is gone too, and stays gone:

  * static: no tracked file under the four code and prompt directories holds the
    envelope's type, the removed function's name, or the removed command-line
    mode's name;
  * the poster's command line refuses the retired mode's name as an unknown mode,
    with the usage exit code 2;
  * the mode that still exists runs as it did.

The three search strings are assembled from halves, so this file does not hold
the strings it searches for and a wider scan would not match it.

The command-line cases run a COPY of the poster from a temp directory. The script
works out its secret and settings paths from its own location, so a copy can reach
neither the real secret file nor the real settings, whichever branch an argument
reaches. That matters for the retired mode: while its branch was still reachable
it went on to the secret read, and with no secret the command exits 1. Exit 1 is
therefore exactly the code this file must NOT accept, and "non-zero" would prove
nothing. Nothing here sends a request: without `--send` the poster is in its
dry-run path, and with no items it never reaches a post at all.
"""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

import pytest

ROOT = Path(__file__).resolve().parent.parent  # hubspot_ticket_automation/
POSTER = ROOT / "scripts" / "post_action_items.py"
SKILL = ROOT / ".claude" / "skills" / "hubspot-tickets" / "SKILL.md"

SCANNED_DIRECTORIES = ("scripts", "ticket_pipeline", "packn_os_hubspot_client", ".claude")

# Assembled from halves: see the module docstring.
_FIRST = "ticket"
_SECOND = "closed"
RETIRED_TYPE = _FIRST + "_" + _SECOND
RETIRED_MODE = _FIRST + "-" + _SECOND
RETIRED_FUNCTION = "post" + "_" + RETIRED_TYPE

LIVING_MODE = "action-items"
USAGE_EXIT = 2
CHILD_TIMEOUT_S = 60


# --------------------------------------------------------------------------- #
# The static scan
# --------------------------------------------------------------------------- #


def tracked_files(directories: Iterable[str] = SCANNED_DIRECTORIES) -> list[Path]:
    """The tracked files under the given directories of the package root."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", *directories],
        cwd=ROOT,
        capture_output=True,
        check=True,
        timeout=CHILD_TIMEOUT_S,
    ).stdout.decode("utf-8")
    return [ROOT / name for name in listed.split("\0") if name]


def occurrences(needle: str, paths: Iterable[Path]) -> list[str]:
    """`<path>:<line number>` for every line of every file that holds `needle`.

    A file is read as bytes and decoded leniently, so a file that is not UTF-8
    is still searched and never skipped. The line's text is not returned: a
    failure names the place, not the content.
    """
    found: list[str] = []
    for path in paths:
        text = path.read_bytes().decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if needle in line:
                found.append(f"{path.as_posix()}:{number}")
    return found


def test_the_scan_covers_the_four_directories_and_the_files_that_matter() -> None:
    files = tracked_files()
    names = {path.relative_to(ROOT).as_posix() for path in files}
    assert len(files) > 40, "git ls-files returned too little to be the four directories"
    for directory in SCANNED_DIRECTORIES:
        assert any(name.startswith(directory + "/") for name in names), directory
    assert "scripts/post_action_items.py" in names
    assert "scripts/forward_action_items.py" in names
    assert ".claude/skills/hubspot-tickets/SKILL.md" in names
    assert ".claude/skills/hubspot-tickets-os/SKILL.md" in names
    assert ".claude/commands/packn-tickets.md" in names
    assert all(path.is_file() for path in files)


def test_the_scan_finds_a_planted_line_and_names_its_place(tmp_path: Path) -> None:
    clean = tmp_path / "clean.py"
    clean.write_text("print('nothing here')\n", encoding="utf-8")
    planted = tmp_path / "planted.py"
    planted.write_text(
        "first = 1\n" + f'envelope = {{"type": "{RETIRED_TYPE}"}}\n' + "last = 3\n",
        encoding="utf-8",
    )
    not_utf8 = tmp_path / "legacy.txt"
    not_utf8.write_bytes(b"\xff\xfe caf\xe9 " + RETIRED_FUNCTION.encode("ascii") + b"\n")

    assert occurrences(RETIRED_TYPE, [clean]) == []
    assert occurrences(RETIRED_TYPE, [clean, planted]) == [f"{planted.as_posix()}:2"]
    assert occurrences(RETIRED_FUNCTION, [not_utf8]) == [f"{not_utf8.as_posix()}:1"]


def test_no_tracked_file_holds_the_retired_envelope_type() -> None:
    """Quoted or bare: the type's name is in no script, module, prompt or command."""
    assert occurrences(RETIRED_TYPE, tracked_files()) == []


def test_no_tracked_file_names_the_removed_function() -> None:
    assert occurrences(RETIRED_FUNCTION, tracked_files()) == []


def test_no_tracked_file_names_the_retired_command_line_mode() -> None:
    assert occurrences(RETIRED_MODE, tracked_files()) == []


# --------------------------------------------------------------------------- #
# The poster module
# --------------------------------------------------------------------------- #


def _poster_tree() -> ast.Module:
    return ast.parse(POSTER.read_text(encoding="utf-8"))


def test_the_poster_defines_the_action_items_sender_and_no_close_sender() -> None:
    functions = {
        node.name for node in _poster_tree().body if isinstance(node, ast.FunctionDef)
    }
    assert {"build_envelope", "post_action_items", "_post", "_cli"} <= functions
    assert [name for name in sorted(functions) if _SECOND in name] == []


def test_the_usage_block_lists_only_the_mode_that_exists() -> None:
    docstring = ast.get_docstring(_poster_tree()) or ""
    usage = [line.strip() for line in docstring.splitlines() if "scripts/post_action_items.py" in line]
    assert len(usage) == 2, usage
    for line in usage:
        assert f"scripts/post_action_items.py {LIVING_MODE} " in line, line
    assert _SECOND not in docstring


# --------------------------------------------------------------------------- #
# The command line
# --------------------------------------------------------------------------- #


@pytest.fixture
def poster_copy(tmp_path: Path) -> Path:
    """The poster's own bytes, under a temp root that holds no secret and no settings."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    copy = scripts / POSTER.name
    shutil.copyfile(POSTER, copy)
    assert copy.read_bytes() == POSTER.read_bytes()
    assert not (tmp_path / "config").exists()
    return copy


def _run(copy: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(copy), *arguments],
        cwd=copy.parent.parent,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=CHILD_TIMEOUT_S,
    )


def _text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def test_the_retired_mode_is_refused_as_an_unknown_mode_with_the_usage_code(
    poster_copy: Path,
) -> None:
    done = _run(poster_copy, RETIRED_MODE, "0")
    stderr = _text(done.stderr)

    # Exactly the usage code. 1 is what a post that could not be made returns
    # (no secret file), which is where a still-reachable branch would end.
    assert done.returncode == USAGE_EXIT, (done.returncode, stderr)
    assert "unknown mode" in stderr
    # The message names the argument it was given, once, and offers one mode.
    assert stderr.count(RETIRED_MODE) == 1
    assert f"'{RETIRED_MODE}'" in stderr
    assert f"expected {LIVING_MODE}" in stderr
    # Nothing was attempted: no result is printed and no secret was looked for.
    assert _text(done.stdout).strip() == ""
    assert "secret" not in stderr


def test_the_retired_mode_is_refused_with_send_too(poster_copy: Path) -> None:
    done = _run(poster_copy, RETIRED_MODE, "0", "--send")
    assert done.returncode == USAGE_EXIT, (done.returncode, _text(done.stderr))
    assert "unknown mode" in _text(done.stderr)
    assert _text(done.stdout).strip() == ""


def test_no_argument_prints_a_usage_line_that_names_only_the_living_mode(
    poster_copy: Path,
) -> None:
    done = _run(poster_copy)
    stderr = _text(done.stderr)
    assert done.returncode == USAGE_EXIT
    assert "usage:" in stderr
    assert LIVING_MODE in stderr
    assert _SECOND not in stderr


def test_the_living_mode_still_runs_from_the_command_line(poster_copy: Path) -> None:
    """No items on stdin is the poster's no-op: exit 0, nothing signed, nothing sent."""
    done = _run(poster_copy, LIVING_MODE, "1234567890")
    assert done.returncode == 0, _text(done.stderr)
    assert json.loads(_text(done.stdout)) == {"ok": True, "mode": "noop", "reason": "no_items"}


def test_the_living_mode_without_a_ticket_id_is_a_usage_error(poster_copy: Path) -> None:
    done = _run(poster_copy, LIVING_MODE)
    assert done.returncode == USAGE_EXIT
    assert f"usage: post_action_items.py {LIVING_MODE} " in _text(done.stderr)


# --------------------------------------------------------------------------- #
# The skill
# --------------------------------------------------------------------------- #


def _step_2g5() -> str:
    text = SKILL.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index("\n#### 2g.5. ")
    end = text.index("\n#### 2h. ", start)
    return text[start:end]


def test_step_2g5_has_no_paragraph_about_a_close_signal() -> None:
    section = _step_2g5()
    assert "DEFERRED" not in section
    assert _SECOND not in section.lower()
    assert "ready to wire" not in section


def test_the_rest_of_step_2g5_is_still_there() -> None:
    section = _step_2g5()
    assert "**You (the agent) do NOTHING in this step.**" in section
    assert "`scripts/forward_action_items.py`" in section
    assert section.rstrip().endswith(
        "(secret/url/zod/network) in the cron log instead of swallowing them behind a false \"posted\" claim."
    )
