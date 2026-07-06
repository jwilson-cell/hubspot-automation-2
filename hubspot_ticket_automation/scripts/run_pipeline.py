#!/usr/bin/env python3
"""CLI entry for the direct-API ticket pipeline (shadow mode).

Usage (from the repo root, droplet or laptop-with-secrets):
    .venv/bin/python scripts/run_pipeline.py                 # gated by settings pipeline.shadow_enabled
    .venv/bin/python scripts/run_pipeline.py --limit 2       # cap tickets (first live test)
    .venv/bin/python scripts/run_pipeline.py --ticket 12345  # single ticket, ignores the gate

Artifacts land in outputs/shadow/<run-ts>/ — one JSON per ticket plus
_summary.json. Always exits 0 (never fails the cron).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ticket_pipeline.run import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
