"""Tests for scripts/pre_filter_tickets.py decision logic.

Network I/O is intentionally NOT exercised — the cron wrapper fails open on
any error, so the only thing worth pinning is the `_needs_work` comparison
between HubSpot candidates and the local state.json fingerprint cache.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make scripts/ importable as a package (mirrors the cron environment).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.pre_filter_tickets import _needs_work, _iso_to_ms  # noqa: E402


def _candidate(ticket_id: str, num_notes: int) -> dict:
    return {"id": ticket_id, "properties": {"num_notes": str(num_notes)}}


class TestNeedsWork:
    def test_new_ticket_counts(self):
        candidates = [_candidate("100", 0)]
        needs, total = _needs_work(candidates, fingerprints={})
        assert (needs, total) == (1, 1)

    def test_fingerprinted_same_notes_skipped(self):
        candidates = [_candidate("100", 1)]
        fingerprints = {"100": {"num_notes": 1}}
        needs, total = _needs_work(candidates, fingerprints)
        assert (needs, total) == (0, 1)

    def test_fingerprinted_increased_notes_counts(self):
        candidates = [_candidate("100", 2)]
        fingerprints = {"100": {"num_notes": 1}}
        needs, total = _needs_work(candidates, fingerprints)
        assert (needs, total) == (1, 1)

    def test_mixed_only_changed_counts(self):
        candidates = [
            _candidate("100", 1),  # fingerprinted, same — skip
            _candidate("200", 5),  # fingerprinted, more notes — counts
            _candidate("300", 0),  # brand new — counts
        ]
        fingerprints = {
            "100": {"num_notes": 1},
            "200": {"num_notes": 3},
        }
        needs, total = _needs_work(candidates, fingerprints)
        assert (needs, total) == (2, 3)

    def test_empty_candidates(self):
        needs, total = _needs_work([], fingerprints={"100": {"num_notes": 1}})
        assert (needs, total) == (0, 0)

    def test_missing_id_skipped(self):
        candidates = [{"properties": {"num_notes": "5"}}]  # no id field
        needs, total = _needs_work(candidates, fingerprints={})
        assert (needs, total) == (0, 0)

    def test_malformed_num_notes_treated_as_zero(self):
        candidates = [_candidate("100", 0)]
        candidates[0]["properties"]["num_notes"] = "not-a-number"
        fingerprints = {"100": {"num_notes": "also-bad"}}
        needs, total = _needs_work(candidates, fingerprints)
        # Both coerce to 0 → unchanged → skip.
        assert (needs, total) == (0, 1)


class TestIsoToMs:
    def test_z_and_offset_equivalent(self):
        # The state.json `last_run_at` uses "Z" suffix; ISO 8601 also allows
        # explicit +00:00. Both must convert to the same epoch ms.
        assert _iso_to_ms("2026-04-23T22:27:28.342Z") == _iso_to_ms(
            "2026-04-23T22:27:28.342+00:00"
        )

    def test_millisecond_precision(self):
        # The HubSpot search filter requires millisecond resolution; verify
        # we don't lose the fractional component.
        a = _iso_to_ms("2026-04-23T22:27:28.342Z")
        b = _iso_to_ms("2026-04-23T22:27:28.343Z")
        assert b - a == 1
