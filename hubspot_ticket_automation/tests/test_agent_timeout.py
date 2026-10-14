"""The agent launcher's timeout (REV-OPEN-6, owner ruling 2026-10-14).

On 2026-10-12 a run was found hung on the droplet for 49 days: `claude -p` never
returned, so the run never wrote a record. The launcher now waits at most
AGENT_TIMEOUT_SECONDS, then stops the agent and everything it started (its own
process group), and raises AgentTimedOut. The orchestrator turns that into one
`agent_exited_abnormally` record (tests/test_run_record.py).

These tests start real child processes: a short Python sleep stands in for the
agent, with the timeout shrunk to a second. Nothing here reaches the network, a
database or the real agent.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # hubspot_ticket_automation/
sys.path.insert(0, str(PACKAGE_ROOT / "scripts"))  # scripts/ is imported flat, as run.py does

import run_tickets as rt  # noqa: E402


@pytest.fixture
def short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rt, "AGENT_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(rt, "AGENT_KILL_GRACE_SECONDS", 2)


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_an_agent_that_finishes_in_time_returns_its_exit_code(short_timeout: None) -> None:
    assert rt.launch_agent(_python("import sys; sys.exit(3)"), dict(os.environ)) == 3


def test_an_agent_that_runs_past_the_timeout_is_stopped_and_raises(short_timeout: None) -> None:
    started = time.monotonic()
    with pytest.raises(rt.AgentTimedOut):
        rt.launch_agent(_python("import time; time.sleep(60)"), dict(os.environ))
    assert time.monotonic() - started < 15


def test_an_agent_that_ignores_sigterm_is_still_stopped(short_timeout: None) -> None:
    code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    started = time.monotonic()
    with pytest.raises(rt.AgentTimedOut):
        rt.launch_agent(_python(code), dict(os.environ))
    assert time.monotonic() - started < 15


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX; the droplet is Linux")
def test_what_the_agent_started_is_stopped_with_it(short_timeout: None, tmp_path: Path) -> None:
    # The 49-day hang left a `tail -f` and the CLI's own children behind. The
    # agent here starts a grandchild, writes its pid, and hangs.
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({str(pid_file)!r}, 'w').write(str(child.pid)); "
        "time.sleep(60)"
    )
    with pytest.raises(rt.AgentTimedOut):
        rt.launch_agent(_python(code), dict(os.environ))
    grandchild = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(grandchild, 9)
    pytest.fail("the agent's own child outlived the timeout")


def test_an_engine_shadow_step_that_hangs_is_given_up_without_failing_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict = {}

    def hangs(*args, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr(rt.subprocess, "run", hangs)
    rt.run_engine_shadow_step(dict(os.environ))  # does not raise
    assert seen["timeout"] == rt.ENGINE_SHADOW_TIMEOUT_SECONDS
    assert rt.ENGINE_SHADOW_TIMEOUT_SECONDS < rt.AGENT_TIMEOUT_SECONDS
