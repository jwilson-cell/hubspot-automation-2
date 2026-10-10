"""The cron repo's prompts and docs say what the code does (Phase 93, plan 19).

Wrong text here becomes wrong behaviour: an agent told to send to a customer, an
owner told the server updates itself, a deploy made without a grant. These are
text assertions over the command file, the skill, CLAUDE.md, the two READMEs and
the server runbook. Where a doc states something the code also states (a launch
command line, a variable name, an exit code, a table the helper touches), the
test takes the value from the code, so the doc cannot drift from it unnoticed.

Nothing here opens a secret file, a database or a connection. Two tests ask git
which files are tracked or ignored.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # hubspot_ticket_automation/
sys.path.insert(0, str(PACKAGE_ROOT / "scripts"))  # scripts/ is imported flat, as run.py does

import forward_action_items  # noqa: E402
import run_tickets  # noqa: E402
from packn_os_hubspot_client import tickets_api  # noqa: E402
from ticket_pipeline import os_pass, run_types, source  # noqa: E402

COMMAND = PACKAGE_ROOT / ".claude" / "commands" / "packn-tickets.md"
OS_COMMAND = PACKAGE_ROOT / ".claude" / "commands" / "packn-tickets-os.md"
SKILL = PACKAGE_ROOT / ".claude" / "skills" / "hubspot-tickets" / "SKILL.md"
OS_SKILL = PACKAGE_ROOT / ".claude" / "skills" / "hubspot-tickets-os" / "SKILL.md"
CLAUDE_MD = PACKAGE_ROOT / "CLAUDE.md"
README = PACKAGE_ROOT / "README.md"
HELPER_README = PACKAGE_ROOT / "packn_os_hubspot_client" / "README.md"
SERVER_SETUP = PACKAGE_ROOT / "docs" / "server-setup.md"
CLIENT = PACKAGE_ROOT / "packn_os_hubspot_client" / "client.py"

PHASE_DOCS = (COMMAND, OS_COMMAND, SKILL, OS_SKILL, CLAUDE_MD, README, HELPER_README, SERVER_SETUP)

GUARD_COMMAND = "py -m ticket_pipeline.source --require hubspot,shadow"
CRON_GATE = (
    "py -3 -m pytest tests/ packn_os_hubspot_client/tests/test_pregate.py "
    "packn_os_hubspot_client/tests/test_pipeline.py -q -p no:cacheprovider"
)
SECRET_FILE_NAME = tickets_api.SECRET_PATH.name
SECRET_ENV_NAME = "TICKETS_API_SECRET"
SUPERSEDE_COLUMNS = ("state", "superseded_at", "superseded_by_draft_id", "updated_at")


def _md(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _section(text: str, heading: str) -> str:
    """From `heading`'s own line to the next heading of the same or a higher
    level. An empty string when the heading is not in the text.

    A line inside a fenced code block is never a heading: a shell comment such
    as `# wait for the next tick` would otherwise end the section early.
    """
    lines = text.split("\n")
    if heading not in lines:
        return ""
    start = lines.index(heading)
    depth = len(heading) - len(heading.lstrip("#"))
    fenced = False
    for end in range(start + 1, len(lines)):
        if lines[end].lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        marks = re.match(r"^(#{1,6}) ", lines[end])
        if marks and len(marks.group(1)) <= depth:
            return "\n".join(lines[start:end])
    return "\n".join(lines[start:])


def _numbered_steps(text: str) -> dict[int, str]:
    """The top-level numbered items of a Markdown text, each with its own line only."""
    steps: dict[int, str] = {}
    for line in text.split("\n"):
        found = re.match(r"^(\d+)\. (.*)$", line)
        if found:
            steps[int(found.group(1))] = found.group(2)
    return steps


def test_the_section_reader_stops_at_the_next_heading_of_its_level() -> None:
    text = "# A\n\n## One\nfirst\n### Inner\ninside\n## Two\nsecond\n"
    assert _section(text, "## One") == "## One\nfirst\n### Inner\ninside"
    assert _section(text, "### Inner") == "### Inner\ninside"
    assert _section(text, "## Missing") == ""


def test_the_section_reader_does_not_take_a_fenced_comment_for_a_heading() -> None:
    text = "## One\nbefore\n```bash\n# a shell comment\nrun\n```\nafter\n## Two\n"
    assert _section(text, "## One") == "## One\nbefore\n```bash\n# a shell comment\nrun\n```\nafter"


# ======================================================================================
# The command file: .claude/commands/packn-tickets.md
# ======================================================================================


def test_the_command_names_the_two_sources_it_serves_and_points_to_the_os_command() -> None:
    text = _md(COMMAND)
    assert "serves the `hubspot` and `shadow` ticket sources" in text
    assert "does not serve `os`" in text
    assert "/packn-tickets-os" in text
    assert "scripts/run_tickets.sh" in text and "scripts/run_tickets.py" in text


def test_the_commands_first_step_is_the_source_guard_and_a_stop_rule() -> None:
    text = _md(COMMAND)
    steps = _numbered_steps(text)
    assert sorted(steps) == list(range(1, len(steps) + 1)), "the steps are numbered 1..n"
    first = steps[1]
    assert GUARD_COMMAND in first
    assert "FIRST" in first
    assert "stop at once and do nothing else" in first
    assert text.count(GUARD_COMMAND) == 1
    # the same command the skill itself runs first
    assert _md(SKILL).count(GUARD_COMMAND) == 1
    # no step that reads a ticket comes before it
    assert text.index(GUARD_COMMAND) < text.index("Fetch active-queue tickets")


def test_the_command_states_what_each_guard_exit_code_means() -> None:
    first = _numbered_steps(_md(COMMAND))[1]
    assert re.search(rf"`{source.GUARD_EXIT_SERVED}` = the source is served", first)
    assert f"`{source.GUARD_EXIT_NOT_SERVED}` = `{source.TOKEN_NOT_SERVED} <mode>`" in first
    assert (
        f"`{source.GUARD_EXIT_NOT_ESTABLISHED}` = `{source.TOKEN_UNKNOWN}` or `{source.TOKEN_UNREADABLE}`"
        in first
    )
    assert f"`{source.GUARD_EXIT_USAGE}` = a usage error" in first
    assert "never assume `hubspot`" in first


def test_the_command_does_not_describe_sending_a_reply_to_the_customer() -> None:
    text = _md(COMMAND)
    lowered = text.lower()
    for stale in (
        "auto-send",
        "auto_send",
        "auto-sent",
        "send_customer_reply",
        "packn_metadata_v1",
        "review before sending",
        "manage_crm_objects",
        "[draft",
        "note engagement",
    ):
        assert stale not in lowered, stale
    assert "Nothing is sent to a customer" in text
    assert "no note is posted on the HubSpot ticket" in text
    assert "pending draft in Pack'N OS" in text
    assert "`client.write_cron_draft`" in text


def test_the_command_describes_the_result_file_and_the_two_ways_it_is_started() -> None:
    text = _md(COMMAND)
    assert run_types.RESULT_PATH_ENV in text
    assert run_types.RUN_ID_ENV in text
    assert "do not write a run record" in text
    assert "hand-run" in text
    assert "bash scripts/run_tickets.sh" in text


def test_the_command_says_shadow_asks_nothing_new_of_the_agent() -> None:
    text = _md(COMMAND)
    assert "In `shadow` you do exactly what you do in `hubspot`." in text
    assert "done by code before and after you run" in text


def test_the_commands_summary_line_is_the_skills() -> None:
    line = "Processed N tickets — M drafts written to Pack'N OS, U urgent emails, Q tickets queued for digest."
    assert line in _md(COMMAND)
    assert line in _section(_md(SKILL), "### 5. Exit")
    assert "auto-sent to customer" not in _section(_md(SKILL), "### 5. Exit")


# ======================================================================================
# The skill: .claude/skills/hubspot-tickets/SKILL.md
# ======================================================================================

DEDUPE_HEADING = "#### 2a-pre. Early-exit dedupe check (before hydrating)"


def test_the_dedupe_step_states_the_rule_for_a_fingerprint_written_by_code() -> None:
    step = _section(_md(SKILL), DEDUPE_HEADING)
    assert step
    assert "`num_notes` is `null`" in step
    assert "written by code" in step
    assert "`processed_at`" in step
    # the rule, both ways
    assert "is not later than `fingerprint.processed_at`" in step
    assert "is later than `fingerprint.processed_at`" in step
    assert "skip this ticket without hydrating" in step
    # never read a null count as a number
    assert "Never compare a `null` note count" in step
    assert "never write a number into it" in step


def test_the_dedupe_step_defers_to_the_one_shared_function() -> None:
    step = _section(_md(SKILL), DEDUPE_HEADING)
    assert "`ticket_pipeline/source.py`" in step
    assert "`unprocessed_candidates`" in step
    assert callable(source.unprocessed_candidates)
    assert (PACKAGE_ROOT / "ticket_pipeline" / "source.py").is_file()


def test_the_numeric_dedupe_rule_is_still_stated_for_an_agent_written_fingerprint() -> None:
    step = _section(_md(SKILL), DEDUPE_HEADING)
    assert "`current_num_notes <= fingerprint.num_notes`" in step
    assert "`current_num_notes > fingerprint.num_notes`" in step
    assert "no fingerprint exists" in step


def test_the_manual_fingerprint_clear_is_limited_to_the_hubspot_source() -> None:
    step = _section(_md(SKILL), DEDUPE_HEADING)
    note = next(p for p in step.split("\n\n") if p.startswith("**Fingerprint reset**"))
    assert "manually clear" in note
    assert "`hubspot` source only" in note
    assert "`os`" in note


TOOL_NAMES_HEADING = "## HubSpot tool names on the droplet"


def test_the_skill_says_once_which_tool_prefix_the_droplet_has() -> None:
    text = _md(SKILL)
    note = _section(text, TOOL_NAMES_HEADING)
    assert note, "the note is its own section"
    assert "`mcp__hubspot__*`" in note
    assert "`mcp__claude_ai_HubSpot__*`" in note
    assert "locally registered" in note
    # once, and only there
    assert text.count("mcp__hubspot__") == note.count("mcp__hubspot__") == 1
    # after the guard (no HubSpot tool is named before it), before the first step that uses one
    assert text.index(GUARD_COMMAND) < text.index(TOOL_NAMES_HEADING) < text.index("## Discovery step")


def test_the_droplets_prefix_is_the_one_its_settings_allow() -> None:
    settings = (PACKAGE_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8")
    assert '"mcp__hubspot__*"' in settings
    assert os_pass.DISALLOWED_TOOLS == "mcp__hubspot"


# ======================================================================================
# CLAUDE.md
# ======================================================================================


def test_claude_md_does_not_say_the_server_pulls_on_each_cron_start() -> None:
    text = _md(CLAUDE_MD)
    assert not re.search(r"pulls from `?main`? each time", text)
    assert "each time cron starts" not in text
    assert "does not pull from git" in text
    assert "manual `git pull`" in text


def test_claude_md_says_replies_are_drafts_for_approval() -> None:
    summary = _section(_md(CLAUDE_MD), "## Architecture at a glance")
    assert summary
    lowered = summary.lower()
    assert "auto-sends the reply" not in lowered
    assert "posts a `[draft" not in lowered
    assert "pending draft" in lowered
    assert "Nothing is sent to a customer by this automation" in summary
    assert "approves and sends" in summary


def test_claude_md_has_the_ticket_source_paragraph() -> None:
    paragraph = _section(_md(CLAUDE_MD), "## Ticket source (Phase 93)")
    assert paragraph
    for mode in source.MODES:
        assert f"`{mode}`" in paragraph, mode
    assert f"`{source.SETTING_KEY}`" in paragraph
    assert "`app_settings`" in paragraph
    assert "Ticket flow card" in paragraph
    assert "email thread" in paragraph
    assert "empty order or tracking number" in paragraph
    assert "Phase 94" in paragraph
    assert "never falls back" in paragraph
    assert "`scripts/run_tickets.py`" in paragraph


@pytest.mark.parametrize(
    "path",
    [
        "scripts/run_tickets.sh",
        "scripts/run_tickets.py",
        "scripts/pregate_tickets.py",
        "scripts/forward_action_items.py",
        "scripts/post_action_items.py",
        "ticket_pipeline/source.py",
        "ticket_pipeline/os_pass.py",
        "ticket_pipeline/os_bundle.py",
        "ticket_pipeline/source_shadow.py",
        "packn_os_hubspot_client/tickets_api.py",
        ".claude/commands/packn-tickets-os.md",
        ".claude/skills/hubspot-tickets-os/SKILL.md",
    ],
)
def test_claude_md_lists_a_phase_93_file_that_exists(path: str) -> None:
    assert f"`{path}`" in _md(CLAUDE_MD), path
    assert (PACKAGE_ROOT / path).is_file(), path


def test_claude_md_names_the_new_secret_file_by_name_only() -> None:
    text = _md(CLAUDE_MD)
    assert f"`config/.secrets/{SECRET_FILE_NAME}`" in text


# ======================================================================================
# README.md — the Phase 93 section
# ======================================================================================

PHASE_93_HEADING = "## Phase 93 — ticket source"


def _phase_93() -> str:
    section = _section(_md(README), PHASE_93_HEADING)
    assert section, "the README has a Phase 93 section"
    return section


def test_the_readme_section_names_the_three_modes_and_the_setting() -> None:
    section = _phase_93()
    for mode in source.MODES:
        assert f"| `{mode}` |" in section, mode
    assert f"`{source.SETTING_KEY}`" in section
    assert "`app_settings`" in section
    assert "Ticket flow card" in section
    assert "never falls back to a mode" in section
    assert "HubSpot is not used instead" in section
    assert "until Phase 94" in section


def test_the_readme_section_describes_the_run_sequence_and_the_one_record() -> None:
    section = _phase_93()
    assert "python scripts/run_tickets.py [--manual-request <uuid>]" in section
    assert "bash scripts/run_tickets.sh [--manual-request <uuid>]" in section
    assert "writes exactly one automation_runs record" in section
    assert "scripts/forward_action_items.py" in section
    assert "scripts/write_complaints.py" in section
    assert "poll_manual_runs.sh" in section
    # the three runners, as the registry has them
    assert set(run_tickets.RUNNER_SPECS) == set(source.MODES)
    for mode, (module, function) in run_tickets.RUNNER_SPECS.items():
        target = f"`{function}`" if module is None else f"`{module}.{function}`"
        assert f"`{mode}` → {target}" in section, mode


def test_the_readme_launch_lines_are_the_codes() -> None:
    section = _phase_93()
    hubspot_line = " ".join(run_tickets.agent_command())
    os_line = " ".join(os_pass.OS_AGENT_COMMAND)
    assert f"`{hubspot_line}`" in section
    assert f"`{os_line}`" in section
    assert os_line.endswith("--disallowedTools mcp__hubspot")
    assert "--disallowedTools" not in hubspot_line


def test_the_readme_names_every_variable_a_run_exports() -> None:
    section = _phase_93()
    for name in (*run_types.RUN_ENV_NAMES, os_pass.BUNDLE_PATH_ENV):
        assert f"`{name}`" in section, name
    assert "None of them is a line in `.env`" in section


def test_the_readme_states_the_guards_exit_codes() -> None:
    section = _phase_93()
    assert "`py -m ticket_pipeline.source --require <modes>`" in section
    assert f"exit {source.GUARD_EXIT_SERVED} when the source is one the skill serves" in section
    assert f"{source.GUARD_EXIT_NOT_SERVED} with `{source.TOKEN_NOT_SERVED} <mode>`" in section
    assert f"{source.GUARD_EXIT_NOT_ESTABLISHED} when the setting is unknown or unreadable" in section
    assert f"{source.GUARD_EXIT_USAGE} for a usage error" in section


def test_the_readme_states_the_limit_of_removing_the_hubspot_tools() -> None:
    section = _phase_93()
    assert "closes the tool path, not the shell path" in section
    assert "the agent still has Bash and the HubSpot token file is on the droplet" in section
    assert "would not be counted" in section
    assert "which is Phase 94" in section
    # and it does not claim more than that
    assert "cannot reach HubSpot" not in section
    assert "has no access to HubSpot" not in section


@pytest.mark.parametrize(
    "fragment",
    [
        # os: no urgent email for a ticket with no HubSpot id
        "an urgent ticket with no HubSpot id sends no urgent solo email",
        # os: an agent that leaves no result is retried, not parked
        "when the agent leaves no result at all, nothing is reported as failed",
        "retried on the next run rather than parked",
        # shadow: classifier outage
        "a classifier outage means no comparison is posted for that run",
        "`source_shadow_skipped:classify_unavailable`",
        # shadow: pre-gate fail-open
        "a run whose pre-gate failed open is not compared",
        # shadow: the first run
        "It classifies up to 100 Pack'N OS tickets once before the agent starts",
        # rollback: hand-runs
        "can leave two pending drafts",
        "start a hand-run through the wrapper",
        "`rollback_entries_unreadable`",
        # counts
        "show no HubSpot call counts",
    ],
)
def test_the_readme_states_an_owner_visible_behaviour_plainly(fragment: str) -> None:
    assert fragment in _section(_phase_93(), "### What an operator should know")


def test_the_readme_names_the_secret_file_and_never_a_value() -> None:
    section = _phase_93()
    assert f"`config/.secrets/{SECRET_FILE_NAME}`" in section
    assert f"`{SECRET_ENV_NAME}`" in section
    assert "at least 32 characters" in section
    assert "never committed" in section
    assert tickets_api.MIN_SECRET_LENGTH == 32
    assert "`os_ingest_url`" in section


def test_the_readme_names_the_runtime_state_files_and_git_ignores_them() -> None:
    section = _phase_93()
    seen = forward_action_items.SEEN_PATH.relative_to(PACKAGE_ROOT).as_posix()
    retry = forward_action_items.retry_path(forward_action_items.SEEN_PATH).relative_to(PACKAGE_ROOT).as_posix()
    assert seen == "config/.action_items_forwarded.json"
    assert retry == "config/.action_items_retry.json"
    for path in (seen, retry, "outputs/runs/source_shadow_seen.json", "outputs/runs/os/<run id>/"):
        assert f"`{path}`" in section, path
    ignored = subprocess.run(
        ["git", "check-ignore", seen, retry, "outputs/runs/source_shadow_seen.json", "outputs/runs/os/x/bundle.json"],
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert ignored.returncode == 0, ignored.stderr
    assert len(ignored.stdout.split()) == 4


def test_the_readme_names_both_grants_and_points_to_the_checks() -> None:
    section = _section(_phase_93(), "### Database grants")
    assert "`packn_os_existing_automation`" in section
    assert "`SELECT` on `app_settings`" in section
    assert f"`UPDATE ({', '.join(SUPERSEDE_COLUMNS)})` on `automation_drafts`" in section
    assert "`source_unreadable`" in section
    assert "`packn_os_hubspot_client/README.md`" in section
    assert "`has_table_privilege`" in section and "`has_column_privilege`" in section
    assert "before this code is deployed" in section


def test_the_readme_deploy_steps_survive_the_two_tracked_mutated_files() -> None:
    section = _section(_phase_93(), "### Deploying on the droplet")
    assert "git status --short config/" in section
    assert "cp config/state.json config/state.json.bak" in section
    assert "git stash push -- config/state.json config/pending_actions.json" in section
    assert "git stash pop" in section
    assert "No commit of this phase touches either file" in section
    # the order: the check, then the pull, then the stash fallback
    assert (
        section.index("git status --short config/")
        < section.index("git pull")
        < section.index("git stash push")
        < section.index("git stash pop")
    )
    assert "must never be deployed alone" in section
    assert "claude --version" in section and "claude mcp list" in section
    assert "registered under the name `hubspot`" in section


def test_the_readme_deploy_steps_put_the_grants_and_the_secret_before_the_pull() -> None:
    section = _section(_phase_93(), "### Deploying on the droplet")
    steps = _numbered_steps(section)
    assert sorted(steps) == list(range(1, len(steps) + 1))
    order = {
        "os": next(n for n, s in steps.items() if s.startswith("Pack'N OS first")),
        "grants": next(n for n, s in steps.items() if "two grants" in s),
        "secret": next(n for n, s in steps.items() if SECRET_FILE_NAME in s),
        "pull": next(n for n, s in steps.items() if s.startswith("Pull.")),
        "shadow": next(n for n, s in steps.items() if "Shadow" in s),
    }
    assert order["os"] < order["grants"] < order["pull"] < order["shadow"]
    assert order["secret"] < order["shadow"]


def test_the_readme_says_a_rollback_is_the_setting_and_no_code_change() -> None:
    section = _section(_phase_93(), "### Rolling back")
    assert "Set the source back to HubSpot on the Ticket flow card" in section
    assert "no code change" in section
    assert "scripts/run_tickets.sh" in section


def test_the_readme_gives_the_cron_gate_and_the_local_database_rule() -> None:
    section = _section(_phase_93(), "### Tests")
    assert CRON_GATE in section
    assert "packn_os_hubspot_client/tests/test_drafts.py" in section
    assert "test_run_records.py" in section
    assert "REFUSING_NON_LOCAL_DATABASE" in section
    assert "port 5433" in section


# ======================================================================================
# README.md — outside the Phase 93 section
# ======================================================================================


def test_the_readme_lead_no_longer_says_replies_are_auto_sent() -> None:
    text = _md(README)
    lead = text[: text.index("## What ships out of the box")]
    lowered = lead.lower()
    assert "auto-sends" not in lowered
    assert "auto-sent to customer" not in lowered
    assert "review before sending" not in lowered
    assert "pending draft" in lowered
    assert "Nothing is sent to a customer by this automation" in lead


def test_the_readme_scheduler_example_carries_the_supported_entry_note() -> None:
    text = _md(README)
    option = _section(text, "#### Option 2 (recommended for production) — Windows Task Scheduler")
    assert option
    note = option[: option.index("```powershell")]
    assert "`scripts/run_tickets.sh` is the supported entry for every scheduled and manual run" in note
    assert "source guard" in note
    assert "is stopped by the skill's first step" in note
    assert "is not one that skill serves" in note
    # the example itself is unchanged: it still launches the skill directly
    assert 'schtasks /create /tn "HubSpot-Tickets-Hourly"' in option
    assert "Run the hubspot-tickets skill following .claude/skills/hubspot-tickets/SKILL.md exactly." in option


def test_the_readme_marks_its_pre_cutover_sections() -> None:
    text = _md(README)
    banner = "describe the first local setup of April 2026"
    assert banner in text
    assert text.index(banner) < text.index("## First-time local run")
    assert "the Pack'N OS and Phase 93 sections above win" in text


def test_the_readme_does_not_say_the_server_picks_changes_up_by_itself() -> None:
    text = _md(README)
    assert "picks them up on the next cron fire" not in text
    assert "the cron does not pull" in text


def test_the_readme_grant_section_defers_to_the_helper_readme() -> None:
    section = _section(_md(README), "### Postgres role + grants (run ONCE pre-cutover by lconner against the prod DB)")
    assert section
    assert "`packn_os_hubspot_client/README.md`" in section
    assert "complete, current list" in section
    # the count it gives for today is the helper's own
    assert "reaches seven tables today" in section
    assert len(_tables_the_helper_touches()) == 7


def test_the_skills_own_description_no_longer_says_it_sends_to_the_customer() -> None:
    text = _md(SKILL)
    assert text.startswith("---\nname: hubspot-tickets\ndescription: ")
    description = text.split("\n")[2]
    lowered = description.lower()
    assert "auto-send" not in lowered
    assert "internal draft note" not in lowered
    assert "pending draft in Pack'N OS" in description
    assert "Nothing is sent to a customer" in description
    assert "`hubspot` or `shadow`" in description


def test_the_readme_says_which_copy_of_the_helper_is_live() -> None:
    layout = _section(_md(README), "### Module layout (helper at the repo root)")
    assert layout
    assert "The copy in this repo is the live one" in layout
    assert "future updates re-copy from the OS source" not in layout
    # and the helper's own README says the same
    assert "is the live one" in _md(HELPER_README)


def test_the_readme_gives_the_paused_records_summary_as_the_code_writes_it() -> None:
    section = _section(_md(README), "## Pack'N OS Control Panel integration (Phase 4)")
    assert run_tickets.SUMMARY_ROUTINE_PAUSED == os_pass.SUMMARY_ROUTINE_PAUSED == "routine_paused"
    paused = next(p for p in section.split("\n- ") if p.startswith("**Pause/resume routines**"))
    assert f"`{run_tickets.SUMMARY_ROUTINE_PAUSED}`" in paused
    assert "`status='skipped'`" in paused


def test_the_readme_rate_limit_line_names_the_droplets_tools() -> None:
    section = _section(_md(README), "## Pack'N OS Control Panel integration (Phase 4)")
    assert "`mcp__hubspot__*`" in section
    assert "mcp__claude_ai_HubSpot__" not in section


def test_the_readme_slash_command_row_and_the_hand_run_note() -> None:
    section = _section(_md(README), "## Slash commands (manual trigger)")
    assert "draft internal notes" not in section
    assert "pending drafts in Pack'N OS" in section
    assert "bash scripts/run_tickets.sh" in section
    assert "`/packn-tickets-os`" in section and "never started by hand" in section


# ======================================================================================
# The two READMEs agree with each other and with the helper's code
# ======================================================================================


def _helper_phase_93() -> str:
    section = _section(_md(HELPER_README), "### Phase 93: the cron reads OS tickets (two more grants)")
    assert section
    return section


def test_the_two_phase_93_grants_are_the_same_in_both_readmes() -> None:
    helper = _helper_phase_93()
    assert 'GRANT SELECT ON "app_settings" TO "packn_os_existing_automation";' in helper
    update = re.search(r'GRANT UPDATE \(([^)]*)\)\s+ON "automation_drafts" TO "packn_os_existing_automation";', helper)
    assert update
    columns = tuple(name.strip().strip('"') for name in update.group(1).split(","))
    assert columns == SUPERSEDE_COLUMNS
    # seven checks: five that must print t, two that must print f
    checks = re.findall(r"^SELECT has_(?:table|column)_privilege\(.*--\s*([tf])\s*$", helper, flags=re.M)
    assert checks == ["t", "t", "t", "t", "t", "f", "f"]
    assert f"`config/.secrets/{SECRET_FILE_NAME}`" in helper


SQL_TABLE = re.compile(r"\b(?:FROM|INTO|UPDATE|JOIN)\s+\"?([a-z][a-z_.]*)\"?")
NEEDS_NO_GRANT = {"information_schema.columns"}


def _tables_the_helper_touches() -> set[str]:
    """Table names after an upper-case SQL keyword in client.py. A plain word
    (prose such as "UPDATE is") has no underscore and is not a table here."""
    names = set(SQL_TABLE.findall(CLIENT.read_text(encoding="utf-8")))
    return {name for name in names if "_" in name} - NEEDS_NO_GRANT


def test_the_helper_touches_the_tables_this_test_expects() -> None:
    assert _tables_the_helper_touches() == {
        "app_settings",
        "automation_drafts",
        "automation_rerun_requests",
        "automation_routines",
        "automation_run_requests",
        "automation_runs",
        "customer_complaints",
    }


def test_every_table_the_helper_touches_has_a_grant_in_the_helper_readme() -> None:
    """A table the client reads or writes with no grant listed is a deploy that
    fails on the droplet and nowhere else: the tests connect as the owner."""
    grants = re.findall(r'^GRANT [^;]*?\bON "?([a-z_]+)"?\s', _md(HELPER_README), flags=re.M | re.S)
    granted = set(grants)
    missing = _tables_the_helper_touches() - granted
    assert missing == set(), missing


def test_the_helper_readme_lists_the_draft_update_as_column_scoped_only() -> None:
    text = _md(HELPER_README)
    assert not re.search(r'^GRANT UPDATE ON "?automation_drafts', text, flags=re.M)
    assert not re.search(r'^GRANT [A-Z, ]*\bDELETE\b', text, flags=re.M)


# ======================================================================================
# docs/server-setup.md
# ======================================================================================


def test_server_setup_notes_the_os_launch_beside_the_mcp_registration() -> None:
    phase = _section(_md(SERVER_SETUP), "## Phase 7: HubSpot MCP (for Claude Code CLI)")
    assert phase
    assert "claude mcp add hubspot" in phase
    assert "`--disallowedTools mcp__hubspot`" in phase
    assert "registered under the name `hubspot`" in phase
    assert "closes the tool path, not the shell path" in phase
    assert phase.index("claude mcp add hubspot") < phase.index("`--disallowedTools mcp__hubspot`")


def test_server_setup_qualifies_the_direct_rest_sentence() -> None:
    phase = _section(_md(SERVER_SETUP), "## Phase 7: HubSpot MCP (for Claude Code CLI)")
    assert "fall back to direct REST calls" in phase
    after = phase[phase.index("fall back to direct REST calls") :]
    assert "The `os`-mode skill contains no HubSpot instruction" in after
    assert "code makes the permitted HubSpot requests" in after


def test_server_setup_no_longer_promises_notes_or_auto_sent_replies() -> None:
    smoke = _section(_md(SERVER_SETUP), "## Phase 9: Smoke tests")
    assert smoke
    lowered = smoke.lower()
    assert "auto-sent replies" not in lowered
    assert "packn_metadata_v1" not in lowered
    assert "pending draft" in lowered


def test_server_setup_names_the_new_secret_file() -> None:
    assert f"`{SECRET_FILE_NAME}`" in _section(_md(SERVER_SETUP), "## Phase 6: Copy secrets from the laptop")


# ======================================================================================
# Every doc
# ======================================================================================


def test_no_phase_doc_names_the_old_wrapper_file_or_a_self_updating_server() -> None:
    old_wrapper = "cron" + "_tickets.sh"
    for path in PHASE_DOCS:
        text = _md(path)
        assert old_wrapper not in text, path.name
        assert not re.search(r"pulls from `?main`? each time", text), path.name
        assert "picks them up on the next cron fire" not in text, path.name


def test_every_phase_doc_that_names_the_wrapper_names_the_real_file() -> None:
    assert run_tickets.CRON_MARKER == "run_tickets.sh"
    assert (PACKAGE_ROOT / "scripts" / run_tickets.CRON_MARKER).is_file()
    for path in (COMMAND, SKILL, CLAUDE_MD, README, SERVER_SETUP):
        assert f"scripts/{run_tickets.CRON_MARKER}" in _md(path), path.name


# A value for the tickets secret: an assignment whose right-hand side is not a
# placeholder, or a long token on a line that names the secret.
SECRET_ASSIGNMENT = re.compile(
    rf"{SECRET_ENV_NAME}\s*[=:]\s*[\"']?(?![<\[$]|\.\.\.|REPLACE|[\"']?\s*$)[A-Za-z0-9+/=_-]{{8,}}"
)
LONG_TOKEN = re.compile(r"[A-Za-z0-9+=_-]{32,}")
SECRET_NAMES = (SECRET_ENV_NAME, SECRET_FILE_NAME)


def secret_value_lines(text: str) -> list[int]:
    """Line numbers that look like they hold a value for the tickets secret."""
    flagged: list[int] = []
    for number, line in enumerate(text.split("\n"), start=1):
        if SECRET_ASSIGNMENT.search(line):
            flagged.append(number)
        elif any(name in line for name in SECRET_NAMES) and LONG_TOKEN.search(line):
            flagged.append(number)
    return flagged


def test_the_secret_scan_flags_a_value_and_passes_a_name() -> None:
    planted = "0123456789abcdef" * 3
    assert secret_value_lines(f"{SECRET_ENV_NAME}={planted}") == [1]
    assert secret_value_lines(f"export {SECRET_ENV_NAME}=\"{planted[:12]}\"") == [1]
    assert secret_value_lines(f"{SECRET_ENV_NAME}: {planted[:10]}") == [1]
    assert secret_value_lines(f"echo {planted} > config/.secrets/{SECRET_FILE_NAME}") == [1]
    # names, placeholders and prose pass
    assert secret_value_lines(f"`{SECRET_ENV_NAME}` in the Pack'N OS environment") == []
    assert secret_value_lines(f"{SECRET_ENV_NAME}=<the shared secret>") == []
    assert secret_value_lines(f"{SECRET_ENV_NAME}=...") == []
    assert secret_value_lines(f"| `config/.secrets/{SECRET_FILE_NAME}` | at least 32 characters |") == []
    assert secret_value_lines(f"/opt/packn/hubspot_ticket_automation/config/.secrets/{SECRET_FILE_NAME}") == []


def _tracked_docs() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"], cwd=PACKAGE_ROOT, capture_output=True, check=True, timeout=60
    ).stdout.decode("utf-8")
    return [PACKAGE_ROOT / name for name in listed.split("\0") if name]


def test_no_tracked_doc_assigns_the_tickets_secret_a_value() -> None:
    docs = _tracked_docs()
    assert len(docs) > 20
    for path in PHASE_DOCS:
        assert path in docs, path.name
    flagged = {
        path.relative_to(PACKAGE_ROOT).as_posix(): lines
        for path in docs
        if (lines := secret_value_lines(path.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n")))
    }
    assert flagged == {}
