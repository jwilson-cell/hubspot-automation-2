"""The SOURCE shadow (Phase 93, plan 18). RED skeleton: names and signatures, no behaviour yet."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

_ROOT = Path(__file__).resolve().parent.parent

LOG_TAG = "[source_shadow]"
COMPARISON_LIMIT = 100
COMPARISON_CLASSIFY_FAILURE_LIMIT = 3

SEEN_CACHE_PATH = _ROOT / "outputs" / "runs" / "source_shadow_seen.json"
SEEN_CACHE_MAX_ENTRIES = 5000

OBSERVATION_CONTRACT_VERSION = "1"
OBSERVATION_MAX_TICKETS_PER_LIST = 200
OBSERVATION_MAX_ACTIVE_STAGES = 50
OBSERVATION_MAX_PER_RUN_CAP = 1000

SOURCE_SHADOW_CLASSIFY_FAILED = "source_shadow_classify_failed"
SOURCE_SHADOW_HYDRATE_FAILED = "source_shadow_hydrate_failed"
SOURCE_SHADOW_THREAD_FETCH_FAILED = "source_shadow_thread_fetch_failed"
SOURCE_SHADOW_OS_TICKET_UNUSABLE = "source_shadow_os_ticket_unusable"
SOURCE_SHADOW_RESOLVE_FAILED = "source_shadow_resolve_failed"
SOURCE_SHADOW_RESOLVE_INVALID = "source_shadow_resolve_invalid"


@dataclass
class ComparisonDeps:
    classify: Callable[[dict], Any]
    hydrate_hubspot_context: Callable[[str], Any]
    fetch_hubspot_thread: Callable[[str], Any]
    engine_classification: Callable[[str], Optional[dict]]
    settings: Any
    os_origin: str
    cache: dict


@dataclass
class ComparisonResult:
    hubspot_side: dict = field(default_factory=dict)
    os_side: dict = field(default_factory=dict)
    codes: dict = field(default_factory=dict)
    classify_calls: int = 0
    classify_failures: int = 0
    classifier_unavailable: bool = False


def seen_cache_key(os_ticket_id: Any, signal_id: Any) -> str:
    return ""


def seen_cache_path(root: Any) -> Path:
    return Path(".")


def load_seen_cache(path: Any) -> dict:
    return {"skeleton": True}


def save_seen_cache(path: Any, cache: Any) -> None:
    return None


def _engine_classification(root: Any, hubspot_ticket_id: Any, since: Any) -> Optional[dict]:
    return {"skeleton": True}


def resolve_hubspot_candidates(candidates: Any, deps: Any, codes: Optional[dict] = None) -> dict:
    return {"skeleton": True}


def compare_sources(hubspot_side: dict, os_list: list, deps: ComparisonDeps) -> ComparisonResult:
    return ComparisonResult()


def build_observation(run_id: Any, started_at: Any, settings: Any, hubspot_side: Any, os_side: Any) -> dict:
    return {}


def run_source_shadow(ctx: Any, deps: Any) -> Any:
    return None
