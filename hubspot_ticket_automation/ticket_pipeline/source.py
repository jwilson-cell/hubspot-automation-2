"""Skeleton (Phase 93, plan 13, RED): names and signatures only, no behaviour."""
from __future__ import annotations

MODES: tuple = ()
MODE_HUBSPOT = ""
MODE_SHADOW = ""
MODE_OS = ""
DEFAULT_MODE = ""
SETTING_KEY = ""
MODE_ENV = ""
SOURCE_UNREADABLE = ""
SOURCE_UNKNOWN = ""


class SourceModeError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def parse_mode(found, value):
    return None


def validate_mode(mode):
    return None


def resolve_mode(reader=None):
    return None


def mode_from_env(environ=None):
    return None


def current_mode(reader=None, environ=None):
    return None


def iso_to_ms(value):
    return None


def parse_last_run_at(value, now_ms=None):
    return None


def build_search_body(last_run_ms, active_stages, require_last_message_from_visitor, per_run_cap):
    return None


def unprocessed_candidates(results, fingerprints):
    return None
