"""Pure-suite database guard (Phase 93, plan 12).

RED skeleton: the names exist so the self-tests import; no guard is installed yet.
"""
from __future__ import annotations


def _no_database_pool(*_args, **_kwargs):
    return None


def _hand_back(real, modules=None):
    return None
