"""What the engine shadow leaves on disk (SEC-OPEN-2 and SEC-OPEN-3, owner ruling 2026-10-14).

Each engine-shadow artifact holds a ticket's full context and a draft. On
2026-10-14 the droplet held 1,664 of them from 2026-07-06 on: never removed,
mode 664, and not ignored by git. Now the directory is private to the cron's
user, run directories older than SHADOW_RETENTION_DAYS are removed at the start
of each step, and git ignores it.

Nothing here reaches the network, a database or a model.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # hubspot_ticket_automation/
sys.path.insert(0, str(PACKAGE_ROOT))
sys.path.insert(0, str(PACKAGE_ROOT / "scripts"))

from ticket_pipeline import run as engine  # noqa: E402

NOW = datetime(2026, 10, 14, 16, 0, 0, tzinfo=timezone.utc)
posix_only = pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX; the droplet is Linux")


def _run_dir(base: Path, name: str) -> Path:
    directory = base / name
    directory.mkdir(parents=True)
    (directory / "123.json").write_text("{}", encoding="utf-8")
    return directory


def test_run_directories_older_than_the_retention_are_removed(tmp_path: Path) -> None:
    old = _run_dir(tmp_path, "2026-07-06T20-36-07Z")
    edge_old = _run_dir(tmp_path, "2026-09-14T15-59-59Z")  # 30 days and a second ago
    edge_kept = _run_dir(tmp_path, "2026-09-14T16-00-00Z")  # exactly 30 days ago
    recent = _run_dir(tmp_path, "2026-10-14T14-00-07Z")

    assert engine.prune_shadow_dir(now=NOW, base=tmp_path) == 2

    assert not old.exists() and not edge_old.exists()
    assert edge_kept.exists() and recent.exists()


def test_only_run_directories_are_touched(tmp_path: Path) -> None:
    other_dir = _run_dir(tmp_path, "keep-me")
    stray_file = tmp_path / "2026-01-01T00-00-00Z"  # a file with a run directory's name
    stray_file.write_text("x", encoding="utf-8")

    assert engine.prune_shadow_dir(now=NOW, base=tmp_path) == 0
    assert other_dir.exists() and stray_file.exists()


def test_a_missing_directory_is_nothing_to_do(tmp_path: Path) -> None:
    assert engine.prune_shadow_dir(now=NOW, base=tmp_path / "absent") == 0


def test_a_failed_prune_does_not_raise(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _run_dir(tmp_path, "2026-07-06T20-36-07Z")

    def refuses(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(engine.shutil, "rmtree", refuses)
    assert engine.prune_shadow_dir(now=NOW, base=tmp_path) == 0


def test_the_prune_runs_even_when_the_shadow_is_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old = _run_dir(tmp_path / "shadow", "2026-07-06T20-36-07Z")
    settings = tmp_path / "settings.yaml"
    settings.write_text("pipeline:\n  shadow_enabled: false\n", encoding="utf-8")
    monkeypatch.setattr(engine, "SHADOW_DIR", tmp_path / "shadow")
    monkeypatch.setattr(engine, "SETTINGS_PATH", settings)

    assert engine.run_shadow() == 0
    assert not old.exists()


@posix_only
def test_artifacts_are_written_private(tmp_path: Path) -> None:
    directory = tmp_path / "shadow" / "2026-10-14T16-00-00Z"
    engine._make_private_dir(directory)
    target = directory / "123.json"
    target.write_text("old", encoding="utf-8")
    os.chmod(target, 0o664)  # an existing file with the old, wider mode

    engine._write_private(target, '{"ticket_context": {}}')

    assert target.read_text(encoding="utf-8") == '{"ticket_context": {}}'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@posix_only
def test_the_prune_closes_the_directory_to_other_users(tmp_path: Path) -> None:
    # The files already on the droplet are 664; a 0700 parent is what keeps them from other users.
    os.chmod(tmp_path, 0o775)
    engine.prune_shadow_dir(now=NOW, base=tmp_path)
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    "path",
    [
        "outputs/shadow/2026-10-14T16-00-00Z/123.json",
        "config/.action_items_forwarded.json.tmp",
        "config/.action_items_retry.json.tmp",
        "config/.state.json.k3j2h1.tmp",
    ],
)
def test_git_ignores_what_holds_ticket_text_or_runtime_ids(path: str) -> None:
    result = subprocess.run(
        ["git", "check-ignore", "-q", path], cwd=PACKAGE_ROOT, capture_output=True, check=False
    )
    if result.returncode == 128:
        pytest.skip("not inside a git checkout")
    assert result.returncode == 0, f"{path} is not ignored"
