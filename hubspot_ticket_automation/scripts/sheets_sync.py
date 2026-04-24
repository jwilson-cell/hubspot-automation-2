"""Append one run's rows to the Pack'N HubSpot Automation workbook.

Invoked by the hubspot-tickets skill at step 3.5 with a single JSON payload:

    py scripts/sheets_sync.py <payload_path>

Payload shape (all keys optional except run_id):
    {
      "run_id": "uuid",
      "kpi":     { <one flat dict matching kpi_system skill-owned columns> },
      "mispack": [ { <one flat dict per row> }, ... ],
      "carrier": [ { <one flat dict per row> }, ... ]
    }

Guarantees:
  1. Column order in the Sheet is the SOURCE OF TRUTH. Each row is written
     by resolving column-name → column-index from the live header row.
     Operators can reorder columns or insert their own without breaking sync.
  2. Operator-owned columns (claim_status, coverage_usd, etc.) are written
     as empty strings only on FIRST insert of a row. Existing rows are never
     touched — dedupe by ticket_id prevents updates.
  3. Failures never block the caller. If the Sheets API is unreachable or
     errors, the entire payload is appended to sheets_export.pending_sync_file
     and retried on the next run. Current-run rows take precedence over
     stale pending rows on merge (see combine_with_pending).
  4. A local CSV mirror under sheets_export.local_mirror_dir is updated on
     every run regardless of Sheets success — recovery source of truth.
  5. Only one sheets_sync.py runs at a time (lockfile). Concurrent invocations
     exit cleanly rather than corrupting the pending queue.

Auth note: This uses the same Google Workspace OAuth client as gmail_auth.py
(both target Internal users on gopackn.com). Internal-app refresh tokens do
not rotate, so independent refreshes from parallel scripts do not invalidate
each other. If the OAuth client is ever switched to External, revisit the
token-file locking story.

Exit codes:
    0 = success (rows appended OR queued for retry; local mirror updated)
    2 = bad invocation (missing arg / file / settings)
    3 = missing OAuth token (run sheets_auth.py first)
    4 = local mirror write failed (unexpected — caller should abort)
    5 = state.json integrity failure (run sheets_bootstrap.py)
    6 = skill-owned column(s) missing from live Sheet (manual repair needed)
"""
from __future__ import annotations

import atexit
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from sheets_schema import (
    BOOLEAN_COLUMNS,
    FREETEXT_COLUMNS,
    NUMERIC_COLUMNS,
    TAB_COLUMNS,
    TAB_SCHEMAS,
    skill_owned,
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
TOKEN_PATH = ROOT / "config" / ".secrets" / "sheets_token.json"
STATE_PATH = ROOT / "config" / "sheets_state.json"
LOCK_PATH = ROOT / "config" / ".sheets_sync.lock"
LOCK_STALE_SECONDS = 300  # a lock older than 5 min from a crashed run is stale


# ---------- single-instance lock ----------
def acquire_lock() -> None:
    """Exit 0 if another sheets_sync.py is already running (not an error).

    Prevents concurrent writers from corrupting the pending-sync queue or
    double-appending rows. Stale locks (>5 min, presumably from a crashed
    prior run) are reclaimed.
    """
    if LOCK_PATH.exists():
        try:
            age = time.time() - LOCK_PATH.stat().st_mtime
        except OSError:
            age = 0
        if age < LOCK_STALE_SECONDS:
            print(f"another sheets_sync is running (lock age {age:.0f}s); skipping",
                  file=sys.stderr)
            sys.exit(0)
        try:
            LOCK_PATH.unlink()
        except OSError:
            pass

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print("race on lock acquisition; skipping", file=sys.stderr)
        sys.exit(0)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)

    def _release() -> None:
        try:
            LOCK_PATH.unlink()
        except OSError:
            pass
    atexit.register(_release)


# ---------- carrier inference ----------
# Heuristics are conservative by design — "unknown" is better than a wrong
# guess because operators can fix unknown in one click, but a wrong "FedEx"
# routes a claim to the wrong carrier contact. Known unsupported: LaserShip
# (numeric collides with FedEx), generic 10-digit DHL (collides with FedEx).
_UPS_PREFIX = re.compile(r"^1Z[A-Z0-9]{16}$", re.IGNORECASE)
_USPS_PREFIX_LETTERS = re.compile(r"^(EA|EC|LK|RA|RB|RD|RR|VA)[0-9]{9}US$", re.IGNORECASE)
_DHL_EXPRESS = re.compile(r"^JD[A-Z0-9]{16,20}$", re.IGNORECASE)
_AMZN_LOGISTICS = re.compile(r"^TBA[0-9]{9,12}$", re.IGNORECASE)
_ONTRAC = re.compile(r"^[CD][0-9]{14}$", re.IGNORECASE)  # 15 chars, starts C or D


def infer_carrier(tracking_number: str) -> str:
    if not tracking_number:
        return "unknown"
    t = tracking_number.strip().replace(" ", "").upper()
    if _UPS_PREFIX.match(t):
        return "UPS"
    if _USPS_PREFIX_LETTERS.match(t):
        return "USPS"
    if _DHL_EXPRESS.match(t):
        return "DHL"
    if _AMZN_LOGISTICS.match(t):
        return "Amazon"
    if _ONTRAC.match(t):
        return "OnTrac"
    if t.isdigit():
        if len(t) in (12, 15):
            return "FedEx"
        if len(t) in (20, 22) and t.startswith("9"):
            return "USPS"
    return "unknown"


# ---------- value coercion ----------
_TRUE_STRINGS = frozenset({"true", "yes", "1", "y", "on", "t"})
_FALSE_STRINGS = frozenset({"false", "no", "0", "n", "off", "f"})
_FORMULA_LEAD = ("=", "+", "-", "@")
_NUMBER_STRIP_RE = re.compile(r"[\$,\s]")


def _coerce(value: Any, column: str) -> Any:
    """Schema-driven coercion for a single cell value.

    - BOOLEAN_COLUMNS: normalize "true"/"yes"/bool(True) → "TRUE", etc.
      Empty input → empty string (preserves "unknown" vs "false" distinction).
    - NUMERIC_COLUMNS: strip $, commas, whitespace; float-parse; empty on fail.
    - FREETEXT_COLUMNS: escape leading =/+/-/@ (formula injection) with
      a leading apostrophe (Sheets convention: treat cell as literal text).
    - Everything else: pass through, stringifying None → "".
    """
    if value is None:
        return ""

    if column in BOOLEAN_COLUMNS:
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, str):
            s = value.strip().lower()
            if not s:
                return ""
            if s in _TRUE_STRINGS:
                return "TRUE"
            if s in _FALSE_STRINGS:
                return "FALSE"
        # Fall through for unexpected types — write as-is for operator to see.
        return value

    if column in NUMERIC_COLUMNS:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            cleaned = _NUMBER_STRIP_RE.sub("", value).strip()
            if not cleaned:
                return ""
            try:
                # Prefer int for whole numbers so Sheets doesn't show "250.0".
                f = float(cleaned)
                return int(f) if f.is_integer() else f
            except ValueError:
                return value  # operator sees the raw string and can fix it
        return value

    if column in FREETEXT_COLUMNS and isinstance(value, str) and value.startswith(_FORMULA_LEAD):
        return "'" + value

    # Booleans outside BOOLEAN_COLUMNS (shouldn't happen in practice).
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"

    return value


# ---------- sheet column utilities ----------
def col_index_to_letter(idx: int) -> str:
    """0 → A, 25 → Z, 26 → AA, 27 → AB, 701 → ZZ, 702 → AAA."""
    letters = ""
    n = idx
    while n >= 0:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
    return letters


def fetch_header_map(sheets_svc, spreadsheet_id: str, tab: str) -> dict[str, int]:
    resp = sheets_svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{tab}!1:1",
    ).execute()
    row = resp.get("values", [[]])[0]
    return {name: i for i, name in enumerate(row) if name}


def fetch_existing_ids(sheets_svc, spreadsheet_id: str, tab: str, id_col: str) -> set[str]:
    headers = fetch_header_map(sheets_svc, spreadsheet_id, tab)
    if id_col not in headers:
        raise KeyError(f"column '{id_col}' not found in {tab!r} headers: {list(headers)}")
    letter = col_index_to_letter(headers[id_col])
    resp = sheets_svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{tab}!{letter}2:{letter}",
    ).execute()
    return {str(v[0]).strip() for v in resp.get("values", []) if v and str(v[0]).strip()}


def row_to_values(row: dict, headers: dict[str, int]) -> list:
    """Map a row dict into a values list aligned to the live header row.

    Unknown headers (operator-added columns) → empty string.
    """
    out: list = [""] * len(headers)
    for name, idx in headers.items():
        out[idx] = _coerce(row.get(name, ""), name)
    return out


# ---------- local CSV mirror ----------
def write_local_mirror(payload: dict, mirror_dir: Path) -> None:
    """Append rows to per-tab CSVs using canonical TAB_SCHEMAS columns.

    Headers are pinned to the canonical schema — a CSV that's been on disk
    through multiple schema versions keeps its original header row, and new
    skill columns land as empty cells under the new column's position if
    and only if the CSV was just created. If the CSV pre-exists with a
    different header row, we keep the existing header (no destructive
    rotation) and write rows aligned to THAT header. This preserves the
    recovery-truth property while tolerating schema drift; operators who
    want the new columns in the CSV can delete the file and let it
    re-seed on the next run.
    """
    mirror_dir.mkdir(parents=True, exist_ok=True)
    tab_to_file = {
        "kpi": ("kpi_system", "kpi_system.csv"),
        "mispack": ("mispack_log", "mispack_log.csv"),
        "carrier": ("carrier_issue_log", "carrier_issue_log.csv"),
    }

    for tab_key, (schema_tab, csv_name) in tab_to_file.items():
        rows = payload.get(tab_key)
        if rows is None:
            continue
        if isinstance(rows, dict):
            rows = [rows]
        if not rows:
            continue

        path = mirror_dir / csv_name
        if path.exists() and path.stat().st_size > 0:
            # Read existing header and respect it.
            with path.open("r", newline="", encoding="utf-8") as f:
                existing_header = next(csv.reader(f), [])
            fieldnames = existing_header or TAB_SCHEMAS[schema_tab]
            write_header = False
        else:
            fieldnames = TAB_SCHEMAS[schema_tab]
            write_header = True

        with path.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if write_header:
                w.writeheader()
            for r in rows:
                w.writerow({k: _coerce(r.get(k, ""), k) for k in fieldnames})


# ---------- pending sync queue ----------
def load_pending(path: Path) -> list:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    return []


def save_pending(path: Path, queue: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write via a temp file + rename for atomicity within the lock's scope.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(queue, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def combine_with_pending(pending: list, current: dict) -> dict:
    """Merge queued failed payloads with the current payload.

    Current batch is listed FIRST so its rows win on the ticket_id dedupe
    inside append_rows_for_tab — a re-processed ticket brings fresh
    classifier output, a newer draft_note_id, etc. Stale pending rows are
    still written if the sheet doesn't already have them.
    """
    out: dict[str, Any] = {"kpi_rows": [], "mispack": [], "carrier": []}
    for batch in [current] + pending:
        kpi = batch.get("kpi")
        if isinstance(kpi, dict):
            out["kpi_rows"].append(kpi)
        elif isinstance(kpi, list):
            out["kpi_rows"].extend(kpi)
        mp = batch.get("mispack") or []
        if isinstance(mp, dict):
            mp = [mp]
        out["mispack"].extend(mp)
        cr = batch.get("carrier") or []
        if isinstance(cr, dict):
            cr = [cr]
        out["carrier"].extend(cr)
    return out


# ---------- sheets append ----------
def append_rows_for_tab(
    sheets_svc,
    spreadsheet_id: str,
    tab: str,
    schema_tab: str,
    rows: list[dict],
    *,
    dedupe_on: str | None,
) -> tuple[int, list[str]]:
    """Append rows to one tab. Returns (appended_count, warnings).

    Raises RuntimeError if any skill-owned column is missing from the live
    header row — the sheet has drifted from the schema and the caller should
    queue the payload for retry after manual repair rather than writing
    partial rows.
    """
    if not rows:
        return 0, []
    warnings: list[str] = []
    headers = fetch_header_map(sheets_svc, spreadsheet_id, tab)
    if not headers:
        raise RuntimeError(f"tab {tab!r} has no header row — bootstrap not run?")

    required = set(skill_owned(schema_tab))
    missing = required - set(headers.keys())
    if missing:
        raise RuntimeError(
            f"tab {tab!r} is missing skill-owned column(s): {sorted(missing)}. "
            f"Restore the header(s) in the Sheet or re-run scripts/sheets_bootstrap.py."
        )

    existing: set[str] = set()
    if dedupe_on:
        existing = fetch_existing_ids(sheets_svc, spreadsheet_id, tab, dedupe_on)

    fresh: list[list] = []
    for r in rows:
        if dedupe_on:
            key = str(r.get(dedupe_on, "")).strip()
            if not key:
                warnings.append(f"{tab}: row missing dedupe key {dedupe_on!r}; skipped")
                continue
            if key in existing:
                continue
            existing.add(key)
        unknown = [k for k in r.keys() if k not in headers]
        if unknown:
            warnings.append(f"{tab}: skill emitted columns not in sheet headers: {unknown}")
        fresh.append(row_to_values(r, headers))

    if not fresh:
        return 0, warnings

    sheets_svc.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{tab}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": fresh},
    ).execute()
    return len(fresh), warnings


# ---------- enrich rows ----------
def enrich_carrier_rows(rows: list[dict]) -> list[dict]:
    """Fill carrier_inferred from tracking_number if not already set by caller."""
    for r in rows:
        if not r.get("carrier_inferred"):
            r["carrier_inferred"] = infer_carrier(r.get("tracking_number", ""))
    return rows


# ---------- creds / settings / state ----------
def load_creds() -> Credentials:
    if not TOKEN_PATH.exists():
        print(f"token missing at {TOKEN_PATH} — run scripts/sheets_auth.py first",
              file=sys.stderr)
        sys.exit(3)
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        # Internal Workspace OAuth clients do not rotate refresh tokens, so
        # concurrent refreshes are benign. See module docstring.
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
    return creds


def load_settings() -> dict:
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if not STATE_PATH.exists():
        print(f"sheets_state.json missing at {STATE_PATH} — run sheets_bootstrap.py",
              file=sys.stderr)
        sys.exit(5)
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"sheets_state.json malformed ({e}) — re-run sheets_bootstrap.py",
              file=sys.stderr)
        sys.exit(5)
    if not state.get("spreadsheet_id") or not state.get("tab_ids"):
        print("sheets_state.json missing spreadsheet_id or tab_ids — re-run sheets_bootstrap.py",
              file=sys.stderr)
        sys.exit(5)
    return state


# ---------- main ----------
def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: sheets_sync.py <payload_path>", file=sys.stderr)
        return 2
    payload_path = Path(argv[1])
    if not payload_path.exists():
        print(f"payload not found: {payload_path}", file=sys.stderr)
        return 2

    settings = load_settings()
    cfg = settings.get("sheets_export", {})
    if not cfg.get("enabled", False):
        print("sheets_export.enabled is false — skipping sync", file=sys.stderr)
        return 0

    acquire_lock()  # exits 0 if another instance holds the lock

    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    mirror_dir = ROOT / cfg.get("local_mirror_dir", "outputs/kpi")
    pending_path = ROOT / cfg.get("pending_sync_file", "config/sheets_pending_sync.json")

    # Local mirror first — this is our recovery source if Sheets fails.
    try:
        write_local_mirror(payload, mirror_dir)
    except OSError as e:
        print(f"local mirror write failed: {e}", file=sys.stderr)
        return 4

    pending = load_pending(pending_path)
    merged = combine_with_pending(pending, payload)
    merged["carrier"] = enrich_carrier_rows(merged["carrier"])

    state = load_state()
    spreadsheet_id = state["spreadsheet_id"]
    tab_map = cfg.get("tabs", {
        "kpi": "kpi_system",
        "mispack": "mispack_log",
        "carrier": "carrier_issue_log",
    })

    creds = load_creds()
    sheets_svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    all_warnings: list[str] = []
    try:
        k, w = append_rows_for_tab(
            sheets_svc, spreadsheet_id, tab_map["kpi"], "kpi_system",
            merged["kpi_rows"], dedupe_on="run_id",
        )
        print(f"kpi_system:       +{k} row(s)")
        all_warnings.extend(w)

        m, w = append_rows_for_tab(
            sheets_svc, spreadsheet_id, tab_map["mispack"], "mispack_log",
            merged["mispack"], dedupe_on="ticket_id",
        )
        print(f"mispack_log:      +{m} row(s)")
        all_warnings.extend(w)

        c, w = append_rows_for_tab(
            sheets_svc, spreadsheet_id, tab_map["carrier"], "carrier_issue_log",
            merged["carrier"], dedupe_on="ticket_id",
        )
        print(f"carrier_issue_log: +{c} row(s)")
        all_warnings.extend(w)
    except (HttpError, OSError) as e:
        # Sheets unreachable — queue current payload for retry.
        print(f"sheets sync failed, queuing for retry: {e}", file=sys.stderr)
        save_pending(pending_path, pending + [payload])
        return 0  # local mirror succeeded; skill run is not a failure
    except RuntimeError as e:
        # Schema drift: a skill-owned header is missing from the sheet.
        # Surface loudly (exit 6) AND queue payload so the operator can repair
        # the sheet and the next run replays cleanly.
        print(f"sheet schema drift: {e}", file=sys.stderr)
        save_pending(pending_path, pending + [payload])
        return 6

    # Success — clear the pending queue (merged rows are now in the sheet).
    if pending:
        save_pending(pending_path, [])
        print(f"flushed {len(pending)} pending payload(s)")

    for warn in all_warnings:
        print(f"  warn: {warn}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
