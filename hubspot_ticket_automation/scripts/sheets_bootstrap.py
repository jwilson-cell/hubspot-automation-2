"""Bootstrap the Pack'N HubSpot Automation workbook.

Idempotent: first run creates a new Google Sheets workbook with three tabs
(kpi_system, mispack_log, carrier_issue_log), seeds headers, formats the
header row (bold + frozen), and shares writer access with the operator team
listed in config/settings.yaml. Subsequent runs verify the workbook still
exists, repair any missing tabs, re-seed headers, and top up sharing.

Run from the project root after sheets_auth.py:
    py scripts/sheets_bootstrap.py

On success, writes config/sheets_state.json with spreadsheet_id, URL, and
tab (sheet) IDs. Downstream sync code reads that file — never re-creates the
workbook once state exists.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from sheets_schema import TAB_SCHEMAS

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "config" / "settings.yaml"
TOKEN_PATH = ROOT / "config" / ".secrets" / "sheets_token.json"
STATE_PATH = ROOT / "config" / "sheets_state.json"


def load_creds() -> Credentials:
    if not TOKEN_PATH.exists():
        print(f"token missing at {TOKEN_PATH} — run scripts/sheets_auth.py first",
              file=sys.stderr)
        sys.exit(3)
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
    return creds


def load_settings() -> dict:
    with open(SETTINGS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def create_workbook(sheets_svc, title: str) -> dict:
    body = {"properties": {"title": title}}
    ss = sheets_svc.spreadsheets().create(
        body=body,
        fields="spreadsheetId,spreadsheetUrl,sheets.properties",
    ).execute()
    ss_id = ss["spreadsheetId"]
    ss_url = ss["spreadsheetUrl"]
    default_sheet_id = ss["sheets"][0]["properties"]["sheetId"]

    requests: list = [{
        "updateSheetProperties": {
            "properties": {"sheetId": default_sheet_id, "title": "kpi_system"},
            "fields": "title",
        }
    }]
    for name in ("mispack_log", "carrier_issue_log"):
        requests.append({"addSheet": {"properties": {"title": name}}})

    resp = sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=ss_id,
        body={"requests": requests},
    ).execute()

    tab_ids = {"kpi_system": default_sheet_id}
    for r in resp.get("replies", []):
        if "addSheet" in r:
            props = r["addSheet"]["properties"]
            tab_ids[props["title"]] = props["sheetId"]

    return {"spreadsheet_id": ss_id, "spreadsheet_url": ss_url, "tab_ids": tab_ids}


def verify_tabs(sheets_svc, spreadsheet_id: str) -> dict[str, int]:
    ss = sheets_svc.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets.properties(sheetId,title)",
    ).execute()
    return {s["properties"]["title"]: s["properties"]["sheetId"] for s in ss["sheets"]}


def add_missing_tabs(sheets_svc, spreadsheet_id: str, missing: list[str]) -> dict[str, int]:
    reqs = [{"addSheet": {"properties": {"title": t}}} for t in missing]
    resp = sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": reqs},
    ).execute()
    new_ids = {}
    for r in resp.get("replies", []):
        if "addSheet" in r:
            props = r["addSheet"]["properties"]
            new_ids[props["title"]] = props["sheetId"]
    return new_ids


def seed_headers(sheets_svc, spreadsheet_id: str, tabs: list[str]) -> None:
    """Seed row 1 with canonical column names for the listed tabs.

    Only called for tabs that are newly created by this bootstrap run.
    Existing tabs keep whatever header order the user arranged them in —
    column reordering in Sheets must survive bootstrap re-runs.
    """
    if not tabs:
        return
    data = [{
        "range": f"{tab}!A1",
        "majorDimension": "ROWS",
        "values": [TAB_SCHEMAS[tab]],
    } for tab in tabs]
    sheets_svc.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "RAW", "data": data},
    ).execute()


def format_headers(sheets_svc, spreadsheet_id: str, tab_ids: dict) -> None:
    """Bold + freeze row 1 for the given tabs. Only called on newly-created tabs."""
    if not tab_ids:
        return
    requests = []
    for tab, sheet_id in tab_ids.items():
        requests.append({
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {"frozenRowCount": 1},
                },
                "fields": "gridProperties.frozenRowCount",
            }
        })
        requests.append({
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }
        })
    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests},
    ).execute()


def ensure_sharing(drive_svc, spreadsheet_id: str, emails: list[str]) -> None:
    existing = drive_svc.permissions().list(
        fileId=spreadsheet_id,
        fields="permissions(id,emailAddress,role,type)",
    ).execute().get("permissions", [])
    already = {p.get("emailAddress", "").lower() for p in existing if p.get("emailAddress")}

    for email in emails:
        if email.lower() in already:
            print(f"  already shared: {email}")
            continue
        try:
            drive_svc.permissions().create(
                fileId=spreadsheet_id,
                body={"type": "user", "role": "writer", "emailAddress": email},
                sendNotificationEmail=True,
                fields="id",
            ).execute()
            print(f"  shared with: {email} (writer)")
        except HttpError as e:
            print(f"  failed to share with {email}: {e}", file=sys.stderr)


def main() -> int:
    settings = load_settings()
    cfg = settings.get("sheets_export", {})
    if not cfg.get("enabled", False):
        print("sheets_export.enabled is false in settings.yaml; exiting", file=sys.stderr)
        return 1
    title = cfg["workbook_title"]
    share_with = cfg.get("share_with", [])

    creds = load_creds()
    sheets_svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive_svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    state = load_state()
    new_tabs: set[str] = set()  # only seed/format headers for tabs created this run

    if state.get("spreadsheet_id"):
        ss_id = state["spreadsheet_id"]
        print(f"existing workbook: {ss_id}")
        try:
            tab_ids = verify_tabs(sheets_svc, ss_id)
        except HttpError as e:
            print(f"cannot open existing workbook {ss_id}: {e}", file=sys.stderr)
            return 4
        missing = [t for t in TAB_SCHEMAS if t not in tab_ids]
        if missing:
            print(f"repairing missing tabs: {missing}")
            tab_ids.update(add_missing_tabs(sheets_svc, ss_id, missing))
            new_tabs.update(missing)
        state["tab_ids"] = {k: tab_ids[k] for k in TAB_SCHEMAS}
    else:
        print(f"creating new workbook: {title}")
        result = create_workbook(sheets_svc, title)
        state = {
            **result,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        new_tabs.update(TAB_SCHEMAS.keys())

    seed_headers(sheets_svc, state["spreadsheet_id"], sorted(new_tabs))
    format_headers(
        sheets_svc,
        state["spreadsheet_id"],
        {t: state["tab_ids"][t] for t in new_tabs},
    )

    print("sharing:")
    ensure_sharing(drive_svc, state["spreadsheet_id"], share_with)

    save_state(state)

    print()
    print(f"workbook id:  {state['spreadsheet_id']}")
    print(f"workbook url: {state['spreadsheet_url']}")
    print(f"tabs:         {', '.join(state['tab_ids'].keys())}")
    print(f"state file:   {STATE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
