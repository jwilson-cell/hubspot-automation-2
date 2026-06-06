#!/usr/bin/env python3
"""Read-only Phase 20 planning probe — run ONCE on the droplet, paste output back.

Pack'N OS Phase 20 (complaint mirror + brand attribution) needs four pieces of
evidence from the droplet before downstream design hardens. Long pastes are
forbidden on this box (SSH paste-wrap mangling), so this single committed
script collects everything and the operator pastes the labeled output back:

    [probe-3-csv-format]   mispack_log.csv header + first 3 first_seen_utc values
    [probe-4-volume]       CSV data-row counts (total / tracking_number / company_name)
    [probe-2-brands]       up to 30 distinct company_name values + distinct count
    [probe-1-topic-search] HubSpot tickets-search total for the mispack form topic
    [preflight-env]        psycopg importability + PACKN_OS_DATABASE_URL visibility

Usage (on the droplet, from the repo root):
    .venv/bin/python scripts/probe_phase20.py

Strictly READ-ONLY: no DB writes, no HubSpot writes, no file writes.
Exactly ONE HubSpot API call (limit:1, behind a time.sleep(0.34) budget floor).

Secret/PII discipline (load-bearing — output is pasted into chat):
  - NEVER prints the HubSpot token.
  - NEVER prints the PACKN_OS_DATABASE_URL value (it contains the DB
    password) — membership YES/NO checks only.
  - NEVER prints customer_name / customer_email cells from the CSV —
    company names and timestamps only.

Always exits 0 — a partial probe result is still useful.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = ROOT / "outputs" / "kpi" / "mispack_log.csv"
TOKEN_PATH = ROOT / "config" / ".secrets" / "hubspot_token.txt"
SEARCH_URL = "https://api.hubapi.com/crm/v3/objects/tickets/search"


def probe_csv() -> None:
    """probe-3 (first_seen_utc format) + probe-4 (volume) + probe-2 (brands).

    Single pass with csv.DictReader — columns resolved BY NAME because the
    on-disk column order can drift (operator-controlled sheet mirrors).
    """
    if not CSV_PATH.exists():
        print("[probe-3-csv-format] CSV-MISSING")
        print("[probe-4-volume] CSV-MISSING")
        print("[probe-2-brands] CSV-MISSING")
        return

    with CSV_PATH.open(newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        first_seen_samples: list[str] = []
        total_rows = 0
        tracking_nonempty = 0
        company_nonempty = 0
        companies: set[str] = set()
        for row in reader:
            total_rows += 1
            if len(first_seen_samples) < 3:
                first_seen_samples.append((row.get("first_seen_utc") or "").strip())
            if (row.get("tracking_number") or "").strip():
                tracking_nonempty += 1
            company = (row.get("company_name") or "").strip()
            if company:
                company_nonempty += 1
                companies.add(company)

    # probe-3: header as read + first 3 first_seen_utc values (timestamps only — no PII cells)
    print(f"[probe-3-csv-format] header: {header}")
    if first_seen_samples:
        for i, val in enumerate(first_seen_samples, start=1):
            print(f"[probe-3-csv-format] first_seen_utc row {i}: {val}")
    else:
        print("[probe-3-csv-format] no data rows")

    # probe-4: backfill sizing
    print(f"[probe-4-volume] total data rows: {total_rows}")
    print(f"[probe-4-volume] rows with non-empty tracking_number: {tracking_nonempty}")
    print(f"[probe-4-volume] rows with non-empty company_name: {company_nonempty}")

    # probe-2: brand-overlap sizing (company names only — never customer_name/customer_email)
    for name in sorted(companies)[:30]:
        print(f"[probe-2-brands] {name}")
    print(f"[probe-2-brands] total distinct company_name: {len(companies)}")


def probe_topic_search() -> None:
    """probe-1: is topic_of_ticket searchable via filterGroups EQ, and what's the total?"""
    if not TOKEN_PATH.exists():
        print("[probe-1-topic-search] TOKEN-MISSING")
        return
    token = TOKEN_PATH.read_text(encoding="utf-8").strip()
    if not token:
        print("[probe-1-topic-search] TOKEN-MISSING")
        return

    time.sleep(0.34)  # shared 3 req/s HubSpot budget floor — exactly one call below
    body = json.dumps(
        {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "topic_of_ticket",
                            "operator": "EQ",
                            "value": "Mispack (Provide Order Number)",
                        }
                    ]
                }
            ],
            "limit": 1,
        }
    ).encode("utf-8")
    req = urllib.request.Request(SEARCH_URL, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")  # token itself is NEVER printed
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8") or "{}"
        payload = json.loads(raw)
        print(f"[probe-1-topic-search] HTTP {status}")
        print(f"[probe-1-topic-search] total: {payload.get('total')}")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else str(e)
        print(f"[probe-1-topic-search] HTTP {e.code}")
        print(f"[probe-1-topic-search] error body (first 200): {err_body[:200]}")
    except urllib.error.URLError as e:
        print(f"[probe-1-topic-search] network error: {e!r}")


def probe_preflight() -> None:
    """preflight-env (A4): psycopg importable + PACKN_OS_DATABASE_URL visibility."""
    try:
        import psycopg  # noqa: F401  — availability CHECK only, not a script dependency
        import psycopg_pool  # noqa: F401

        print("[preflight-env] psycopg+psycopg_pool import: ok")
    except Exception as exc:
        print(f"[preflight-env] psycopg+psycopg_pool import: FAIL {exc!r}")

    # Membership checks ONLY — the value contains the DB password; never print it.
    in_env = "YES" if "PACKN_OS_DATABASE_URL" in os.environ else "NO"
    print(f"[preflight-env] PACKN_OS_DATABASE_URL in os.environ: {in_env}")

    in_etc = "NO"
    try:
        if "PACKN_OS_DATABASE_URL" in Path("/etc/environment").read_text(encoding="utf-8"):
            in_etc = "YES"
    except OSError:
        pass  # unreadable/missing /etc/environment -> NO
    print(f"[preflight-env] PACKN_OS_DATABASE_URL in /etc/environment: {in_etc}")


def main() -> int:
    for section in (probe_csv, probe_topic_search, probe_preflight):
        try:
            section()
        except Exception as exc:  # a partial probe result is still useful
            print(f"[probe-error] {section.__name__}: {exc!r}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[probe-error] fatal: {exc!r}")
        sys.exit(0)
