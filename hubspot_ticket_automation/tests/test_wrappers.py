"""The two shell entry scripts and the skill's reworded steps (Phase 93, plan 15).

A scheduled tick (`scripts/run_tickets.sh`) and a pressed Run now
(`scripts/poll_manual_runs.sh`) both enter through the Python orchestrator,
`scripts/run_tickets.py`. Neither shell script launches the agent, the pre-gate
or the engine-shadow pipeline itself any more.

These tests read the two scripts and the skill as TEXT. Nothing here executes a
run: the only process started is `bash -n`, a syntax check that runs no command
of the script it reads.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # hubspot_ticket_automation/
sys.path.insert(0, str(PACKAGE_ROOT / "scripts"))  # scripts/ is imported flat, as run.py does

import pregate_tickets as pg  # noqa: E402
import run_tickets as rt  # noqa: E402

RUN_TICKETS_SH = PACKAGE_ROOT / "scripts" / "run_tickets.sh"
POLL_SH = PACKAGE_ROOT / "scripts" / "poll_manual_runs.sh"
SKILL = PACKAGE_ROOT / ".claude" / "skills" / "hubspot-tickets" / "SKILL.md"

ORCHESTRATOR_CALL = '.venv/bin/python scripts/run_tickets.py "$@" || RUN_STATUS=$?'
FORWARD_CALL = ".venv/bin/python scripts/forward_action_items.py"
COMPLAINTS_CALL = ".venv/bin/python scripts/write_complaints.py"
MANUAL_CALL = 'exec bash scripts/run_tickets.sh --manual-request "$REQUEST_ID"'


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _commands(path: Path) -> list[str]:
    """The script's lines that are not blank and not a comment, as written."""
    return [
        line
        for line in _text(path).split("\n")
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _arm(path: Path, name: str) -> list[str]:
    """The command lines of one `case` arm, stripped."""
    lines = _text(path).split("\n")
    start = next(i for i, line in enumerate(lines) if line.strip() == f"{name})")
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == ";;")
    body = [line.strip() for line in lines[start + 1 : end]]
    return [line for line in body if line and not line.startswith("#")]


# ------------------------------------------------------------ run_tickets.sh


def test_the_wrapper_keeps_its_name_and_the_crontab_marker() -> None:
    assert RUN_TICKETS_SH.is_file()
    assert pg.CRON_MARKER == rt.CRON_MARKER == RUN_TICKETS_SH.name == "run_tickets.sh"


def test_the_wrapper_still_sets_up_the_environment_as_before() -> None:
    commands = _commands(RUN_TICKETS_SH)
    assert _text(RUN_TICKETS_SH).startswith("#!/bin/bash\n")
    assert "set -u" in commands
    assert 'cd "$(dirname "$0")/.."' in commands
    assert "if [ -r /etc/environment ]; then set -a; . /etc/environment; set +a; fi" in commands


def test_the_wrapper_calls_the_orchestrator_once_and_passes_its_arguments_through() -> None:
    commands = _commands(RUN_TICKETS_SH)
    assert commands.count(ORCHESTRATOR_CALL) == 1
    assert sum("run_tickets.py" in line for line in commands) == 1
    assert "RUN_STATUS=0" in commands
    assert commands.index("RUN_STATUS=0") < commands.index(ORCHESTRATOR_CALL)


def test_the_two_post_steps_always_run_after_the_orchestrator() -> None:
    commands = _commands(RUN_TICKETS_SH)
    orchestrator = commands.index(ORCHESTRATOR_CALL)
    forward = commands.index(FORWARD_CALL)
    complaints = commands.index(COMPLAINTS_CALL)
    assert orchestrator < forward < complaints
    # whatever the orchestrator returned: nothing between it and the end can skip them
    between = commands[orchestrator + 1 : complaints + 1]
    assert between == [FORWARD_CALL, COMPLAINTS_CALL]
    environment = commands.index("if [ -r /etc/environment ]; then set -a; . /etc/environment; set +a; fi")
    assert environment < orchestrator
    assert not [line for line in commands[:complaints] if re.match(r"\s*(exit|return)\b", line)]


def test_the_wrapper_exits_with_the_orchestrators_status() -> None:
    commands = _commands(RUN_TICKETS_SH)
    assert commands[-1] == 'exit "$RUN_STATUS"'
    assert commands.index(COMPLAINTS_CALL) == len(commands) - 2


def test_the_wrapper_no_longer_launches_the_pregate_the_pipeline_or_the_agent() -> None:
    commands = "\n".join(_commands(RUN_TICKETS_SH))
    assert "pregate_tickets.py" not in commands
    assert "run_pipeline.py" not in commands
    assert "claude" not in commands
    assert "PREGATE_EXIT" not in commands
    assert "--model" not in commands


def test_the_wrappers_header_says_why_the_filename_stays() -> None:
    text = _text(RUN_TICKETS_SH)
    assert "scripts/run_tickets.py" in text
    assert "filename" in text and "crontab" in text
    # the crontab example still names this script
    assert "bash scripts/run_tickets.sh >> outputs/runs/cron-tickets.log 2>&1" in text


# -------------------------------------------------------- poll_manual_runs.sh


def test_run_now_goes_through_the_wrapper_with_the_claimed_request_id() -> None:
    assert _arm(POLL_SH, "tickets-process") == [MANUAL_CALL]


def test_run_now_no_longer_launches_the_agent_directly() -> None:
    arm = "\n".join(_arm(POLL_SH, "tickets-process"))
    assert "claude" not in arm
    assert "/packn-tickets" not in "\n".join(_commands(POLL_SH))


def test_the_digest_arm_is_unchanged() -> None:
    assert _arm(POLL_SH, "digest") == [
        "exec claude -p /packn-digest --model claude-sonnet-5 --dangerously-skip-permissions"
    ]


def test_the_poll_script_still_claims_one_request_and_reads_its_id() -> None:
    text = _text(POLL_SH)
    assert "client.claim_pending_manual_run()" in text
    assert "REQUEST_ID=$(echo \"$CLAIMED\" | py -c \"import sys,json; print(json.load(sys.stdin)['request_id'])\")" in text
    assert text.index("REQUEST_ID=$(") < text.index(MANUAL_CALL)


def test_the_poll_scripts_comments_describe_the_wrapper_not_the_old_launch() -> None:
    text = _text(POLL_SH)
    assert "is a v2 enhancement" not in text  # the orchestrator links the run now
    assert "mark_manual_run_completed" in text
    assert "scripts/run_tickets.py" in text


@pytest.mark.parametrize("script", [RUN_TICKETS_SH, POLL_SH], ids=["run_tickets.sh", "poll_manual_runs.sh"])
def test_both_scripts_pass_a_shell_syntax_check(script: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash on PATH: the syntax check needs a shell")
    probe = subprocess.run([bash, "--version"], capture_output=True, text=True, timeout=60)
    if probe.returncode != 0 or "GNU bash" not in probe.stdout:
        pytest.skip("the `bash` on PATH is not a usable GNU bash (a launcher with no shell behind it)")
    # -n reads the script and runs none of it. The text goes in on stdin with Unix
    # line endings, as the droplet has them.
    checked = subprocess.run(
        [bash, "-n"], input=_text(script).encode("utf-8"), capture_output=True, timeout=60
    )
    assert checked.returncode == 0, checked.stderr.decode("utf-8", "replace")


def test_the_syntax_check_would_catch_a_broken_script() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash on PATH: the syntax check needs a shell")
    probe = subprocess.run([bash, "--version"], capture_output=True, text=True, timeout=60)
    if probe.returncode != 0 or "GNU bash" not in probe.stdout:
        pytest.skip("the `bash` on PATH is not a usable GNU bash (a launcher with no shell behind it)")
    broken = subprocess.run([bash, "-n"], input=b'if [ -z "$X" ]; then\n', capture_output=True, timeout=60)
    assert broken.returncode != 0


# ------------------------------------------------------- the skill, four edits


def _skill() -> str:
    return _text(SKILL)


def _section(text: str, heading: str, level: str = "## ") -> str:
    start = text.index(heading)
    following = text.find(f"\n{level}", start + 1)
    return text[start:] if following == -1 else text[start:following]


def test_skill_step_0_is_skipped_under_the_wrapper() -> None:
    step_0 = _section(_skill(), "## Step 0")
    assert "PACKN_RUN_ID" in step_0
    assert "SKIP Steps 0a and 0b" in step_0
    assert "do NOT write a `skipped` run record" in step_0
    # the rerun poll stays, in both cases
    assert "client.read_pending_rerun_requests('tickets-process')" in step_0
    assert "client.mark_rerun_processed(" in step_0
    # a hand-run still runs the gate as written
    assert "client.read_routine_enabled('tickets-process')" in step_0


def test_skill_crontab_marker_is_the_wrappers_filename() -> None:
    text = _skill()
    assert "cron_tickets.sh" not in text
    assert "client.report_routine_schedule('tickets-process', 'run_tickets.sh')" in text


def test_skill_both_draft_shell_outs_call_write_cron_draft() -> None:
    step_2f = _section(_skill(), "#### 2f.", level="#### ")
    assert step_2f.count("draft_id = client.write_cron_draft(") == 2
    assert "draft_id = client.write_draft(" not in step_2f
    assert "client.write_draft(" not in _skill().replace("`client.write_draft(...)`", "")
    # the arguments are the ones the plain write takes
    for argument in (
        "ticket_id='<ticket_id>'",
        "routine_name='tickets-process'",
        "model='claude-sonnet-5'",
        "prompt_version='v3.3.0'",
    ):
        assert step_2f.count(argument) == 2
    # and one sentence says why
    assert "replaces the earlier pending draft" in step_2f
    assert "exactly as before" in step_2f


def test_skill_reruns_are_drafted_through_the_same_call() -> None:
    step_0 = _section(_skill(), "## Step 0")
    assert "client.write_cron_draft(...)" in step_0
    assert "client.write_draft(" not in step_0


def test_skill_step_4_5_writes_the_result_file_under_the_wrapper() -> None:
    step = _section(_skill(), "### 4.5.", level="### ")
    assert "PACKN_RESULT_PATH" in step
    assert "do NOT call `client.write_run_record`" in step
    for key in (
        "'run_id'",
        "'status'",
        "'tickets_processed'",
        "'drafts_created'",
        "'error_summary'",
        "'tickets'",
        "'ref'",
        "'outcome'",
        "'classification'",
        "'draft_id'",
        "'action_items'",
    ):
        assert key in step, key
    for word in ("completed", "failed", "category", "form_topic", "priority", "confidence", "reason"):
        assert word in step, word
    # a hand-run still writes the record itself, as before
    assert "client.write_run_record(" in step
    assert "routine_name='tickets-process'," in step


def test_skill_result_file_keeps_message_text_out_of_the_summary() -> None:
    step = _section(_skill(), "### 4.5.", level="### ")
    assert "codes, counts and ticket ids only" in step


def test_skill_model_pin_matches_the_orchestrators() -> None:
    assert f"model='{rt.AGENT_MODEL}'" in _skill()
