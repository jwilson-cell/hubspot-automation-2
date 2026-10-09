"""`os` mode, first half (Phase 93, plan 16). SKELETON: names and signatures only.

The behaviour lands in the next commit; this file exists so the tests of
tests/test_os_mode_run.py fail on their assertions, not on a missing name.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import run_types
from .run_types import Deps, RunContext, RunOutcome

ROUTINE = run_types.ROUTINE

AGENT_MODEL = "claude-sonnet-5"
OS_AGENT_SKILL = "/packn-tickets-os"
DISALLOWED_TOOLS = "mcp__hubspot"
OS_AGENT_COMMAND = (
    "claude",
    "-p",
    OS_AGENT_SKILL,
    "--model",
    AGENT_MODEL,
    "--dangerously-skip-permissions",
    "--disallowedTools",
    DISALLOWED_TOOLS,
)

BUNDLE_PATH_ENV = "PACKN_BUNDLE_PATH"
BUNDLE_FILE_NAME = "bundle.json"
DEFAULT_PER_RUN_CAP = 25

LIST_FAILURE_CODES = ("list_timeout", "list_error", "signature_rejected", "secret_missing")
LIST_ERROR = "list_error"

OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
AGENT_NO_RESULT = "agent_no_result"
AGENT_TICKET_ERROR = "agent_ticket_error"
BUNDLE_BUILD_FAILED = "bundle_build_failed"
OTHER = "other"
AGENT_TICKET_CODES = (AGENT_TICKET_ERROR, OTHER)

SUMMARY_ROUTINE_PAUSED = "routine_paused"
THREAD_FETCH_FAILED = "thread_fetch_failed"

ENTRY_KEYS = (
    "ref",
    "os_ticket_id",
    "hubspot_ticket_id",
    "cycle_key",
    "is_rerun",
    "rerun_request_ids",
    "signal",
    "thread_source",
    "backfill_source",
    "ticket_context",
)


@dataclass
class OsRunOutcome(RunOutcome):
    tickets: list = field(default_factory=list)
    bundle_entries: list = field(default_factory=list)
    result: Optional[dict] = None


def agent_command() -> list[str]:
    return []


def list_limit(settings: Any) -> int:
    return 0


def reconcile(entries: list, result: Any) -> tuple[list, dict]:
    return [], {}


def run_engine_shadow_on_contexts(contexts: list) -> None:
    return None


def collect(ctx: RunContext, deps: Deps) -> Any:
    return None


def run_os(ctx: RunContext, deps: Deps) -> Any:
    return None
