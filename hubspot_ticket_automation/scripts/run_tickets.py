#!/usr/bin/env python3
"""One entry point for every tickets-process run (Phase 93, plan 15).

SKELETON: names and signatures only, no behaviour yet. The tests in
tests/test_run_record.py describe what each of these does.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
for _entry in (str(ROOT), str(ROOT / "scripts")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

ROUTINE = "tickets-process"
CRON_MARKER = ""
AGENT_MODEL = ""
AGENT_SKILL = ""

EXIT_RECORDED = 0
EXIT_RECORD_UNWRITABLE = 1

MODE_RUNNERS: dict[str, Callable[..., Any]] = {}
RUNNER_SPECS: dict[str, tuple[Optional[str], str]] = {}

RESULT_MISSING = "missing"
RESULT_UNREADABLE = "unreadable"
RESULT_INCOMPLETE = "incomplete"
RESULT_COMPLETE = "complete"

ROLLBACK_ENTRIES_UNREADABLE = "rollback_entries_unreadable"
SUMMARY_ROUTINE_PAUSED = "routine_paused"

_RE_EXPORTED = ("RunContext", "RunOutcome", "Deps")


def _types() -> Any:
    from ticket_pipeline import run_types

    return run_types


def __getattr__(name: str) -> Any:
    if name in _RE_EXPORTED:
        return getattr(_types(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def new_run_id() -> str:
    return ""


def agent_command() -> list[str]:
    return []


def launch_agent(command: Sequence[str], env: Mapping[str, str]) -> int:
    return 0


def run_engine_shadow_step(env: Mapping[str, str]) -> None:
    return None


def child_environment(ctx: Any, deps: Any) -> dict[str, str]:
    return {}


def read_result_file(path: Any, expected_run_id: Optional[str] = None) -> dict:
    return {}


def build_run_context(ctx: Any, deps: Any, reruns: Any) -> tuple[dict, Optional[str]]:
    return {}, None


def write_run_context(ctx: Any, context: dict) -> None:
    return None


def launch_and_collect(
    ctx: Any,
    deps: Any,
    command: Sequence[str],
    env: Mapping[str, str],
    notes: Sequence[str] = (),
) -> Any:
    return None


def run_hubspot(ctx: Any, deps: Any) -> Any:
    return None


def load_runners() -> dict[str, Callable[..., Any]]:
    return MODE_RUNNERS


def run(deps: Any, manual_request_id: Optional[str] = None) -> int:
    return None  # type: ignore[return-value]


def build_deps() -> Any:
    return None


def record_startup_failure(failure_class: str, manual_request_id: Optional[str] = None) -> int:
    return None  # type: ignore[return-value]


def _close_pool() -> None:
    return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    return None  # type: ignore[return-value]


if __name__ == "__main__":
    sys.exit(1)
